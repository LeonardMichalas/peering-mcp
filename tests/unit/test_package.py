"""Bootstrap test.

Proves the package imports, the version is exposed, and the quality gate runs
end to end. Replaced by real tests from T3 onward; until then it is what makes
`uv run pytest` a meaningful check rather than a no-op.
"""

from __future__ import annotations

import re
from pathlib import Path

import peering_mcp


def test_the_exposed_version_is_the_one_in_pyproject() -> None:
    """One source of truth, checked rather than trusted.

    `__version__` is read from the installed package metadata, and it leaves
    this machine in the User-Agent sent to PeeringDB and RDAP. It used to be a
    literal, which meant a release could ship identifying itself as the
    version before it.
    """
    pyproject = (Path(__file__).parents[2] / "pyproject.toml").read_text()
    declared = re.search(r'^version = "([^"]+)"', pyproject, re.M)

    assert declared is not None
    assert peering_mcp.__version__ == declared.group(1)
