"""Recorded responses: tests and offline runs never touch the network (docs/PRD.md 8.8).

A fixture directory holds one ``<cache key>.json`` per request (directly in it, or one folder
down):

    {"key": ..., "request": <the Messages API body>, "response": <normalized response>,
     "latency_ms": ..., "origin": "recorded" | "synthetic"}

``recorded`` fixtures were captured from real API calls (``RecordingTransport`` wraps the
live transport). ``synthetic`` ones were written by hand or by a script from known answers,
to exercise the pipeline before any live call is allowed; their usage numbers are estimates.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from blackout.llm.cache import canonical_json

DEFAULT_FIXTURES = Path("tests") / "fixtures" / "llm_recordings"


class FixtureMissing(LookupError):  # noqa: N818 (a lookup miss)
    """No recorded response for this request."""


@dataclass(frozen=True, slots=True)
class Sent:
    """What a transport got back: the normalized response, and its latency if it knows it."""

    response: dict[str, Any]
    latency_ms: int | None = None


class Transport(Protocol):
    """Sends one request (``body`` is the Messages API body) and normalizes the reply."""

    source: str

    def send(self, key: str, body: dict[str, Any], *, fallbacks: bool) -> Sent: ...


def write_fixture(
    folder: Path,
    key: str,
    body: dict[str, Any],
    response: dict[str, Any],
    *,
    latency_ms: int,
    origin: str,
) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    data = {
        "key": key,
        "origin": origin,
        "latency_ms": latency_ms,
        "request": stored_request(body),
        "response": response,
    }
    path = folder / f"{key}.json"
    text = json.dumps(json.loads(canonical_json(data)), indent=1, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def stored_request(body: dict[str, Any]) -> dict[str, Any]:
    """The request as a fixture keeps it: system blocks and tool definitions (long and the
    same in every call of a run) are replaced by their SHA-256, so fixture files stay small.
    The cache key still covers the full text."""
    system = [
        {**block, "text": "sha256:" + hashlib.sha256(block["text"].encode()).hexdigest()}
        for block in body.get("system", [])
    ]
    stored = {**body, "system": system}
    if "tools" in body:
        stored["tools"] = [
            {
                "name": tool["name"],
                "sha256": hashlib.sha256(canonical_json(tool).encode()).hexdigest(),
            }
            for tool in body["tools"]
        ]
    return stored


class FixtureTransport:
    """Answers from a fixture directory; a request it has no recording for is an error."""

    source = "fixture"

    def __init__(self, folder: Path = DEFAULT_FIXTURES) -> None:
        self.folder = folder

    def send(self, key: str, body: dict[str, Any], *, fallbacks: bool) -> Sent:
        path = self.folder / f"{key}.json"
        if not path.is_file():
            path = next(iter(sorted(self.folder.glob(f"*/{key}.json"))), path)
        if not path.is_file():
            raise FixtureMissing(
                f"no recorded response {key[:12]}... in {self.folder.as_posix()}"
                f" (model {body.get('model')}); record it live or regenerate the fixtures"
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        return Sent(data["response"], int(data.get("latency_ms", 0)))


class RecordingTransport:
    """Wraps a live transport and writes every successful response as a fixture."""

    def __init__(self, inner: Transport, folder: Path) -> None:
        self.inner = inner
        self.folder = folder
        self.source = inner.source

    def send(self, key: str, body: dict[str, Any], *, fallbacks: bool) -> Sent:
        sent = self.inner.send(key, body, fallbacks=fallbacks)
        write_fixture(
            self.folder,
            key,
            body,
            sent.response,
            latency_ms=sent.latency_ms or 0,
            origin="recorded",
        )
        return sent
