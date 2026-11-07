"""Mechanical checks for the determinism rules in CLAUDE.md.

The checks scan the source tree, so each package is guarded as soon as it exists (the simulation
arrives in M1, the solver in M7). Each checker is also tested on deliberately bad code, because a
guard that can never fire proves nothing.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "blackout"

# Packages whose behaviour must be a pure function of their inputs (PRD sections 8.2 and 8.7).
DETERMINISTIC_PACKAGES = ("sim", "info", "graph", "controllers/solver")
FORBIDDEN_MODULES = frozenset({"random", "secrets", "time", "uuid", "numpy.random"})
CLOCK_METHODS = frozenset({"now", "utcnow", "today"})
CLOCK_OWNERS = frozenset({"datetime", "date"})


def builtin_hash_calls(source: str) -> list[int]:
    """Line numbers where the builtin ``hash()`` is called. It is salted per process."""
    return sorted(
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "hash"
    )


def nondeterminism(source: str) -> list[str]:
    """Imports of randomness or clock modules, and wall-clock calls such as ``datetime.now()``."""
    problems: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            problems += [
                f"line {node.lineno}: import {alias.name}"
                for alias in node.names
                if alias.name in FORBIDDEN_MODULES
            ]
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported = {node.module} | {f"{node.module}.{alias.name}" for alias in node.names}
            if imported & FORBIDDEN_MODULES:
                problems.append(f"line {node.lineno}: from {node.module} import ...")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in CLOCK_METHODS
        ):
            owner = node.func.value
            if isinstance(owner, ast.Attribute):
                owner_name = owner.attr
            else:
                owner_name = getattr(owner, "id", "")
            if owner_name in CLOCK_OWNERS:
                problems.append(f"line {node.lineno}: {owner_name}.{node.func.attr}()")
    return problems


def _python_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py")) if root.is_dir() else []


def test_no_builtin_hash_in_source() -> None:
    offenders = {
        str(path.relative_to(SRC)): lines
        for path in _python_files(SRC)
        if (lines := builtin_hash_calls(path.read_text(encoding="utf-8")))
    }
    assert not offenders, f"use hashlib.blake2b instead of hash(): {offenders}"


@pytest.mark.parametrize("package", DETERMINISTIC_PACKAGES)
def test_deterministic_packages_avoid_randomness_and_clocks(package: str) -> None:
    offenders = {
        str(path.relative_to(SRC)): problems
        for path in _python_files(SRC / package)
        if (problems := nondeterminism(path.read_text(encoding="utf-8")))
    }
    assert not offenders, f"randomness or wall-clock use in {package}: {offenders}"


def test_hash_checker_catches_violations() -> None:
    bad = "def f(x):\n    return hash(x)\n"
    fine = (
        "import hashlib\n# hash(x) in a comment is fine\ns = 'hash(x)'\nh = hashlib.blake2b(b'x')\n"
    )
    assert builtin_hash_calls(bad) == [2]
    assert builtin_hash_calls(fine) == []


@pytest.mark.parametrize(
    "source",
    [
        "import random\n",
        "from random import choice\n",
        "import time\n",
        "from numpy import random\n",
        "import numpy.random\n",
        "from datetime import datetime\nstamp = datetime.now()\n",
        "import datetime\nstamp = datetime.datetime.utcnow()\n",
        "from datetime import date\nday = date.today()\n",
    ],
)
def test_nondeterminism_checker_catches_violations(source: str) -> None:
    assert nondeterminism(source)


def test_nondeterminism_checker_allows_seeded_and_pure_code() -> None:
    fine = "import hashlib\nfrom dataclasses import dataclass\nnow = 5\nx = sorted({1, 2})\n"
    assert nondeterminism(fine) == []
