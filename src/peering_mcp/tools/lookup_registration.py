"""The `lookup_registration` tool.

Answers "whose is this" for an address, a prefix or an AS number, out of the
registries' own records rather than PeeringDB's. The only tool here that does
not touch PeeringDB, and the only one whose answer nobody self-reports: a
registry publishes what it allocated.

The same division of labour as every other tool. This module turns a caller's
string into a target, and turns what comes back — including what goes wrong —
into the envelope. Working out which registry to ask belongs to
`clients/rdap.py`, and deciding what is worth returning belongs to `shaping.py`.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from peering_mcp.clients.rdap import IpNetwork, RdapClient
from peering_mcp.errors import UpstreamError, UpstreamNotFoundError
from peering_mcp.models.domain import Provenance, Registration, Status, ToolResult
from peering_mcp.shaping import LAST_CHANGED_EVENT, event_date, shape_registration
from peering_mcp.tools._envelope import upstream_failure

#: The valid 32-bit AS number range. 0 and 4294967295 are reserved.
_MIN_ASN = 1
_MAX_ASN = 4_294_967_294

_ASN_PATTERN = re.compile(r"^(?:as)?(\d+)$", re.IGNORECASE)

_REGISTRY_SOURCED = (
    "Registry data: it says who an allocation was made to, which is not always "
    "who operates the resource today."
)


@dataclass(frozen=True, slots=True)
class AsnTarget:
    """The caller asked about an AS number."""

    asn: int


@dataclass(frozen=True, slots=True)
class IpTarget:
    """The caller asked about an address or a block of them."""

    network: IpNetwork

    @property
    def kind(self) -> Literal["address", "prefix"]:
        """Whether that was one address or a whole block."""
        return "address" if self.network.prefixlen == self.network.max_prefixlen else "prefix"

    @property
    def shown(self) -> str:
        """The target as it is echoed back, normalised."""
        if self.kind == "address":
            return str(self.network.network_address)
        return str(self.network)


def parse_target(raw: str) -> AsnTarget | IpTarget | None:
    """Read a caller's string as an AS number, an address or a prefix.

    AS numbers are tried first, because `3320` is a plain integer and an
    address never is. The AS spellings are exactly the ones `lookup_network`
    accepts — `AS3320`, `as3320`, `3320` — so a caller moving between the two
    tools does not have to rewrite the argument.

    A prefix with host bits set is accepted and normalised: `193.0.0.5/21`
    becomes `193.0.0.0/21`, because the caller has still said exactly which
    block they mean, and refusing it would be pedantry in the place a typo is
    most likely.
    """
    match = _ASN_PATTERN.match(raw)
    if match is not None:
        value = int(match.group(1))
        return AsnTarget(asn=value) if _MIN_ASN <= value <= _MAX_ASN else None

    try:
        return IpTarget(network=ipaddress.ip_network(raw, strict=False))
    except ValueError:
        return None


async def lookup_registration(client: RdapClient, target: str) -> ToolResult[Registration]:
    """Look up who an address, a prefix or an AS number is registered to."""
    cleaned = target.strip()
    if not cleaned:
        return ToolResult(
            status=Status.INVALID_INPUT,
            note="Give an IP address, a prefix such as 193.0.0.0/21, "
            "or an AS number such as AS3320.",
        )

    parsed = parse_target(cleaned)
    if parsed is None:
        return ToolResult(
            status=Status.INVALID_INPUT,
            note=f"{cleaned!r} is not an IP address, a prefix or an AS number. "
            "Domain names are not supported.",
        )

    try:
        if isinstance(parsed, AsnTarget):
            return await _by_asn(client, parsed)
        return await _by_ip(client, parsed)
    except UpstreamNotFoundError:
        return ToolResult(
            status=Status.NOT_FOUND,
            note=f"No registry has a record for {cleaned}. "
            "Either nothing is registered there, or the block is registered in "
            "smaller pieces — ask about one address inside it.",
        )
    except UpstreamError as exc:
        return upstream_failure(exc)


async def _by_asn(client: RdapClient, target: AsnTarget) -> ToolResult[Registration]:
    fetched = await client.autnum(target.asn)
    answer = fetched.value
    if answer is None:
        return ToolResult(
            status=Status.NOT_FOUND,
            note=f"No registry is responsible for AS{target.asn}. "
            "Numbers reserved for private use, such as 64512, are the usual reason.",
        )
    return _envelope(
        shape_registration(
            answer.record,
            target=f"AS{target.asn}",
            kind="asn",
            registry=answer.service.registry,
        ),
        record_updated=event_date(answer.record, LAST_CHANGED_EVENT),
        from_cache=fetched.from_cache,
    )


async def _by_ip(client: RdapClient, target: IpTarget) -> ToolResult[Registration]:
    fetched = await client.ip(target.network)
    answer = fetched.value
    if answer is None:
        return ToolResult(
            status=Status.NOT_FOUND,
            note=f"No registry is responsible for {target.shown}. "
            "Reserved and documentation ranges are the usual reason.",
        )
    return _envelope(
        shape_registration(
            answer.record,
            target=target.shown,
            kind=target.kind,
            registry=answer.service.registry,
        ),
        record_updated=event_date(answer.record, LAST_CHANGED_EVENT),
        from_cache=fetched.from_cache,
    )


def _envelope(
    registration: Registration, *, record_updated: datetime | None, from_cache: bool
) -> ToolResult[Registration]:
    """Wrap a shaped registration, or report that the record is empty.

    A registry can answer 200 with an object carrying nothing but its own
    identifier: a delegation that exists and has no published detail. That is
    `not_recorded` rather than `ok` — the resource is registered, and this
    server has learned nothing about who holds it. Returning `ok` with empty
    fields would be exactly the answer-that-is-not-one the taxonomy exists to
    prevent.
    """
    provenance = Provenance.now("rdap", record_updated=record_updated, from_cache=from_cache)

    if not (registration.holder or registration.registered or registration.abuse):
        return ToolResult(
            status=Status.NOT_RECORDED,
            data=registration,
            note=f"{registration.registry} has a record for {registration.target} "
            "but publishes no holder, allocation date or abuse contact for it.",
            provenance=provenance,
        )
    return ToolResult(
        status=Status.OK,
        data=registration,
        note=_REGISTRY_SOURCED,
        provenance=provenance,
    )
