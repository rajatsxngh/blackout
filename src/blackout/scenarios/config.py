"""The generator's city config (``configs/city/default.yaml``), validated by Pydantic."""

from pathlib import Path
from typing import Annotated, Self

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, PositiveInt, model_validator

from blackout.sim.city import MAX_HEIGHT, MAX_WIDTH
from blackout.sim.hashing import digest

DEFAULT_CITY_CONFIG = Path("configs") / "city" / "default.yaml"


def _ordered(value: tuple[int, int]) -> tuple[int, int]:
    if value[0] > value[1]:
        raise ValueError(f"range low {value[0]} is above high {value[1]}")
    return value


type Range = Annotated[tuple[int, int], AfterValidator(_ordered)]
"""An inclusive [low, high] range of whole units."""


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GridCfg(_Cfg):
    width: int = Field(ge=2, le=MAX_WIDTH)
    height: int = Field(ge=2, le=MAX_HEIGHT)


class ArterialCfg(_Cfg):
    travel_minutes: Range


class ZoneCfg(_Cfg):
    blocks: tuple[PositiveInt, PositiveInt]
    demand_kw: Range
    demand_step_kw: PositiveInt
    critical: PositiveInt
    essential: int = Field(ge=0)


class SubstationCfg(_Cfg):
    capacity_kw: Range
    capacity_step_kw: PositiveInt
    headroom_kw: PositiveInt


class HospitalCfg(_Cfg):
    tank_l: PositiveInt
    fuel_l: Range
    fuel_step_l: PositiveInt
    burn_lpm: PositiveInt


class DepotCfg(_Cfg):
    count: PositiveInt
    stock_l: Range
    stock_step_l: PositiveInt
    min_separation: int = Field(ge=0)


class TruckCfg(_Cfg):
    count: PositiveInt
    capacity_l: PositiveInt


class CrewCfg(_Cfg):
    count: PositiveInt


class FleetCfg(_Cfg):
    scatter_pct: int = Field(ge=0, le=100)  # chance each truck starts out on the road
    loaded_pct: int = Field(ge=0, le=100)  # chance a scattered truck carries fuel
    load_l: Range


class T01Cfg(_Cfg):
    block_tick: Range
    report_delay: Range
    caller_first_pct: int = Field(ge=0, le=100)
    repair_minutes: Range
    min_naive_loss_minutes: PositiveInt
    clear_tick: Range


class T02Cfg(_Cfg):
    fail_tick: Range
    repair_minutes: Range
    overload_kw: Range
    hospital_fuel_l: Range


class T03Cfg(_Cfg):
    repair_minutes: Range
    near_minutes: Range  # the one loaded truck's drive to the nearer hospital
    far_minutes: PositiveInt  # every other truck is at least this far from both hospitals
    second_fuel_minutes: Range  # the other hospital's fuel, in minutes of burn


class T04Cfg(_Cfg):
    loaded_drive_minutes: Range
    breakdown_pct: Range
    repair_minutes: Range
    side_block_pct: int = Field(ge=0, le=100)


class T05Cfg(_Cfg):
    repair_minutes: Range
    spike_tick: Range
    spike_lpm: Range
    second_fuel_minutes: Range


class T06Cfg(_Cfg):
    repair_minutes: Range
    low_stock_l: Range
    far_stock_l: Range


class T07Cfg(_Cfg):
    short_repair_minutes: Range
    long_repair_minutes: Range
    trucks: PositiveInt
    depot_stock_l: Range
    second_fuel_minutes: Range


class T08Cfg(_Cfg):
    reopen_tick: Range
    caller_first_pct: int = Field(ge=0, le=100)
    repair_minutes: Range
    min_gain_minutes: PositiveInt  # the reopened road must save at least this much


class T09Cfg(_Cfg):
    blocks: Range
    block_tick: Range
    report_delay: Range
    caller_only_pct: int = Field(ge=0, le=100)
    unreported_pct: int = Field(ge=0, le=100)
    repair_minutes: Range


class T10Cfg(_Cfg):
    ingredients: PositiveInt
    repair_minutes: Range


class TemplateCfg(_Cfg):
    T01: T01Cfg
    T02: T02Cfg
    T03: T03Cfg
    T04: T04Cfg
    T05: T05Cfg
    T06: T06Cfg
    T07: T07Cfg
    T08: T08Cfg
    T09: T09Cfg
    T10: T10Cfg


class ReportCfg(_Cfg):
    bundle_pct: int = Field(ge=0, le=100)
    public_echo_pct: int = Field(ge=0, le=100)
    depot_report_pct: int = Field(ge=0, le=100)
    police_late_minutes: Range  # after a caller's first word, when police confirm it


class CalibrationCfg(_Cfg):
    """How a world's tight hospital gets its fuel (blackout.scenarios.calibrate)."""

    easy_slack_minutes: PositiveInt  # easy: Rules survives and the reference keeps this slack
    hard_extra_minutes: Range  # hard: this much fuel above the reference's minimum
    hard_min_rules_loss: PositiveInt  # hard: Rules must lose at least this many minutes
    medium_margin_minutes: Range  # medium: this much above what Rules needs
    easy_margin_minutes: Range  # easy: this much above what both need
    probes: PositiveInt  # at most this many runs per search


class ConflictCfg(_Cfg):
    """L2: false, stale and correcting reports (blackout.scenarios.conflict)."""

    false_reports: Range
    stale_pct: int = Field(ge=0, le=100)
    correction_pct: int = Field(ge=0, le=100)
    field_false_pct: int = Field(ge=0, le=100)


class InjectionCfg(_Cfg):
    """L3: injection reports (blackout.scenarios.injection)."""

    injections: Range
    tick: Range
    window_minutes: Range


class CityConfig(_Cfg):
    """Everything the generator draws a world from. Ranges are inclusive, in whole units."""

    horizon: PositiveInt
    grid: GridCfg
    travel_minutes: Range
    arterials: ArterialCfg
    landmarks: int = Field(ge=0)
    zones: ZoneCfg
    substations: SubstationCfg
    hospitals: HospitalCfg
    depots: DepotCfg
    trucks: TruckCfg
    crews: CrewCfg
    repair_minutes: Range
    repair_step_minutes: PositiveInt
    fleet: FleetCfg
    templates: TemplateCfg
    reports: ReportCfg
    calibration: CalibrationCfg
    conflict: ConflictCfg
    injection: InjectionCfg

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        columns, rows = self.zones.blocks
        if self.grid.width % columns or self.grid.height % rows:
            raise ValueError(
                f"a {self.grid.width}x{self.grid.height} grid can't be split into"
                f" {columns}x{rows} zone blocks"
            )
        if self.zones.critical + self.zones.essential > columns * rows:
            raise ValueError("more critical and essential zones than zones")
        if columns < 2:
            raise ValueError("at least two columns of zones are needed (one substation each)")
        if min(self.travel_minutes) < 1 or min(self.arterials.travel_minutes) < 1:
            raise ValueError("travel times must be at least 1 minute")
        return self

    @property
    def substation_count(self) -> int:
        return self.zones.blocks[0]

    def digest(self) -> str:
        return digest(self.model_dump(mode="json"))


def load_city_config(path: Path = DEFAULT_CITY_CONFIG) -> CityConfig:
    """Read and validate a city config file."""
    with path.open(encoding="utf-8") as handle:
        return CityConfig.model_validate(yaml.safe_load(handle))
