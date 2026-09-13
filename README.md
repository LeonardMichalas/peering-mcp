# peering-mcp

**An MCP server that lets an AI agent look up how the internet is actually wired together** — which networks connect to each other, at which internet exchanges and facilities, under what peering policy, and who a given address range is registered to.

> **Status: early development.** The foundation and quality gate are in place. Tools land next. Nothing is published to PyPI yet.

> **A personal project.** Built in my own free time, out of curiosity about the Model Context Protocol. See [About this project](#about-this-project).

## Why this exists

The internet is roughly eighty thousand independent networks that agree to carry each other's traffic. Which networks connect to which, where they meet, and on what terms is public, free and well structured — published through stable APIs by [PeeringDB](https://www.peeringdb.com) and the regional internet registries.

None of it is reachable by an AI agent. Ask a coding assistant which internet exchanges a given carrier is present at and it will answer from memory: fluent, confident, and often wrong. It has no way to check, so it does not check.

This server is that way to check.

```mermaid
graph LR
    subgraph before["Without this server"]
        Q1["Which exchanges is<br/>this network at?"] --> M1["Agent answers<br/>from memory"]
        M1 --> A1["Fluent.<br/>Sometimes wrong.<br/>No source."]
    end

    subgraph after["With this server"]
        Q2["Which exchanges is<br/>this network at?"] --> M2["Agent calls a tool"]
        M2 --> P["PeeringDB"]
        P --> A2["Checked.<br/>Cited.<br/>Says when unknown."]
    end

    style A1 fill:#a33,color:#fff
    style A2 fill:#2a6,color:#fff
```

## What it will do

| Tool | Question it answers |
| --- | --- |
| `lookup_network` | Who is this network, and what is their peering policy? |
| `list_presence` | Which internet exchanges and facilities are they present at? |
| `find_at_exchange` | Who else is at this exchange, and would they peer? |
| `find_common_presence` | **Where can these networks meet each other?** |
| `lookup_registration` | Who is this IP range or AS number registered to? |

`find_common_presence` is the tool that motivated the project. Working out where two or more networks could interconnect means looking each one up, listing everywhere it is present, and intersecting the results by hand. That is about an hour and a dozen browser tabs. It should be one question.

```mermaid
graph TD
    Q["Where can AS3320, AS6695<br/>and AS20940 meet?"]
    Q --> T["find_common_presence"]
    T --> F["One PeeringDB query<br/>filtered to all three ASNs"]
    F --> I["Intersect their<br/>exchanges and facilities"]
    I --> R["Shared locations<br/>+ per-network totals<br/>+ source and age"]

    style T fill:#2d6a9f,color:#fff
    style R fill:#2a6,color:#fff
```

The per-network totals matter. If the answer is empty, they tell the agent *why* — either the networks genuinely do not overlap, or one of them has no records at all, which is a very different thing.

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

## About this project

This is a personal side project, written in my own free time because I wanted to understand the Model Context Protocol by building something real with it rather than reading about it.

It is not affiliated with, sponsored by, or connected to my employer or any other organisation. It uses only data that PeeringDB and the regional internet registries publish openly for anyone to use. No proprietary, internal or commercially sensitive information is involved anywhere in it.

Opinions, design decisions and mistakes here are entirely my own.

## Licence

MIT. See [LICENSE](LICENSE).
