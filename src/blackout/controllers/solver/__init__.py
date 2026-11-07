"""C2 Solver and C3 Reference, and the ``solve_plan`` contract they share (PRD section 8.7).

- ``state``: the planning snapshot of a belief, and the ``SolverBeliefs`` overrides.
- ``power``: exact shedding search, brute-force crew orders, the power timeline.
- ``fuel``: the CP-SAT model over enumerated truck plans (and its greedy fallback).
- ``contract``: ``solve_plan`` -> ``SolverResult``, the frozen library contract.
- ``policy``: the source filter and confirm policy.
- ``controller``: the two contestants.
"""

from blackout.controllers.solver.contract import (
    CrewPlan,
    PlannedTrip,
    PowerPlan,
    SolverBeliefs,
    SolverResult,
    SolveStats,
    TruckPlan,
    solve_plan,
)
from blackout.controllers.solver.controller import ReferenceController, SolverController

__all__ = [
    "CrewPlan",
    "PlannedTrip",
    "PowerPlan",
    "ReferenceController",
    "SolveStats",
    "SolverBeliefs",
    "SolverController",
    "SolverResult",
    "TruckPlan",
    "solve_plan",
]
