"""The run log format (docs/PRD.md section 8.12).

A run log is JSON Lines, one record per line, gzip-compressed. Every record carries the log
schema version ``v``, its ``type`` and its tick ``t``:

    header  who ran which scenario under which rules; the scenario is embedded in full
    tick    the state hash after that tick's events, and the events themselves
    dp      a decision point: why it was opened, what was observed, every action and outcome
    end     how the run ended, its final metrics and its run hash

A run log is deterministic: the same run gives the same text on any machine. Anything about
the machine or the wall clock goes in summary.json instead. Fields marked "reserved" stay empty
until the milestone that fills them (LLM usage in M10, parsing and injection data in M8 and
M10). Since M4, ``dp.belief`` holds the contestant's belief digest after it decided.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, TypeAdapter

LOG_VERSION = 1
LOG_FILE = "run.jsonl.gz"
SUMMARY_FILE = "summary.json"


class LogError(Exception):
    """A run log can't be read, or doesn't match what it claims to record."""


class UnsupportedLogVersionError(LogError):
    """The log was written in a schema version this build can't read."""


class LogFormatError(LogError):
    """The log isn't valid JSON Lines, or a record doesn't match the schema."""


class _Part(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Record(_Part):
    v: Literal[1] = LOG_VERSION
    t: NonNegativeInt


class ScenarioRef(_Part):
    id: str
    hash: str = Field(description="scenario_digest() of the embedded spec.")
    spec: dict[str, Any] = Field(description="The full scenario, so the log stands alone.")


class ContestantRef(_Part):
    name: str
    model: str | None = None
    effort: str | None = None
    run_index: NonNegativeInt = 0
    prompt_hash: str | None = None  # reserved for M10


class EngineRef(_Part):
    version: str
    config: dict[str, Any]
    config_hash: str


class HeaderRecord(_Record):
    type: Literal["header"] = "header"
    run_id: str
    scenario: ScenarioRef
    contestant: ContestantRef
    engine: EngineRef


class TickRecord(_Record):
    type: Literal["tick"] = "tick"
    h: str = Field(description="State hash after this tick's events, before any decisions.")
    ev: list[dict[str, Any]] = Field(default_factory=list, description="Events applied.")


class TriggerRecord(_Part):
    kind: str
    subject: str = ""
    detail: str = ""


class ActionRecord(_Part):
    raw: dict[str, Any] = Field(description="The action exactly as the contestant gave it.")
    accepted: bool
    reason: str | None = None
    message: str = ""
    route: list[str] = Field(default_factory=list)
    eta_minutes: int | None = None


class LlmRecord(_Part):
    """Reserved for M10: what a decision point cost in model calls."""

    calls: NonNegativeInt = 0
    input_tokens: NonNegativeInt = 0
    output_tokens: NonNegativeInt = 0
    cache_creation_input_tokens: NonNegativeInt = 0
    cache_read_input_tokens: NonNegativeInt = 0
    cost_usd: float = 0.0
    latency_ms: NonNegativeInt = 0
    served_models: list[str] = Field(default_factory=list)
    stop_reasons: list[str] = Field(default_factory=list)
    cache_keys: list[str] = Field(default_factory=list)
    agent_faults: NonNegativeInt = 0
    infra_faults: NonNegativeInt = 0
    refusals: NonNegativeInt = 0
    fallback_calls: NonNegativeInt = 0


class DecisionPointRecord(_Record):
    type: Literal["dp"] = "dp"
    i: NonNegativeInt
    role: str
    triggers: list[TriggerRecord]
    obs: str = Field(description="Digest of the observation the role was shown.")
    actions: list[ActionRecord] = Field(default_factory=list)
    belief: dict[str, Any] | None = None  # the contestant's belief digest, if it keeps one
    notes: str | None = None  # reserved for M10: notes an agent carries to its next turn
    llm: LlmRecord | None = None  # reserved for M10
    info: dict[str, Any] | None = None  # reserved for M8 and M10: parse and injection data


class EndRecord(_Record):
    type: Literal["end"] = "end"
    reason: str
    decision_points: NonNegativeInt
    run_hash: str
    metrics: dict[str, Any]


type AnyRecord = HeaderRecord | TickRecord | DecisionPointRecord | EndRecord

RECORD: TypeAdapter[HeaderRecord | TickRecord | DecisionPointRecord | EndRecord] = TypeAdapter(
    Annotated[
        HeaderRecord | TickRecord | DecisionPointRecord | EndRecord, Field(discriminator="type")
    ]
)
