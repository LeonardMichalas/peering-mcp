"""Runtime configuration, loaded from the environment.

Everything has a working default. The server starts and answers questions with
no environment set at all; a PeeringDB API key only raises the rate limit.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

from pydantic import BaseModel, Field

from peering_mcp import __version__

#: PeeringDB throttles every endpoint to one request per second.
#: https://docs.peeringdb.com/howto/work_with_api/
PEERINGDB_REQUESTS_PER_SECOND: Final = 1.0

_REPO_URL: Final = "https://github.com/LeonardMichalas/peering-mcp"


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, *, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, *, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class Config(BaseModel):
    """Immutable runtime configuration."""

    model_config = {"frozen": True}

    peeringdb_base_url: str = "https://www.peeringdb.com/api"
    peeringdb_api_key: str | None = None
    peeringdb_requests_per_second: float = PEERINGDB_REQUESTS_PER_SECOND

    request_timeout_seconds: float = 10.0
    max_retry_attempts: int = Field(default=3, ge=1)

    cache_enabled: bool = True
    cache_ttl_seconds: int = Field(default=86_400, ge=0)
    cache_dir: Path | None = None

    #: Sent as User-Agent, and as the `sourceapp` query parameter to RIPEstat
    #: in a later version. Upstream operators ask callers to identify
    #: themselves; this is how we do it.
    sourceapp: str = "peering-mcp"

    @property
    def user_agent(self) -> str:
        return f"{self.sourceapp}/{__version__} (+{_REPO_URL})"

    @property
    def has_api_key(self) -> bool:
        return bool(self.peeringdb_api_key)

    @classmethod
    def from_env(cls) -> Config:
        """Build configuration from environment variables."""
        cache_dir = os.environ.get("PEERING_MCP_CACHE_DIR")
        return cls(
            peeringdb_base_url=os.environ.get(
                "PEERING_MCP_PEERINGDB_URL", "https://www.peeringdb.com/api"
            ).rstrip("/"),
            peeringdb_api_key=os.environ.get("PEERINGDB_API_KEY") or None,
            peeringdb_requests_per_second=_env_float(
                "PEERING_MCP_RATE_PER_SECOND", default=PEERINGDB_REQUESTS_PER_SECOND
            ),
            request_timeout_seconds=_env_float("PEERING_MCP_TIMEOUT", default=10.0),
            max_retry_attempts=_env_int("PEERING_MCP_MAX_RETRIES", default=3),
            # PEERING_MCP_NO_CACHE is the documented switch, so it inverts.
            cache_enabled=not _env_bool("PEERING_MCP_NO_CACHE", default=False),
            cache_ttl_seconds=_env_int("PEERING_MCP_CACHE_TTL", default=86_400),
            cache_dir=Path(cache_dir) if cache_dir else None,
        )
