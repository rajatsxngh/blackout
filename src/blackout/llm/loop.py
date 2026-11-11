"""The manual tool loop (docs/PRD.md 8.8): append-only, capped, strict tools, parallel calls.

Each decision point is one fresh conversation. The loop:

1. calls the model (``tool_choice: auto``; forced choice is rejected on Opus 5.5);
2. stops unless the call finished with ``stop_reason: tool_use`` (a fault stops it too);
3. runs **every** ``tool_use`` block of the reply and sends all the results back in **one**
   user message (a handler's ``ToolError`` becomes an ``is_error`` result: for action tools
   that counts as an invalid action);
4. appends the assistant turn unchanged (thinking blocks included) and repeats, at most
   ``max_calls`` model calls in all. A ``stop`` callback lets a tool end the conversation
   (the agents' ``end_turn``): it's checked after every round of tool results, and if it says
   so no further call is made.

History is never edited, so the cached prefix stays valid and preserved thinking holds.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from blackout.llm.client import CallRecord, LLMClient, LLMRequest, LLMResult

MAX_CALLS = 6


class ToolError(Exception):
    """A tool refused its input; the message goes back to the model as an error result."""


type ToolHandler = Callable[[dict[str, Any]], Any]


@dataclass(frozen=True, slots=True)
class ToolCall:
    name: str
    input: dict[str, Any]
    output: str
    is_error: bool


@dataclass(slots=True)
class LoopResult:
    calls: list[CallRecord] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    final: LLMResult | None = None
    stopped: str = ""  # end_turn, fault, max_calls, no_tools, stopped (by the stop callback)
    responses: list[dict[str, Any] | None] = field(default_factory=list)
    """Each call's normalized response (None for an infra fault), for transcripts."""
    tool_results: list[list[dict[str, Any]]] = field(default_factory=list)
    """The tool_result blocks sent back after each call that asked for tools."""

    @property
    def errors(self) -> int:
        return sum(1 for t in self.tool_calls if t.is_error)


def _render(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True)


def run_tool(handlers: Mapping[str, ToolHandler], block: dict[str, Any]) -> ToolCall:
    name, args = block["name"], dict(block.get("input") or {})
    handler = handlers.get(name)
    if handler is None:
        return ToolCall(name, args, f"unknown tool {name!r}", True)
    try:
        return ToolCall(name, args, _render(handler(args)), False)
    except ToolError as error:
        return ToolCall(name, args, str(error), True)


def run_tool_loop(
    client: LLMClient,
    request: LLMRequest,
    handlers: Mapping[str, ToolHandler],
    *,
    run_index: int = 0,
    max_calls: int = MAX_CALLS,
    stop: Callable[[], bool] | None = None,
) -> LoopResult:
    out = LoopResult()
    for _ in range(max_calls):
        result = client.call(request, run_index=run_index)
        out.calls.append(result.record)
        out.responses.append(result.response)
        out.final = result
        if not result.ok:
            out.stopped = "fault"
            return out
        if result.record.stop_reason != "tool_use":
            out.stopped = "end_turn"
            return out
        blocks = result.tool_uses()
        if not blocks:
            out.stopped = "no_tools"
            return out
        results = []
        for block in blocks:
            call = run_tool(handlers, block)
            out.tool_calls.append(call)
            item: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": block["id"],
                "content": call.output,
            }
            if call.is_error:
                item["is_error"] = True
            results.append(item)
        out.tool_results.append(results)
        if stop is not None and stop():
            out.stopped = "stopped"
            return out
        request = request.followed_by(
            {"role": "assistant", "content": result.content},
            {"role": "user", "content": results},
        )
    out.stopped = "max_calls"
    return out
