"""Does a model pick the right tool from its description?

Run by hand, never in CI: it calls a real model, costs real money, and its
result is a number to read rather than a gate to pass.

**What it measures, and what it does not.** Each question goes to Claude once,
with the five real tool schemas this server advertises and the server's own
instructions as the system prompt, and nothing is executed — the answer being
graded is which tool the model reached for, not what came back. So a failure
here is a *description* that misleads, and the fix is prose in `server.py`.

The tool schemas come from the server itself rather than from a copy, so the
descriptions under test are the ones a client would actually receive.

    export ANTHROPIC_API_KEY=...
    uv run --group eval python tests/eval/run_eval.py

    uv run --group eval python tests/eval/run_eval.py --effort high
    uv run --group eval python tests/eval/run_eval.py --only lookup_registration
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anthropic
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from peering_mcp.server import INSTRUCTIONS, mcp

QUESTIONS = Path(__file__).parent / "questions.yaml"

#: The model a person asking these questions would most likely be talking to.
DEFAULT_MODEL = "claude-opus-5"

#: Deliberately low. A harder-thinking model can reason its way past a vague
#: description, which is exactly the fault this harness exists to find: the
#: descriptions have to carry the decision on their own.
DEFAULT_EFFORT = "low"

#: Enough for a short reasoning pass and one tool call.
MAX_TOKENS = 4096

#: Claude Opus 5, US dollars per million tokens, for the cost line at the end.
INPUT_COST = 5.00
OUTPUT_COST = 25.00


@dataclass(frozen=True, slots=True)
class Question:
    ask: str
    expect: str
    accept: tuple[str, ...]
    why: str

    @property
    def allowed(self) -> tuple[str, ...]:
        return (self.expect, *self.accept)


@dataclass(frozen=True, slots=True)
class Outcome:
    question: Question
    called: str | None
    arguments: dict[str, Any]
    input_tokens: int
    output_tokens: int

    @property
    def verdict(self) -> str:
        if self.called is None:
            return "NO CALL"
        if self.called == self.question.expect:
            return "ok"
        if self.called in self.question.accept:
            return "accepted"
        return "WRONG"

    @property
    def passed(self) -> bool:
        return self.verdict in ("ok", "accepted")


def load_questions(path: Path) -> list[Question]:
    payload = yaml.safe_load(path.read_text())
    return [
        Question(
            ask=item["ask"],
            expect=item["expect"],
            accept=tuple(item.get("accept", ())),
            why=item.get("why", ""),
        )
        for item in payload["questions"]
    ]


async def tool_schemas() -> list[dict[str, Any]]:
    """The tools exactly as a client receives them, descriptions and all."""
    return [
        {
            "name": tool.name,
            "description": tool.description or "",
            "input_schema": tool.input_schema,
        }
        for tool in await mcp.list_tools()
    ]


def ask_once(
    client: anthropic.Anthropic,
    question: Question,
    tools: list[dict[str, Any]],
    *,
    model: str,
    effort: str,
) -> Outcome:
    """One question, one call, no execution.

    `tool_choice` is left on auto on purpose. Forcing a call would measure
    only the choice between tools and would hide the failure this project
    exists to prevent: a model answering an interconnection question from
    memory because nothing told it there was something to look up.
    """
    response = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=INSTRUCTIONS,
        tools=tools,
        output_config={"effort": effort},
        messages=[{"role": "user", "content": question.ask}],
    )

    for block in response.content:
        if block.type == "tool_use":
            arguments = block.input if isinstance(block.input, dict) else {}
            return Outcome(
                question=question,
                called=block.name,
                arguments=arguments,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )

    return Outcome(
        question=question,
        called=None,
        arguments={},
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )


def report(outcomes: list[Outcome], *, model: str, effort: str) -> int:
    """Print what happened, and return the number of questions that failed."""
    width = max(len(outcome.question.ask) for outcome in outcomes)
    print(f"\n{model}, effort {effort}, {len(outcomes)} questions\n")

    for outcome in outcomes:
        mark = {"ok": "  ", "accepted": "~ ", "WRONG": "! ", "NO CALL": "! "}[outcome.verdict]
        called = outcome.called or "nothing"
        print(f"{mark}{outcome.question.ask:<{width}}  {called}")
        if not outcome.passed:
            print(f"{'':>{width + 4}}expected {outcome.question.expect} — {outcome.question.why}")

    failures = [outcome for outcome in outcomes if not outcome.passed]
    accepted = [outcome for outcome in outcomes if outcome.verdict == "accepted"]
    inputs = sum(outcome.input_tokens for outcome in outcomes)
    outputs = sum(outcome.output_tokens for outcome in outcomes)
    cost = inputs / 1_000_000 * INPUT_COST + outputs / 1_000_000 * OUTPUT_COST

    print(
        f"\n{len(outcomes) - len(failures)} of {len(outcomes)} selected an allowed tool"
        f" ({len(accepted)} on a second reading), {len(failures)} did not."
    )
    print(f"{inputs:,} input and {outputs:,} output tokens, about ${cost:.2f}.")
    if failures:
        print("\nEach failure is a description to rewrite, not a model to argue with.")
    return len(failures)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--effort",
        default=DEFAULT_EFFORT,
        choices=("low", "medium", "high", "xhigh", "max"),
        help="How hard the model may think before choosing. Low is the harsher test.",
    )
    parser.add_argument(
        "--only",
        default=None,
        help="Run only the questions expecting this tool.",
    )
    args = parser.parse_args()

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("Set ANTHROPIC_API_KEY first. This harness calls a real model.", file=sys.stderr)
        return 2

    questions = load_questions(QUESTIONS)
    if args.only:
        questions = [question for question in questions if question.expect == args.only]
        if not questions:
            print(f"No questions expect {args.only!r}.", file=sys.stderr)
            return 2

    tools = asyncio.run(tool_schemas())
    client = anthropic.Anthropic()

    outcomes = [
        ask_once(client, question, tools, model=args.model, effort=args.effort)
        for question in questions
    ]
    return 1 if report(outcomes, model=args.model, effort=args.effort) else 0


if __name__ == "__main__":
    raise SystemExit(main())
