"""Canonical JSON and stable digests."""

import pytest

from blackout.sim.hashing import canonical_json, chain_digest, digest


def test_canonical_json_sorts_keys_and_drops_whitespace() -> None:
    assert canonical_json({"b": 1, "a": [1, None, True]}) == '{"a":[1,null,true],"b":1}'


@pytest.mark.parametrize("value", [0.5, {"x": 1.0}, [1, 2.5], {"deep": [{"er": 3.0}]}])
def test_floats_are_rejected(value: object) -> None:
    with pytest.raises(TypeError, match="floats are not allowed"):
        canonical_json(value)


def test_digest_ignores_key_order() -> None:
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})
    assert len(digest({})) == 32


def test_chain_digest_depends_on_order() -> None:
    assert chain_digest(["a", "b"]) != chain_digest(["b", "a"])
    assert chain_digest([]) == chain_digest(())
