"""Unit tests for the shaping layer.

Shaping is tested against real PeeringDB records rather than hand-written ones,
because the point of the layer is what it discards from a real 42-field
response.
"""

from __future__ import annotations

import json
from pathlib import Path

from peering_mcp.models.upstream import UpstreamNetwork
from peering_mcp.sanitize import STRUCTURAL_CHARACTERS
from peering_mcp.shaping import shape_network, shape_network_match

FIXTURES = Path(__file__).parent.parent / "fixtures" / "peeringdb"


def load_raw(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())["data"][0]


def load(name: str) -> UpstreamNetwork:
    return UpstreamNetwork.model_validate(load_raw(name))


def test_shapes_a_real_record() -> None:
    network = shape_network(load("net_as3320.json"))

    assert network.asn == 3320
    assert network.name == "Deutsche Telekom"
    assert network.long_name == "Deutsche Telekom AG"
    assert network.network_type == "NSP"
    assert network.scope == "Global"
    assert network.exchange_count == 7
    assert network.facility_count == 53
    assert network.policy.general == "Restrictive"
    assert network.policy.contract_required == "Required"
    assert network.policy.ratio_required is True
    assert network.looking_glass == "https://lg.telekom.com"


def test_shaping_discards_most_of_the_record() -> None:
    """The shaping layer exists to throw things away. Prove it does."""
    raw = load_raw("net_as3320.json")
    shaped = shape_network(UpstreamNetwork.model_validate(raw))

    assert len(raw) == 42, "fixture drifted; re-record it"
    assert len(shaped.model_dump()) < len(raw) / 2

    serialised = shaped.model_dump_json()
    assert len(serialised) < 2048, "lookup_network has a 2 KB budget"


def test_free_text_fields_are_never_carried_through() -> None:
    """`notes` and `aka` are network-authored free text with no decision value.

    They are also the obvious place to hide instructions aimed at whatever
    model reads the result. Since T5 they are dropped at the boundary rather
    than by this layer, so the assertion holds for a reason it did not before:
    there is nothing on the upstream model left to carry through.

    Each dropped field carries a phrase that appears nowhere else in the
    fixture, so this distinguishes "the field was dropped" from "the field was
    cleaned".
    """
    raw = load_raw("net_hostile.json")
    assert "SSH keys" in raw["aka"]
    assert "shell_exec" in raw["notes"]

    serialised = shape_network(UpstreamNetwork.model_validate(raw)).model_dump_json()

    assert "SSH keys" not in serialised
    assert "shell_exec" not in serialised
    assert "Totally Normal Net" in serialised, "the legitimate name should survive"


def test_structure_is_gone_from_every_field_that_does_pass_through() -> None:
    """The fields that survive carry no character that builds a control shape.

    Checked on the values rather than the serialised JSON, which has braces and
    quotes of its own.
    """
    raw = load_raw("net_hostile.json")
    shaped = shape_network(UpstreamNetwork.model_validate(raw))

    values = [v for v in shaped.model_dump().values() if isinstance(v, str)]
    values += [v for v in shaped.policy.model_dump().values() if isinstance(v, str)]
    assert values, "the hostile fixture should leave some text behind to check"

    for value in values:
        for character in STRUCTURAL_CHARACTERS:
            assert character not in value, f"{character!r} survived in {value!r}"


def test_empty_strings_become_none() -> None:
    """PeeringDB uses "" and null interchangeably for "not filled in"."""
    network = shape_network(load("net_sparse.json"))

    assert network.long_name is None
    assert network.website is None
    assert network.network_type is None
    assert network.policy.general is None


def test_missing_fields_do_not_raise() -> None:
    minimal = shape_network(UpstreamNetwork.model_validate({"asn": 1, "name": "Minimal"}))

    assert minimal.asn == 1
    assert minimal.name == "Minimal"
    assert minimal.exchange_count is None
    assert minimal.policy.general is None


def test_a_candidate_carries_only_what_a_choice_needs() -> None:
    match = shape_network_match(load("net_as3320.json"))

    assert match.model_dump() == {"asn": 3320, "name": "Deutsche Telekom"}
