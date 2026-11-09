"""The LLM layer (M10): config, accounting, schemas, cache, client faults, budget, tool loop,
batches and the live guard. No network: every reply comes from a fake transport."""

import json
import sqlite3
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.types import Message
from pydantic import BaseModel

from blackout.llm.accounting import Usage, cost_usd
from blackout.llm.batches import BatchItem, BatchOutcome, MessageBatches
from blackout.llm.budget import (
    BudgetConfig,
    BudgetError,
    BudgetExceeded,
    BudgetGuard,
    Ledger,
    RunAborted,
    load_budget_config,
)
from blackout.llm.cache import ResponseCache, cache_key
from blackout.llm.client import (
    AGENT_FAULT,
    FALLBACK_BETA,
    INFRA_FAULT,
    RETRY_ATTEMPTS,
    AnthropicTransport,
    LLMClient,
    LLMContentError,
    LLMRequest,
    LLMRequestError,
    normalize,
)
from blackout.llm.config import load_models_config
from blackout.llm.fixtures import (
    FixtureMissing,
    FixtureTransport,
    RecordingTransport,
    Sent,
    write_fixture,
)
from blackout.llm.live import API_KEY_VAR, LiveRefused, api_key, live_problems, require_live
from blackout.llm.loop import ToolError, run_tool_loop
from blackout.llm.schemas import output_format, strict_schema, strict_tool
from blackout.llm.setup import LLMSettings, make_client
from blackout.scenarios.text.bank_check import write_lock

MODELS = load_models_config()
HAIKU = "claude-haiku-5-5"
OPUS = "claude-opus-5-5"
URL = "https://api.anthropic.com/v1/messages"


def request(text: str = "hello", model: str = HAIKU, **extra: Any) -> LLMRequest:
    return LLMRequest(
        model=model,
        effort="low",
        max_tokens=1000,
        system=("You read reports.", "Gazetteer: none."),
        messages=({"role": "user", "content": text},),
        **extra,
    )


def reply(
    text: str = '{"ok": true}',
    *,
    stop: str = "end_turn",
    model: str = HAIKU,
    usage: dict[str, int] | None = None,
    content: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "id": "msg_test",
        "model": model,
        "stop_reason": stop,
        "stop_details": None,
        "content": content if content is not None else [{"type": "text", "text": text}],
        "usage": usage
        or {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 4000,
        },
        "fallback": False,
        **extra,
    }


def rate_limited() -> anthropic.RateLimitError:
    response = httpx2.Response(429, request=httpx2.Request("POST", URL))
    return anthropic.RateLimitError("slow down", response=response, body=None)


def server_error() -> anthropic.InternalServerError:
    response = httpx2.Response(529, request=httpx2.Request("POST", URL))
    return anthropic.InternalServerError("overloaded", response=response, body=None)


def bad_request() -> anthropic.BadRequestError:
    response = httpx2.Response(400, request=httpx2.Request("POST", URL))
    return anthropic.BadRequestError("bad field", response=response, body=None)


def disconnected() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(request=httpx2.Request("POST", URL))


class FakeTransport:
    """Replays a script: each item is a reply dict, an exception to raise, or a callable."""

    def __init__(self, script: Iterable[Any], source: str = "live") -> None:
        self.script = list(script)
        self.source = source
        self.sent: list[dict[str, Any]] = []
        self.fallbacks: list[bool] = []

    def send(self, key: str, body: dict[str, Any], *, fallbacks: bool) -> Sent:
        self.sent.append(body)
        self.fallbacks.append(fallbacks)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            item = item(body)
        assert isinstance(item, dict)
        return Sent(item, 1234)


def guard(tmp_path: Path | None = None, **caps: Any) -> BudgetGuard:
    config = load_budget_config().model_copy(update=caps)
    return BudgetGuard(config, Ledger.load(None if tmp_path is None else tmp_path / "ledger.json"))


def client(transport: Any, **kwargs: Any) -> LLMClient:
    sleeps: list[float] = kwargs.pop("sleeps", [])
    return LLMClient(transport, MODELS, sleep=sleeps.append, **kwargs)


# --- config and accounting -----------------------------------------------------------------


def test_models_config_sets_effort_and_dates_prices() -> None:
    assert MODELS.dev_model == HAIKU
    assert set(MODELS.final_models) == {OPUS, HAIKU}
    assert MODELS.effort == {"parser": "low", "agent": "medium"}
    assert MODELS.prices_as_of == "2026-10-06"
    assert MODELS.spec(OPUS).prices.input == 4.0
    assert MODELS.spec(OPUS).fallbacks
    assert not MODELS.spec(HAIKU).fallbacks  # a fallbacks parameter is a 400 on Haiku 5.5
    with pytest.raises(KeyError, match="unknown model"):
        MODELS.spec("claude-unknown")


def test_cost_from_the_price_table() -> None:
    usage = Usage(1_000_000, 100_000, 10_000, 500_000)
    # Opus: 1M in at $4 + 100k out at $20 + 10k write at $5 + 500k read at $0.20
    assert cost_usd(MODELS.spec(OPUS), usage) == pytest.approx(4 + 2 + 0.05 + 0.1)
    assert cost_usd(MODELS.spec(OPUS), usage, batch_discount_pct=50) == pytest.approx(3.075)
    small = Usage(1000, 1000)
    assert cost_usd(MODELS.spec(HAIKU), small) == pytest.approx(0.0006)
    # Haiku prompts over 100K tokens bill at the long-context rate.
    long = Usage(150_000, 1000)
    assert cost_usd(MODELS.spec(HAIKU), long) == pytest.approx(0.075 + 0.0025)
    assert (Usage(1, 2, 3, 4) + Usage(1, 1, 1, 1)).to_dict()["cache_read_input_tokens"] == 5
    assert Usage.from_dict(None) == Usage()


# --- strict schemas ------------------------------------------------------------------------


class Inner(BaseModel):
    kind: str
    amount: int | None = None


class Outer(BaseModel):
    name: str = "x"
    items: list[Inner]
    best: Inner | None


def _objects(node: Any) -> Iterable[dict[str, Any]]:
    if isinstance(node, dict):
        if node.get("type") == "object":
            yield node
        for value in node.values():
            yield from _objects(value)
    elif isinstance(node, list):
        for value in node:
            yield from _objects(value)


def test_strict_schema_inlines_defs_and_closes_objects() -> None:
    schema = strict_schema(Outer)
    text = json.dumps(schema)
    assert "$defs" not in text
    assert "$ref" not in text
    assert '"title"' not in text
    assert '"default"' not in text
    objects = list(_objects(schema))
    assert len(objects) == 3  # outer, and Inner twice (inlined)
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert obj["required"] == list(obj["properties"])
    tool = strict_tool("pick", "Pick one.", Outer)
    assert tool["strict"] is True
    assert tool["input_schema"] == schema
    assert output_format(Outer) == {"type": "json_schema", "schema": schema}


def test_strict_schema_rejects_bad_refs() -> None:
    recursive = {"$defs": {"N": {"type": "object", "properties": {"n": {"$ref": "#/$defs/N"}}}}}
    with pytest.raises(ValueError, match="recursive"):
        strict_schema({**recursive, "$ref": "#/$defs/N"})
    with pytest.raises(ValueError, match="unresolved"):
        strict_schema({"$ref": "#/$defs/Missing"})


# --- request layout ------------------------------------------------------------------------


def test_request_body_is_cache_friendly_and_explicit() -> None:
    tools = (
        {"name": "zeta", "description": "z", "input_schema": {"type": "object"}},
        {"name": "alpha", "description": "a", "input_schema": {"type": "object"}},
    )
    body = request(tools=tools, output_schema={"type": "object"}).body()
    assert [t["name"] for t in body["tools"]] == ["alpha", "zeta"]  # deterministic order
    assert body["tool_choice"] == {"type": "auto"}  # forced choice is a 400 on Opus 5.5
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"]["effort"] == "low"  # always explicit
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert all(b["cache_control"] == {"type": "ephemeral"} for b in body["system"])
    assert "temperature" not in body
    assert "fallbacks" not in body
    longer = request().followed_by({"role": "assistant", "content": "x"})
    assert longer.messages[:1] == request().messages  # appended, never edited
    with pytest.raises(ValueError, match="at most 4"):
        LLMRequest(HAIKU, "low", 10, ("a",) * 5, ()).body()


# --- cache ---------------------------------------------------------------------------------


def test_cache_key_covers_the_sample_and_the_harness() -> None:
    body = request().body()
    key = cache_key(body, run_index=0, harness_version="h1")
    assert key == cache_key(dict(reversed(body.items())), run_index=0, harness_version="h1")
    assert key != cache_key(body, run_index=1, harness_version="h1")
    assert key != cache_key(body, run_index=0, harness_version="h2")
    assert key != cache_key(request("other").body(), run_index=0, harness_version="h1")
    assert len(key) == 64


def test_cache_is_wal_sqlite_and_never_stores_errors(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite"
    cache = ResponseCache(path)
    assert cache.journal_mode() == "wal"
    cache.put("k1", {"model": HAIKU}, reply(), cost_usd=0.5, latency_ms=900)
    cache.put("k1", {"model": HAIKU}, reply("second"), cost_usd=9.0, latency_ms=1)  # first wins
    hit = cache.get("k1")
    assert hit is not None
    assert (hit.cost_usd, hit.latency_ms) == (0.5, 900)
    assert hit.response["content"][0]["text"] == '{"ok": true}'
    with pytest.raises(ValueError, match="never errors"):
        cache.put("k2", {}, {"type": "error", "error": {}}, cost_usd=0, latency_ms=0)
    assert "k1" in cache
    assert "k2" not in cache
    assert len(cache) == 1
    cache.close()
    reopened = ResponseCache(path)  # it persists
    assert reopened.get("k1") is not None
    reopened.close()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


# --- client --------------------------------------------------------------------------------


def test_live_call_is_costed_cached_and_replayed_for_free(tmp_path: Path) -> None:
    transport = FakeTransport([reply()])
    cache = ResponseCache(tmp_path / "c.sqlite")
    llm = client(transport, cache=cache)
    first = llm.call(request(), run_index=0)
    assert first.ok
    assert first.text() == '{"ok": true}'
    expected = cost_usd(MODELS.spec(HAIKU), Usage(100, 50, 0, 4000))
    record = first.record
    assert (record.source, record.cost_usd, record.spend_usd) == ("live", expected, expected)
    assert record.latency_ms == 1234
    assert record.served_model == HAIKU
    again = llm.call(request(), run_index=0)  # the transport has nothing left: must be the cache
    assert again.record.source == "cache"
    assert again.record.cached
    assert (again.record.cost_usd, again.record.spend_usd) == (expected, 0.0)
    assert again.record.latency_ms == 1234  # the original latency, so logs replay byte for byte
    assert again.response == first.response
    with pytest.raises(IndexError):  # another run index is another sample
        llm.call(request(), run_index=1)
    cache.close()


def test_retries_then_success() -> None:
    sleeps: list[float] = []
    transport = FakeTransport([rate_limited(), server_error(), disconnected(), reply()])
    result = client(transport, sleeps=sleeps).call(request())
    assert result.ok
    assert result.record.attempts == 4
    assert sleeps == [2.0, 4.0, 8.0]


def test_retries_run_out_into_an_infra_fault(tmp_path: Path) -> None:
    sleeps: list[float] = []
    cache = ResponseCache(tmp_path / "c.sqlite")
    transport = FakeTransport([rate_limited() for _ in range(RETRY_ATTEMPTS)])
    result = client(transport, sleeps=sleeps, cache=cache).call(request())
    assert not result.ok
    assert result.response is None
    assert result.record.fault == INFRA_FAULT
    assert "rate limited" in result.record.fault_detail
    assert len(transport.sent) == RETRY_ATTEMPTS
    assert len(sleeps) == RETRY_ATTEMPTS - 1
    assert len(cache) == 0  # errors are never cached
    with pytest.raises(LLMContentError):
        result.text()
    cache.close()


def test_other_api_errors_are_not_retried() -> None:
    transport = FakeTransport([bad_request(), reply()])
    with pytest.raises(LLMRequestError, match="400"):
        client(transport).call(request())
    assert len(transport.sent) == 1


def test_refusal_is_an_agent_fault_with_its_category(tmp_path: Path) -> None:
    refused = reply(stop="refusal", content=[], stop_details={"category": "cyber"})
    cache = ResponseCache(tmp_path / "c.sqlite")
    result = client(FakeTransport([refused]), cache=cache).call(request())
    assert not result.ok
    record = result.record
    assert (record.fault, record.refusal_category, record.stop_reason) == (
        AGENT_FAULT,
        "cyber",
        "refusal",
    )
    with pytest.raises(LLMContentError):  # stop_reason is checked before content is read
        _ = result.content
    assert len(cache) == 1  # a refusal is a response: the same sample replays
    cache.close()


def test_unfinished_stops_are_agent_faults() -> None:
    result = client(FakeTransport([reply(stop="max_tokens")])).call(request())
    assert result.record.fault == AGENT_FAULT
    assert "max_tokens" in result.record.fault_detail


def test_fallback_and_served_model_are_recorded() -> None:
    served = reply(model="claude-opus-5", fallback=True)
    transport = FakeTransport([served])
    result = client(transport).call(request(model=OPUS))
    assert transport.fallbacks == [True]  # Opus is called with server-side fallback
    assert result.record.fallback
    assert result.record.served_model == "claude-opus-5"
    haiku = FakeTransport([reply()])
    client(haiku).call(request())
    assert haiku.fallbacks == [False]


def test_normalize_reads_sdk_messages() -> None:
    message = Message.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": HAIKU,
            "content": [{"type": "text", "text": "hi"}],
            "stop_reason": "refusal",
            "stop_sequence": None,
            "stop_details": {"type": "refusal", "category": "bio", "explanation": "no"},
            "usage": {"input_tokens": 5, "output_tokens": 1, "cache_read_input_tokens": 7},
        }
    )
    data = normalize(message)
    assert data["stop_reason"] == "refusal"
    assert data["stop_details"]["category"] == "bio"
    assert data["usage"] == {
        "input_tokens": 5,
        "output_tokens": 1,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 7,
    }
    assert data["fallback"] is False
    assert FALLBACK_BETA.startswith("server-side-fallback-")


def test_the_live_transport_is_blocked_in_tests() -> None:
    transport = AnthropicTransport("sk-test-not-a-key")
    with pytest.raises(RuntimeError, match="tried to call the Anthropic API"):
        transport.send("k", request().body(), fallbacks=False)


def test_calls_are_charged_to_the_run(tmp_path: Path) -> None:
    book = guard(tmp_path)
    book.start_run("dev_x", "run1", "c4/haiku")
    llm = client(FakeTransport([reply(), reply()]), guard=book)
    llm.context = ("dev_x", "run1")
    llm.call(request("a"))
    llm.call(request("b"))
    run = book.ledger.data["experiments"]["dev_x"]["runs"]["run1"]
    assert run["calls"] == 2
    assert run["spend_usd"] == pytest.approx(
        2 * cost_usd(MODELS.spec(HAIKU), Usage(100, 50, 0, 4000))
    )
    assert book.ledger.spend_usd == pytest.approx(run["spend_usd"])
    fixture = client(FakeTransport([reply()], source="fixture"), guard=book)
    fixture.context = ("dev_x", "run1")
    assert fixture.call(request("c")).record.spend_usd == 0.0  # fixtures cost nothing


def test_a_breached_cap_stops_live_calls(tmp_path: Path) -> None:
    book = guard(tmp_path, global_cap_usd=0.0)
    with pytest.raises(BudgetExceeded):
        book.start_run("dev_x", "run1", "g")
    book = guard(tmp_path)
    book.start_run("dev_x", "run1", "g")
    book.ledger.data["spend_usd"] = 999.0
    llm = client(FakeTransport([reply()]), guard=book)
    llm.context = ("dev_x", "run1")
    with pytest.raises(BudgetExceeded, match="global cap"):
        llm.call(request())


# --- fixtures ------------------------------------------------------------------------------


def test_fixture_transport_and_recording(tmp_path: Path) -> None:
    body = request().body()
    live = FakeTransport([reply()])
    recorder = RecordingTransport(live, tmp_path / "rec")
    recorder.send("abc", body, fallbacks=False)
    stored = json.loads((tmp_path / "rec" / "abc.json").read_text(encoding="utf-8"))
    assert stored["origin"] == "recorded"
    assert stored["latency_ms"] == 1234
    assert stored["request"]["system"][0]["text"].startswith("sha256:")  # kept small
    replayed = FixtureTransport(tmp_path).send("abc", body, fallbacks=False)  # one folder down
    assert replayed == Sent(reply(), 1234)
    with pytest.raises(FixtureMissing, match="no recorded response"):
        FixtureTransport(tmp_path).send("nope", body, fallbacks=False)
    write_fixture(tmp_path, "top", body, reply(), latency_ms=5, origin="synthetic")
    assert FixtureTransport(tmp_path).send("top", body, fallbacks=False).latency_ms == 5


# --- budget --------------------------------------------------------------------------------


def test_every_experiment_needs_a_bucket() -> None:
    config = load_budget_config()
    assert config.global_cap_usd == 400
    assert config.bucket_for("dev_baselines").name == "dev"
    assert config.bucket_for("adhoc").name == "dev"
    assert config.bucket_for("v1_main").name == "v1_test"
    assert sum(b.cap_usd for b in config.buckets) == 400
    with pytest.raises(BudgetError, match="no budget bucket"):
        config.bucket_for("mystery")


def test_ledger_persists_spend(tmp_path: Path) -> None:
    book = guard(tmp_path)
    book.start_run("dev_x", "r1", "g")
    book.charge("dev_x", "r1", cost_usd=0.25, spend_usd=0.25)
    book.finish_run("dev_x", "r1")
    reloaded = Ledger.load(tmp_path / "ledger.json")
    assert reloaded.spend_usd == 0.25
    assert reloaded.bucket_spend("dev") == 0.25
    assert reloaded.data["experiments"]["dev_x"]["runs"]["r1"]["status"] == "done"
    (tmp_path / "bad.json").write_text('{"version": 99}', encoding="utf-8")
    with pytest.raises(BudgetError, match="ledger version"):
        Ledger.load(tmp_path / "bad.json")
    with pytest.raises(BudgetError, match="wasn't started"):
        book.charge("dev_x", "ghost", cost_usd=0, spend_usd=0)


def test_a_run_over_its_cap_is_aborted(tmp_path: Path) -> None:
    book = guard(tmp_path, run_cap_usd=1.0)
    book.start_run("dev_x", "r1", "g")
    book.charge("dev_x", "r1", cost_usd=0.6, spend_usd=0.6)
    with pytest.raises(RunAborted, match="per-run cap"):
        book.charge("dev_x", "r1", cost_usd=0.6, spend_usd=0.6)
    with pytest.raises(RunAborted, match="aborted"):
        book.check_call("dev_x", "r1")
    book.finish_run("dev_x", "r1")  # stays aborted
    assert book.ledger.data["experiments"]["dev_x"]["runs"]["r1"]["status"] == "aborted"
    book.start_run("dev_x", "r1", "g")  # a re-run starts clean
    assert book.ledger.data["experiments"]["dev_x"]["runs"]["r1"]["attempts"] == 2


def test_a_run_over_five_times_the_median_is_aborted(tmp_path: Path) -> None:
    book = guard(tmp_path)
    for i, cost in enumerate((0.10, 0.12, 0.08)):
        book.start_run("dev_x", f"r{i}", "c4/haiku")
        book.charge("dev_x", f"r{i}", cost_usd=cost, spend_usd=0.0)
        book.finish_run("dev_x", f"r{i}")
    assert book.median_cost("dev_x", "c4/haiku") == pytest.approx(0.10)
    assert book.median_cost("dev_x", "other") is None  # too few runs to judge
    book.start_run("dev_x", "other", "other")
    book.charge("dev_x", "other", cost_usd=5.0, spend_usd=0.0)  # no median for its group yet
    book.start_run("dev_x", "loop", "c4/haiku")
    book.charge("dev_x", "loop", cost_usd=0.45, spend_usd=0.0)
    with pytest.raises(RunAborted, match="5x the median"):
        book.charge("dev_x", "loop", cost_usd=0.1, spend_usd=0.0)


def test_budget_config_validates() -> None:
    with pytest.raises(ValueError, match="buckets"):
        BudgetConfig.model_validate({"global_cap_usd": 1, "run_cap_usd": 1, "buckets": []})


# --- tool loop -----------------------------------------------------------------------------


def tool_use(*calls: tuple[str, str, dict[str, Any]]) -> dict[str, Any]:
    blocks = [{"type": "thinking", "thinking": "", "signature": "sig"}]
    blocks += [{"type": "tool_use", "id": i, "name": n, "input": a} for i, n, a in calls]
    return reply(stop="tool_use", content=blocks)


def test_tool_loop_runs_parallel_calls_in_one_reply() -> None:
    def add(args: dict[str, Any]) -> Any:
        return {"sum": args["a"] + args["b"]}

    def strict(args: dict[str, Any]) -> Any:
        raise ToolError("truck T9 doesn't exist")

    transport = FakeTransport(
        [
            tool_use(("t1", "add", {"a": 1, "b": 2}), ("t2", "dispatch", {"truck": "T9"})),
            tool_use(("t3", "nope", {})),
            reply("done"),
        ]
    )
    out = run_tool_loop(client(transport), request(), {"add": add, "dispatch": strict})
    assert out.stopped == "end_turn"
    assert len(out.calls) == 3
    assert [t.is_error for t in out.tool_calls] == [False, True, True]
    assert out.errors == 2
    second = transport.sent[1]["messages"]
    assert second[0] == request().messages[0]  # history only grows
    assert second[1]["content"][0]["type"] == "thinking"  # the assistant turn goes back as is
    results = second[2]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]  # all results, one message
    assert json.loads(results[0]["content"]) == {"sum": 3}
    assert results[1]["is_error"] is True
    assert "T9" in results[1]["content"]
    assert "is_error" not in results[0]


def test_tool_loop_stops_at_the_cap_and_on_faults() -> None:
    forever = FakeTransport([tool_use((f"t{i}", "noop", {})) for i in range(10)])
    out = run_tool_loop(client(forever), request(), {"noop": lambda a: "ok"}, max_calls=3)
    assert out.stopped == "max_calls"
    assert len(out.calls) == 3
    refusal = FakeTransport([reply(stop="refusal", content=[])])
    assert run_tool_loop(client(refusal), request(), {}).stopped == "fault"
    empty = FakeTransport([reply(stop="tool_use", content=[])])
    assert run_tool_loop(client(empty), request(), {}).stopped == "no_tools"


# --- batches -------------------------------------------------------------------------------


class FakeBatchAPI:
    def __init__(self, outcomes: Callable[[list[dict[str, Any]]], list[BatchOutcome]]) -> None:
        self.outcomes = outcomes
        self.created: list[list[dict[str, Any]]] = []
        self.polls = 0

    def create(self, requests: list[dict[str, Any]]) -> str:
        self.created.append(requests)
        return "batch_1"

    def ended(self, batch_id: str) -> bool:
        self.polls += 1
        return self.polls >= 2

    def results(self, batch_id: str) -> Iterable[BatchOutcome]:
        return self.outcomes(self.created[-1])


def test_batches_precompute_into_the_cache(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path / "c.sqlite")
    llm = client(FakeTransport([]), cache=cache)
    items = [BatchItem(request(t)) for t in ("a", "b", "c", "a")]

    def outcomes(sent: list[dict[str, Any]]) -> list[BatchOutcome]:
        ids = {r["params"]["messages"][0]["content"]: r["custom_id"] for r in sent}
        planned = [  # out of order, one error
            ("c", BatchOutcome(ids.get("c", ""), "succeeded", reply("C"))),
            ("a", BatchOutcome(ids.get("a", ""), "succeeded", reply("A"))),
            ("b", BatchOutcome(ids.get("b", ""), "errored", error="overloaded_error")),
        ]
        return [outcome for text, outcome in planned if text in ids]

    api = FakeBatchAPI(outcomes)
    book = guard(tmp_path)
    sleeps: list[float] = []
    report = MessageBatches(llm, api, book, "dev_x", sleep=sleeps.append).run(items)
    assert report.submitted == 3
    assert report.succeeded == 2
    assert list(report.failed.values()) == ["overloaded_error"]
    assert sleeps == [60.0]
    one = cost_usd(MODELS.spec(HAIKU), Usage(100, 50, 0, 4000), batch_discount_pct=50)
    assert report.spend_usd == pytest.approx(2 * one)
    assert book.ledger.data["batches"]["batch_1"]["requests"] == 3
    assert book.ledger.spend_usd == pytest.approx(2 * one)
    hit = llm.call(request("c"))  # the run now replays from the cache
    assert hit.record.source == "cache"
    assert hit.text() == "C"
    assert len(cache) == 2  # the errored one isn't cached
    again = MessageBatches(llm, api, book, "dev_x", sleep=sleeps.append).run(items)
    assert again.submitted == 1
    assert again.already_cached == 2
    cache.close()


def test_batches_go_through_the_budget_guard(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path / "c.sqlite")
    llm = client(FakeTransport([]), cache=cache)
    api = FakeBatchAPI(lambda sent: [])
    book = guard(tmp_path)
    book.ledger.data["buckets"]["dev"] = 59.9999  # nearly at the dev cap
    with pytest.raises(BudgetExceeded):
        MessageBatches(llm, api, book, "dev_x").run([BatchItem(request("x"))])
    assert api.created == []  # refused before anything was sent
    with pytest.raises(ValueError, match="response cache"):
        MessageBatches(client(FakeTransport([])), api, book, "dev_x")
    cache.close()


# --- live guard ----------------------------------------------------------------------------


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def test_live_mode_waits_for_the_locked_test_bank(tmp_path: Path) -> None:
    problems = live_problems(tmp_path)
    assert any("doesn't exist" in p for p in problems)
    assert any("PRD 8.4" in p for p in problems)
    with pytest.raises(LiveRefused, match="test bank"):
        require_live(tmp_path)
    with pytest.raises(LiveRefused, match="test bank"):
        make_client(LLMSettings(mode="live", root=tmp_path))
    # A locked bank that isn't committed is still refused; committed, it's allowed.
    bank = tmp_path / "data" / "phrasing" / "test"
    bank.mkdir(parents=True)
    (bank / "road_blocked.yaml").write_text("placeholder: true\n", encoding="utf-8")
    write_lock(bank)
    _git(tmp_path, "init", "-q")
    assert any("isn't committed" in p for p in live_problems(tmp_path))
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "bank")
    assert live_problems(tmp_path) == []
    (bank / "road_blocked.yaml").write_text("placeholder: false\n", encoding="utf-8")
    assert live_problems(tmp_path)  # changed since the lock


def test_api_key_comes_from_the_environment_or_dotenv(tmp_path: Path) -> None:
    assert api_key(tmp_path, {API_KEY_VAR: " sk-env "}) == "sk-env"
    with pytest.raises(LiveRefused, match="no ANTHROPIC_API_KEY"):
        api_key(tmp_path, {})
    (tmp_path / ".env").write_text(f"OTHER=1\nexport {API_KEY_VAR}='sk-dotenv'\n", encoding="utf-8")
    assert api_key(tmp_path, {}) == "sk-dotenv"


def test_dotenv_is_never_committed() -> None:
    root = Path(__file__).resolve().parents[1]
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", ".env"], cwd=root, check=False, capture_output=True
    )
    assert ignored.returncode == 0


def test_fixture_clients_spend_nothing_and_keep_the_ledger_in_memory(tmp_path: Path) -> None:
    llm = make_client(LLMSettings(fixtures=tmp_path))
    assert llm.transport.source == "fixture"
    assert not llm.live
    assert llm.guard is not None
    assert llm.guard.ledger.path is None
    cached = make_client(LLMSettings(fixtures=tmp_path, cache=tmp_path / "c.sqlite"))
    assert cached.cache is not None
    cached.cache.close()
