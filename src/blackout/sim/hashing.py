"""Canonical serialization and stable hashing.

Python's built-in ``hash()`` is salted per process, so everything here uses blake2b over
canonical JSON (sorted keys, no whitespace). Hashed state may contain only ints, strings,
booleans, None, lists and dicts. Floats are rejected so results can't drift between platforms.
"""

import hashlib
import json
from collections.abc import Iterable


def canonical_json(value: object) -> str:
    """Serialize ``value`` deterministically. Raises TypeError if it contains a float."""
    _reject_floats(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value: object) -> str:
    """32-character hex blake2b digest of the canonical JSON of ``value``."""
    return hashlib.blake2b(canonical_json(value).encode("ascii"), digest_size=16).hexdigest()


def chain_digest(hashes: Iterable[str]) -> str:
    """One digest that summarizes a sequence of digests (order matters)."""
    combined = hashlib.blake2b(digest_size=16)
    for item in hashes:
        combined.update(item.encode("ascii"))
        combined.update(b"\n")
    return combined.hexdigest()


def _reject_floats(value: object) -> None:
    if isinstance(value, float):
        raise TypeError(f"floats are not allowed in hashed state: {value!r}")
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_floats(key)
            _reject_floats(item)
    elif isinstance(value, list | tuple):
        for item in value:
            _reject_floats(item)
