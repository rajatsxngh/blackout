"""Token usage and what it costs, from the dated price table (docs/PRD.md 8.8).

Costs are rounded to whole micro-dollars so sums are exact and logs stay stable.
"""

from dataclasses import dataclass
from typing import Any

from blackout.llm.config import ModelSpec, Prices

PER_MILLION = 1_000_000


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_creation_input_tokens + other.cache_creation_input_tokens,
            self.cache_read_input_tokens + other.cache_read_input_tokens,
        )

    @property
    def prompt_tokens(self) -> int:
        """Every input token of the request, cached or not."""
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Usage":
        data = data or {}
        return cls(
            int(data.get("input_tokens") or 0),
            int(data.get("output_tokens") or 0),
            int(data.get("cache_creation_input_tokens") or 0),
            int(data.get("cache_read_input_tokens") or 0),
        )


def prices_for(spec: ModelSpec, usage: Usage) -> Prices:
    """The rates that apply to a request of this size (long-context pricing above the cut)."""
    long = spec.long_context
    if long is not None and usage.prompt_tokens > long.above_input_tokens:
        return long
    return spec.prices


def cost_usd(spec: ModelSpec, usage: Usage, *, batch_discount_pct: int = 0) -> float:
    """What ``usage`` costs, in USD rounded to the micro-dollar (thinking bills as output)."""
    rates = prices_for(spec, usage)
    micro = (
        usage.input_tokens * rates.input
        + usage.output_tokens * rates.output
        + usage.cache_creation_input_tokens * rates.cache_write_5m
        + usage.cache_read_input_tokens * rates.cache_read
    )  # tokens x USD per million = micro-dollars
    micro = micro * (100 - batch_discount_pct) / 100
    return round(micro) / PER_MILLION
