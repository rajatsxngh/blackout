"""Stable seeds and the generator's random streams (docs/PRD.md sections 8.2 and 10).

The scenario generator is the only code allowed to draw random numbers, and it only ever draws
them from an ``Rng`` built here. Seeds are blake2b digests of their labelled parts, never
Python's ``hash()`` (salted per process), so a seed means the same thing on every machine. The
bit generator is numpy's PCG64; the version is pinned by ``uv.lock``, and the dev-v0 manifest
test notices if a numpy upgrade ever changes a stream.

Seeds are 48-bit, so they survive JSON round trips through JavaScript (the M9 viewer) intact.
"""

import hashlib
from collections.abc import Sequence

import numpy as np

SEED_BITS = 48


def stable_seed(*parts: object) -> int:
    """A 48-bit seed from labelled parts, e.g. ``stable_seed(scenario_seed, "report", "R03")``.

    Parts are joined by their ``str()`` with a separator that can't appear in them by accident.
    """
    joined = "\x1f".join(str(part) for part in parts)
    raw = hashlib.blake2b(joined.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(raw, "big") >> (64 - SEED_BITS)


class Rng:
    """A small, explicit random stream: every draw is an int, an item or a bool.

    Wrapping numpy keeps the generator's draws to a handful of methods whose behaviour is easy
    to reason about (and to keep stable), and keeps numpy scalars out of scenario files.
    """

    def __init__(self, seed: int) -> None:
        self.seed = seed
        self._gen = np.random.Generator(np.random.PCG64(seed))

    @classmethod
    def derive(cls, *parts: object) -> "Rng":
        """An independent stream for the given labelled parts."""
        return cls(stable_seed(*parts))

    def between(self, low: int, high: int, step: int = 1) -> int:
        """A whole number from ``low`` to ``high`` inclusive, on multiples of ``step`` from low."""
        if high < low:
            raise ValueError(f"empty range [{low}, {high}]")
        return low + step * int(self._gen.integers(0, (high - low) // step + 1))

    def pick[T](self, items: Sequence[T]) -> T:
        """One item, uniformly."""
        if not items:
            raise ValueError("can't pick from an empty sequence")
        return items[int(self._gen.integers(0, len(items)))]

    def chance(self, percent: int) -> bool:
        """True with probability ``percent`` / 100."""
        return int(self._gen.integers(0, 100)) < percent

    def shuffled[T](self, items: Sequence[T]) -> list[T]:
        """A shuffled copy (Fisher-Yates over the draws above)."""
        out = list(items)
        for i in range(len(out) - 1, 0, -1):
            j = int(self._gen.integers(0, i + 1))
            out[i], out[j] = out[j], out[i]
        return out

    def sample[T](self, items: Sequence[T], k: int) -> list[T]:
        """``k`` distinct items, in the order drawn."""
        if k > len(items):
            raise ValueError(f"can't sample {k} of {len(items)} items")
        return self.shuffled(items)[:k]
