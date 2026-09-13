"""Tests for the shared HTTP core.

respx intercepts at the transport layer, so these exercise the real httpx
client, the real retry loop and the real GET-only transport without a network.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from peering_mcp.clients.http import HttpCore, _retry_after_seconds
from peering_mcp.clients.rate_limit import RateLimiter
from peering_mcp.config import Config
from peering_mcp.errors import (
    UpstreamNotFoundError,
    UpstreamProtocolError,
    UpstreamRateLimitedError,
    UpstreamUnavailableError,
    WriteAttemptError,
)

BASE = "https://upstream.test"


@pytest.fixture
def config() -> Config:
    # No sleeping in tests: one attempt unless a test asks for more.
    return Config(max_retry_attempts=1, request_timeout_seconds=1.0)


@pytest.fixture
async def core(config: Config):
    async with HttpCore(config, base_url=BASE) as client:
        yield client


# --- The happy path --------------------------------------------------------


@respx.mock
async def test_returns_decoded_json(core: HttpCore) -> None:
    respx.get(f"{BASE}/net").mock(return_value=httpx.Response(200, json={"data": [{"asn": 3320}]}))

    payload = await core.get_json("/net", params={"asn": 3320})

    assert payload == {"data": [{"asn": 3320}]}


@respx.mock
async def test_identifies_itself_to_upstream(core: HttpCore, config: Config) -> None:
    """Upstream operators ask callers to say who they are. We do."""
    route = respx.get(f"{BASE}/net").mock(return_value=httpx.Response(200, json={}))

    await core.get_json("/net")

    sent = route.calls.last.request
    assert sent.headers["User-Agent"] == config.user_agent
    assert "peering-mcp" in sent.headers["User-Agent"]
    assert "github.com" in sent.headers["User-Agent"]


@respx.mock
async def test_extra_headers_are_sent(config: Config) -> None:
    async with HttpCore(
        config, base_url=BASE, headers={"Authorization": "api-key secret"}
    ) as client:
        route = respx.get(f"{BASE}/net").mock(return_value=httpx.Response(200, json={}))
        await client.get_json("/net")

    assert route.calls.last.request.headers["Authorization"] == "api-key secret"


# --- Read-only enforcement -------------------------------------------------


@respx.mock
async def test_post_is_refused_by_the_transport(core: HttpCore) -> None:
    """The acceptance criterion: the transport refuses any verb but GET.

    Enforced below the client API, so it holds even for a hand-built request.
    """
    respx.post(f"{BASE}/net").mock(return_value=httpx.Response(200, json={}))

    with pytest.raises(WriteAttemptError, match="read-only"):
        await core._client.post("/net", json={"asn": 3320})


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@respx.mock
async def test_every_write_verb_is_refused(core: HttpCore, method: str) -> None:
    with pytest.raises(WriteAttemptError):
        await core._client.request(method, "/net")


# --- Error mapping ---------------------------------------------------------


@respx.mock
async def test_404_becomes_not_found(core: HttpCore) -> None:
    respx.get(f"{BASE}/net").mock(return_value=httpx.Response(404, json={"error": "not found"}))

    with pytest.raises(UpstreamNotFoundError):
        await core.get_json("/net")


@respx.mock
async def test_500_becomes_unavailable(core: HttpCore) -> None:
    respx.get(f"{BASE}/net").mock(return_value=httpx.Response(500))

    with pytest.raises(UpstreamUnavailableError):
        await core.get_json("/net")


@respx.mock
async def test_timeout_becomes_unavailable(core: HttpCore) -> None:
    respx.get(f"{BASE}/net").mock(side_effect=httpx.ConnectTimeout("too slow"))

    with pytest.raises(UpstreamUnavailableError, match="unreachable"):
        await core.get_json("/net")


@respx.mock
async def test_non_json_body_becomes_protocol_error(core: HttpCore) -> None:
    respx.get(f"{BASE}/net").mock(return_value=httpx.Response(200, text="<html>nope</html>"))

    with pytest.raises(UpstreamProtocolError, match="non-JSON"):
        await core.get_json("/net")


@respx.mock
async def test_json_array_becomes_protocol_error(core: HttpCore) -> None:
    """A bare list is valid JSON and still not what any caller expects."""
    respx.get(f"{BASE}/net").mock(return_value=httpx.Response(200, json=[1, 2, 3]))

    with pytest.raises(UpstreamProtocolError, match="expected a JSON object"):
        await core.get_json("/net")


# --- Retries ---------------------------------------------------------------


@respx.mock
async def test_429_is_retried_then_surfaced(config: Config) -> None:
    """The acceptance criterion: a 429 is retried, and then reported honestly."""
    retrying = config.model_copy(update={"max_retry_attempts": 3})
    route = respx.get(f"{BASE}/net").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0"})
    )

    async with HttpCore(retrying, base_url=BASE) as client:
        with pytest.raises(UpstreamRateLimitedError, match="after retries"):
            await client.get_json("/net")

    assert route.call_count == 3, "should have used every attempt before giving up"


@respx.mock
async def test_transient_500_then_success(config: Config) -> None:
    retrying = config.model_copy(update={"max_retry_attempts": 3})
    route = respx.get(f"{BASE}/net").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json={"data": []}),
        ]
    )

    async with HttpCore(retrying, base_url=BASE) as client:
        payload = await client.get_json("/net")

    assert payload == {"data": []}
    assert route.call_count == 2


@respx.mock
async def test_404_is_not_retried(config: Config) -> None:
    """Retrying a permanent answer just annoys upstream."""
    retrying = config.model_copy(update={"max_retry_attempts": 3})
    route = respx.get(f"{BASE}/net").mock(return_value=httpx.Response(404))

    async with HttpCore(retrying, base_url=BASE) as client:
        with pytest.raises(UpstreamNotFoundError):
            await client.get_json("/net")

    assert route.call_count == 1


# --- Rate limiting is actually wired in ------------------------------------


@respx.mock
async def test_limiter_is_acquired_once_per_attempt(config: Config) -> None:
    """A retry is another request and must wait its turn like any other."""
    acquisitions = 0

    class CountingLimiter(RateLimiter):
        async def acquire(self) -> None:
            nonlocal acquisitions
            acquisitions += 1

    retrying = config.model_copy(update={"max_retry_attempts": 3})
    respx.get(f"{BASE}/net").mock(return_value=httpx.Response(429, headers={"Retry-After": "0"}))

    limiter = CountingLimiter(1.0)
    async with HttpCore(retrying, base_url=BASE, limiter=limiter) as client:
        with pytest.raises(UpstreamRateLimitedError):
            await client.get_json("/net")

    assert acquisitions == 3


# --- Retry-After parsing ---------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("0", 0.0),
        ("2.5", 2.5),
        ("  3 ", 3.0),
        ("9999", 30.0),  # clamped: a long silence looks like a hang
        ("-5", None),  # nonsense
        ("Wed, 21 Oct 2026 07:28:00 GMT", None),  # legal HTTP-date, we fall back
        ("", None),
    ],
)
def test_retry_after_parsing(header: str, expected: float | None) -> None:
    """A malformed Retry-After must degrade to our own backoff, never crash."""
    response = httpx.Response(429, headers={"Retry-After": header} if header else {})

    assert _retry_after_seconds(response) == expected


@respx.mock
async def test_transport_error_is_retried_then_surfaced(config: Config) -> None:
    """A dropped connection is transient and deserves another go."""
    retrying = config.model_copy(update={"max_retry_attempts": 2})
    route = respx.get(f"{BASE}/net").mock(side_effect=httpx.ConnectError("refused"))

    async with HttpCore(retrying, base_url=BASE) as client:
        with pytest.raises(UpstreamUnavailableError):
            await client.get_json("/net")

    assert route.call_count == 2
