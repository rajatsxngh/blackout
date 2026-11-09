"""Message Batches: precompute stateless calls at 50% of the price (docs/PRD.md 8.7, 8.11).

C4's parse calls depend only on the report and the scenario's prompt, so they can all be sent
as one batch before the runs. ``MessageBatches.run`` submits every request that isn't cached
yet, waits, and stores each successful result **in the response cache under the same key a
direct call would use**; the runs then replay from the cache for free. Results are matched by
``custom_id`` (the cache key), never by position.

Every batch goes through the budget guard: its worst-case cost (all input uncached, every
output at ``max_tokens``, at the batch discount) is reserved against the caps before it's
submitted, and its actual cost is charged to the ledger when it's collected. Errored, expired
and canceled requests are never cached; the run makes those calls directly later.

The Batches API rejects the ``fallbacks`` parameter, so batched calls run without server-side
fallback: a refusal stays a refusal.
"""

import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from blackout.llm.accounting import Usage, cost_usd
from blackout.llm.budget import BudgetGuard
from blackout.llm.client import LLMClient, LLMRequest, normalize

POLL_S = 60.0
CHARS_PER_TOKEN_FLOOR = 3  # a deliberately high token estimate for the reservation


@dataclass(frozen=True, slots=True)
class BatchItem:
    request: LLMRequest
    run_index: int = 0


@dataclass(frozen=True, slots=True)
class BatchOutcome:
    custom_id: str
    kind: str  # succeeded, errored, canceled, expired
    response: dict[str, Any] | None = None
    error: str = ""


class BatchAPI(Protocol):
    def create(self, requests: list[dict[str, Any]]) -> str: ...
    def ended(self, batch_id: str) -> bool: ...
    def results(self, batch_id: str) -> Iterable[BatchOutcome]: ...


class AnthropicBatchAPI:
    """``client.messages.batches`` of the official SDK."""

    def __init__(self, sdk_client: Any) -> None:
        self._batches = sdk_client.messages.batches

    def create(self, requests: list[dict[str, Any]]) -> str:
        return str(self._batches.create(requests=requests).id)

    def ended(self, batch_id: str) -> bool:
        return self._batches.retrieve(batch_id).processing_status == "ended"

    def results(self, batch_id: str) -> Iterable[BatchOutcome]:
        for item in self._batches.results(batch_id):
            result = item.result
            if result.type == "succeeded":
                yield BatchOutcome(item.custom_id, "succeeded", normalize(result.message))
            elif result.type == "errored":
                yield BatchOutcome(item.custom_id, "errored", error=str(result.error.type))
            else:
                yield BatchOutcome(item.custom_id, str(result.type))


@dataclass(frozen=True, slots=True)
class BatchReport:
    batch_id: str | None
    submitted: int
    already_cached: int
    succeeded: int
    failed: dict[str, str]  # custom_id -> why
    spend_usd: float


class MessageBatches:
    def __init__(
        self,
        client: LLMClient,
        api: BatchAPI,
        guard: BudgetGuard,
        experiment: str,
        *,
        sleep: Callable[[float], None] = time.sleep,
        poll_s: float = POLL_S,
    ) -> None:
        if client.cache is None:
            raise ValueError("batches precompute into the response cache: give the client one")
        self.client = client
        self.api = api
        self.guard = guard
        self.experiment = experiment
        self.sleep = sleep
        self.poll_s = poll_s

    def pending(self, items: Sequence[BatchItem]) -> dict[str, BatchItem]:
        """Items not in the cache yet, by cache key (duplicates collapse)."""
        cache = self.client.cache
        assert cache is not None
        out: dict[str, BatchItem] = {}
        for item in items:
            key = self.client.key_for(item.request, item.run_index)
            if key not in cache:
                out.setdefault(key, item)
        return dict(sorted(out.items()))

    def estimate_usd(self, items: Iterable[BatchItem]) -> float:
        """Worst case: every input token uncached, every output token used."""
        total = 0.0
        discount = self.client.models.batch_discount_pct
        for item in items:
            body = item.request.body()
            chars = len(str(body["system"])) + len(str(body["messages"]))
            usage = Usage(
                input_tokens=chars // CHARS_PER_TOKEN_FLOOR + 1,
                output_tokens=item.request.max_tokens,
            )
            spec = self.client.models.spec(item.request.model)
            total += cost_usd(spec, usage, batch_discount_pct=discount)
        return total

    def run(self, items: Sequence[BatchItem]) -> BatchReport:
        todo = self.pending(items)
        cached = len({self.client.key_for(i.request, i.run_index) for i in items}) - len(todo)
        if not todo:
            return BatchReport(None, 0, cached, 0, {}, 0.0)
        self.guard.reserve(self.experiment, self.estimate_usd(todo.values()))
        requests = [{"custom_id": key, "params": item.request.body()} for key, item in todo.items()]
        batch_id = self.api.create(requests)
        while not self.api.ended(batch_id):
            self.sleep(self.poll_s)
        return self._collect(batch_id, todo, cached)

    def _collect(self, batch_id: str, todo: dict[str, BatchItem], cached: int) -> BatchReport:
        cache = self.client.cache
        assert cache is not None
        discount = self.client.models.batch_discount_pct
        spend = 0.0
        succeeded = 0
        failed: dict[str, str] = {}
        seen: set[str] = set()
        for outcome in self.api.results(batch_id):
            item = todo.get(outcome.custom_id)
            if item is None:
                continue
            seen.add(outcome.custom_id)
            if outcome.kind != "succeeded" or outcome.response is None:
                failed[outcome.custom_id] = outcome.error or outcome.kind
                continue
            spec = self.client.models.spec(item.request.model)
            usage = Usage.from_dict(outcome.response.get("usage"))
            cost = cost_usd(spec, usage, batch_discount_pct=discount)
            spend += cost
            succeeded += 1
            cache.put(
                outcome.custom_id,
                item.request.body(),
                outcome.response,
                cost_usd=cost,
                latency_ms=0,
                source="batch",
            )
        for key in todo:
            if key not in seen:
                failed[key] = "missing from results"
        self.guard.charge_batch(self.experiment, batch_id, spend_usd=spend, requests=len(todo))
        return BatchReport(batch_id, len(todo), cached, succeeded, failed, round(spend, 6))
