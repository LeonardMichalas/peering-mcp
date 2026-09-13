"""Configuration must work with an empty environment.

The server has to start and answer questions with nothing set. An API key only
raises the rate limit; it is never a prerequisite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from peering_mcp.config import Config

ENV_VARS = [
    "PEERINGDB_API_KEY",
    "PEERING_MCP_PEERINGDB_URL",
    "PEERING_MCP_RATE_PER_SECOND",
    "PEERING_MCP_TIMEOUT",
    "PEERING_MCP_MAX_RETRIES",
    "PEERING_MCP_NO_CACHE",
    "PEERING_MCP_CACHE_TTL",
    "PEERING_MCP_CACHE_DIR",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_works_with_an_empty_environment() -> None:
    config = Config.from_env()

    assert config.peeringdb_api_key is None
    assert config.has_api_key is False
    assert config.peeringdb_requests_per_second == 1.0
    assert config.cache_enabled is True
    assert config.cache_ttl_seconds == 86_400


def test_api_key_is_picked_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PEERINGDB_API_KEY", "secret")

    config = Config.from_env()

    assert config.peeringdb_api_key == "secret"
    assert config.has_api_key is True


def test_empty_api_key_is_treated_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """An exported-but-empty variable is a very common way to configure nothing."""
    monkeypatch.setenv("PEERINGDB_API_KEY", "")

    assert Config.from_env().has_api_key is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", False), ("true", False), ("YES", False), ("0", True), ("", True), ("nonsense", True)],
)
def test_no_cache_switch_inverts(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    monkeypatch.setenv("PEERING_MCP_NO_CACHE", value)

    assert Config.from_env().cache_enabled is expected


def test_garbage_numbers_fall_back_to_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo in an env var should not stop the server from starting."""
    monkeypatch.setenv("PEERING_MCP_CACHE_TTL", "not-a-number")
    monkeypatch.setenv("PEERING_MCP_TIMEOUT", "")

    config = Config.from_env()

    assert config.cache_ttl_seconds == 86_400
    assert config.request_timeout_seconds == 10.0


def test_cache_dir_is_a_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PEERING_MCP_CACHE_DIR", "/tmp/peering")

    assert Config.from_env().cache_dir == Path("/tmp/peering")


def test_trailing_slash_is_stripped_from_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PEERING_MCP_PEERINGDB_URL", "https://example.test/api/")

    assert Config.from_env().peeringdb_base_url == "https://example.test/api"


def test_user_agent_identifies_the_project() -> None:
    agent = Config().user_agent

    assert agent.startswith("peering-mcp/")
    assert "github.com/LeonardMichalas/peering-mcp" in agent


def test_config_is_immutable() -> None:
    config = Config()

    with pytest.raises(ValueError, match="frozen"):
        config.cache_ttl_seconds = 5  # type: ignore[misc]


def test_garbage_float_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PEERING_MCP_RATE_PER_SECOND", "fast please")

    assert Config.from_env().peeringdb_requests_per_second == 1.0
