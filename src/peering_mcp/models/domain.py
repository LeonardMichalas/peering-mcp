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
    """How a tool call turned out."""

    OK = "ok"
    #: The thing genuinely does not exist upstream.
    NOT_FOUND = "not_found"
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
    shape covers both, so the caller never has to branch on which it got.
    """

    network: Network | None = Field(
        default=None, description="Set when the query resolved to exactly one network."
    )
    candidates: list[NetworkMatch] = Field(
        default_factory=list,
        description="Set when a name matched several networks. Call again with one of these ASNs.",
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
