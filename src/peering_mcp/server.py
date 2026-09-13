"""MCP server entry point.

Tools are registered in later tasks. This module exists from the bootstrap so
that the console script, the packaging metadata and the type checker all have a
real target to resolve, rather than being wired up at the same time as the first
piece of behaviour.
"""

from __future__ import annotations


def main() -> None:
    """Run the MCP server over stdio.

    Raises:
        NotImplementedError: until T4 registers the first tool.
    """
    raise NotImplementedError("No tools registered yet. See tasks/plan.md, T4.")
