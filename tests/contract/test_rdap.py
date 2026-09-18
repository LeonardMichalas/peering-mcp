"""Contract tests for `lookup_registration` against recorded RDAP responses.

Four recordings from two registries, made on 2026-09-18: RIPE's records for
193.0.0.0/21, 2001:67c:2e8::/48 and AS3320, and ARIN's for 8.8.8.0/24. Two
registries rather than one on purpose — they disagree about where the abuse
contact lives and about what a holder's name looks like, and a tool tested
against a single registry would pass while being wrong about the other four.

The IANA bootstrap files are recorded alongside them, so these tests exercise
the real routing decision rather than a stubbed one: nothing here tells the
client that 8.8.8.0/24 is ARIN's, it works that out.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from peering_mcp.clients.rdap import IANA_BOOTSTRAP_URL, RdapClient
from peering_mcp.config import Config
from peering_mcp.models.domain import Status
from peering_mcp.tools.lookup_registration import lookup_registration

FIXTURES = Path(__file__).parent.parent / "fixtures" / "rdap"

RIPE = "https://rdap.db.ripe.net"
ARIN = "https://rdap.arin.net/registry"
APNIC = "https://rdap.apnic.net"

#: The budget for this tool, in bytes of compact JSON. The largest real answer
#: measured against live registries on 2026-09-18 was 673 bytes, for a RIPE
#: IPv6 record whose holder's name runs to 58 characters.
BUDGET = 2 * 1024


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def config(monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("PEERING_MCP_MAX_RETRIES", "1")
    return Config.from_env()


@pytest.fixture
def bootstrap() -> dict[str, respx.Route]:
    """IANA's files, which every lookup consults before it can ask anything."""
    return {
        family: respx.get(f"{IANA_BOOTSTRAP_URL}/{family}.json").mock(
            return_value=httpx.Response(200, json=fixture(f"bootstrap_{family}.json"))
        )
        for family in ("ipv4", "ipv6", "asn")
    }


@pytest.fixture
def registries() -> dict[str, respx.Route]:
    """The four recorded records, each at the URL its registry serves it from."""
    return {
        "ripe_v4": respx.get(f"{RIPE}/ip/193.0.0.0/21").mock(
            return_value=httpx.Response(200, json=fixture("ip_ripe_193_0_0_0_21.json"))
        ),
        "ripe_v6": respx.get(f"{RIPE}/ip/2001:67c:2e8::/48").mock(
            return_value=httpx.Response(200, json=fixture("ip6_ripe_2001_67c_2e8_48.json"))
        ),
        "ripe_asn": respx.get(f"{RIPE}/autnum/3320").mock(
            return_value=httpx.Response(200, json=fixture("autnum_ripe_as3320.json"))
        ),
        "arin_v4": respx.get(f"{ARIN}/ip/8.8.8.8").mock(
            return_value=httpx.Response(200, json=fixture("ip_arin_8_8_8_0_24.json"))
        ),
    }


async def call(config: Config, target: str) -> Any:
    async with RdapClient(config) as client:
        return await lookup_registration(client, target)


def compact_size(result: Any) -> int:
    return len(result.model_dump_json())


# --- The four questions the task exists to answer ---------------------------


@respx.mock
async def test_an_ipv4_prefix_names_its_holder(
    config: Config, bootstrap: dict[str, respx.Route], registries: dict[str, respx.Route]
) -> None:
    result = await call(config, "193.0.0.0/21")

    assert result.status is Status.OK
    assert result.data.registry == "RIPE NCC"
    assert result.data.kind == "prefix"
    assert result.data.holder == "Reseaux IP Europeens Network Coordination Centre (RIPE NCC)"
    assert result.data.covers == "193.0.0.0 - 193.0.7.255"
    assert result.data.country == "NL"
    assert result.data.allocation_type == "ASSIGNED PA"
    assert result.data.registered is not None
    assert result.data.abuse.email == "abuse@ripe.net"
    assert result.provenance.source == "rdap"
    assert result.provenance.record_updated is not None, "the last-changed event"


@respx.mock
async def test_an_ipv6_prefix_resolves_against_the_ipv6_bootstrap_file(
    config: Config, bootstrap: dict[str, respx.Route], registries: dict[str, respx.Route]
) -> None:
    result = await call(config, "2001:67c:2e8::/48")

    assert result.status is Status.OK
    assert result.data.registry == "RIPE NCC"
    assert result.data.covers == "2001:67c:2e8:: - 2001:67c:2e8:ffff:ffff:ffff:ffff:ffff"
    assert bootstrap["ipv6"].called
    assert not bootstrap["ipv4"].called, "the wrong file was never opened"


@respx.mock
async def test_an_as_number_names_the_organisation_behind_it(
    config: Config, bootstrap: dict[str, respx.Route], registries: dict[str, respx.Route]
) -> None:
    result = await call(config, "AS3320")

    assert result.status is Status.OK
    assert result.data.kind == "asn"
    assert result.data.target == "AS3320"
    assert result.data.holder == "Deutsche Telekom AG"
    assert result.data.covers == "AS3320"
    assert result.data.abuse.email == "abuse@telekom.de"


@respx.mock
async def test_an_unallocated_range_is_not_found(
    config: Config, bootstrap: dict[str, respx.Route]
) -> None:
    """A registry that answers 404 is saying it has no record. Reserved space,
    which no registry is responsible for at all, is the test below.

    The documentation range sits inside APNIC's delegation, which is what the
    bootstrap file says and not what anyone would have guessed.
    """
    route = respx.get(f"{APNIC}/ip/2001:db8::/32").mock(
        return_value=httpx.Response(404, json=fixture("not_found_ripe.json"))
    )

    result = await call(config, "2001:db8::/32")

    assert route.called
    assert result.status is Status.NOT_FOUND
    assert result.data is None
    assert "2001:db8::/32" in result.note


@respx.mock
async def test_reserved_space_is_answered_without_asking_anyone(
    config: Config, bootstrap: dict[str, respx.Route], registries: dict[str, respx.Route]
) -> None:
    """No registry is responsible for 240.0.0.0/8, and the bootstrap file says
    so. Asking a registry anyway would be a request that could only fail."""
    result = await call(config, "240.0.0.0/8")

    assert result.status is Status.NOT_FOUND
    assert "No registry is responsible" in result.note
    assert not any(route.called for route in registries.values())


@respx.mock
async def test_a_private_use_as_number_is_answered_without_asking_anyone(
    config: Config, bootstrap: dict[str, respx.Route], registries: dict[str, respx.Route]
) -> None:
    """No registry is responsible for AS64512, and the bootstrap file says so."""
    result = await call(config, "AS64512")

    assert result.status is Status.NOT_FOUND
    assert "private use" in result.note
    assert not any(route.called for route in registries.values())


# --- How the two registries differ ------------------------------------------


@respx.mock
async def test_a_nested_abuse_contact_is_found(
    config: Config, bootstrap: dict[str, respx.Route], registries: dict[str, respx.Route]
) -> None:
    """ARIN nests abuse under the registrant; RIPE does not. Both are read."""
    result = await call(config, "8.8.8.8")

    assert result.status is Status.OK
    assert result.data.registry == "ARIN"
    assert result.data.holder == "Google LLC"
    assert result.data.abuse.email == "network-abuse@google.com"


@respx.mock
async def test_one_address_is_answered_with_the_block_it_sits_in(
    config: Config, bootstrap: dict[str, respx.Route], registries: dict[str, respx.Route]
) -> None:
    """The distinction the `covers` field exists for: the question was about
    one address and the registration is a /24."""
    result = await call(config, "8.8.8.8")

    assert result.data.kind == "address"
    assert result.data.target == "8.8.8.8"
    assert result.data.covers == "8.8.8.0 - 8.8.8.255"
    assert registries["arin_v4"].calls.last.request.url.path == "/registry/ip/8.8.8.8"


# --- Cost --------------------------------------------------------------------


@respx.mock
async def test_the_response_stays_inside_budget(
    config: Config, bootstrap: dict[str, respx.Route], registries: dict[str, respx.Route]
) -> None:
    """RIPE's raw answer for this prefix is 18.5 KB. Shaping is the product."""
    result = await call(config, "193.0.0.0/21")

    assert compact_size(result) <= BUDGET


@respx.mock
async def test_the_bootstrap_file_is_fetched_once_for_two_lookups(
    config: Config, bootstrap: dict[str, respx.Route], registries: dict[str, respx.Route]
) -> None:
    """Otherwise every registration lookup would cost two requests for good."""
    await call(config, "193.0.0.0/21")
    await call(config, "8.8.8.8")

    assert bootstrap["ipv4"].call_count == 1
    assert registries["ripe_v4"].call_count == 1
    assert registries["arin_v4"].call_count == 1


# --- When the registry does not answer properly ------------------------------


@respx.mock
async def test_a_registry_outage_is_upstream_unavailable(
    config: Config, bootstrap: dict[str, respx.Route]
) -> None:
    respx.get(f"{RIPE}/autnum/3320").mock(return_value=httpx.Response(503))

    result = await call(config, "AS3320")

    assert result.status is Status.UPSTREAM_UNAVAILABLE
    assert result.data is None


@respx.mock
async def test_a_throttled_registry_is_rate_limited(
    config: Config, bootstrap: dict[str, respx.Route]
) -> None:
    respx.get(f"{RIPE}/autnum/3320").mock(return_value=httpx.Response(429))

    result = await call(config, "AS3320")

    assert result.status is Status.RATE_LIMITED


@respx.mock
async def test_an_answer_about_the_wrong_kind_of_object_is_not_an_answer(
    config: Config, bootstrap: dict[str, respx.Route]
) -> None:
    """Asked for an autnum, told about an address range. Reporting that as a
    registration would be inventing one."""
    respx.get(f"{RIPE}/autnum/3320").mock(
        return_value=httpx.Response(200, json=fixture("ip_ripe_193_0_0_0_21.json"))
    )

    result = await call(config, "AS3320")

    assert result.status is Status.UPSTREAM_UNAVAILABLE


@respx.mock
async def test_a_broken_bootstrap_file_is_never_a_missing_registration(config: Config) -> None:
    respx.get(f"{IANA_BOOTSTRAP_URL}/asn.json").mock(
        return_value=httpx.Response(200, json={"version": "1.0"})
    )

    result = await call(config, "AS3320")

    assert result.status is Status.UPSTREAM_UNAVAILABLE
    assert "bootstrap" in result.note


@respx.mock
async def test_a_record_with_nothing_in_it_is_not_recorded(
    config: Config, bootstrap: dict[str, respx.Route]
) -> None:
    """A delegation that exists and publishes no detail. Saying `ok` here
    would hand back an answer that answers nothing."""
    respx.get(f"{RIPE}/autnum/12345").mock(
        return_value=httpx.Response(200, json=fixture("autnum_sparse.json"))
    )

    result = await call(config, "AS12345")

    assert result.status is Status.NOT_RECORDED
    assert result.data.handle == "AS12345"
    assert result.data.holder is None
    assert "publishes no holder" in result.note


# --- Untrusted text ----------------------------------------------------------


@respx.mock
async def test_registry_text_reaches_the_caller_without_its_structure(
    config: Config, bootstrap: dict[str, respx.Route]
) -> None:
    """A registration record is registry-published rather than self-reported,
    which makes it a weaker attack channel than PeeringDB and not a closed
    one: holder names and abuse contacts are still written by the holder.
    Three of the four channels RDAP offers — remarks, notices and links — are
    not declared at the boundary at all, so they cannot arrive here.
    """
    respx.get(f"{RIPE}/autnum/12345").mock(
        return_value=httpx.Response(200, json=fixture("autnum_hostile.json"))
    )

    result = await call(config, "AS12345")

    payload = result.model_dump_json()
    assert result.status is Status.OK
    assert "<b>" not in payload
    assert "[INST]" not in payload
    assert "```" not in payload
    assert "\\u202e" not in payload, "no right-to-left override"
    assert "\\u0000" not in payload
    assert "\\n" not in payload, "nothing spans what looks like a line"
    assert "remarks" not in payload
    assert "notices" not in payload
    assert result.data.abuse.email == "abuse@example.com", "a mailbox survives cleaning"


# --- Input -------------------------------------------------------------------


@respx.mock
@pytest.mark.parametrize("target", ["", "   ", "example.com", "1.2.3.4.5", "AS0", "AS4294967295"])
async def test_a_target_that_is_not_a_resource_is_invalid_input(
    config: Config, target: str
) -> None:
    result = await call(config, target)

    assert result.status is Status.INVALID_INPUT
    assert not respx.calls, "nothing is asked upstream about a target we cannot read"


@respx.mock
@pytest.mark.parametrize("target", ["AS3320", "as3320", "3320", " AS3320 "])
async def test_an_as_number_is_recognised_however_it_is_written(
    config: Config,
    bootstrap: dict[str, respx.Route],
    registries: dict[str, respx.Route],
    target: str,
) -> None:
    result = await call(config, target)

    assert result.status is Status.OK
    assert result.data.target == "AS3320"


@respx.mock
async def test_a_prefix_with_host_bits_set_is_normalised(
    config: Config, bootstrap: dict[str, respx.Route], registries: dict[str, respx.Route]
) -> None:
    """The caller has still said which block they mean, and this is the place
    a typo is most likely."""
    result = await call(config, "193.0.0.5/21")

    assert result.status is Status.OK
    assert result.data.target == "193.0.0.0/21"
