"""Unit tests for query parsing.

Shaping used to live here and now lives in `test_shaping.py`, alongside the
module it tests.
"""

from __future__ import annotations

import pytest

from peering_mcp.tools.lookup_network import parse_asn


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("AS3320", 3320),
        ("as3320", 3320),
        ("As3320", 3320),
        ("3320", 3320),
        ("  3320  ", 3320),
        (" AS3320 ", 3320),
        ("4294967294", 4_294_967_294),
        ("1", 1),
    ],
)
def test_recognises_asn_forms(query: str, expected: int) -> None:
    assert parse_asn(query) == expected


@pytest.mark.parametrize(
    "query",
    [
        "Hurricane",
        "Deutsche Telekom",
        "AS",
        "",
        "3320 and friends",
        "AS 3320",  # a space after AS is a name, not an ASN
        "0",  # reserved
        "4294967295",  # reserved
        "99999999999",  # beyond 32 bits
        "-1",
        "3.320",
    ],
)
def test_rejects_non_asn_queries(query: str) -> None:
    assert parse_asn(query) is None
