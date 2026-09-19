"""peering-mcp: public internet interconnection data for AI agents."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("peering-mcp")
except PackageNotFoundError:  # pragma: no cover - only when run from a bare source tree
    # Not installed, so there is no metadata to read. Say so rather than
    # guessing a number: this string goes out as the User-Agent, and an
    # invented version misidentifies the client to two public APIs.
    __version__ = "0+unknown"
