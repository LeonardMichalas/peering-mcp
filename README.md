# peering-mcp

**An MCP server that lets an AI agent look up how the internet is actually wired together** — which networks connect to each other, at which internet exchanges and facilities, under what peering policy, and who a given address range is registered to.

> **Status: early development.** `lookup_network` works against live data. The other four tools are next. Nothing is published to PyPI yet.

> A personal side project, written in my own free time.

## Why this exists

The internet is roughly eighty thousand independent networks that agree to carry each other's traffic. Which networks connect to which, where they meet, and on what terms is public, free and well structured — published through stable APIs by [PeeringDB](https://www.peeringdb.com) and the regional internet registries.

None of it is reachable by an AI agent. Ask a coding assistant which internet exchanges a given carrier is present at and it will answer from memory: fluent, confident, and often wrong. It has no way to check, so it does not check.

This server is that way to check.

## What it does

| Tool | Question it answers | |
| --- | --- | --- |
| `lookup_network` | Who is this network, and what is their peering policy? | ✅ |
| `list_presence` | Which internet exchanges and facilities are they present at? | planned |
| `find_at_exchange` | Who else is at this exchange, and would they peer? | planned |
| `find_common_presence` | **Where can these networks meet each other?** | planned |
| `lookup_registration` | Who is this IP range or AS number registered to? | planned |

`find_common_presence` is the tool that motivated the project. Working out where two or more networks could interconnect means looking each one up, listing everywhere it is present, and intersecting the results by hand. That is about an hour and a dozen browser tabs. It should be one question.

It also returns how many locations each network has on its own, so an empty answer is explainable: either the networks genuinely do not overlap, or one of them has no records at all, which is a very different thing.

### What a result looks like

Asking `lookup_network` for `AS3320` returns 909 bytes, not the 42-field upstream record:

```json
{
  "status": "ok",
  "data": {
    "network": {
      "asn": 3320,
      "name": "Deutsche Telekom",
      "network_type": "NSP",
      "scope": "Global",
      "exchange_count": 7,
      "facility_count": 53,
      "policy": {
        "general": "Restrictive",
        "contract_required": "Required",
        "ratio_required": true
      }
    }
  },
  "note": "PeeringDB records are maintained by the networks themselves. Treat a missing field as unrecorded, not as evidence it is untrue.",
  "provenance": {
    "source": "peeringdb",
    "record_updated": "2026-08-31T13:30:19Z"
  }
}
```

An ambiguous name returns candidates rather than a guess, and an unlisted AS number returns `not_found` with a note saying a network can route traffic without being registered.

## How it works

```mermaid
graph LR
    subgraph local["Your machine"]
        A["AI agent<br/>Claude Code, Codex,<br/>Cursor, …"]
        S["peering-mcp"]
        C[("Disk cache")]
    end
    subgraph public["Public APIs"]
        P["PeeringDB<br/>1 request/second"]
        R["RDAP"]
    end

    A -->|stdio| S
    S <--> C
    S -->|HTTPS| P
    S -->|HTTPS| R

    style S fill:#2d6a9f,color:#fff
```

The agent never reaches the internet itself. Everything goes through the server, which is the only place rate limiting, caching, validation and sanitisation can actually be enforced.

A request takes one of two paths:

```mermaid
sequenceDiagram
    participant A as Agent
    participant S as peering-mcp
    participant C as Cache
    participant P as PeeringDB

    A->>S: look up a network
    S->>C: seen this recently?
    alt cached
        C-->>S: yes
    else not cached
        S->>S: wait for the rate limiter
        S->>P: GET
        P-->>S: JSON (can be 130 KB)
        S->>S: validate, sanitise, shape
        S->>C: store
    end
    S-->>A: compact result + source + age
```

That shaping step is not cosmetic. One network's raw presence records can exceed 130 KB, and returning that would flood the agent's context window and make it measurably worse at the actual task.

## Design principles

These are load-bearing, not aspirational. Pull requests are reviewed against them.

- **Read-only, permanently.** Only `GET` is ever sent, enforced at the transport rather than by convention. There is no write path and there will not be one.
- **It says when it does not know.** PeeringDB is self-reported, so a missing record is common and is *not* evidence that something is untrue. The server distinguishes "this network does not exist" from "nobody filled this in", and never fills a gap with a plausible guess.
- **Every answer carries its source and age.** Including when the upstream record was last edited, because a record untouched since 2019 deserves less weight than one edited last month.
- **Responses are small on purpose.** Every tool returns a shaped, compact result rather than passing upstream JSON through.
- **Upstream text is untrusted.** PeeringDB free-text fields are written by the networks themselves and end up in a language model's context. They are allowlisted, length-capped and sanitised before they leave the server.
- **Polite to upstream.** PeeringDB permits one request per second; the server holds itself to that, caches aggressively, and identifies itself in every request.

## Data sources

All public, all free, no scraping.

| Source | Used for | Auth | Cost |
| --- | --- | --- | --- |
| [PeeringDB API v2](https://www.peeringdb.com/apidocs/) | Networks, exchanges, facilities, presence, peering policy | API key optional | Free |
| [RDAP](https://about.rdap.org/) | Registration data for IPs, prefixes and AS numbers | None | Free |

Later versions may add observed routing data from [RIPEstat](https://stat.ripe.net) and topology from [CAIDA AS Rank](https://asrank.caida.org).

## Requirements

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/)
- Optionally a free [PeeringDB API key](https://docs.peeringdb.com/howto/api_keys/), which raises the rate limit

## Use it with an agent

Until it is published, point your agent at a local checkout.

**Claude Code:**

```bash
claude mcp add peering-mcp -- uv run --directory /path/to/peering-mcp peering-mcp
```

**Anything that reads a JSON MCP config:**

```json
{
  "mcpServers": {
    "peering-mcp": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/peering-mcp", "peering-mcp"]
    }
  }
}
```

Then ask it something an agent normally gets wrong: *"What is Deutsche Telekom's peering policy, and how many internet exchanges are they at?"*

## Development

```bash
git clone https://github.com/LeonardMichalas/peering-mcp.git
cd peering-mcp
uv sync --all-groups

uv run pytest              # tests
uv run ruff check .        # lint
uv run ruff format .       # format
uv run mypy src            # types
```

`uv run` handles the environment. There is no virtualenv to activate.

Install the git hooks once, and lint, format and types run before every commit:

```bash
uv run pre-commit install
```

### Configuration

Everything has a working default. The server starts and answers questions with nothing set.

| Variable | Default | Purpose |
| --- | --- | --- |
| `PEERINGDB_API_KEY` | unset | Raises the PeeringDB rate limit. Works without it |
| `PEERING_MCP_CACHE_TTL` | `86400` | Cache lifetime in seconds |
| `PEERING_MCP_CACHE_DIR` | platform cache dir | Where the on-disk cache lives |
| `PEERING_MCP_NO_CACHE` | unset | Set to `1` to disable caching, for testing |
| `PEERING_MCP_TIMEOUT` | `10` | Per-request timeout in seconds |
| `PEERING_MCP_MAX_RETRIES` | `3` | Attempts before an upstream failure is reported |

### Tests

Four levels, each answering a different question:

| Directory | Answers |
| --- | --- |
| `tests/unit/` | Is the pure logic right? |
| `tests/contract/` | Does the server handle what upstream actually sends, including malformed and hostile responses? |
| `tests/integration/` | Does it behave as an MCP server? |
| `tests/eval/` | Does a model pick the right tool from its description? |

Tests marked `live` hit the real API and are opt-in, never run in CI:

```bash
uv run pytest -m live
```

## Contributing

Issues and pull requests are welcome. Before opening a PR:

1. `uv run pytest`, `uv run ruff check .` and `uv run mypy src` all pass.
2. New behaviour has a test at the appropriate level.
3. The change respects the design principles above. In particular, a tool that returns a large or unshaped response, or that could pass raw upstream free text to a model, will be sent back.

## Licence

MIT. See [LICENSE](LICENSE).
