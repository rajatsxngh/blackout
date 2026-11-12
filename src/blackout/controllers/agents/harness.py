"""The LLM agent harness (docs/PRD.md 8.8): C5 Single and C6 Single+Solver play through it.

**Each decision point is a fresh conversation**, so no history is ever edited:

- the cached prefix: the tools (sorted), then system block 1 (the prompt, the same for every
  scenario) and system block 2 (this city's map and gazetteer, the same all run). Both system
  blocks are cache breakpoints (``LLMRequest``);
- one user message: the rendered observation, the notes carried over (at most 800
  characters), the belief table (at most 30 rows) and the last 5 actions with their outcomes.

Then the manual tool loop (``blackout.llm.loop``): append-only, at most 6 model calls,
``tool_choice: auto``, strict tools, every tool result of a reply sent back in one user
message. Action tools call ``env.act()`` at once (the harness is an ``acts_live``
contestant, driven through an ``Actor``). The turn ends when the model calls ``end_turn``,
stops calling tools, hits the call cap, or a call faults.

**The belief** (``BeliefState``) persists across decision points: telemetry and confirm
replies go in automatically, report claims only through the agent's ``update_belief``.

**Faults** (PRD 8.8): ``LLMClient`` retries and classifies. Out of retries, the call is an
``infra_fault`` and the rest of the decision point is a logged no-op (actions already applied
stay applied: the clock didn't move). A ``refusal`` (or another unfinished stop) is an
``agent_fault``, logged with its category, and counts against the contestant. The model that
served each call is logged, and fallback-served calls are counted, so such runs can be flagged.

**Logs** (deterministic given the replies, D033/D089): ``dp.llm`` has usage, cost, latency (as
first recorded) and the cache keys; ``dp.info.agent`` what the turn did (tools used, errors,
invalid actions, solver calls, plans, report verdicts, how it stopped); ``dp.notes`` the notes
carried to the next turn. Full conversations go to ``transcripts.jsonl.gz``; solve wall times
to summary.json only.
"""

from collections import deque
from collections.abc import Sequence
from typing import Any

from blackout.controllers.agents.render import (
    RECENT_ACTIONS,
    city_text,
    prompt_source,
    system_prompt,
    user_message,
)
from blackout.controllers.agents.tools import TurnTools, tool_definitions
from blackout.controllers.base import Actor
from blackout.controllers.keyword_parser import gazette_of
from blackout.controllers.llm_parser import known_subjects, llm_summary
from blackout.controllers.solver.contract import SolverResult
from blackout.info.belief import BeliefState
from blackout.info.observation import Observation
from blackout.llm.client import AGENT_FAULT, INFRA_FAULT, CallRecord, LLMClient, LLMRequest
from blackout.llm.config import Effort
from blackout.llm.loop import MAX_CALLS, run_tool_loop
from blackout.sim.actions import AnyAction
from blackout.sim.config import SimConfig
from blackout.sim.hashing import digest

AGENT_HARNESS_VERSION = "m11.2"
"""Part of the agents' prompt hash: bump it when the harness changes what the agents are shown
or how their replies are read."""

RECENT_ACTION_CHARS = 220


def prompt_hash(solver: bool, config: SimConfig | None = None) -> str:
    """The agent's prompt version: the prompt files, the filled-in system text, the tool
    definitions and the harness version (docs/PROMPTS.md)."""
    rules = config or SimConfig()
    return digest(
        {
            "source": prompt_source(solver),
            "system": system_prompt(solver, rules, MAX_CALLS),
            "tools": list(tool_definitions(solver)),
            "harness": AGENT_HARNESS_VERSION,
        }
    )


class AgentController:
    """An LLM coordinator for the whole city (role ``all``). Subclasses set ``name`` and
    ``solver``."""

    name = "llm-agent"
    solver = False
    acts_live = True

    def __init__(
        self,
        client: LLMClient,
        *,
        model: str,
        effort: Effort,
        run_index: int = 0,
        config: SimConfig | None = None,
        max_calls: int = MAX_CALLS,
    ) -> None:
        self.client = client
        self.model = model
        self.effort: Effort = effort
        self.run_index = run_index
        self.config = config or SimConfig()
        self.max_calls = max_calls
        self.max_tokens = client.models.max_tokens(model, "agent")
        self.prompt_hash = prompt_hash(self.solver, self.config)
        self._system = system_prompt(self.solver, self.config, max_calls)
        self._tools = tool_definitions(self.solver)
        self.reset()

    def reset(self) -> None:
        self.belief = BeliefState()
        self.notes = ""
        self._recent: deque[str] = deque(maxlen=RECENT_ACTIONS)
        self._verdicts: dict[str, str] = {}
        self._city: str | None = None
        self._known: dict[str, frozenset[str]] = {}
        self._last_plan: SolverResult | None = None
        self._dp = 0
        self._log: dict[str, Any] | None = None
        self._transcript: list[dict[str, Any]] = []
        self._all_calls: list[CallRecord] = []
        self._turns: list[dict[str, Any]] = []
        self.solve_stats: list[dict[str, object]] = []

    def decide(self, observation: Observation) -> Sequence[AnyAction]:
        raise RuntimeError(f"{self.name} acts as it goes: drive it with decide_live (run_episode)")

    # --- one decision point -------------------------------------------------------------------

    def _take(self, observation: Observation) -> None:
        """Telemetry, report arrivals and confirm replies into the persistent belief."""
        belief = self.belief
        belief.apply_telemetry(observation)
        for report in observation.reports:
            belief.reports[report.id] = report
        for reply in observation.confirmations:
            belief.apply_confirm(reply)

    def request_for(self, observation: Observation) -> LLMRequest:
        """This decision point's opening request (after ``_take``)."""
        if self._city is None:
            self._city = city_text(observation)
            self._known = known_subjects(gazette_of(observation))
        message = user_message(
            observation,
            self.belief,
            decision=self._dp,
            notes=self.notes,
            recent=tuple(self._recent),
        )
        return LLMRequest(
            model=self.model,
            effort=self.effort,
            max_tokens=self.max_tokens,
            system=(self._system, self._city),
            messages=({"role": "user", "content": message},),
            tools=self._tools,
        )

    def decide_live(self, observation: Observation, actor: Actor) -> None:
        self._take(observation)
        request = self.request_for(observation)
        tools = TurnTools(
            actor=actor,
            belief=self.belief,
            config=self.config,
            known=self._known,
            verdicts=self._verdicts,
            solver=self.solver,
            previous_plan=self._last_plan,
        )
        loop = run_tool_loop(
            self.client,
            request,
            tools.handlers(),
            run_index=self.run_index,
            max_calls=self.max_calls,
            stop=lambda: tools.ended,
        )
        if tools.notes is not None:
            self.notes = tools.notes
        self._last_plan = tools.previous_plan
        self.solve_stats += tools.solve_stats
        for result in tools.taken:
            reason = result.action.reason
            line = f"t{observation.tick} {result.summary()}" + (f' ("{reason}")' if reason else "")
            self._recent.append(line[:RECENT_ACTION_CHARS])
        self._all_calls += loop.calls
        last = loop.calls[-1] if loop.calls else None
        fault = None if last is None else last.fault
        turn = {
            "stopped": loop.stopped,
            "calls": len(loop.calls),
            "tools": tools.used,
            "tool_errors": tools.tool_errors,
            "actions": len(tools.taken),
            "rejected": tools.rejected,
            "invalid_inputs": tools.invalid_inputs,
            "fault": fault,
            "fault_detail": None if last is None or fault is None else last.fault_detail,
            "refusal_category": None if last is None else last.refusal_category,
            "verdicts": dict(sorted(tools.new_verdicts.items())),
        }
        if self.solver:
            turn["solver_calls"] = tools.solver_calls
            turn["plans"] = list(tools.plans)
            turn["accepted_plans"] = list(tools.accepted)
        self._turns.append(turn)
        self._log = {"llm": llm_summary(loop.calls), "info": {"agent": turn}, "notes": self.notes}
        entry: dict[str, Any] = {
            "dp": self._dp,
            "tick": observation.tick,
            "user": request.messages[0]["content"],
            "calls": [c.to_dict() for c in loop.calls],
            "responses": [None if r is None else r.get("content") for r in loop.responses],
            "tool_results": loop.tool_results,
            "turn": turn,
        }
        if self._dp == 0:
            entry["system"] = list(request.system)
        self._transcript.append(entry)
        self._dp += 1

    # --- logging hooks -------------------------------------------------------------------------

    def decision_log(self) -> dict[str, Any] | None:
        return self._log

    def transcripts(self) -> list[dict[str, Any]]:
        return list(self._transcript)

    def run_stats(self) -> dict[str, object]:
        calls = self._all_calls
        totals = llm_summary(calls)
        for per_call in ("cache_keys", "stop_reasons"):
            totals.pop(per_call)
        turns = self._turns
        attempted = sum(t["actions"] + t["invalid_inputs"] for t in turns)
        invalid = sum(t["rejected"] + t["invalid_inputs"] for t in turns)
        stopped: dict[str, int] = {}
        for t in turns:
            stopped[t["stopped"]] = stopped.get(t["stopped"], 0) + 1
        infra_dps = sum(1 for t in turns if t["fault"] == INFRA_FAULT)
        stats: dict[str, object] = {
            "llm": {
                **totals,
                "spend_usd": round(sum(c.spend_usd for c in calls), 6),
                "sources": _count(c.source for c in calls),
                # PRD 8.8: a run with more than 10% infra-fault decision points is re-run.
                "infra_fault_pct": _pct(infra_dps, len(turns)),
                "fallback_run": any(c.fallback for c in calls),
            },
            "agent": {
                "decision_points": len(turns),
                "calls_per_decision": round(len(calls) / len(turns), 3) if turns else 0.0,
                "actions": sum(t["actions"] for t in turns),
                # Actions the engine saw, plus action inputs that never made a valid action.
                "attempted_actions": attempted,
                "invalid_actions": invalid,
                "invalid_action_rate": round(invalid / attempted, 4) if attempted else None,
                "tool_errors": sum(t["tool_errors"] for t in turns),
                "agent_fault_decisions": sum(1 for t in turns if t["fault"] == AGENT_FAULT),
                "infra_fault_decisions": infra_dps,
                "stopped": dict(sorted(stopped.items())),
            },
        }
        if self.solver:
            times = sorted(float(s["wall_time_s"]) for s in self.solve_stats)  # type: ignore[arg-type]
            stats["solver"] = {
                "solves": len(times),
                "wall_time_s_max": times[-1] if times else 0.0,
                "wall_time_s_total": round(sum(times), 3),
            }
        return stats


def _count(values: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items()))


def _pct(part: int, whole: int) -> float:
    return 0.0 if not whole else round(100 * part / whole, 2)
