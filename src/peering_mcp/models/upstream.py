"""What the upstreams send. Untrusted, and validated here before anything uses it.

This module is the boundary. Above it, the rest of the server works with typed
objects and can assume a field is either a usable value or `None`. Below it is
JSON written by whoever owns that record.

PeeringDB comes first, RDAP second; the same three rules shape every model
here.

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

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

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


# --- RDAP -------------------------------------------------------------------
#
# The second upstream, and a different shape of problem. PeeringDB answers with
# a flat list of records; RDAP answers with one nested object whose useful
# parts sit in two structures the JSON does not make obvious: a list of events
# keyed by an action string, and a *tree* of entities keyed by role. Both are
# modelled here, so that no tool ever walks raw RDAP JSON.
#
# The three rules above still hold, and the middle one earns its keep twice
# over: an RDAP object carries `remarks`, `notices` and `links`, three separate
# channels of registry-editable free text. None of them is declared below, so
# none of them can reach a model. Identity degrades rather than failing,
# because registries publish different things — RIPE's autnum record for
# AS3320 carries no country and no type at all, measured on 2026-09-18.

#: Longest list this boundary carries through, whatever upstream sent. Real
#: records hold five or six entities and two events.
MAX_LIST_ITEMS = 25

#: jCard properties worth carrying out of an entity. A vCard can also hold a
#: postal address, a photo, a timezone and a fax number; a caller asking who an
#: address range belongs to, and where to report abuse, needs a name and a
#: mailbox.
_VCARD_WANTED = frozenset({"fn", "email"})


def _as_text_list(value: object) -> list[str]:
    """A capped list of cleaned strings. Anything else is an empty list."""
    if not isinstance(value, list):
        return []
    cleaned = (_as_text(item) for item in value[:MAX_LIST_ITEMS])
    return [item for item in cleaned if item is not None]


def _as_plain_strings(value: object) -> list[str]:
    """Strings kept verbatim: the bootstrap file's prefixes, ranges and URLs.

    The one place upstream text is *not* cleaned, because cleaning it would
    destroy it — a URL is structure, and the slash in a prefix is the point.
    What protects this path instead is that none of it is ever shown to a
    model: these values are matched against parsed addresses, and the URL they
    produce has to survive `service_url`, which accepts plain HTTPS and nothing
    else.
    """
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and len(item) <= MAX_TEXT_LENGTH]


def _as_objects(value: object) -> list[object]:
    """Keep the list entries that are objects, drop the rest, cap the length.

    One malformed entry must not cost the whole record, and a registry
    answering with ten thousand entities must not cost the whole context
    window.
    """
    if not isinstance(value, list):
        return []
    kept: list[object] = [item for item in value if isinstance(item, dict)]
    return kept[:MAX_LIST_ITEMS]


def _as_bootstrap_services(value: object) -> list[object]:
    """Keep the bootstrap lines that have the documented two-part shape."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, list) and len(item) == 2]


def _as_vcard(value: object) -> dict[str, object]:
    """Flatten a jCard array into the two fields worth carrying.

    RDAP carries contact details as jCard (RFC 7095): a two-element array whose
    second element is a list of `[name, parameters, type, value]` properties.
    None of that shape is enforced by the servers that send it, so every level
    is checked rather than unpacked.
    """
    if not isinstance(value, list) or len(value) != 2:
        return {}
    properties = value[1]
    if not isinstance(properties, list):
        return {}

    found: dict[str, object] = {}
    for entry in properties:
        if not isinstance(entry, list) or len(entry) < 4:
            continue
        name = entry[0]
        if not isinstance(name, str) or name.lower() not in _VCARD_WANTED:
            continue
        # First one wins. A vCard may repeat a property, and a record listing
        # two abuse mailboxes has not told us which of them is right.
        found.setdefault(name.lower(), entry[3])
    return {"full_name": found.get("fn"), "email": found.get("email")}


class RdapVCard(BaseModel):
    """An entity's contact details, flattened out of jCard."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    full_name: Text = None
    email: Text = None


VCard = Annotated[RdapVCard, BeforeValidator(_as_vcard)]
TextList = Annotated[list[str], BeforeValidator(_as_text_list)]
PlainStrings = Annotated[list[str], BeforeValidator(_as_plain_strings)]
Objects = BeforeValidator(_as_objects)


class RdapEvent(BaseModel):
    """One dated thing that happened to a record.

    The action stays a string rather than becoming an enum: registries publish
    actions this server has no use for, and an unmatched value is cheaper than
    a record that fails to parse over one.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True, frozen=True)

    action: Text = Field(default=None, alias="eventAction")
    date: Timestamp = Field(default=None, alias="eventDate")


class RdapEntity(BaseModel):
    """A person or organisation attached to a record, by role.

    Recursive, because the registries disagree about where the abuse contact
    lives: RIPE publishes it beside the registrant, ARIN nests it underneath
    the registrant. Measured against both on 2026-09-18 — anything reading only
    the top level finds `abuse@ripe.net` for 193.0.0.0/21 and nothing at all
    for 8.8.8.0/24.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True, frozen=True)

    handle: Text = None
    roles: TextList = []
    contact: VCard = Field(default=RdapVCard(), alias="vcardArray")
    entities: Annotated[list[RdapEntity], Objects] = []


class RdapObject(BaseModel):
    """What every RDAP answer this server reads has in common."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, frozen=True)

    object_class_name: Text = Field(default=None, alias="objectClassName")
    handle: Text = None
    name: Text = None
    allocation_type: Text = Field(
        default=None,
        alias="type",
        description="How the registry allocated it, for example ASSIGNED PA.",
    )
    country: Text = None
    status: TextList = []
    events: Annotated[list[RdapEvent], Objects] = []
    entities: Annotated[list[RdapEntity], Objects] = []


class RdapIpNetwork(RdapObject):
    """An `ip network` object: one delegated range of addresses."""

    start_address: Text = Field(default=None, alias="startAddress")
    end_address: Text = Field(default=None, alias="endAddress")
    ip_version: Text = Field(default=None, alias="ipVersion")
    parent_handle: Text = Field(default=None, alias="parentHandle")


class RdapAutnum(RdapObject):
    """An `autnum` object: one AS number, or a block of them."""

    start_autnum: Integer = Field(default=None, alias="startAutnum")
    end_autnum: Integer = Field(default=None, alias="endAutnum")


class RdapBootstrapService(BaseModel):
    """One line of an IANA bootstrap file: what it covers, and who answers.

    IANA writes it as a two-element array — the resources, then the servers —
    which becomes named fields here, so the matching code reads as what it
    means rather than as `service[0]` and `service[1]`.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    entries: PlainStrings = []
    urls: PlainStrings = []

    @model_validator(mode="before")
    @classmethod
    def _from_pair(cls, value: object) -> object:
        if isinstance(value, list) and len(value) == 2:
            return {"entries": value[0], "urls": value[1]}
        return value


class RdapBootstrap(BaseModel):
    """An IANA bootstrap file: which registry answers for which resources."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    version: Text = None
    publication: Timestamp = None
    services: Annotated[list[RdapBootstrapService], BeforeValidator(_as_bootstrap_services)] = []


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
    "MAX_LIST_ITEMS",
    "MAX_TEXT_LENGTH",
    "UPSTREAM_MODELS",
    "RdapAutnum",
    "RdapBootstrap",
    "RdapBootstrapService",
    "RdapEntity",
    "RdapEvent",
    "RdapIpNetwork",
    "RdapObject",
    "RdapVCard",
    "UpstreamExchange",
    "UpstreamFacility",
    "UpstreamNetwork",
    "UpstreamNetworkFacility",
    "UpstreamNetworkIxLan",
    "UpstreamRecord",
    "field_names",
]
