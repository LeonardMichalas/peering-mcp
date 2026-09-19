"""Runtime configuration, loaded from the environment.

Everything has a working default. The server starts and answers questions with
no environment set at all; a PeeringDB API key only raises the rate limit.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from pydantic import BaseModel, Field

from peering_mcp import __version__

#: PeeringDB throttles every endpoint to one request per second.
#: https://docs.peeringdb.com/howto/work_with_api/
PEERINGDB_REQUESTS_PER_SECOND: Final = 1.0

_REPO_URL: Final = "https://github.com/LeonardMichalas/peering-mcp"

#: What a tool's answer is allowed to cost, in bytes of compact JSON.
#:
#: **Per list, not per response.** `list_presence` with `kind="both"` returns
#: two lists and each is held to this line on its own, because the alternative
#: is a budget that contradicts itself: exchanges alone measured 6,100 bytes at
#: T8 and facilities 3,821, both of which are answers somebody asked for, and
#: their sum is not. The tools with no list in them — `lookup_network` and
#: `lookup_registration` — are held to the whole response.
#:
#: Kept here rather than in each test, so the line exists once and every test
#: that claims to enforce it enforces the same number.
RESPONSE_BUDGETS: Final[Mapping[str, int]] = {
    "lookup_network": 2 * 1024,
    "lookup_registration": 2 * 1024,
    "list_presence": 6 * 1024,
    "find_at_exchange": 6 * 1024,
    "find_common_presence": 4 * 1024,
}

#: What is set aside, inside a list's budget, for everything that is not the
#: list: the status, the note, the provenance, and the object the list belongs
#: to. The largest of those measured 1,062 bytes with every field at its cap —
#: an exchange with a 200-character name and city, the longest note any tool
#: writes, and a full provenance. `test_budgets.py` fails if it ever outgrows
#: this reserve, which is what the 90 bytes of margin are for.
LIST_ENVELOPE_RESERVE: Final = 1152


def list_budget(tool: str) -> int:
    """How many bytes one list from `tool` may spend on its items."""
    return RESPONSE_BUDGETS[tool] - LIST_ENVELOPE_RESERVE


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
