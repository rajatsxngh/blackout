"""C5 Single and C6 Single + Solver: one LLM coordinator for the whole city (PRD 8.7, M11).

Both see the union of every role's view and play through the same harness
(``agents.harness``). C6 adds ``call_solver`` and ``accept_plan``: the CP-SAT planner C2 uses,
fed the agent's own belief, at most twice per decision point; it never acts on its own.
"""

from blackout.controllers.agents.harness import AgentController


class SingleAgentController(AgentController):
    """C5: central command, no solver."""

    name = "llm-single"
    solver = False


class SingleSolverController(AgentController):
    """C6: central command with the planning solver as a tool (the main hybrid contender)."""

    name = "llm-single-solver"
    solver = True
