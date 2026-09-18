"""Unit tests for RDAP bootstrap: which registry answers for what.

Pure matching against the recorded IANA files, with no HTTP anywhere. This is
the part of the RDAP path that decides where a request is sent, so it is also
the part where being wrong is expensive: a bad match either asks the wrong
registry, which answers "no record" for a resource that exists, or builds a
request URL out of upstream data, which is the one place a bootstrap file
could do real harm.
"""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path

import pytest

from peering_mcp.clients.rdap import (
    REGISTRY_NAMES,
    match_asn,
    match_ip,
    registry_name,
    service_url,
)
from peering_mcp.errors import UpstreamProtocolError
from peering_mcp.models.upstream import RdapBootstrap

FIXTURES = Path(__file__).parent.parent / "fixtures" / "rdap"


def bootstrap(name: str) -> RdapBootstrap:
    return RdapBootstrap.model_validate(json.loads((FIXTURES / name).read_text()))


def network(value: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
    return ipaddress.ip_network(value, strict=False)


# --- The recorded files are what these tests assume ------------------------


def test_the_recorded_bootstrap_files_still_cover_the_registries() -> None:
    v4 = bootstrap("bootstrap_ipv4.json")
    hosts = {service_url(url) for entry in v4.services for url in entry.urls}

    assert len(v4.services) == 5, "five regional registries"
    assert any(url and "ripe" in url for url in hosts)


# --- Addresses -------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "registry"),
    [
        ("193.0.0.0/21", "RIPE NCC"),
        ("8.8.8.0/24", "ARIN"),
        ("1.1.1.1/32", "APNIC"),
        ("196.10.0.0/16", "AFRINIC"),
        ("200.3.12.0/24", "LACNIC"),
    ],
)
def test_an_address_resolves_to_the_registry_that_holds_it(target: str, registry: str) -> None:
    service = match_ip(bootstrap("bootstrap_ipv4.json"), network(target))

    assert service is not None
    assert service.registry == registry


def test_an_ipv6_prefix_resolves_against_the_ipv6_file() -> None:
    service = match_ip(bootstrap("bootstrap_ipv6.json"), network("2001:67c:2e8::/48"))

    assert service is not None
    assert service.registry == "RIPE NCC"


def test_reserved_space_belongs_to_nobody() -> None:
    """The honest answer is that no registry is responsible, and it costs no request."""
    assert match_ip(bootstrap("bootstrap_ipv4.json"), network("240.0.0.0/8")) is None


def test_a_range_wider_than_any_delegation_belongs_to_nobody() -> None:
    """A /4 spans several registries, so no single one covers it."""
    assert match_ip(bootstrap("bootstrap_ipv4.json"), network("192.0.0.0/4")) is None


def test_the_longest_entry_wins() -> None:
    """IANA delegates a /8 and then carves exceptions out of it, so a longer
    entry is the specific answer and a shorter one is only the fallback."""
    file = RdapBootstrap.model_validate(
        {
            "services": [
                [["23.0.0.0/8"], ["https://rdap.arin.net/registry/"]],
                [["23.128.0.0/10"], ["https://rdap.apnic.net/"]],
            ]
        }
    )

    wide = match_ip(file, network("23.0.0.0/16"))
    narrow = match_ip(file, network("23.128.0.0/16"))

    assert wide is not None and wide.registry == "ARIN"
    assert narrow is not None and narrow.registry == "APNIC"


def test_an_entry_that_is_not_a_prefix_is_skipped_rather_than_fatal() -> None:
    file = RdapBootstrap.model_validate(
        {"services": [[["not-a-prefix", "8.0.0.0/8"], ["https://rdap.arin.net/registry/"]]]}
    )

    service = match_ip(file, network("8.8.8.0/24"))

    assert service is not None and service.registry == "ARIN"


# --- AS numbers ------------------------------------------------------------


@pytest.mark.parametrize(
    ("asn", "registry"),
    [(3320, "RIPE NCC"), (6939, "ARIN"), (4608, "APNIC"), (36864, "AFRINIC")],
)
def test_an_as_number_resolves_to_the_registry_that_holds_it(asn: int, registry: str) -> None:
    service = match_asn(bootstrap("bootstrap_asn.json"), asn)

    assert service is not None
    assert service.registry == registry


def test_a_private_use_as_number_belongs_to_nobody() -> None:
    assert match_asn(bootstrap("bootstrap_asn.json"), 64512) is None


def test_a_single_number_entry_is_a_range_of_one() -> None:
    file = RdapBootstrap.model_validate(
        {"services": [[["1"], ["https://rdap.arin.net/registry/"]]]}
    )

    assert match_asn(file, 1) is not None
    assert match_asn(file, 2) is None


def test_the_narrowest_range_wins() -> None:
    file = RdapBootstrap.model_validate(
        {
            "services": [
                [["1-1000"], ["https://rdap.arin.net/registry/"]],
                [["500-600"], ["https://rdap.apnic.net/"]],
            ]
        }
    )

    service = match_asn(file, 550)

    assert service is not None and service.registry == "APNIC"


@pytest.mark.parametrize("entry", ["", "abc", "10-", "not-a-range", "600-500"])
def test_an_unreadable_range_is_skipped(entry: str) -> None:
    file = RdapBootstrap.model_validate(
        {"services": [[[entry], ["https://rdap.arin.net/registry/"]]]}
    )

    assert match_asn(file, 550) is None


# --- Turning a bootstrap entry into a request target -----------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://rdap.afrinic.net/rdap/",
        "ftp://rdap.example.net/",
        "javascript:alert(1)",
        "https:///no-host",
        "https://rdap.example.net/?redirect=elsewhere",
        "not a url at all",
    ],
)
def test_only_plain_https_is_accepted(url: str) -> None:
    """A bootstrap file names the host every later request goes to. It is the
    one place upstream data becomes a request target, so it is checked."""
    assert service_url(url) is None


def test_a_matched_entry_with_no_usable_url_is_an_upstream_problem() -> None:
    """Something covers the target and cannot be reached. Reporting that as
    'no registry holds this' would turn a broken file into a claim about the
    world."""
    file = RdapBootstrap.model_validate(
        {"services": [[["8.0.0.0/8"], ["http://rdap.arin.net/registry/"]]]}
    )

    with pytest.raises(UpstreamProtocolError, match="no HTTPS service"):
        match_ip(file, network("8.8.8.0/24"))


def test_a_registry_is_named_by_its_host() -> None:
    assert registry_name("https://rdap.db.ripe.net/") == "RIPE NCC"
    assert set(REGISTRY_NAMES.values()) == {"RIPE NCC", "ARIN", "APNIC", "LACNIC", "AFRINIC"}


def test_an_unknown_host_keeps_its_hostname() -> None:
    """Registries delegate, and new RDAP servers appear. A host nobody has
    named is still a usable answer."""
    assert registry_name("https://rdap.example.net/") == "rdap.example.net"


def test_a_bootstrap_url_joins_without_doubling_the_slash() -> None:
    file = RdapBootstrap.model_validate(
        {"services": [[["8.0.0.0/8"], ["https://rdap.arin.net/registry/"]]]}
    )
    service = match_ip(file, network("8.8.8.0/24"))

    assert service is not None
    assert service.url_for("ip/8.8.8.0/24") == "https://rdap.arin.net/registry/ip/8.8.8.0/24"
