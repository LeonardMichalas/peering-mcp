"""Shared pytest fixtures.

Kept deliberately thin. Fixtures that only two tests need belong next to those
tests, not here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from peering_mcp.clients import cache as cache_module

FIXTURE_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixture_dir() -> Path:
    """Directory holding recorded upstream responses."""
    return FIXTURE_DIR


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may touch the developer's real cache directory.

    Autouse and unconditional, because the default `Config` caches to
    `~/.cache/peering-mcp` and a test that builds one without saying so would
    otherwise write there — and read back what an earlier test wrote, which is
    how a suite starts passing for reasons nobody chose. Caching stays on and
    real; it is just per-test.

    Found the hard way: wiring the cache in turned seventeen tests red at once,
    all of them serving a previous test's response.

    The switch is cleared rather than inherited, so what the suite proves does
    not depend on who is running it. CI had `PEERING_MCP_NO_CACHE` set, which
    meant the cache tests passed on a developer's machine and nowhere else.
    """
    monkeypatch.delenv("PEERING_MCP_NO_CACHE", raising=False)
    monkeypatch.setattr(cache_module, "default_cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setenv("PEERING_MCP_CACHE_DIR", str(tmp_path / "cache"))
