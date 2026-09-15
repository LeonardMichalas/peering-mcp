"""Typed access to the PeeringDB API.

A thin layer over `HttpCore`. It owns the rate limiter for this upstream, so
every caller is throttled whether or not they remember to be, and it is where
raw JSON stops: every method returns validated upstream models, never a dict.

**Never call `get_json` from a tool.** The whole point of this module is that
upstream shape is checked in exactly one place.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from types import TracebackType
from typing import Any, Self

from pydantic import ValidationError

from peering_mcp.clients.http import Fetched, HttpCore
from peering_mcp.clients.rate_limit import RateLimiter
from peering_mcp.config import Config
from peering_mcp.errors import UpstreamProtocolError
from peering_mcp.models.upstream import (
    UpstreamExchange,
    UpstreamNetwork,
    UpstreamNetworkFacility,
    UpstreamNetworkIxLan,
    UpstreamRecord,
)

#: Logging goes to stderr. The MCP transport owns stdout, and a stray line
#: there corrupts the protocol.
logger = logging.getLogger(__name__)

#: Cap on how many name matches are worth returning. A model asked to choose
#: between thirty candidates will choose badly; five is a decision, thirty is a
#: list to scroll past.
NAME_MATCH_LIMIT = 5


class PeeringDBClient:
    """Reads networks, exchanges and presence records from PeeringDB."""

    def __init__(self, config: Config, *, core: HttpCore | None = None) -> None:
        self._config = config
        headers = {}
        if config.peeringdb_api_key:
            # Basic auth was removed on 2025-07-01; the key goes in a header.
            headers["Authorization"] = f"api-key {config.peeringdb_api_key}"
        self._core = core or HttpCore(
            config,
            base_url=config.peeringdb_base_url,
            limiter=RateLimiter(config.peeringdb_requests_per_second),
            headers=headers,
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

    async def network_by_asn(self, asn: int) -> Fetched[UpstreamNetwork | None]:
        """Return the network record for an AS number, or None if unlisted.

        PeeringDB answers an unknown ASN with 404, which `HttpCore` turns into
        `UpstreamNotFoundError`; that is handled by the caller. An empty `data`
        array is the other way it says no, so both are collapsed to None here.
        """
        fetched = await self._core.get_json("/net", params={"asn": asn})
        records = parse_records(fetched.value, UpstreamNetwork, source="/net")
        return fetched.with_value(records[0] if records else None)

    async def networks_by_name(self, fragment: str) -> Fetched[list[UpstreamNetwork]]:
        """Return networks whose name contains `fragment`."""
        fetched = await self._core.get_json("/net", params={"name__contains": fragment})
        return fetched.with_value(parse_records(fetched.value, UpstreamNetwork, source="/net"))

    async def exchange_presence(self, asn: int) -> Fetched[list[UpstreamNetworkIxLan]]:
        """Return every exchange port a network records, one row per port.

        An unlisted ASN is an empty list here, not a 404: `/netixlan` answers
        200 for any filter value. Whether the network exists at all is a
        question for `network_by_asn`.
        """
        fetched = await self._core.get_json("/netixlan", params={"asn": asn})
        rows = parse_records(fetched.value, UpstreamNetworkIxLan, source="/netixlan")
        _require_filter_applied("/netixlan", "asn", {row.asn for row in rows}, {asn})
        return fetched.with_value(rows)

    async def facility_presence(self, net_id: int) -> Fetched[list[UpstreamNetworkFacility]]:
        """Return every facility a network records a presence in.

        Keyed by PeeringDB's own network id, not by ASN. `/netfac` has no
        working ASN filter: `asn` and `local_asn` are both silently ignored and
        return the whole table, measured at 61,855 rows and 14 MB. The id is on
        the `/net` record, which any caller has already fetched.
        """
        fetched = await self._core.get_json("/netfac", params={"net_id": net_id})
        rows = parse_records(fetched.value, UpstreamNetworkFacility, source="/netfac")
        _require_filter_applied("/netfac", "net_id", {row.net_id for row in rows}, {net_id})
        return fetched.with_value(rows)

    async def exchanges_by_id(self, ids: Iterable[int]) -> Fetched[list[UpstreamExchange]]:
        """Return the exchange records for a set of ids, in one request.

        Sorted and de-duplicated before building the query, so the same set
        always produces the same cache key.
        """
        wanted = sorted(set(ids))
        if not wanted:
            return Fetched(value=[], from_cache=True)
        joined = ",".join(str(i) for i in wanted)
        fetched = await self._core.get_json("/ix", params={"id__in": joined})
        rows = parse_records(fetched.value, UpstreamExchange, source="/ix")
        _require_filter_applied("/ix", "id__in", {row.record_id for row in rows}, set(wanted))
        return fetched.with_value(rows)


def _require_filter_applied(
    source: str, name: str, seen: set[int | None], wanted: set[int]
) -> None:
    """Raise if upstream answered with records the filter should have excluded.

    PeeringDB does not reject a filter it does not know. It ignores it and
    returns the entire table, which arrives as a plausible-looking list of the
    wrong records rather than as an error. Every query this client builds uses
    a filter verified by hand, and this is the check that the verification is
    still true: a value outside the requested set means the filter did not
    apply, and the honest report is a broken upstream, never a wrong answer.
    """
    stray = {value for value in seen if value is not None} - wanted
    if stray:
        msg = (
            f"{source} ignored the {name} filter: "
            f"{len(stray)} record(s) belong to values that were not requested"
        )
        logger.warning("%s", msg)
        raise UpstreamProtocolError(msg)


def parse_records[T: UpstreamRecord](
    payload: dict[str, Any], model: type[T], *, source: str
) -> list[T]:
    """Validate a PeeringDB list envelope into models.

    Two failures are treated differently on purpose.

    A payload with no `data` list is a **protocol** failure: PeeringDB did not
    answer in its own format, and the honest report is `upstream_unavailable`.
    Returning an empty list instead would present a broken upstream as "no such
    network", which is the exact confusion the status taxonomy exists to stop.

    A single unusable **row** among several is skipped and logged. The models
    degrade almost everything to `None`, so a row only fails when its identity
    is unusable, and denying an answer about fifty good rows over one broken
    one helps nobody. If every row fails, there is no answer left to give and
    it becomes a protocol failure again.
    """
    data = payload.get("data")
    if not isinstance(data, list):
        found = type(data).__name__ if data is not None else "nothing"
        msg = f"{source} returned {found} where a list of records was expected"
        logger.warning("%s", msg)
        raise UpstreamProtocolError(msg)

    records: list[T] = []
    skipped = 0
    for row in data:
        try:
            records.append(model.model_validate(row))
        except ValidationError as exc:
            skipped += 1
            logger.warning(
                "%s: skipped an unusable %s record: %s",
                source,
                model.__name__,
                _first_reason(exc),
            )

    if skipped and not records:
        msg = f"{source} returned {skipped} record(s), none of them usable"
        raise UpstreamProtocolError(msg)
    return records


def _first_reason(exc: ValidationError) -> str:
    """One line describing why a record failed, for the log.

    The full pydantic report is several lines per field and includes the
    offending value, which is upstream-controlled text. One field name and one
    message is enough to debug with and carries nothing worth injecting.
    """
    errors = exc.errors()
    if not errors:
        return "validation failed"
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ())) or "record"
    return f"{location}: {first.get('msg', 'invalid')}"
