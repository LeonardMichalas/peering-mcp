"""Unit tests for the RDAP half of the upstream boundary.

RDAP is nested where PeeringDB is flat, so the guards here are about shape
rather than type: a jCard that is not a jCard, an entity list that is not a
list, a tree deep enough to exhaust the interpreter. Every one of them has to
end as `None` or an empty list, because the promise this layer makes to
everything above it is that a parsed record can be read without checking.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from peering_mcp.clients.rdap import parse_object
from peering_mcp.errors import UpstreamProtocolError
from peering_mcp.models.upstream import (
    MAX_LIST_ITEMS,
    MAX_TEXT_LENGTH,
    RdapAutnum,
    RdapBootstrap,
    RdapBootstrapService,
    RdapEntity,
    RdapEvent,
    RdapIpNetwork,
    RdapObject,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "rdap"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


# --- The fields that must never exist ---------------------------------------


@pytest.mark.parametrize("model", [RdapObject, RdapIpNetwork, RdapAutnum, RdapEntity])
def test_no_rdap_model_declares_a_free_text_channel(model: type[Any]) -> None:
    """`remarks`, `notices` and `links` are registry- and holder-editable, and
    no tool needs any of them. Undeclared is stronger than filtered: a field
    nobody declared cannot survive parsing."""
    declared = {field.alias or name for name, field in model.model_fields.items()}

    assert {"remarks", "notices", "links", "port43"}.isdisjoint(declared)


def test_the_undeclared_fields_are_dropped_from_a_real_record() -> None:
    record = RdapIpNetwork.model_validate(fixture("ip_ripe_193_0_0_0_21.json"))

    assert "remarks" not in record.model_dump()
    assert "notices" not in record.model_dump()


def test_a_parsed_record_cannot_be_edited() -> None:
    record = RdapAutnum.model_validate(fixture("autnum_ripe_as3320.json"))

    with pytest.raises(ValueError, match="frozen"):
        record.handle = "AS1"


# --- Shapes that are not the documented shape -------------------------------


@pytest.mark.parametrize("value", ["not a list", 7, None, {"roles": "abuse"}])
def test_a_role_list_that_is_not_a_list_is_empty(value: Any) -> None:
    assert RdapEntity.model_validate({"roles": value}).roles == []


@pytest.mark.parametrize("value", ["not a list", 7, None])
def test_an_entity_list_that_is_not_a_list_is_empty(value: Any) -> None:
    assert RdapObject.model_validate({"entities": value}).entities == []


def test_entries_that_are_not_objects_are_skipped() -> None:
    """One malformed entry must not cost the whole record."""
    record = RdapObject.model_validate({"entities": ["nonsense", {"handle": "REAL"}, 7]})

    assert [entity.handle for entity in record.entities] == ["REAL"]


def test_a_list_longer_than_anything_real_is_capped() -> None:
    record = RdapObject.model_validate({"entities": [{"handle": f"E{i}"} for i in range(200)]})

    assert len(record.entities) == MAX_LIST_ITEMS


@pytest.mark.parametrize(
    "vcard",
    [
        "not a vcard",
        ["vcard"],
        ["vcard", "not a property list"],
        ["vcard", [["fn", {}, "text"]]],
        ["vcard", ["not a property"]],
        ["vcard", [[7, {}, "text", "value"]]],
        None,
    ],
)
def test_a_jcard_that_is_not_a_jcard_carries_nothing(vcard: Any) -> None:
    contact = RdapEntity.model_validate({"vcardArray": vcard}).contact

    assert contact.full_name is None
    assert contact.email is None


def test_the_first_property_of_a_kind_wins() -> None:
    """A record listing two abuse mailboxes has not said which one is right."""
    entity = RdapEntity.model_validate(
        {
            "vcardArray": [
                "vcard",
                [
                    ["email", {}, "text", "first@example.com"],
                    ["email", {}, "text", "second@example.com"],
                ],
            ]
        }
    )

    assert entity.contact.email == "first@example.com"


def test_an_entity_with_no_vcard_at_all_still_parses() -> None:
    entity = RdapEntity.model_validate({"handle": "H", "roles": ["abuse"]})

    assert entity.contact.full_name is None


def test_an_event_without_a_date_keeps_its_action() -> None:
    event = RdapEvent.model_validate({"eventAction": "registration", "eventDate": None})

    assert event.action == "registration"
    assert event.date is None


def test_an_unreadable_date_is_none_rather_than_a_guess() -> None:
    assert RdapEvent.model_validate({"eventDate": "last tuesday"}).date is None


# --- The bootstrap file ------------------------------------------------------


@pytest.mark.parametrize("value", ["not a list", 7, None, {}])
def test_a_services_field_that_is_not_a_list_is_empty(value: Any) -> None:
    assert RdapBootstrap.model_validate({"services": value}).services == []


def test_a_service_line_that_is_not_a_pair_is_skipped() -> None:
    file = RdapBootstrap.model_validate(
        {"services": [["only one part"], [["8.0.0.0/8"], ["https://rdap.arin.net/registry/"]]]}
    )

    assert len(file.services) == 1


@pytest.mark.parametrize("value", ["8.0.0.0/8", 7, None])
def test_bootstrap_entries_that_are_not_a_list_are_empty(value: Any) -> None:
    file = RdapBootstrap.model_validate({"services": [[value, value]]})

    assert file.services[0].entries == []
    assert file.services[0].urls == []


def test_a_service_line_already_in_named_form_is_left_alone() -> None:
    """IANA writes a pair; the model also accepts the shape it turns that into,
    so a caller building one by hand does not have to imitate the file."""
    service = RdapBootstrapService.model_validate(
        {"entries": ["8.0.0.0/8"], "urls": ["https://rdap.arin.net/registry/"]}
    )

    assert service.entries == ["8.0.0.0/8"]


def test_a_bootstrap_string_longer_than_any_real_one_is_dropped() -> None:
    """A prefix or URL is short. Anything near the cap is not one."""
    file = RdapBootstrap.model_validate(
        {"services": [[["8.0.0.0/8", "x" * (MAX_TEXT_LENGTH + 1)], ["https://rdap.arin.net/"]]]}
    )

    assert file.services[0].entries == ["8.0.0.0/8"]


def test_bootstrap_urls_keep_their_structure() -> None:
    """The one upstream text the sanitiser must not touch: cleaning a URL
    would destroy it, and these are checked by `service_url` instead."""
    file = RdapBootstrap.model_validate(
        {"services": [[["8.0.0.0/8"], ["https://rdap.arin.net/registry/"]]]}
    )

    assert file.services[0].urls == ["https://rdap.arin.net/registry/"]


# --- Reading one object ------------------------------------------------------


def test_an_object_of_the_wrong_class_is_refused() -> None:
    with pytest.raises(UpstreamProtocolError, match="ip network"):
        parse_object(fixture("autnum_ripe_as3320.json"), RdapIpNetwork, source="RIPE NCC")


def test_an_object_that_does_not_say_what_it_is_is_accepted() -> None:
    """`objectClassName` is optional in practice. A record that omits it is
    not thereby the wrong kind of record."""
    record = parse_object({"handle": "AS1"}, RdapAutnum, source="RIPE NCC")

    assert record.handle == "AS1"


def test_an_object_nested_beyond_reading_is_refused_rather_than_fatal() -> None:
    """Entity trees are recursive, so depth is an upstream-controlled number.
    A thousand levels is a broken upstream, not a crash here — and it is the
    one way an RDAP object can fail validation at all, since every field on it
    degrades to None."""
    payload: dict[str, Any] = {"handle": "AS1"}
    for _ in range(2_000):
        payload = {"entities": [payload]}

    with pytest.raises(UpstreamProtocolError, match="cannot read"):
        parse_object(payload, RdapAutnum, source="RIPE NCC")
