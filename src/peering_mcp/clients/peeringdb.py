"""Typed access to the PeeringDB API.

A thin layer over `HttpCore`. It owns the rate limiter for this upstream, so
every caller is throttled whether or not they remember to be, and it knows the
two ways a network can be identified: by AS number or by name.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any, Self

from peering_mcp.clients.http import HttpCore
from peering_mcp.clients.rate_limit import RateLimiter
from peering_mcp.config import Config

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

    async def network_by_asn(self, asn: int) -> dict[str, Any] | None:
        """Return the network record for an AS number, or None if unlisted.

        PeeringDB answers an unknown ASN with 404, which `HttpCore` turns into
        `UpstreamNotFoundError`; that is handled by the caller. An empty `data`
        array is the other way it says no, so both are collapsed to None here.
        """
        payload = await self._core.get_json("/net", params={"asn": asn})
        return _first_record(payload)

    async def networks_by_name(self, fragment: str) -> list[dict[str, Any]]:
        """Return networks whose name contains `fragment`, newest match first."""
        payload = await self._core.get_json("/net", params={"name__contains": fragment})
        return _records(payload)


def _records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


def _first_record(payload: dict[str, Any]) -> dict[str, Any] | None:
    rows = _records(payload)
    return rows[0] if rows else None
