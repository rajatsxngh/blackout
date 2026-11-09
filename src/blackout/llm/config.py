"""Models, effort and the dated price table (``configs/models.yaml``, docs/PRD.md 8.8)."""

from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, NonNegativeFloat, PositiveInt, model_validator

MODELS_CONFIG = Path(__file__).resolve().parents[3] / "configs" / "models.yaml"

type Effort = Literal["low", "medium", "high", "xhigh", "max"]
type Purpose = Literal["parser", "agent"]


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Prices(_Config):
    """USD per million tokens."""

    input: NonNegativeFloat
    output: NonNegativeFloat
    cache_write_5m: NonNegativeFloat
    cache_read: NonNegativeFloat


class LongContext(Prices):
    above_input_tokens: PositiveInt


class ModelSpec(_Config):
    fallbacks: bool
    max_tokens: dict[str, PositiveInt]
    prices: Prices
    long_context: LongContext | None = None


class ModelsConfig(_Config):
    prices_as_of: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    price_source: str
    dev_model: str
    final_models: tuple[str, ...]
    optional_models: tuple[str, ...] = ()
    effort: dict[str, Effort]
    batch_discount_pct: int = Field(ge=0, le=100)
    models: dict[str, ModelSpec]

    @model_validator(mode="after")
    def _known(self) -> Self:
        named = [self.dev_model, *self.final_models, *self.optional_models]
        missing = sorted({m for m in named if m not in self.models})
        if missing:
            raise ValueError(f"models without a price table entry: {missing}")
        for purpose in ("parser", "agent"):
            if purpose not in self.effort:
                raise ValueError(f"no effort set for {purpose!r} (effort is always explicit)")
        return self

    def spec(self, model: str) -> ModelSpec:
        if model not in self.models:
            raise KeyError(f"unknown model {model!r}; configured: {', '.join(self.models)}")
        return self.models[model]

    def max_tokens(self, model: str, purpose: Purpose) -> int:
        return self.spec(model).max_tokens[purpose]


def load_models_config(path: Path = MODELS_CONFIG) -> ModelsConfig:
    with path.open(encoding="utf-8") as handle:
        return ModelsConfig.model_validate(yaml.safe_load(handle))
