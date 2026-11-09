"""``LLMClient``: the one way the project calls the Messages API (docs/PRD.md 8.8).

Every call goes: cache lookup -> (budget check) -> transport with retries -> accounting ->
cache store -> budget charge -> ``stop_reason`` check. Content is only readable from a result
whose ``stop_reason`` says the model finished (``LLMResult.ok``).

**Prompt layout.** A request is built cache-first: ``tools`` (sorted by name, deterministic)
-> ``system`` blocks (the stable prefix, with the cache breakpoint on the last block) -> the
``messages``. Callers keep the system blocks byte-stable within a run so every call after the
first reads the prefix from the prompt cache (``usage.cache_read_input_tokens``).

**Faults** (PRD 8.8):

- 429, 5xx, connection errors and timeouts are retried with backoff, 5 attempts in all (the
  SDK's own retries are off, so the count is exact). If they run out the result is an
  ``infra_fault`` with no response; the caller turns it into a logged no-op.
- ``stop_reason: refusal`` is an ``agent_fault`` (it counts against the contestant), logged
  with its ``stop_details`` category; so is ``max_tokens`` and any other unfinished stop.
- Other API errors (400, 401, 403, 404, ...) are bugs or setup problems: ``LLMRequestError``.

**Fallbacks.** Models configured with ``fallbacks: true`` (Opus, Sonnet) are called through
the beta endpoint with ``fallbacks: "default"``. Every result records the model that actually
served it and whether a fallback ran, so such runs can be flagged.

**Modes.** ``source`` says where responses come from: ``live`` (the API, allowed only after
``blackout.llm.live.require_live``), ``fixture`` (recorded responses), or a test double.
Live calls charge real money to the ledger; cache hits and fixtures charge $0 but still count
their original cost toward the run's cost (the 5x-median check).
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

import anthropic

from blackout.llm.accounting import Usage, cost_usd
from blackout.llm.budget import BudgetGuard
from blackout.llm.cache import ResponseCache, cache_key
from blackout.llm.config import Effort, ModelsConfig
from blackout.llm.fixtures import Sent, Transport

HARNESS_VERSION = "m10.1"
"""Part of every cache key: bump it when the harness changes what it sends or how it reads."""

FALLBACK_BETA = "server-side-fallback-2026-07-01"
RETRY_ATTEMPTS = 5
BACKOFF_S = (2.0, 4.0, 8.0, 16.0)
FINISHED = frozenset({"end_turn", "tool_use", "stop_sequence"})

INFRA_FAULT = "infra_fault"
AGENT_FAULT = "agent_fault"


class LLMRequestError(Exception):
    """A non-retryable API error (bad request, auth, unknown model...): fix the setup."""


class LLMContentError(Exception):
    """Content was read from a result that didn't finish (check ``ok`` first)."""


@dataclass(frozen=True)
class LLMRequest:
    """One Messages API request, laid out for prompt caching."""

    model: str
    effort: Effort
    max_tokens: int
    system: tuple[str, ...]
    messages: tuple[dict[str, Any], ...]
    tools: tuple[dict[str, Any], ...] = ()
    output_schema: dict[str, Any] | None = None

    def body(self) -> dict[str, Any]:
        """The request body, in canonical order (it's also what the cache key hashes)."""
        if len(self.system) > 4:
            raise ValueError("at most 4 system blocks: each one is a cache breakpoint")
        # A breakpoint on every block: a prefix shared by several requests (the parser's
        # instructions) caches even when a later block (one scenario's gazetteer) differs.
        system: list[dict[str, Any]] = [
            {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}
            for text in self.system
        ]
        output_config: dict[str, Any] = {"effort": self.effort}
        if self.output_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": self.output_schema}
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "thinking": {"type": "adaptive"},
            "output_config": output_config,
            "system": system,
            "messages": list(self.messages),
        }
        if self.tools:
            body["tools"] = sorted(self.tools, key=lambda t: t["name"])
            body["tool_choice"] = {"type": "auto"}  # forced choice is a 400 on Opus 5.5
        return body

    def followed_by(self, *messages: dict[str, Any]) -> "LLMRequest":
        """The same request with messages appended (the loop never edits history)."""
        return replace(self, messages=(*self.messages, *messages))


@dataclass(frozen=True, slots=True)
class CallRecord:
    """What one call did and cost. ``cost_usd`` is what the call cost when it was made;
    ``spend_usd`` is new money (0 for cache hits and fixtures)."""

    key: str
    model: str
    effort: str
    source: str  # live, cache, fixture
    served_model: str | None = None
    stop_reason: str | None = None
    refusal_category: str | None = None
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    spend_usd: float = 0.0
    latency_ms: int = 0
    fallback: bool = False
    fault: str | None = None
    fault_detail: str = ""
    attempts: int = 0

    @property
    def cached(self) -> bool:
        return self.source == "cache"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "model": self.model,
            "effort": self.effort,
            "source": self.source,
            "served_model": self.served_model,
            "stop_reason": self.stop_reason,
            "refusal_category": self.refusal_category,
            "usage": self.usage.to_dict(),
            "cost_usd": self.cost_usd,
            "spend_usd": self.spend_usd,
            "latency_ms": self.latency_ms,
            "fallback": self.fallback,
            "fault": self.fault,
            "fault_detail": self.fault_detail,
        }


@dataclass(frozen=True, slots=True)
class LLMResult:
    record: CallRecord
    response: dict[str, Any] | None

    @property
    def ok(self) -> bool:
        return self.record.fault is None and self.response is not None

    @property
    def content(self) -> list[dict[str, Any]]:
        if not self.ok or self.response is None:
            raise LLMContentError(
                f"call {self.record.key[:12]} didn't finish ({self.record.fault}:"
                f" {self.record.fault_detail}); check ok before reading content"
            )
        return list(self.response.get("content", []))

    def text(self) -> str:
        return "".join(b.get("text", "") for b in self.content if b.get("type") == "text")

    def tool_uses(self) -> list[dict[str, Any]]:
        return [b for b in self.content if b.get("type") == "tool_use"]


def normalize(message: Any) -> dict[str, Any]:
    """A Messages API response (SDK object) as plain JSON data, with the fallback flag."""
    data = message.model_dump(mode="json", exclude_none=True)
    usage = data.get("usage", {})
    iterations = usage.get("iterations") or []
    fallback = any(b.get("type") == "fallback" for b in data.get("content", [])) or any(
        isinstance(i, dict) and i.get("type") == "fallback_message" for i in iterations
    )
    details = data.get("stop_details") or None
    return {
        "id": data.get("id"),
        "model": data.get("model"),
        "stop_reason": data.get("stop_reason"),
        "stop_details": details,
        "content": data.get("content", []),
        "usage": Usage.from_dict(usage).to_dict(),
        "fallback": fallback,
    }


class AnthropicTransport:
    """The real API, through the official SDK (retries are ``LLMClient``'s, not the SDK's)."""

    source = "live"

    def __init__(self, api_key: str, *, timeout_s: float = 300.0) -> None:
        self._client = anthropic.Anthropic(api_key=api_key, max_retries=0, timeout=timeout_s)

    def send(self, key: str, body: dict[str, Any], *, fallbacks: bool) -> Sent:
        started = time.perf_counter()
        if fallbacks:
            message = self._client.beta.messages.create(
                **body, betas=[FALLBACK_BETA], fallbacks="default"
            )
        else:
            message = self._client.messages.create(**body)
        latency = round((time.perf_counter() - started) * 1000)
        return Sent(normalize(message), latency)

    def count_tokens(self, body: dict[str, Any]) -> int:
        """Input tokens of a request (``messages.count_tokens``), for estimates."""
        counted = self._client.messages.count_tokens(
            model=body["model"],
            system=body["system"],
            messages=body["messages"],
            **({"tools": body["tools"]} if "tools" in body else {}),
        )
        return int(counted.input_tokens)


def _retry_after(error: anthropic.APIStatusError) -> float | None:
    try:
        value = error.response.headers.get("retry-after")
        return None if value is None else min(float(value), 60.0)
    except (AttributeError, ValueError):
        return None


class LLMClient:
    """Cached, budgeted, fault-classifying calls. Set ``context`` to charge a run."""

    def __init__(
        self,
        transport: Transport,
        models: ModelsConfig,
        *,
        cache: ResponseCache | None = None,
        guard: BudgetGuard | None = None,
        harness_version: str = HARNESS_VERSION,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.transport = transport
        self.models = models
        self.cache = cache
        self.guard = guard
        self.harness_version = harness_version
        self.sleep = sleep
        self.context: tuple[str, str] | None = None  # (experiment, run_id) to charge

    @property
    def live(self) -> bool:
        return self.transport.source == "live"

    def key_for(self, request: LLMRequest, run_index: int) -> str:
        spec = self.models.spec(request.model)
        payload = {"params": request.body(), "fallbacks": spec.fallbacks}
        return cache_key(payload, run_index=run_index, harness_version=self.harness_version)

    def call(self, request: LLMRequest, *, run_index: int = 0) -> LLMResult:
        spec = self.models.spec(request.model)
        body = request.body()
        key = self.key_for(request, run_index)
        base = CallRecord(key=key, model=request.model, effort=request.effort, source="cache")
        hit = None if self.cache is None else self.cache.get(key)
        if hit is not None:
            record = replace(base, cost_usd=hit.cost_usd, latency_ms=hit.latency_ms)
            return self._finish(record, hit.response)
        if self.live and self.guard is not None and self.context is not None:
            self.guard.check_call(*self.context)
        sent, attempts, error = self._send(key, body, spec.fallbacks)
        if sent is None:
            record = replace(
                base,
                source=self.transport.source,
                fault=INFRA_FAULT,
                fault_detail=error,
                attempts=attempts,
            )
            return LLMResult(record, None)
        response = sent.response
        usage = Usage.from_dict(response.get("usage"))
        cost = cost_usd(spec, usage)
        latency = sent.latency_ms or 0
        record = replace(
            base,
            source=self.transport.source,
            cost_usd=cost,
            spend_usd=cost if self.live else 0.0,
            latency_ms=latency,
            attempts=attempts,
        )
        if self.cache is not None:
            self.cache.put(
                key, body, response, cost_usd=cost, latency_ms=latency, source=record.source
            )
        return self._finish(record, response)

    def _finish(self, record: CallRecord, response: dict[str, Any]) -> LLMResult:
        details = response.get("stop_details") or {}
        stop = response.get("stop_reason")
        record = replace(
            record,
            served_model=response.get("model"),
            stop_reason=stop,
            usage=Usage.from_dict(response.get("usage")),
            fallback=bool(response.get("fallback")),
        )
        if stop == "refusal":
            category = details.get("category") if isinstance(details, dict) else None
            record = replace(
                record,
                fault=AGENT_FAULT,
                refusal_category=category,
                fault_detail=f"refusal ({category or 'no category'})",
            )
        elif stop not in FINISHED:
            record = replace(record, fault=AGENT_FAULT, fault_detail=f"stop_reason {stop}")
        if self.guard is not None and self.context is not None:
            self.guard.charge(*self.context, cost_usd=record.cost_usd, spend_usd=record.spend_usd)
        return LLMResult(record, response)

    def _send(
        self, key: str, body: dict[str, Any], fallbacks: bool
    ) -> tuple[Sent | None, int, str]:
        """Send with retries: (response or None, attempts made, last error)."""
        last = ""
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            wait: float | None = None
            try:
                return self.transport.send(key, body, fallbacks=fallbacks), attempt, ""
            except anthropic.RateLimitError as error:  # 429
                last, wait = f"rate limited: {error.message}", _retry_after(error)
            except anthropic.InternalServerError as error:  # 5xx, overloaded
                last, wait = (
                    f"server error {error.status_code}: {error.message}",
                    _retry_after(error),
                )
            except (anthropic.RequestTimeoutError, anthropic.ConflictError) as error:  # 408, 409
                last = f"retryable {error.status_code}: {error.message}"
            except anthropic.APIStatusError as error:  # any other HTTP error: not retryable
                raise LLMRequestError(f"{error.status_code} {error.message}") from error
            except anthropic.APIConnectionError as error:  # network, timeouts
                last = f"connection: {error.message}"
            if attempt < RETRY_ATTEMPTS:
                self.sleep(wait if wait is not None else BACKOFF_S[attempt - 1])
        return None, RETRY_ATTEMPTS, last
