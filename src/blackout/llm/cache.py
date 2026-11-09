"""The response cache: SQLite in WAL mode (docs/PRD.md 8.8).

Key: ``sha256(canonical_json(model, effort, system, tools, messages, ..., run_index,
harness_version))`` (``cache_key``). Only successful API responses are stored (a refusal is a
response, so it's cached and replays as the same sample); errors and retries never are.

Different run indexes are separate samples. Re-running a run index replays it exactly, for
free: a hit returns the original response with its original cost and latency, and adds $0 to
current spend.
"""

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CACHE = Path("runs") / "llm_cache.sqlite"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def cache_key(body: dict[str, Any], *, run_index: int, harness_version: str) -> str:
    """The key of one request: everything that can change the response, plus the sample."""
    payload = {"request": body, "run_index": run_index, "harness_version": harness_version}
    return hashlib.sha256(canonical_json(payload).encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class CachedResponse:
    key: str
    response: dict[str, Any]
    cost_usd: float
    latency_ms: int


_SCHEMA = """
CREATE TABLE IF NOT EXISTS responses (
    key TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    request TEXT NOT NULL,
    response TEXT NOT NULL,
    cost_usd REAL NOT NULL,
    latency_ms INTEGER NOT NULL,
    source TEXT NOT NULL
)
"""


class ResponseCache:
    """One SQLite file; safe to share between processes (WAL)."""

    def __init__(self, path: Path = DEFAULT_CACHE) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._db = sqlite3.connect(path, timeout=30)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute(_SCHEMA)
        self._db.commit()

    def get(self, key: str) -> CachedResponse | None:
        row = self._db.execute(
            "SELECT response, cost_usd, latency_ms FROM responses WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        return CachedResponse(key, json.loads(row[0]), float(row[1]), int(row[2]))

    def put(
        self,
        key: str,
        request: dict[str, Any],
        response: dict[str, Any],
        *,
        cost_usd: float,
        latency_ms: int,
        source: str = "live",
    ) -> None:
        """Store a successful response (the first one wins: a key is one sample)."""
        if response.get("type") == "error" or "stop_reason" not in response:
            raise ValueError("only successful API responses are cached, never errors")
        self._db.execute(
            "INSERT OR IGNORE INTO responses VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                key,
                str(request.get("model", "")),
                canonical_json(request),
                canonical_json(response),
                cost_usd,
                latency_ms,
                source,
            ),
        )
        self._db.commit()

    def __contains__(self, key: str) -> bool:
        row = self._db.execute("SELECT 1 FROM responses WHERE key = ?", (key,)).fetchone()
        return row is not None

    def __len__(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM responses").fetchone()[0])

    def journal_mode(self) -> str:
        return str(self._db.execute("PRAGMA journal_mode").fetchone()[0])

    def close(self) -> None:
        self._db.close()
