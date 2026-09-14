"""What PeeringDB sends. Untrusted, and validated here before anything uses it.

This module is the boundary. Above it, the rest of the server works with typed
objects and can assume a field is either a usable value or `None`. Below it is
JSON written by whoever owns that record.

Three rules shape every model here.

**Identity is required; everything else degrades.** A network without a usable
AS number, or an exchange without an id, is not a partial record — it is a
record nothing can be done with, and accepting it means inventing the missing
part. Those fields are required, and a row lacking one fails validation. Every
other field falls back to `None`, which the tool layer reports as
`not_recorded`. A registry that has not been filled in is the normal case here,
not an error.

**Unknown fields are dropped, not carried.** `extra="ignore"` means a field
PeeringDB adds next year cannot reach a model's context window without somebody
adding it here first. That is also why `notes` and `aka` are absent: they are
free text written by the network itself, they carry nothing a decision depends
on, and the cheapest way to guarantee they never leak is for them not to exist
past this line.

**Coercion is deliberate and narrow.** PeeringDB uses `""` and `null`
interchangeably for "not filled in", returns numbers as strings in places, and
occasionally sends a type nobody expected. Each field type below converts what
it can and gives up quietly on the rest. It never guesses.

**Every string is cleaned here, not later.** `Text` and `RequiredText` run
`sanitize.clean` as part of validation, so a value that has been parsed has
already had its structure removed. Putting it in the shaping layer instead
would work today and would be one forgotten call away from not working, which
is the same reason read-only is enforced at the transport rather than by
convention. See `sanitize.py` for what that cleaning can and cannot do.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from peering_mcp.sanitize import DEFAULT_MAX_LENGTH, clean_optional

#: Longest free-text value accepted from upstream, owned by the sanitiser.
MAX_TEXT_LENGTH = DEFAULT_MAX_LENGTH


def _as_text(value: object) -> str | None:
    """A cleaned string, or None. Empty and whitespace-only mean "not filled in"."""
    return clean_optional(value)


def _require_text(value: object) -> str:
    """Same, but a missing value is a broken record rather than an empty field.

    A value that cleans away to nothing counts as missing. A network whose name
    is only punctuation has not given us a name.
    """
    text = _as_text(value)
    if text is None:
        msg = "expected a non-empty string"
        raise ValueError(msg)
    return text


def _as_int(value: object) -> int | None:
    """An integer, or None.

    `True` is an int in Python and never an int in a registry, so booleans are
    refused rather than silently read as 1.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _as_bool(value: object) -> bool | None:
    """A boolean, or None. A string is not coerced: "no" reads as False too easily."""
    return value if isinstance(value, bool) else None


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _as_timestamp(value: object) -> datetime | None:
    """An ISO-8601 instant, or None. PeeringDB writes the UTC suffix as `Z`."""
    raw = _as_text(value)
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


Text = Annotated[str | None, BeforeValidator(_as_text)]
RequiredText = Annotated[str, BeforeValidator(_require_text)]
Integer = Annotated[int | None, BeforeValidator(_as_int)]
RequiredInteger = Annotated[int, BeforeValidator(_as_int)]
Flag = Annotated[bool | None, BeforeValidator(_as_bool)]
Decimal = Annotated[float | None, BeforeValidator(_as_float)]
Timestamp = Annotated[datetime | None, BeforeValidator(_as_timestamp)]


class UpstreamRecord(BaseModel):
    """Fields every PeeringDB object carries.

    Frozen because nothing should edit an upstream record after parsing it. If
    a value needs changing, that is shaping, and it happens one layer up.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True, frozen=True)

    record_id: Integer = Field(default=None, alias="id")
    created: Timestamp = None
    updated: Timestamp = None
    status: Text = None


class UpstreamNetwork(UpstreamRecord):
    """A `/net` object: a network, as its own operators describe it."""

    asn: RequiredInteger
    name: RequiredText
    name_long: Text = None
    website: Text = None

    info_type: Text = None
    info_traffic: Text = None
    info_scope: Text = None
    info_ratio: Text = None
    info_prefixes4: Integer = None
    info_prefixes6: Integer = None

    ix_count: Integer = None
    fac_count: Integer = None

    policy_general: Text = None
    policy_locations: Text = None
    policy_ratio: Flag = None
    policy_contracts: Text = None
    policy_url: Text = None

    irr_as_set: Text = None
    looking_glass: Text = None


class UpstreamNetworkIxLan(UpstreamRecord):
    """A `/netixlan` object: one network's presence on one exchange LAN.

    `ix_id` is required alongside `asn`, because a presence record that cannot
    say which exchange it is at answers nothing.
    """

    asn: RequiredInteger
    ix_id: RequiredInteger
    net_id: Integer = None
    ixlan_id: Integer = None
    name: Text = None
    speed: Integer = Field(default=None, description="Port speed in Mbps.")
    ipaddr4: Text = None
    ipaddr6: Text = None
    is_rs_peer: Flag = None
    operational: Flag = None


class UpstreamNetworkFacility(UpstreamRecord):
    """A `/netfac` object: one network's presence in one facility.

    Filtered by `net_id`, not by ASN. See the note in `clients/peeringdb.py`.
    """

    fac_id: RequiredInteger
    net_id: RequiredInteger
    local_asn: Integer = None
    name: Text = None
    city: Text = None
    country: Text = None


class UpstreamExchange(UpstreamRecord):
    """An `/ix` object: an internet exchange."""

    record_id: RequiredInteger = Field(alias="id")
    name: RequiredText
    name_long: Text = None
    city: Text = None
    country: Text = None
    region_continent: Text = None
    media: Text = None
    website: Text = None
    tech_email: Text = None
    policy_email: Text = None
    proto_unicast: Flag = None
    proto_ipv6: Flag = None
    net_count: Integer = None
    fac_count: Integer = None
    service_level: Text = None
    terms: Text = None


class UpstreamFacility(UpstreamRecord):
    """A `/fac` object: a data centre or colocation site."""

    record_id: RequiredInteger = Field(alias="id")
    name: RequiredText
    name_long: Text = None
    org_name: Text = None
    website: Text = None
    city: Text = None
    country: Text = None
    state: Text = None
    region_continent: Text = None
    clli: Text = None
    latitude: Decimal = None
    longitude: Decimal = None
    net_count: Integer = None
    ix_count: Integer = None


#: Every upstream model, so a test can assert the whole family behaves.
UPSTREAM_MODELS: tuple[type[UpstreamRecord], ...] = (
    UpstreamNetwork,
    UpstreamNetworkIxLan,
    UpstreamNetworkFacility,
    UpstreamExchange,
    UpstreamFacility,
)


def field_names(model: type[UpstreamRecord]) -> set[str]:
    """The upstream field names a model accepts, aliases included."""
    names: set[str] = set()
    for name, field in model.model_fields.items():
        names.add(field.alias or name)
    return names


__all__ = [
    "MAX_TEXT_LENGTH",
    "UPSTREAM_MODELS",
    "UpstreamExchange",
    "UpstreamFacility",
    "UpstreamNetwork",
    "UpstreamNetworkFacility",
    "UpstreamNetworkIxLan",
    "UpstreamRecord",
    "field_names",
]
