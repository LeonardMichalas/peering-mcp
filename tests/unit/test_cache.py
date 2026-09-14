"""Tests for the disk cache.

The cache is load-bearing rather than an optimisation: at one request per
second, a miss that should have been a hit is a second of a person's time. So
the tests that matter are the ones counting upstream requests, not the ones
checking a value round-trips.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from peering_mcp.clients.cache import (
    DiskCache,
    NullCache,
    build_cache,
    cache_key,
    default_cache_dir,
)
from peering_mcp.clients.http import HttpCore
from peering_mcp.config import Config
from peering_mcp.errors import UpstreamError, UpstreamProtocolError

BASE = "https://upstream.test"


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        max_retry_attempts=1,
        request_timeout_seconds=1.0,
        cache_dir=tmp_path / "cache",
    )


# --- Keys ------------------------------------------------------------------


def test_the_same_request_makes_the_same_key() -> None:
    first = cache_key(BASE, "/net", {"asn": 3320})
    second = cache_key(BASE, "/net", {"asn": 3320})

    assert first == second


def test_parameter_order_does_not_change_the_key() -> None:
    """Two callers asking the same question should share one entry."""
    first = cache_key(BASE, "/netixlan", {"asn": 3320, "limit": 50})
    second = cache_key(BASE, "/netixlan", {"limit": 50, "asn": 3320})

    assert first == second


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/net", {"asn": 6939}),
        ("/net", {"name__contains": "Hurricane"}),
        ("/netixlan", {"asn": 3320}),
        ("/net", None),
    ],
)
def test_a_different_request_makes_a_different_key(
    path: str, params: dict[str, Any] | None
) -> None:
    assert cache_key(BASE, path, params) != cache_key(BASE, "/net", {"asn": 3320})


def test_a_different_upstream_makes_a_different_key() -> None:
    """A test server and the real one must never share entries."""
    assert cache_key(BASE, "/net", {"asn": 1}) != cache_key(
        "https://www.peeringdb.com/api", "/net", {"asn": 1}
    )


def test_a_trailing_slash_on_the_base_url_is_not_a_different_upstream() -> None:
    assert cache_key(BASE + "/", "/net", None) == cache_key(BASE, "/net", None)


# --- What is stored, and for how long --------------------------------------


def test_a_value_survives_a_new_cache_object(tmp_path: Path) -> None:
    """The reason it is on disk: an agent process does not live long."""
    with DiskCache(tmp_path, ttl_seconds=60) as first:
        first.set("k", {"data": [1]})

    with DiskCache(tmp_path, ttl_seconds=60) as second:
        assert second.get("k") == {"data": [1]}


def test_an_expired_value_is_not_returned(tmp_path: Path) -> None:
    with DiskCache(tmp_path, ttl_seconds=1) as cache:
        cache.set("k", {"data": [1]})
        assert cache.get("k") == {"data": [1]}

        time.sleep(1.1)

        assert cache.get("k") is None


def test_a_missing_key_is_none(tmp_path: Path) -> None:
    with DiskCache(tmp_path, ttl_seconds=60) as cache:
        assert cache.get("never stored") is None


# --- Switching it off ------------------------------------------------------


def test_caching_can_be_switched_off(tmp_path: Path) -> None:
    cache = build_cache(enabled=False, ttl_seconds=86_400, directory=tmp_path)

    assert isinstance(cache, NullCache)


def test_a_zero_ttl_means_the_same_as_off(tmp_path: Path) -> None:
    """An entry that expires immediately is a write nobody reads."""
    cache = build_cache(enabled=True, ttl_seconds=0, directory=tmp_path)

    assert isinstance(cache, NullCache)


def test_the_environment_switch_reaches_the_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PEERING_MCP_NO_CACHE", "1")

    assert Config.from_env().cache_enabled is False


def test_a_null_cache_remembers_nothing() -> None:
    cache = NullCache()
    cache.set("k", {"data": [1]})

    assert cache.get("k") is None


def test_an_unusable_cache_directory_does_not_stop_the_server(tmp_path: Path) -> None:
    """A read-only lookup server must answer even when its cache cannot open.

    Same judgement as a malformed environment variable falling back to its
    default, rather than refusing to boot.
    """
    blocked = tmp_path / "a-file"
    blocked.write_text("not a directory")

    cache = build_cache(enabled=True, ttl_seconds=60, directory=blocked / "cache")

    assert isinstance(cache, NullCache)


def test_the_default_directory_follows_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", "/tmp/xdg-example")

    assert default_cache_dir() == Path("/tmp/xdg-example/peering-mcp")


# --- The criterion that matters: how many requests reach upstream ----------


@respx.mock
async def test_a_miss_then_a_hit_is_exactly_one_upstream_request(config: Config) -> None:
    """The acceptance criterion, and the whole point of the task."""
    route = respx.get(f"{BASE}/net").mock(
        return_value=httpx.Response(200, json={"data": [{"asn": 3320}]})
    )

    async with HttpCore(config, base_url=BASE) as core:
        first = await core.get_json("/net", params={"asn": 3320})
        second = await core.get_json("/net", params={"asn": 3320})

    assert route.call_count == 1
    assert first.value == second.value
    assert first.from_cache is False
    assert second.from_cache is True


@respx.mock
async def test_a_hit_does_not_spend_a_rate_limit_token(config: Config) -> None:
    """Why the cache sits in front of the limiter rather than behind it.

    At one request per second the token is the scarce thing. A cache that
    still queued would save latency and nothing else.
    """
    respx.get(f"{BASE}/net").mock(return_value=httpx.Response(200, json={"data": []}))
    acquisitions = 0

    class CountingLimiter:
        async def acquire(self) -> None:
            nonlocal acquisitions
            acquisitions += 1

    async with HttpCore(config, base_url=BASE, limiter=CountingLimiter()) as core:  # type: ignore[arg-type]
        await core.get_json("/net")
        await core.get_json("/net")
        await core.get_json("/net")

    assert acquisitions == 1, "only the miss should have queued"


@respx.mock
async def test_a_different_question_is_not_answered_from_the_first(config: Config) -> None:
    route = respx.get(f"{BASE}/net").mock(return_value=httpx.Response(200, json={"data": []}))

    async with HttpCore(config, base_url=BASE) as core:
        await core.get_json("/net", params={"asn": 3320})
        await core.get_json("/net", params={"asn": 6939})

    assert route.call_count == 2


@respx.mock
async def test_a_cached_answer_survives_a_new_client(config: Config) -> None:
    """The case an in-memory cache would miss: the next agent process."""
    route = respx.get(f"{BASE}/net").mock(return_value=httpx.Response(200, json={"data": []}))

    async with HttpCore(config, base_url=BASE) as first:
        await first.get_json("/net", params={"asn": 3320})
    async with HttpCore(config, base_url=BASE) as second:
        fetched = await second.get_json("/net", params={"asn": 3320})

    assert route.call_count == 1
    assert fetched.from_cache is True


# --- Failures are not remembered -------------------------------------------


@respx.mock
@pytest.mark.parametrize("status", [404, 429, 500, 503])
async def test_a_failure_is_never_cached(config: Config, status: int) -> None:
    """A 404 is the answer most likely to become wrong, and 500 is not an answer.

    Remembering either would turn a passing problem into a lasting one: a
    network that registers today would look absent until the TTL ran out.
    """
    route = respx.get(f"{BASE}/net").mock(return_value=httpx.Response(status))

    async with HttpCore(config, base_url=BASE) as core:
        for _ in range(2):
            with pytest.raises(UpstreamError):
                await core.get_json("/net", params={"asn": 3320})

    assert route.call_count == 2, "the failure should have been asked again"


@respx.mock
async def test_an_unreadable_body_is_not_cached(config: Config) -> None:
    route = respx.get(f"{BASE}/net").mock(return_value=httpx.Response(200, text="not json"))

    async with HttpCore(config, base_url=BASE) as core:
        for _ in range(2):
            with pytest.raises(UpstreamProtocolError):
                await core.get_json("/net")

    assert route.call_count == 2


@respx.mock
async def test_switching_the_cache_off_means_every_call_goes_upstream(tmp_path: Path) -> None:
    disabled = Config(max_retry_attempts=1, cache_enabled=False, cache_dir=tmp_path)
    route = respx.get(f"{BASE}/net").mock(return_value=httpx.Response(200, json={"data": []}))

    async with HttpCore(disabled, base_url=BASE) as core:
        await core.get_json("/net")
        fetched = await core.get_json("/net")

    assert route.call_count == 2
    assert fetched.from_cache is False


# --- Warm latency ----------------------------------------------------------


@respx.mock
async def test_a_warm_call_is_well_inside_the_budget(config: Config) -> None:
    """The non-functional requirement: any cached call under 50 ms."""
    respx.get(f"{BASE}/net").mock(return_value=httpx.Response(200, json={"data": [{"asn": 3320}]}))

    async with HttpCore(config, base_url=BASE) as core:
        await core.get_json("/net", params={"asn": 3320})

        started = time.perf_counter()
        fetched = await core.get_json("/net", params={"asn": 3320})
        elapsed_ms = (time.perf_counter() - started) * 1000

    assert fetched.from_cache is True
    assert elapsed_ms < 50, f"warm call took {elapsed_ms:.1f} ms against a 50 ms budget"
