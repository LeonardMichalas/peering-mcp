"""Typed access to RDAP, the registries' own registration data.

The second upstream, and the only one this server reaches without knowing in
advance where it lives. PeeringDB is one host; RDAP is five regional registries
plus everyone they delegate to, and which of them holds a given address range
or AS number is itself a lookup. That lookup is **bootstrap**: IANA publishes
one file per resource type saying which registry answers for which block, and
a client reads it before it can ask anything.

**Bootstrap is done here rather than through a redirector.** `rdap.org` would
resolve a target in one request and save this whole module, at the cost of
putting somebody else's service in the path of every answer and hiding which
registry replied. The files are small — 5.6 KB, 1.5 KB and 4.4 KB on
2026-09-18 — they cache like anything else, and the match itself is what tells
us the registry's name, which is part of the answer.

**No rate limiter.** RDAP has no published per-client limit and no
authentication; the registries serve it as a public lookup service. The
politeness that does apply is the shared one: a cached answer never leaves the
machine, and every request identifies this server by name.

**Never call `get_json` from a tool.** As with PeeringDB, raw JSON stops here.
"""

from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass
from types import TracebackType
from typing import Self
from urllib.parse import urlsplit

from pydantic import ValidationError

from peering_mcp.clients.http import Fetched, HttpCore
from peering_mcp.config import Config
from peering_mcp.errors import UpstreamProtocolError
from peering_mcp.models.upstream import (
    RdapAutnum,
    RdapBootstrap,
    RdapBootstrapService,
    RdapIpNetwork,
    RdapObject,
)

logger = logging.getLogger(__name__)

#: Where IANA publishes the bootstrap files. RFC 9224 for AS numbers, RFC 7484
#: for the rest.
IANA_BOOTSTRAP_URL = "https://data.iana.org/rdap"

#: The five regional registries, by the host their bootstrap entry points at.
#: A name a person recognises is worth carrying: "RIPE NCC" means something to
#: a network engineer and `rdap.db.ripe.net` means one more thing to look up.
#: Anything not listed falls back to its hostname, which is honest and still
#: useful — delegated RDAP servers exist and new ones will appear.
REGISTRY_NAMES = {
    "rdap.db.ripe.net": "RIPE NCC",
    "rdap.arin.net": "ARIN",
    "rdap.apnic.net": "APNIC",
    "rdap.lacnic.net": "LACNIC",
    "rdap.afrinic.net": "AFRINIC",
}

IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass(frozen=True, slots=True)
class RdapService:
    """One registry's RDAP endpoint, and what to call that registry."""

    base_url: str
    registry: str

    def url_for(self, path: str) -> str:
        """The absolute URL for a query against this registry."""
        return f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"


@dataclass(frozen=True, slots=True)
class RdapAnswer[T: RdapObject]:
    """A registry's answer, and which registry gave it.

    The registry travels with the record because it is part of the answer, not
    metadata about how we got it: "ARIN says Google LLC" is a different claim
    from "somebody says Google LLC", and a caller repeating the second one has
    lost the part that makes it checkable.
    """

    record: T
    service: RdapService


class RdapClient:
    """Reads registration data for address ranges and AS numbers."""

    def __init__(self, config: Config, *, core: HttpCore | None = None) -> None:
        self._config = config
        self._core = core or HttpCore(
            config,
            base_url="",
            # RDAP's own media type first, with plain JSON as a fallback for
            # servers that only ever learned the second one.
            headers={"Accept": "application/rdap+json, application/json;q=0.9"},
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._core.aclose()

    async def ip(self, network: IpNetwork) -> Fetched[RdapAnswer[RdapIpNetwork] | None]:
        """Registration data for an address or a prefix.

        `None` means no registry claims that range: the bootstrap file has no
        entry covering it, which is true of reserved and unallocated space and
        is a different fact from a registry answering that it has no record.
        """
        family = "ipv6" if network.version == 6 else "ipv4"
        bootstrap = await self._bootstrap(family)
        service = match_ip(bootstrap.value, network)
        if service is None:
            return bootstrap.with_value(None)

        query = str(network.network_address) if _is_single_address(network) else str(network)
        fetched = await self._core.get_json(service.url_for(f"ip/{query}"))
        record = parse_object(fetched.value, RdapIpNetwork, source=service.registry)
        return fetched.with_value(RdapAnswer(record=record, service=service))

    async def autnum(self, asn: int) -> Fetched[RdapAnswer[RdapAutnum] | None]:
        """Registration data for an AS number.

        `None` means the same as it does for an address range: no registry
        holds that part of the number space. AS numbers reserved for private
        use, such as 64512, are the common case.
        """
        bootstrap = await self._bootstrap("asn")
        service = match_asn(bootstrap.value, asn)
        if service is None:
            return bootstrap.with_value(None)

        fetched = await self._core.get_json(service.url_for(f"autnum/{asn}"))
        record = parse_object(fetched.value, RdapAutnum, source=service.registry)
        return fetched.with_value(RdapAnswer(record=record, service=service))

    async def _bootstrap(self, family: str) -> Fetched[RdapBootstrap]:
        """The IANA file saying who answers for what, cached like any response."""
        fetched = await self._core.get_json(f"{IANA_BOOTSTRAP_URL}/{family}.json")
        # No `except ValidationError` here, deliberately: every field on
        # `RdapBootstrap` degrades, so validating a JSON object cannot fail.
        # A file that is unusable is an empty one, and that is the check below.
        bootstrap = RdapBootstrap.model_validate(fetched.value)

        if not bootstrap.services:
            msg = f"the IANA {family} bootstrap file lists no services"
            raise UpstreamProtocolError(msg)
        return fetched.with_value(bootstrap)


def service_url(raw: str) -> str | None:
    """The URL if it is one this server is willing to call, otherwise None.

    A bootstrap file names the host every registration query then goes to, so
    this is the one place where upstream data becomes a request target. Plain
    HTTPS with a hostname, and nothing else: no `http://`, which IANA also
    lists and which would send the query in clear, and no scheme that is not
    HTTP at all.
    """
    parts = urlsplit(raw)
    if parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment:
        return None
    return raw


def registry_name(url: str) -> str:
    """What to call the registry behind a bootstrap URL."""
    host = urlsplit(url).hostname or url
    return REGISTRY_NAMES.get(host, host)


def _service_from(entry: RdapBootstrapService, *, covering: str) -> RdapService:
    """Turn a matched bootstrap line into the endpoint to query.

    A line that matched but publishes no usable HTTPS URL is an upstream
    problem, not a missing registration: something covers the target and we
    cannot reach it. Saying "no registry claims this" there would report a
    broken bootstrap file as an unallocated resource, which is the same
    confusion `not_found` versus `upstream_unavailable` exists to prevent.
    """
    for raw in entry.urls:
        url = service_url(raw)
        if url is not None:
            return RdapService(base_url=url, registry=registry_name(url))

    msg = f"the IANA bootstrap entry covering {covering} publishes no HTTPS service"
    logger.warning("%s", msg)
    raise UpstreamProtocolError(msg)


def match_ip(bootstrap: RdapBootstrap, network: IpNetwork) -> RdapService | None:
    """The registry that answers for an address range, or None if none does.

    Longest match wins, the way a routing table works: IANA delegates /8s and
    then carves exceptions out of them, so a shorter entry is the fallback and
    a longer one is the specific answer.
    """
    best: tuple[int, RdapBootstrapService] | None = None
    for service in bootstrap.services:
        for raw in service.entries:
            covered = _parse_network(raw)
            if covered is None or covered.version != network.version:
                continue
            if not _contains(covered, network):
                continue
            if best is None or covered.prefixlen > best[0]:
                best = (covered.prefixlen, service)

    if best is None:
        return None
    return _service_from(best[1], covering=str(network))


def match_asn(bootstrap: RdapBootstrap, asn: int) -> RdapService | None:
    """The registry that answers for an AS number, or None if none does.

    Narrowest range wins, for the same reason longest prefix does. IANA's
    ranges do not overlap today; relying on that would be a rule nobody wrote
    down.
    """
    best: tuple[int, RdapBootstrapService] | None = None
    for service in bootstrap.services:
        for raw in service.entries:
            span = _parse_asn_range(raw)
            if span is None:
                continue
            low, high = span
            if not low <= asn <= high:
                continue
            width = high - low
            if best is None or width < best[0]:
                best = (width, service)

    if best is None:
        return None
    return _service_from(best[1], covering=f"AS{asn}")


def parse_object[T: RdapObject](payload: dict[str, object], model: type[T], *, source: str) -> T:
    """Validate one RDAP object into a model.

    Two failures, one status. A body that will not validate — which includes
    an entity tree nested deeply enough that pydantic stops unwinding it — and
    a body that is a different kind of object from the one asked for both mean
    the same thing: the registry answered, and not with what it was asked.
    Reporting either as "no such record" would turn a broken upstream into a
    claim about the world.
    """
    expected = "autnum" if model is RdapAutnum else "ip network"
    try:
        record = model.model_validate(payload)
    except ValidationError as exc:
        msg = f"{source} answered with an object this server cannot read"
        logger.warning("%s: %s", msg, _first_reason(exc))
        raise UpstreamProtocolError(msg) from exc

    if record.object_class_name is not None and record.object_class_name != expected:
        msg = f"{source} answered a query for {expected} with a {record.object_class_name} object"
        logger.warning("%s", msg)
        raise UpstreamProtocolError(msg)
    return record


def _first_reason(exc: ValidationError) -> str:
    """One bounded line describing why a record failed, for the log.

    Bounded because an RDAP validation failure can be enormous: a thousand
    nested entities report a location a thousand segments long, and a log line
    that size is a second problem rather than a description of the first.
    """
    errors = exc.errors()
    if not errors:
        return "validation failed"
    first = errors[0]
    location = ".".join(str(part) for part in tuple(first.get("loc", ()))[:4]) or "record"
    return f"{location}: {first.get('type', 'invalid')}"


def _parse_network(raw: str) -> IpNetwork | None:
    """A bootstrap entry as a network, or None if it is not one."""
    try:
        return ipaddress.ip_network(raw, strict=False)
    except ValueError:
        return None


def _parse_asn_range(raw: str) -> tuple[int, int] | None:
    """A bootstrap entry as an inclusive AS number range, or None.

    IANA writes both `36864-37887` and the single-number form `1`.
    """
    low, _, high = raw.partition("-")
    try:
        start = int(low)
        end = int(high) if high else start
    except ValueError:
        return None
    return (start, end) if start <= end else None


def _contains(covered: IpNetwork, target: IpNetwork) -> bool:
    """Whether a bootstrap entry covers the whole of the target range.

    Compared as integers rather than as addresses, because the two sides are
    only known to be the same family by the caller checking, and an
    IPv4Address and an IPv6Address do not compare at all.
    """
    return int(covered.network_address) <= int(target.network_address) and int(
        covered.broadcast_address
    ) >= int(target.broadcast_address)


def _is_single_address(network: IpNetwork) -> bool:
    """Whether the target is one address rather than a range.

    RDAP accepts both `/ip/193.0.0.1` and `/ip/193.0.0.0/21`, and asking with
    the form the caller used keeps the query honest: a caller who gave one
    address is not asserting anything about the size of the block around it.
    """
    return network.prefixlen == network.max_prefixlen


__all__ = [
    "IANA_BOOTSTRAP_URL",
    "REGISTRY_NAMES",
    "RdapAnswer",
    "RdapClient",
    "RdapService",
    "match_asn",
    "match_ip",
    "parse_object",
    "registry_name",
    "service_url",
]
