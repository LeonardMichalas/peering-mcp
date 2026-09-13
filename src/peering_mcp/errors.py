"""Exceptions raised by the client layer.

These describe what went wrong upstream. Translating them into the status
values a tool returns to the model happens in the tool layer, so that the
clients stay unaware of MCP.
"""

from __future__ import annotations


class PeeringMCPError(Exception):
    """Base class for every error this package raises deliberately."""


class WriteAttemptError(PeeringMCPError):
    """A non-GET request was attempted.

    This server is read-only by design. Reaching this is a programming error,
    not a runtime condition, so it is never caught and turned into a status.
    """


class UpstreamError(PeeringMCPError):
    """An upstream API did not give us a usable answer."""


class UpstreamNotFoundError(UpstreamError):
    """Upstream reported the resource does not exist (HTTP 404)."""


class UpstreamRateLimitedError(UpstreamError):
    """Upstream throttled us and retries did not clear it (HTTP 429)."""


class UpstreamUnavailableError(UpstreamError):
    """Upstream failed, timed out, or was unreachable."""


class UpstreamProtocolError(UpstreamError):
    """Upstream answered, but not with the JSON we can parse."""
