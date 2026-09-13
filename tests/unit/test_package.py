"""Bootstrap test.

Proves the package imports, the version is exposed, and the quality gate runs
end to end. Replaced by real tests from T3 onward; until then it is what makes
`uv run pytest` a meaningful check rather than a no-op.
"""

from __future__ import annotations

import peering_mcp


def test_version_is_exposed() -> None:
    assert peering_mcp.__version__ == "0.1.0"
