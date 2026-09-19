# Releasing

How a version of `peering-mcp` reaches PyPI and the official MCP registry.

Everything here is manual and deliberate. A release is the one moment this
project is not reversible: a PyPI version number cannot be reused, even after
a delete.

## Before anything

```bash
uv run pytest -m "not live"     # the suite, budgets included
uv run ruff check . && uv run ruff format --check .
uv run mypy src
./tasks/check-docs.sh           # documentation against reality
```

Then read the README as a stranger would. It is what PyPI serves as the
package description, and the first thing anybody sees.

## 1. Set the version

Three files carry it, and they must agree:

| File | Field |
| - | - |
| `pyproject.toml` | `version` |
| `server.json` | `version` |
| `server.json` | `packages[0].version` |

`tasks/check-docs.sh` fails if they drift.

## 2. Build and check the artefacts

```bash
rm -rf dist/
uv build
uv run --with twine twine check dist/*
```

`uv build` writes an sdist and a wheel to `dist/`. `twine check` catches a
README that PyPI will refuse to render, which is the usual reason a first
upload fails.

## 3. Publish to PyPI

Get an API token from <https://pypi.org/manage/account/token/>, scoped to this
project once it exists. Publish to TestPyPI first if anything about the
metadata changed:

```bash
uv publish --publish-url https://test.pypi.org/legacy/ --token pypi-...
uv publish --token pypi-...
```

Then prove the thing works from the published artefact rather than from the
checkout — this is the criterion the project actually cares about:

```bash
uvx peering-mcp --help
```

## 4. Tag the commit

```bash
git tag -a v0.1.0 -m "peering-mcp 0.1.0"
git push origin v0.1.0
```

## 5. Publish to the MCP registry

The registry proves you own the PyPI package by looking for the line
`mcp-name: io.github.LeonardMichalas/peering-mcp` in the package's README.
It is already there as an HTML comment at the top; **check it survived any
README edit before publishing**, or the registry will refuse the server.

```bash
curl -L "https://github.com/modelcontextprotocol/registry/releases/latest/download/mcp-publisher_$(uname -s)_$(uname -m).tar.gz" | tar xz mcp-publisher
./mcp-publisher login github
./mcp-publisher publish
```

`login github` opens a browser and proves the `io.github.LeonardMichalas`
namespace belongs to you. `publish` reads `server.json` from the working
directory.

## 6. Say so

- Update the README status line: it describes the present tense, and "not
  published" stops being true the moment step 3 succeeds.
- `git push`, then run `./tasks/check-docs.sh` again — check 5 compares
  GitHub's README with the local one and cannot pass before the push.

## Yanking

A bad release is yanked, never deleted: deleting frees the version number for
a different artefact, which is how a lockfile ends up resolving to something
nobody published.

```bash
uv run --with twine twine yank peering-mcp 0.1.0 --reason "..."
```
