"""Verifying a frozen suite on this machine (D084).

A frozen suite's files are canonical. They were generated once, and ``blackout gen --check``
regenerates them byte for byte on the OS they were made on. On another OS, CP-SAT's search can
take a different path to an equally good plan (its LP relaxation uses floating point), so the
reference's trajectory, and with it a recorded number like its minimum slack, may differ.
What must hold everywhere is the *validity* of the files, and ``verify_suite`` checks it by
re-running each scenario here:

- the file matches its manifest entry (content hash);
- do-nothing loses exactly the recorded hospital-minutes and trips (the engine alone, which
  is identical on every platform), and at least 30;
- the reference loses nothing;
- at L0, the tier measured here (Rules' downtime and the reference's slack) is the recorded
  tier;
- every false report still has a working cue (``cue_problems``).
"""

import json
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from blackout.scenarios.calibrate import do_nothing, reference, rules, tier_of
from blackout.scenarios.config import CalibrationCfg
from blackout.scenarios.conflict import cue_problems
from blackout.scenarios.schema import load_scenario
from blackout.scenarios.suites import MANIFEST
from blackout.scenarios.templates.common import MIN_DO_NOTHING_LOSS


def verify_scenario(path: Path, cfg: CalibrationCfg, content_hash: str) -> list[str]:
    """Why the frozen scenario at ``path`` isn't valid on this machine (empty if it is)."""
    scenario = load_scenario(path)
    name = scenario.id
    if scenario.content_hash != content_hash:
        return [f"{name}: doesn't match its manifest entry"]
    v = scenario.validity
    if v is None:
        return [f"{name}: no validity record"]
    problems = [f"{name}: {p}" for p in cue_problems(scenario)]
    nothing = do_nothing(scenario)
    if (nothing.critical_downtime, nothing.trips) != (
        v.do_nothing_critical_downtime,
        v.do_nothing_trips,
    ):
        problems.append(
            f"{name}: do-nothing loses {nothing.critical_downtime} min with {nothing.trips}"
            f" trips here, recorded {v.do_nothing_critical_downtime} with {v.do_nothing_trips}"
        )
    if nothing.critical_downtime < MIN_DO_NOTHING_LOSS:
        problems.append(f"{name}: do-nothing loses only {nothing.critical_downtime} min")
    best = reference(scenario)
    if best.critical_downtime > 0:
        problems.append(f"{name}: the reference loses {best.critical_downtime} min here")
    if scenario.level == 0 and scenario.tier is not None:
        measured = tier_of(rules(scenario), best, cfg)
        if measured != scenario.tier:
            problems.append(f"{name}: measures as {measured} here, recorded {scenario.tier}")
    return problems


def verify_suite(folder: Path, cfg: CalibrationCfg, *, workers: int = 1) -> tuple[int, list[str]]:
    """Verify every scenario in a frozen suite: (scenarios checked, problems)."""
    entries = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))["scenarios"]
    jobs = [(folder / f"{e['id']}.json", cfg, e["content_hash"]) for e in entries]
    if workers <= 1:
        results = [verify_scenario(*job) for job in jobs]
    else:
        context = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(max_workers=workers, mp_context=context) as pool:
            results = list(pool.map(verify_scenario, *zip(*jobs, strict=True)))
    return len(jobs), [p for problems in results for p in problems]
