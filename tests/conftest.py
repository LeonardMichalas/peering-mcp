"""Shared pytest fixtures.

Kept deliberately thin. Fixtures that only two tests need belong next to those
tests, not here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixture_dir() -> Path:
    """Directory holding recorded upstream responses."""
    return FIXTURE_DIR
