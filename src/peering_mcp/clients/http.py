"""Shared HTTP core: GET-only transport, rate limiting and retries.

Every upstream client is built on this. Three properties are enforced here
rather than left to callers, because a caller can forget:

* Only GET is ever sent. Enforced at the transport, so it holds even if some
  future code path builds a request by hand.
* Requests to a rate-limited upstream pass through a limiter first.
* Transient failures are retried with backoff, and a permanent failure becomes
  a typed exception rather than a raw status code.
* A cached response is served without touching the network.

**The cache sits in front of the limiter, not behind it.** A hit must not spend
a rate-limit token, because the token is the scarce thing: at one request per
second, a cache that still queued would save latency and nothing else.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

import httpx

from peering_mcp.clients.cache import Cache, build_cache, cache_key
from peering_mcp.clients.rate_limit import RateLimiter
from peering_mcp.config import Config
from peering_mcp.errors import (
    UpstreamNotFoundError,
    UpstreamProtocolError,
    UpstreamRateLimitedError,
    UpstreamUnavailableError,
    WriteAttemptError,
)

#: Status codes worth trying again. 429 is upstream throttling; 5xx is upstream
#: having a bad time. Everything else is our problem and retrying will not fix it.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

_MAX_BACKOFF_SECONDS = 8.0
#: Never honour an absurd Retry-After; a minute of silence looks like a hang.
_MAX_RETRY_AFTER_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class Fetched[T]:
    """A value, and whether it came from the cache rather than the network.

    Carried all the way to the envelope's provenance. A caller deciding how
    much to trust an answer is entitled to know it may be up to a TTL old, on
    top of however stale the upstream record already was.
    """

    value: T
    from_cache: bool

    def with_value[U](self, value: U) -> Fetched[U]:
        """The same provenance, wrapped around something derived from it."""
        return Fetched(value=value, from_cache=self.from_cache)


class GetOnlyTransport(httpx.AsyncBaseTransport):
    """Wraps a transport and refuses anything that is not a GET.

    The server is read-only by design. Enforcing it here means no client, and
    no future tool, can issue a write even by accident.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method != "GET":
            msg = (
                f"peering-mcp is read-only; refused a {request.method} to {request.url}. "
                "If a write is genuinely needed, that is a design change, not a bug fix."
            )
            raise WriteAttemptError(msg)
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse a Retry-After header expressed in seconds, if present and sane."""
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        # The HTTP-date form is legal but rare here, and guessing is worse
        # than falling back to our own backoff.
        return None
    if seconds < 0:
        return None
    return min(seconds, _MAX_RETRY_AFTER_SECONDS)


class HttpCore:
    """An HTTP client for one upstream, with that upstream's politeness rules."""

    def __init__(
        self,
        config: Config,
        *,
        base_url: str,
        limiter: RateLimiter | None = None,
        headers: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        cache: Cache | None = None,
    ) -> None:
        self._config = config
        self._limiter = limiter
        self._base_url = base_url
        self._cache = cache or build_cache(
            enabled=config.cache_enabled,
            ttl_seconds=config.cache_ttl_seconds,
            directory=config.cache_dir,
        )
        base_headers = {"User-Agent": config.user_agent, "Accept": "application/json"}
        if headers:
            base_headers.update(headers)
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(config.request_timeout_seconds),
            headers=base_headers,
            transport=GetOnlyTransport(transport or httpx.AsyncHTTPTransport()),
            follow_redirects=True,
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
        await self._client.aclose()
        self._cache.close()

    async def get_json(
        self, path: str, params: dict[str, str | int] | None = None
    ) -> Fetched[dict[str, Any]]:
        """GET a path and return the decoded JSON object.

        A cached response is returned without contacting upstream and without
        spending a rate-limit token. Only successful responses are ever stored:
        a failure is not an answer, and remembering one would turn a passing
        problem into a lasting one.

        Raises:
            UpstreamNotFoundError: upstream said the resource does not exist.
            UpstreamRateLimitedError: throttled, and retries did not clear it.
            UpstreamUnavailableError: upstream failed, timed out or was unreachable.
            UpstreamProtocolError: the body was not a JSON object.
        """
        key = cache_key(self._base_url, path, params)
        cached = await asyncio.to_thread(self._cache.get, key)
        if cached is not None:
            return Fetched(value=cached, from_cache=True)

        response = await self._get_with_retries(path, params)

        if response.status_code == httpx.codes.NOT_FOUND:
            raise UpstreamNotFoundError(f"{path} not found upstream")
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            raise UpstreamRateLimitedError(f"{path} still throttled after retries")
        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise UpstreamUnavailableError(f"{path} returned HTTP {response.status_code}")

        try:
            payload: object = response.json()
        except ValueError as exc:
            raise UpstreamProtocolError(f"{path} returned a non-JSON body") from exc

        if not isinstance(payload, dict):
            raise UpstreamProtocolError(
                f"{path} returned {type(payload).__name__}, expected a JSON object"
            )

        await asyncio.to_thread(self._cache.set, key, payload)
        return Fetched(value=payload, from_cache=False)

    async def _get_with_retries(
        self, path: str, params: dict[str, str | int] | None
    ) -> httpx.Response:
        attempts = self._config.max_retry_attempts
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            # Inside the loop on purpose: a retry is another request, and it
            # must wait its turn like any other.
            if self._limiter is not None:
                await self._limiter.acquire()

            try:
                response = await self._client.get(path, params=params)
            except httpx.TimeoutException as exc:
                last_error = exc
            except httpx.TransportError as exc:
                last_error = exc
            else:
                if response.status_code not in _RETRYABLE_STATUS or attempt == attempts:
                    return response
                await self._backoff(attempt, _retry_after_seconds(response))
                continue

            if attempt == attempts:
                break
            await self._backoff(attempt, None)

        msg = f"{path} unreachable after {attempts} attempts"
        raise UpstreamUnavailableError(msg) from last_error

    async def _backoff(self, attempt: int, retry_after: float | None) -> None:
        if retry_after is not None:
            await asyncio.sleep(retry_after)
            return
        # Exponential with jitter, so concurrent callers do not retry in lockstep.
        base = min(2.0 ** (attempt - 1), _MAX_BACKOFF_SECONDS)
        await asyncio.sleep(base * (0.5 + random.random() / 2))  # noqa: S311
