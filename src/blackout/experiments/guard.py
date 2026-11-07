"""The test guard: no run touches the held-out test suite before pre-registration (PRD 8.6).

``blackout batch`` refuses a held-out suite unless ``docs/PREREGISTRATION.md``

1. exists and is committed (tracked by git, with no uncommitted changes), and
2. carries a ``frozen-hashes`` block whose values all match the current ones:

   ```frozen-hashes
   code: <digest of the code that decides runs, as in each run's config hash>
   scorer: <digest of the scorer and experiment code>
   train_bank: <digest of data/phrasing/train>
   test_bank: <digest of data/phrasing/test>
   test_manifest: <digest of scenarios/test/manifest.json>
   prompts: <digest of the agent prompts, or "none" before M10>
   ```

``blackout prereg-hashes`` prints the block for the current tree, to paste into the file.
"""

import re
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

from blackout.experiments.runner import PACKAGE, code_digest
from blackout.scenarios.text.bank_check import bank_digest
from blackout.sim.hashing import digest

PREREGISTRATION = Path("docs") / "PREREGISTRATION.md"
HELD_OUT_SUITES = frozenset({"test"})
_BLOCK = re.compile(r"```frozen-hashes\s*\n(.*?)```", re.DOTALL)
PROMPTS = PACKAGE / "controllers" / "agents" / "prompts"


def _files_digest(paths: list[Path]) -> str:
    return digest({p.as_posix(): p.read_bytes().decode("utf-8") for p in sorted(paths)})


def frozen_hashes(root: Path = Path()) -> dict[str, str]:
    """The hashes a pre-registration freezes, for the tree at ``root``."""
    scorer = sorted((PACKAGE / "scoring").rglob("*.py")) + sorted(
        (PACKAGE / "experiments").rglob("*.py")
    )
    test_bank = root / "data" / "phrasing" / "test"
    manifest = root / "scenarios" / "test" / "manifest.json"
    prompts = sorted(PROMPTS.rglob("*.md")) if PROMPTS.is_dir() else []
    return {
        "code": code_digest(),
        "scorer": _files_digest(scorer),
        "train_bank": bank_digest(root / "data" / "phrasing" / "train"),
        "test_bank": bank_digest(test_bank) if test_bank.is_dir() else "missing",
        "test_manifest": _files_digest([manifest]) if manifest.is_file() else "missing",
        "prompts": _files_digest(prompts) if prompts else "none",
    }


def hashes_block(hashes: Mapping[str, str]) -> str:
    lines = "".join(f"{key}: {value}\n" for key, value in hashes.items())
    return f"```frozen-hashes\n{lines}```\n"


def parse_block(text: str) -> dict[str, str] | None:
    match = _BLOCK.search(text)
    if match is None:
        return None
    pairs = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep:
            pairs[key.strip()] = value.strip()
    return pairs


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=False, timeout=30
    )


def committed(root: Path, path: Path) -> list[str]:
    """Why ``path`` isn't committed as it is (empty if it is)."""
    name = path.as_posix()
    if _git(root, "ls-files", "--error-unmatch", "--", name).returncode != 0:
        return [f"{name} isn't committed"]
    status = _git(root, "status", "--porcelain", "--", name)
    if status.returncode != 0:
        return [f"git can't tell whether {name} is committed"]
    if status.stdout.strip():
        return [f"{name} has uncommitted changes"]
    return []


def guard_problems(
    suite: str,
    root: Path = Path(),
    *,
    hashes: Callable[[Path], Mapping[str, str]] = frozen_hashes,
) -> list[str]:
    """Why a batch may not run on ``suite`` (empty if it may)."""
    if suite not in HELD_OUT_SUITES:
        return []
    prereg = root / PREREGISTRATION
    if not prereg.is_file():
        return [
            f"suite {suite!r} is held out: it needs a committed {PREREGISTRATION.as_posix()}"
            " (PRD 8.11, M12)"
        ]
    problems = committed(root, PREREGISTRATION)
    frozen = parse_block(prereg.read_text(encoding="utf-8"))
    if frozen is None:
        return [*problems, f"{PREREGISTRATION.as_posix()} has no ```frozen-hashes block"]
    for key, value in hashes(root).items():
        if frozen.get(key) != value:
            problems.append(
                f"frozen {key} hash {frozen.get(key)!r} doesn't match the current {value!r}"
            )
    return problems
