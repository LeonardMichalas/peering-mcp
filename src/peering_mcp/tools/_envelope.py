"""The part of the exception-to-status mapping that is the same everywhere.

Two of the three upstream failures a tool has to handle mean the same thing
whichever tool hit them: PeeringDB throttled us, or PeeringDB could not be
reached. By T9 that pair was spelled out byte-identically in three tools and
the fourth was about to inherit the copy.

**`not_found` is deliberately not here.** Each tool's note names its own
subject — a query string, one AS number, a list of them, an exchange — and
that specificity is the whole point: "AS65999 is not listed in PeeringDB" is
an answer somebody can act on and "not found" is not. A helper that swallowed
it would turn four good notes into one vague one, which is the failure the
status taxonomy exists to prevent. So the caller catches
`UpstreamNotFoundError` first and keeps writing its own note.

Explicit rather than a decorator, so the control flow stays readable at the
call site and mypy infers the payload type from the caller's return type.
"""

from __future__ import annotations

from peering_mcp.errors import UpstreamError, UpstreamRateLimitedError
from peering_mcp.models.domain import Status, ToolResult


def upstream_failure[T](exc: UpstreamError) -> ToolResult[T]:
    """Map a generic upstream failure onto the envelope.

    Call it from a tool's last `except UpstreamError` branch, after that
    tool's own `not_found` branch has had its chance.
    """
    if isinstance(exc, UpstreamRateLimitedError):
        return ToolResult(
            status=Status.RATE_LIMITED,
            note="PeeringDB is throttling requests. Wait a moment and try again.",
        )
    return ToolResult(
        status=Status.UPSTREAM_UNAVAILABLE,
        note=f"Could not reach PeeringDB: {exc}",
    )


__all__ = ["upstream_failure"]
