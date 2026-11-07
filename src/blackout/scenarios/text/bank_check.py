"""Checking and locking a phrasing bank, for the human-written test bank (PRD 8.4).

The test bank (``data/phrasing/test``) is written by people, never by Claude, and nobody
tunes anything against it. This module is the only code that looks inside it before the
test suite is generated, and it reports **IDs and numbers only**, never phrasing text, so
running it doesn't put the bank's wording in front of whoever runs it.

- ``check_bank`` loads the bank with the same validation the generator uses (fact types,
  slots, at least 8 phrasings per fact type, at least 4 injection phrasings per kind, a
  noise lexicon), then compares its wording with the train bank's: a test phrasing whose
  word trigrams overlap a train phrasing's by more than ``MAX_OVERLAP_PCT`` is flagged by ID,
  because the test bank is meant to read differently from the train bank.
- ``lock`` writes ``data/phrasing/test.lock.json``: a digest of every file's bytes. The
  test suite is generated only from a bank that matches its committed lock.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from blackout.info.reports import FactType, InjectionKind
from blackout.scenarios.text.banks import (
    MIN_INJECTION_PHRASINGS,
    MIN_PHRASINGS,
    TRAIN_BANK,
    PhrasingBank,
    load_bank,
)
from blackout.sim.hashing import digest

MAX_OVERLAP_PCT = 50
LOCK_SUFFIX = ".lock.json"
_WORD = re.compile(r"[a-z0-9']+")


def lock_path(bank_dir: Path) -> Path:
    """Where a bank's lock lives: next to its directory (data/phrasing/test.lock.json)."""
    return bank_dir.with_name(bank_dir.name + LOCK_SUFFIX)


def bank_files(bank_dir: Path) -> dict[str, str]:
    """Each YAML file's name and the digest of its bytes (line endings as committed)."""
    return {
        path.name: digest(path.read_bytes().decode("utf-8"))
        for path in sorted(bank_dir.glob("*.yaml"))
    }


def bank_digest(bank_dir: Path) -> str:
    return digest(bank_files(bank_dir))


@dataclass(slots=True)
class BankReport:
    path: Path
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    digest: str | None = None

    @property
    def ok(self) -> bool:
        return not self.errors


def check_bank(bank_dir: Path, train_dir: Path = TRAIN_BANK) -> BankReport:
    """Validate a bank and compare its wording with the train bank's (IDs only)."""
    report = BankReport(bank_dir)
    if not bank_dir.is_dir():
        report.errors.append(f"{bank_dir} doesn't exist")
        return report
    try:
        bank = load_bank(bank_dir)
    except (ValidationError, ValueError, OSError) as error:
        report.errors.append(_short(error))
        return report
    for kind in FactType:
        report.counts[kind.value] = len(bank.facts[kind].phrasings)
    if bank.injection is None:
        report.errors.append(
            f"no _injection.yaml (L3 needs at least {MIN_INJECTION_PHRASINGS} per kind)"
        )
    else:
        for kind in InjectionKind:
            phrasings = [p for p in bank.injection.phrasings if p.kind is kind]
            report.counts[f"injection/{kind.value}"] = len(phrasings)
    report.warnings += _overlaps(bank, load_bank(train_dir))
    report.digest = bank_digest(bank_dir)
    return report


def _short(error: Exception) -> str:
    """A validation error without the input values (which would quote the bank's text)."""
    if isinstance(error, ValidationError):
        return "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in error.errors()
        )
    return str(error).split("\n")[0]


def _trigrams(text: str) -> set[tuple[str, ...]]:
    words = _WORD.findall(re.sub(r"\{[a-z_]+\}", " slot ", text.lower()))
    return {tuple(words[i : i + 3]) for i in range(len(words) - 2)}


def _overlaps(bank: PhrasingBank, train: PhrasingBank) -> list[str]:
    warnings = []
    for kind in FactType:
        theirs = [(p.id, _trigrams(p.text)) for p in train.facts[kind].phrasings]
        for phrasing in bank.facts[kind].phrasings:
            mine = _trigrams(phrasing.text)
            for train_id, grams in theirs:
                if not mine or not grams:
                    continue
                pct = 100 * len(mine & grams) // len(mine | grams)
                if pct > MAX_OVERLAP_PCT:
                    warnings.append(
                        f"{kind.value}/{phrasing.id} reads like train {train_id} ({pct}% overlap)"
                    )
    return warnings


def write_lock(bank_dir: Path) -> Path:
    """Write the bank's lock file (to commit along with the bank)."""
    path = lock_path(bank_dir)
    data = {
        "bank": bank_dir.name,
        "minimum_phrasings": MIN_PHRASINGS,
        "digest": bank_digest(bank_dir),
        "files": bank_files(bank_dir),
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


def lock_problems(bank_dir: Path) -> list[str]:
    """Why the bank doesn't match its lock (empty if it does)."""
    path = lock_path(bank_dir)
    if not bank_dir.is_dir():
        return [f"{bank_dir} doesn't exist yet (see docs/TEST_BANK_GUIDE.md)"]
    if not path.is_file():
        return [f"{path} is missing: run `blackout bank lock {bank_dir.as_posix()}`"]
    locked = json.loads(path.read_text(encoding="utf-8"))
    now = bank_files(bank_dir)
    problems = []
    for name in sorted(set(now) | set(locked.get("files", {}))):
        if now.get(name) != locked.get("files", {}).get(name):
            problems.append(f"{bank_dir.as_posix()}/{name} doesn't match {path.name}")
    return problems
