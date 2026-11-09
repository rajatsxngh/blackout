"""When the project may call the real API, and with which key (docs/PRD.md 8.4, 8.8).

PRD 8.4: the human-written test bank is committed with its hash **before any LLM run**. So
live mode refuses to start until ``data/phrasing/test`` matches its lock file
(``blackout bank lock``) and both are committed. The check only hashes the bank's files
(through ``bank_check.lock_problems``, the one module allowed to look inside it); it never
reads or prints their text.

The API key comes from ``ANTHROPIC_API_KEY`` in the environment or a git-ignored ``.env``
file, never from code or config.
"""

import os
from pathlib import Path

from blackout.experiments.guard import committed
from blackout.scenarios.text.bank_check import lock_path, lock_problems

TEST_BANK_DIR = Path("data") / "phrasing" / "test"
API_KEY_VAR = "ANTHROPIC_API_KEY"


class LiveRefused(Exception):  # noqa: N818 (a refusal)
    """Live API calls aren't allowed yet (or there's no key)."""


def live_problems(root: Path = Path()) -> list[str]:
    """Why live calls may not be made from the tree at ``root`` (empty if they may)."""
    bank = root / TEST_BANK_DIR
    problems = [f"test bank: {p}" for p in lock_problems(bank)]
    if problems:
        return [
            *problems,
            "live LLM calls wait for the human-written test bank, locked and committed"
            " (PRD 8.4; docs/TEST_BANK_GUIDE.md)",
        ]
    lock = lock_path(TEST_BANK_DIR)
    problems += committed(root, lock)
    problems += committed(root, TEST_BANK_DIR)
    return problems


def api_key(root: Path = Path(), environ: dict[str, str] | None = None) -> str:
    """The API key from the environment, else from ``<root>/.env``."""
    env = os.environ if environ is None else environ
    key = env.get(API_KEY_VAR, "").strip()
    if key:
        return key
    dotenv = root / ".env"
    if dotenv.is_file():
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.strip().removeprefix("export ").partition("=")
            if sep and name.strip() == API_KEY_VAR:
                value = value.strip().strip("'\"")
                if value:
                    return value
    raise LiveRefused(f"no {API_KEY_VAR} in the environment or in {dotenv.as_posix()}")


def require_live(root: Path = Path()) -> str:
    """The API key, if live calls are allowed; ``LiveRefused`` (with every reason) if not."""
    problems = live_problems(root)
    if problems:
        raise LiveRefused("; ".join(problems))
    return api_key(root)
