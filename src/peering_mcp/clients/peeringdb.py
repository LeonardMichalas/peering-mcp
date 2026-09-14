"""Typed access to the PeeringDB API.

A thin layer over `HttpCore`. It owns the rate limiter for this upstream, so
every caller is throttled whether or not they remember to be, and it is where
raw JSON stops: every method returns validated upstream models, never a dict.

**Never call `get_json` from a tool.** The whole point of this module is that
upstream shape is checked in exactly one place.
"""

from __future__ import annotations

import logging
from types import TracebackType
from typing import Any, Self

from pydantic import ValidationError

from peering_mcp.clients.http import HttpCore
from peering_mcp.clients.rate_limit import RateLimiter
from peering_mcp.config import Config
from peering_mcp.errors import UpstreamProtocolError
from peering_mcp.models.upstream import UpstreamNetwork, UpstreamRecord

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

    async def network_by_asn(self, asn: int) -> UpstreamNetwork | None:
        """Return the network record for an AS number, or None if unlisted.

        PeeringDB answers an unknown ASN with 404, which `HttpCore` turns into
        `UpstreamNotFoundError`; that is handled by the caller. An empty `data`
        array is the other way it says no, so both are collapsed to None here.
        """
        payload = await self._core.get_json("/net", params={"asn": asn})
        records = parse_records(payload, UpstreamNetwork, source="/net")
        return records[0] if records else None

    async def networks_by_name(self, fragment: str) -> list[UpstreamNetwork]:
        """Return networks whose name contains `fragment`."""
        payload = await self._core.get_json("/net", params={"name__contains": fragment})
        return parse_records(payload, UpstreamNetwork, source="/net")


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
