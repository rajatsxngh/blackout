"""No truth leaks into observations or wake-ups (invariants I2 and I6, PRD M4).

The test plays the same random contestants on a random scenario twice: once as it is, and once
with extra road blocks that no event or report mentions. While no vehicle runs into one of the
extra blocks, nobody can know about it, so both runs must look exactly the same from every
role's side: the same decision points, triggers, observations and action outcomes. And at every
decision point, an undiscovered block appears nowhere in any observation or belief.
"""

from dataclasses import dataclass, field

from hypothesis import assume, given
from hypothesis import strategies as st

from blackout.env import BlackoutEnv, DecisionRequest
from blackout.info.belief import BeliefState
from blackout.info.reports import Report
from blackout.info.roles import ROLE_ALL, TEAM_ROLES
from blackout.scenarios.schema import Scenario
from blackout.sim.city import edge_ids
from blackout.sim.config import SimConfig
from blackout.sim.events import RoadBlock, RoadClear
from blackout.sim.hashing import canonical_json
from tests.helpers import RandomController
from tests.strategies import scenarios


@dataclass
class Trace:
    seen: list[tuple[object, ...]] = field(default_factory=list)
    discovered: frozenset[str] = frozenset()
    end: str = ""


def play(
    scenario: Scenario, roles: tuple[str, ...], seed: int, delay: int, hidden: frozenset[str]
) -> Trace:
    env = BlackoutEnv(scenario, roles=roles, config=SimConfig(telemetry_delay_ticks=delay))
    players = {role: RandomController(seed + i) for i, role in enumerate(roles)}
    beliefs = {role: BeliefState() for role in roles}
    trace = Trace()
    request = env.reset()
    while isinstance(request, DecisionRequest):
        known = env.engine.known_blocks
        secret = sorted(hidden - known)
        for role, observation in request.observations.items():
            text = canonical_json(observation.to_dict())
            assert set(observation.known_blocks) <= known
            assert not [edge for edge in secret if edge in text], (role, secret)
            beliefs[role].observe(observation)
            assert not set(secret) & set(beliefs[role].blocked_roads())
            outcomes = [
                (r.accepted, r.reason, r.message)
                for r in (env.act(role, a) for a in players[role].decide(observation))
            ]
            trace.seen.append((request.tick, role, observation.digest(), tuple(outcomes)))
        request = env.advance()
    trace.end = request.reason
    trace.discovered = frozenset(env.engine.known_blocks & hidden)
    return trace


@given(
    scenario=scenarios(),
    seed=st.integers(0, 2**32 - 1),
    team=st.booleans(),
    delay=st.integers(0, 3),
    data=st.data(),
)
def test_unreported_blocks_never_reach_any_observation_or_wake_anyone(
    scenario: Scenario, seed: int, team: bool, delay: int, data: st.DataObject
) -> None:
    mentioned = {e.edge for e in scenario.events if isinstance(e, RoadBlock | RoadClear)}
    mentioned |= {
        fact.subject
        for e in scenario.events
        if isinstance(e, Report)
        for fact in e.facts
        if fact.type.startswith("road_")
    }
    free = sorted(set(edge_ids(scenario.city.width, scenario.city.height)) - mentioned)
    assume(free)
    hidden = data.draw(st.lists(st.sampled_from(free), min_size=1, max_size=2, unique=True))
    tick = data.draw(st.integers(0, scenario.horizon - 1))
    blocked = Scenario.model_validate(
        scenario.model_dump()
        | {
            "events": [
                *scenario.model_dump()["events"],
                *({"kind": "road_block", "tick": tick, "edge": edge} for edge in hidden),
            ]
        }
    )
    roles = TEAM_ROLES if team else (ROLE_ALL,)
    plain = play(scenario, roles, seed, delay, frozenset(hidden))
    secret = play(blocked, roles, seed, delay, frozenset(hidden))
    if secret.discovered:
        return
    if plain.end == "stable":
        # The early stop (D021) waits for every pending event, hidden ones included, so the
        # run with the extra block can go on after the plain one has ended (docs D044).
        assert secret.seen[: len(plain.seen)] == plain.seen
    else:
        assert secret.seen == plain.seen
