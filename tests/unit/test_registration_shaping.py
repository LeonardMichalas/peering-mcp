"""Unit tests for shaping an RDAP object into a registration.

Three things here are judgement rather than transcription, and each of them was
measured against real records before it was written: which of several
registrants is the holder, where in the entity tree the abuse contact lives,
and how wide a registration really is.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from peering_mcp.models.upstream import RdapAutnum, RdapEntity, RdapIpNetwork
from peering_mcp.shaping import (
    LAST_CHANGED_EVENT,
    MAX_STATUS_FLAGS,
    REGISTRATION_EVENT,
    abuse_contact,
    event_date,
    find_entity_by_role,
    registration_holder,
    registration_range,
    shape_registration,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "rdap"


def payload(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def ip_record(name: str) -> RdapIpNetwork:
    return RdapIpNetwork.model_validate(payload(name))


def autnum_record(name: str) -> RdapAutnum:
    return RdapAutnum.model_validate(payload(name))


def entity(**fields: Any) -> RdapEntity:
    return RdapEntity.model_validate(fields)


def vcard(**properties: str) -> list[Any]:
    return ["vcard", [[name, {}, "text", value] for name, value in properties.items()]]


# --- Who holds it ----------------------------------------------------------


def test_a_maintainer_named_after_itself_is_passed_over() -> None:
    """AS3320's first registrant is DTAG-RR, whose name is 'DTAG-RR'. The
    answer somebody wants is Deutsche Telekom AG, two entries later."""
    assert registration_holder(autnum_record("autnum_ripe_as3320.json")) == "Deutsche Telekom AG"


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        (
            "ip_ripe_193_0_0_0_21.json",
            "Reseaux IP Europeens Network Coordination Centre (RIPE NCC)",
        ),
        ("ip_arin_8_8_8_0_24.json", "Google LLC"),
        (
            "ip6_ripe_2001_67c_2e8_48.json",
            "Reseaux IP Europeens Network Coordination Centre (RIPE NCC)",
        ),
    ],
)
def test_the_holder_is_the_organisation_not_the_record_name(fixture: str, expected: str) -> None:
    """The record's own `name` is RIPE-NCC, GOGL, DTAG — registry shorthand.
    It is the last resort, not the first."""
    assert registration_holder(ip_record(fixture)) == expected


def test_a_record_with_only_self_named_registrants_falls_back_to_the_record_name() -> None:
    record = RdapAutnum.model_validate(
        {
            "name": "EXAMPLE-AS",
            "entities": [
                {"handle": "EX-MNT", "roles": ["registrant"], "vcardArray": vcard(fn="EX-MNT")}
            ],
        }
    )

    assert registration_holder(record) == "EX-MNT", "a name that says nothing still beats none"


def test_a_record_with_no_registrant_at_all_falls_back_to_the_record_name() -> None:
    record = RdapAutnum.model_validate({"name": "EXAMPLE-AS", "entities": []})

    assert registration_holder(record) == "EXAMPLE-AS"


# --- Where to report abuse -------------------------------------------------


def test_an_abuse_contact_beside_the_registrant_is_found() -> None:
    """RIPE publishes it at the top level."""
    contact = abuse_contact(ip_record("ip_ripe_193_0_0_0_21.json"))

    assert contact is not None
    assert contact.email == "abuse@ripe.net"


def test_an_abuse_contact_nested_under_the_registrant_is_found() -> None:
    """ARIN nests it one level down. Reading only the top level finds nothing."""
    contact = abuse_contact(ip_record("ip_arin_8_8_8_0_24.json"))

    assert contact is not None
    assert contact.name == "Abuse"
    assert contact.email == "network-abuse@google.com"


def test_an_empty_contact_is_no_contact() -> None:
    """An object with neither name nor mailbox reads as 'there is a contact'
    and answers nothing."""
    record = RdapAutnum.model_validate({"entities": [{"handle": "AB", "roles": ["abuse"]}]})

    assert abuse_contact(record) is None


def test_the_shallowest_match_wins() -> None:
    """A deeper entity belongs to somebody the record merely mentions."""
    found = find_entity_by_role(
        [
            entity(handle="A", entities=[{"handle": "DEEP", "roles": ["abuse"]}]),
            entity(handle="B", roles=["abuse"]),
        ],
        "abuse",
    )

    assert found is not None and found.handle == "B"


def test_the_search_stops_before_it_runs_away() -> None:
    nested: dict[str, Any] = {"handle": "ABUSE", "roles": ["abuse"]}
    for index in range(6):
        nested = {"handle": f"L{index}", "entities": [nested]}

    assert find_entity_by_role([entity(**nested)], "abuse") is None


# --- What it covers --------------------------------------------------------


def test_an_address_range_is_reported_whole() -> None:
    assert registration_range(ip_record("ip_ripe_193_0_0_0_21.json")) == "193.0.0.0 - 193.0.7.255"


def test_a_single_as_number_is_not_written_as_a_range() -> None:
    assert registration_range(autnum_record("autnum_ripe_as3320.json")) == "AS3320"


def test_a_block_of_as_numbers_is() -> None:
    record = RdapAutnum.model_validate({"startAutnum": 3320, "endAutnum": 3330})

    assert registration_range(record) == "AS3320 - AS3330"


def test_a_record_with_no_range_at_all_says_nothing() -> None:
    assert registration_range(RdapAutnum.model_validate({})) is None
    assert registration_range(RdapIpNetwork.model_validate({})) is None


# --- Dates -----------------------------------------------------------------


def test_the_two_dates_are_read_by_rdap_s_own_names() -> None:
    record = ip_record("ip_ripe_193_0_0_0_21.json")

    assert event_date(record, REGISTRATION_EVENT) == datetime(2003, 3, 17, 12, 15, 57, tzinfo=UTC)
    assert event_date(record, LAST_CHANGED_EVENT) == datetime(2026, 3, 19, 9, 8, 35, tzinfo=UTC)


def test_an_action_nobody_published_is_none() -> None:
    assert event_date(ip_record("ip_ripe_193_0_0_0_21.json"), "expiration") is None


# --- The whole shape -------------------------------------------------------


def test_a_shaped_registration_keeps_what_the_question_was_about() -> None:
    record = ip_record("ip_arin_8_8_8_0_24.json")

    shaped = shape_registration(record, target="8.8.8.8", kind="address", registry="ARIN")

    assert shaped.target == "8.8.8.8", "the address asked about, not the block it sits in"
    assert shaped.kind == "address"
    assert shaped.registry == "ARIN"
    assert shaped.handle == "NET-8-8-8-0-2"
    assert shaped.holder == "Google LLC"
    assert shaped.covers == "8.8.8.0 - 8.8.8.255"
    assert shaped.allocation_type == "DIRECT ALLOCATION"
    assert shaped.status == ["active"]
    assert shaped.abuse is not None and shaped.abuse.email == "network-abuse@google.com"


def test_status_flags_are_capped() -> None:
    record = autnum_record("autnum_hostile.json")

    shaped = shape_registration(record, target="AS12345", kind="asn", registry="RIPE NCC")

    assert len(shaped.status) == MAX_STATUS_FLAGS
