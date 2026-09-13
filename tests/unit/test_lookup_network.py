"""Unit tests for query parsing and record shaping.

Shaping is tested against a real PeeringDB record rather than a hand-written
one, because the point of the shaping layer is what it discards from a real
42-field response.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from peering_mcp.models.domain import Network
from peering_mcp.tools.lookup_network import parse_asn, shape_network

FIXTURES = Path(__file__).parent.parent / "fixtures" / "peeringdb"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())["data"][0]


# --- Query parsing ---------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("AS3320", 3320),
        ("as3320", 3320),
        ("As3320", 3320),
        ("3320", 3320),
        ("  3320  ", 3320),
        (" AS3320 ", 3320),
        ("4294967294", 4_294_967_294),
        ("1", 1),
    ],
)
def test_recognises_asn_forms(query: str, expected: int) -> None:
    assert parse_asn(query) == expected


@pytest.mark.parametrize(
    "query",
    [
        "Hurricane",
        "Deutsche Telekom",
        "AS",
        "",
        "3320 and friends",
        "AS 3320",  # a space after AS is a name, not an ASN
        "0",  # reserved
        "4294967295",  # reserved
        "99999999999",  # beyond 32 bits
        "-1",
        "3.320",
    ],
)
def test_rejects_non_asn_queries(query: str) -> None:
    assert parse_asn(query) is None


# --- Shaping ---------------------------------------------------------------


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
    raw = load("net_as3320.json")
    shaped = shape_network(raw)

    assert len(raw) == 42, "fixture drifted; re-record it"
    assert len(shaped.model_dump()) < len(raw) / 2

    serialised = shaped.model_dump_json()
    assert len(serialised) < 2048, "lookup_network has a 2 KB budget"


def test_free_text_fields_are_never_carried_through() -> None:
    """`notes` and `aka` are network-authored free text with no decision value.

    They are also the obvious place to hide instructions aimed at whatever
    model reads the result, so they do not leave the server at all.
    """
    hostile = load("net_hostile.json")
    assert "IGNORE PREVIOUS INSTRUCTIONS" in hostile["aka"]
    assert "maintenance mode" in hostile["notes"]

    serialised = shape_network(hostile).model_dump_json()

    assert "IGNORE PREVIOUS INSTRUCTIONS" not in serialised
    assert "maintenance mode" not in serialised
    assert "system>" not in serialised
    assert "Totally Normal Net" in serialised, "the legitimate name should survive"


def test_empty_strings_become_none() -> None:
    """PeeringDB uses "" and null interchangeably for "not filled in"."""
    network = shape_network(load("net_sparse.json"))

    assert network.long_name is None
    assert network.website is None
    assert network.network_type is None
    assert network.policy.general is None


def test_missing_fields_do_not_raise() -> None:
    assert shape_network({"asn": 1, "name": "Minimal"}) == Network(asn=1, name="Minimal")


def test_a_record_with_nothing_at_all_still_shapes() -> None:
    network = shape_network({})

    assert network.asn == 0
    assert network.name == "(unnamed)"
