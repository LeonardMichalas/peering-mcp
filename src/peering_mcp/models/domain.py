"""What the tools return.

One envelope for every tool, so a model learns a single shape rather than one
per tool. The status taxonomy is the important part: `not_found` and
`not_recorded` are deliberately different, because PeeringDB is self-reported
and a missing record is not evidence that something is untrue.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class Status(StrEnum):
    """How a tool call turned out.

    `OK` carries one promise: the answer is in `data` and the caller can use it
    without checking anything else. Every other member exists because some
    result would otherwise have to be smuggled through `OK` with an empty
    payload, and a status nobody can trust is worse than no status at all.
    """

    OK = "ok"
    #: The thing genuinely does not exist upstream.
    NOT_FOUND = "not_found"
    #: The query matched several things and guessing between them would be
    #: worse than asking. Candidates are in `data`; the caller picks one and
    #: calls again.
    AMBIGUOUS = "ambiguous"
    #: It exists, but this field or relationship is not populated. Absence in a
    #: self-reported registry is not evidence of absence in reality.
    NOT_RECORDED = "not_recorded"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    RATE_LIMITED = "rate_limited"
    INVALID_INPUT = "invalid_input"


class Provenance(BaseModel):
    """Where an answer came from and how old it is."""

    source: Literal["peeringdb", "rdap"]
    fetched_at: datetime = Field(description="When this server fetched the data.")
    record_updated: datetime | None = Field(
        default=None,
        description="When the network last edited its own record. Older means less reliable.",
    )
    from_cache: bool = False

    @classmethod
    def now(
        cls,
        source: Literal["peeringdb", "rdap"],
        *,
        record_updated: datetime | None = None,
        from_cache: bool = False,
    ) -> Provenance:
        return cls(
            source=source,
            fetched_at=datetime.now(UTC),
            record_updated=record_updated,
            from_cache=from_cache,
        )


class PeeringPolicy(BaseModel):
    """A network's stated terms for peering. Self-reported."""

    general: str | None = Field(
        default=None, description="Open, Selective, Restrictive or No policy."
    )
    locations: str | None = Field(default=None, description="Location requirement, if any.")
    ratio_required: bool | None = Field(
        default=None, description="Whether a traffic ratio requirement applies."
    )
    contract_required: str | None = Field(
        default=None, description="Whether a contract is required."
    )
    url: str | None = Field(default=None, description="Link to the full policy, if published.")


class Network(BaseModel):
    """A network as PeeringDB describes it."""

    asn: int
    name: str
    long_name: str | None = None
    website: str | None = None

    network_type: str | None = Field(default=None, description="For example NSP, Content, Cable.")
    traffic_estimate: str | None = Field(default=None, description="Self-reported traffic band.")
    scope: str | None = Field(default=None, description="Geographic scope, for example Global.")
    traffic_ratio: str | None = None
    ipv4_prefixes: int | None = None
    ipv6_prefixes: int | None = None

    exchange_count: int | None = Field(
        default=None, description="How many internet exchanges they record a presence at."
    )
    facility_count: int | None = Field(
        default=None, description="How many facilities they record a presence at."
    )

    policy: PeeringPolicy = Field(default_factory=PeeringPolicy)
    irr_as_set: str | None = None
    looking_glass: str | None = None


class NetworkMatch(BaseModel):
    """One candidate from an ambiguous name search."""

    asn: int
    name: str


class NetworkLookup(BaseModel):
    """The payload of `lookup_network`.

    Either one network resolved, or several candidates to choose between. One
    shape covers both; which one it is, is told by the envelope's status, so
    the caller branches on `status` and never has to probe the payload to find
    out what it got.
    """

    network: Network | None = Field(
        default=None, description="Set when status is ok: the query resolved to one network."
    )
    candidates: list[NetworkMatch] = Field(
        default_factory=list,
        description="Set when status is ambiguous: a name matched several networks. "
        "Call again with one of these ASNs.",
    )


class ExchangePresence(BaseModel):
    """One network's presence at one internet exchange, all ports combined."""

    name: str
    city: str | None = None
    country: str | None = Field(default=None, description="ISO 3166-1 two-letter code.")
    speed_mbps: int | None = Field(
        default=None, description="Total port capacity at this exchange, in Mbps."
    )
    ports: int = Field(description="How many separate ports the network records here.")
    route_server: bool | None = Field(
        default=None, description="Whether they peer with the exchange's route server."
    )


class FacilityPresence(BaseModel):
    """One network's presence in one facility."""

    name: str | None = None
    city: str | None = None
    country: str | None = Field(default=None, description="ISO 3166-1 two-letter code.")


class Page[T](BaseModel):
    """A bounded slice of a list, and how much of the list it is.

    `total` and `truncated` travel with the items so a caller can never mistake
    the first fifty of three hundred for the whole.
    """

    items: list[T]
    total: int = Field(description="How many there are in all, not just on this page.")
    truncated: bool = Field(description="True when items holds fewer than total.")


class PresenceList(BaseModel):
    """The payload of `list_presence`.

    A list the caller did not ask for is `None`, so an absent list is never
    confused with an empty one.
    """

    asn: int
    network: str
    exchanges: Page[ExchangePresence] | None = Field(
        default=None, description="Largest total port capacity first. None when kind excluded it."
    )
    facilities: Page[FacilityPresence] | None = Field(
        default=None, description="Ordered by country, then city. None when kind excluded it."
    )


class ParticipantPorts(BaseModel):
    """One network's ports at a location every network in the query is present at."""

    asn: int
    speed_mbps: int | None = Field(
        default=None, description="This network's total port capacity here, in Mbps."
    )
    ports: int = Field(description="How many separate ports this network records here.")
    route_server: bool | None = Field(
        default=None, description="Whether they peer with the exchange's route server."
    )


class SharedExchange(BaseModel):
    """An internet exchange every network in the query records a presence at."""

    name: str
    city: str | None = None
    country: str | None = Field(default=None, description="ISO 3166-1 two-letter code.")
    networks: list[ParticipantPorts] = Field(
        description="One entry per AS number asked about, in the order asked."
    )


class PresenceTotals(BaseModel):
    """How much presence one network records in all.

    Travels beside the shared lists so an empty overlap can be explained
    rather than merely reported: two networks at 300 places each that share
    nothing is a different fact from one of them recording nothing at all.
    """

    asn: int
    name: str
    exchanges: int = Field(description="Exchanges this network records, shared or not.")
    facilities: int = Field(description="Facilities this network records, shared or not.")


class CommonPresence(BaseModel):
    """The payload of `find_common_presence`."""

    networks: list[PresenceTotals] = Field(
        description="One entry per AS number asked about, in the order asked."
    )
    exchanges: Page[SharedExchange] = Field(
        description="Exchanges all of them are present at, largest shared capacity first."
    )
    facilities: Page[FacilityPresence] = Field(
        description="Facilities all of them are present in, ordered by country then city."
    )


class ToolResult[T](BaseModel):
    """The envelope every tool returns."""

    status: Status
    data: T | None = None
    note: str | None = Field(
        default=None,
        description="A caveat the caller should read before using or repeating the data.",
    )
    provenance: Provenance | None = None
