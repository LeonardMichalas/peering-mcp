"""Unit tests for the exchange half of the shaping layer, and the two parsers.

The fold that `find_at_exchange` runs is `group_by_exchange` turned round: one
exchange's rows, keyed by network instead of one network's rows keyed by
exchange. Same dataclass, so what is tested here is the keying and the order,
not the arithmetic that T8 already proved.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from peering_mcp.models.upstream import UpstreamExchange, UpstreamNetwork, UpstreamNetworkIxLan
from peering_mcp.shaping import (
    group_by_network,
    shape_exchange,
    shape_exchange_match,
    shape_participant,
)
from peering_mcp.tools.find_at_exchange import POLICIES, canonical_policy, parse_exchange_id

FIXTURES = Path(__file__).parent.parent / "fixtures" / "peeringdb"


def rows(name: str) -> list[dict[str, Any]]:
    return json.loads((FIXTURES / name).read_text())["data"]


def port(asn: int, **fields: Any) -> UpstreamNetworkIxLan:
    return UpstreamNetworkIxLan.model_validate({"asn": asn, "ix_id": 87, **fields})


def network(asn: int, **fields: Any) -> UpstreamNetwork:
    return UpstreamNetwork.model_validate({"asn": asn, "name": f"AS{asn}", **fields})


# --- Folding one exchange's ports into networks -------------------------------


def test_one_group_per_network_not_per_port() -> None:
    """BCIX: 175 port rows, 142 networks. The caller should see 142."""
    ports = [UpstreamNetworkIxLan.model_validate(r) for r in rows("netixlan_ix87.json")]

    groups = group_by_network(ports)

    assert len(ports) == 175, "fixture drifted; re-record it"
    assert len(groups) == 142
    assert sum(len(group.ports) for group in groups) == 175
    doubled = next(group for group in groups if len(group.ports) > 1)
    assert doubled.speed_mbps == sum(p.speed or 0 for p in doubled.ports)


def test_largest_capacity_comes_first() -> None:
    groups = group_by_network([port(10, speed=10), port(20, speed=1000), port(30, speed=100)])

    assert [group.asn for group in groups] == [20, 30, 10]


def test_ties_break_on_as_number_so_the_order_is_stable() -> None:
    groups = group_by_network([port(9, speed=10), port(3, speed=10), port(5, speed=10)])

    assert [group.asn for group in groups] == [3, 5, 9]


def test_a_network_with_no_speed_anywhere_sorts_last_rather_than_vanishing() -> None:
    groups = group_by_network([port(7), port(8, speed=1)])

    assert [group.asn for group in groups] == [8, 7]
    assert groups[1].speed_mbps is None


# --- The participant entry ----------------------------------------------------


def test_a_participant_carries_who_what_and_whether_they_would_peer() -> None:
    group = group_by_network([port(3320, speed=100, is_rs_peer=True), port(3320, speed=200)])[0]

    entry = shape_participant(
        group, network(3320, name="Deutsche Telekom", policy_general="Selective")
    )

    assert entry.asn == 3320
    assert entry.name == "Deutsche Telekom"
    assert entry.speed_mbps == 300
    assert entry.ports == 2
    assert entry.route_server is True
    assert entry.policy == "Selective"


def test_a_participant_without_a_network_record_keeps_its_ports() -> None:
    """A network that really is there is worth returning unnamed."""
    group = group_by_network([port(65000, speed=10)])[0]

    entry = shape_participant(group, None)

    assert entry.asn == 65000
    assert entry.name is None
    assert entry.policy is None
    assert entry.ports == 1
    assert entry.speed_mbps == 10


# --- The exchange itself ------------------------------------------------------


def test_an_exchange_is_shaped_down_to_which_one_and_where() -> None:
    record = UpstreamExchange.model_validate(rows("ix_bcix.json")[0])

    exchange = shape_exchange(record)

    assert exchange.exchange_id == 87
    assert exchange.name == "BCIX"
    assert exchange.city == "Berlin"
    assert exchange.country == "DE"
    assert exchange.networks_recorded == record.net_count


def test_a_candidate_carries_enough_to_choose_between_ten_of_them() -> None:
    record = UpstreamExchange.model_validate(rows("ix_name_linx.json")[4])

    match = shape_exchange_match(record)

    assert match.name == "LINX NoVA"
    assert match.country == "US", "the reason a guess would be wrong"
    assert match.exchange_id


# --- The two parsers ----------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"), [("87", 87), ("ix87", 87), ("IX87", 87), (" 87 ", 87)]
)
def test_an_id_is_read_however_it_is_written(given: str, expected: int) -> None:
    assert parse_exchange_id(given) == expected


@pytest.mark.parametrize("given", ["BCIX", "DE-CIX Frankfurt", "LINX LON1", "ix", "87 West", ""])
def test_anything_else_is_a_name(given: str) -> None:
    assert parse_exchange_id(given) is None


@pytest.mark.parametrize("policy", POLICIES)
def test_every_policy_survives_a_round_trip(policy: str) -> None:
    assert canonical_policy(policy) == policy
    assert canonical_policy(policy.lower()) == policy
    assert canonical_policy(f"  {policy.upper()} ") == policy


@pytest.mark.parametrize("given", ["friendly", "no policy", "selective-ish", ""])
def test_anything_peeringdb_does_not_know_is_refused(given: str) -> None:
    assert canonical_policy(given) is None
