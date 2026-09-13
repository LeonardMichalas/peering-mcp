# peering-mcp

**An MCP server that lets an AI agent look up how the internet is actually wired together** — who peers with whom, at which internet exchanges and facilities, under what peering policy, and who a given address range is registered to.

> **Status: early development.** The scaffold and quality gate are in place. Tools land next. Nothing is published yet.

## Why this exists

The internet is roughly eighty thousand independent networks that agree to carry each other's traffic. Who connects to whom, where, and on what terms is public, free and well structured — it is published through stable APIs by [PeeringDB](https://www.peeringdb.com) and the regional internet registries.

None of it is reachable by an AI agent. Ask any coding assistant which internet exchanges a given carrier is present at and it will answer from memory: fluent, confident, and often wrong. It has no way to check, so it does not check.

This server is that way to check.

## What it will do

| Tool | Question it answers |
| --- | --- |
| `lookup_network` | Who is this network, and what is their peering policy? |
| `list_presence` | Which internet exchanges and facilities are they present at? |
| `find_at_exchange` | Who else is at this exchange, and would they peer? |
| `find_common_presence` | **Where can these networks meet each other?** |
| `lookup_registration` | Who is this IP range or AS number registered to? |

`find_common_presence` is the one that motivated the project. Answering "which of the networks we buy transit from are also present at an exchange we already sit in" currently takes a person about an hour and a dozen browser tabs.

## Design principles

These are load-bearing, not aspirational. If you send a pull request, it will be reviewed against them.

- **Read-only, permanently.** The server only ever issues `GET`. There is no write path and there will not be one.
- **It says when it does not know.** PeeringDB is self-reported, so a missing record is common and is *not* evidence that something is not true. The server distinguishes "this network does not exist" from "nobody filled this in", and never fills a gap with a plausible guess.
- **Every answer carries its source and age.** Including when the upstream record was last edited, because a record untouched since 2019 deserves less weight.
- **Responses are small on purpose.** One network's raw presence records can exceed 130 KB. Returning that would flood the agent's context window and make it measurably worse, so every tool returns a shaped, compact result rather than passing upstream JSON through.
- **Upstream text is untrusted.** PeeringDB free-text fields are written by the networks themselves and end up in a language model's context. They are allowlisted, length-capped and sanitised before they leave the server.
- **Polite to upstream.** PeeringDB permits one request per second; the server holds itself to that, caches aggressively, and identifies itself.

## Data sources

| Source | Used for | Auth | Cost |
| --- | --- | --- | --- |
| [PeeringDB API v2](https://www.peeringdb.com/apidocs/) | Networks, exchanges, facilities, presence, peering policy | API key recommended | Free |
| [RDAP](https://about.rdap.org/) | Registration data for IPs, prefixes and AS numbers | None | Free |

Later versions will add observed routing data from [RIPEstat](https://stat.ripe.net) and topology from [CAIDA AS Rank](https://asrank.caida.org).

## Requirements

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/)
- Optionally a free [PeeringDB API key](https://docs.peeringdb.com/howto/api_keys/), which raises your rate limit

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

| Variable | Default | Purpose |
| --- | --- | --- |
| `PEERINGDB_API_KEY` | unset | Raises the PeeringDB rate limit. Works without it |
| `PEERING_MCP_CACHE_TTL` | `86400` | Cache lifetime in seconds |
| `PEERING_MCP_CACHE_DIR` | platform cache dir | Where the on-disk cache lives |
| `PEERING_MCP_NO_CACHE` | unset | Set to `1` to disable caching, for testing |

### Tests

Four levels, each answering a different question:

| Directory | Answers |
| --- | --- |
| `tests/unit/` | Is the pure logic right? |
| `tests/contract/` | Do we handle what upstream actually sends, including malformed and hostile responses? |
| `tests/integration/` | Does the server behave as an MCP server? |
| `tests/eval/` | Does a model pick the right tool from its description? |

Tests marked `live` hit the real API and are opt-in:

```bash
uv run pytest -m live
```

## Contributing

Issues and pull requests are welcome. Before opening a PR:

1. `uv run pytest`, `uv run ruff check .` and `uv run mypy src` all pass.
2. New behaviour has a test at the appropriate level.
3. The change respects the design principles above. In particular, a tool that returns a large or unshaped response, or that could pass raw upstream free text to the model, will be sent back.

## Licence

MIT. See [LICENSE](LICENSE).
