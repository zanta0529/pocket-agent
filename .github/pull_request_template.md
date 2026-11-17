<!-- Thanks for contributing! Fill this in so review goes quickly. -->

## What does this PR do?

<!-- Short description of the change and why. Use `Closes #123` to auto-close an issue, `Refs #123` to link one, or omit if N/A. -->

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Docs / comments / tests only
- [ ] Refactor (no behavior change)
- [ ] Security fix / boundary hardening
- [ ] Breaking change

## Affected layers

- [ ] Core (`src/pocket_agent/core/`)
- [ ] LLM engine adapters (`src/pocket_agent/engines/`)
- [ ] CLI frontend (`src/pocket_agent/frontend/cli/`)
- [ ] System & security policies (`src/pocket_agent/system/`)
- [ ] Extensions (`src/pocket_agent/extensions/`: MCP / Skills / Hooks)
- [ ] Built-in tools & MCP servers (`src/pocket_agent/tools/`, `mcp_servers/`)
- [ ] Build scripts / docs / tests (`build_scripts/`, `scripts/`, `docs/`, `tests/`)

## Testing

<!-- Record actual checks executed (real terminal/host vs. mocked). Listing a command is not verification; do not check unrun items below. -->

- Checks run and results:
- CI run (if available):
- Not run / N/A (with reasons):

## Checklist

<!-- Check only verified items. Leave N/A items unchecked (or mark N/A), and explain pending checks under Testing. -->

### Code quality & CI (`.github/workflows/standard-ci.yml`)

- [ ] Compatible with Python 3.14+, UTF-8, LF line endings, and the 120-character line length limit.
- [ ] Ran `ruff format --check .` and `ruff check .` with no remaining issues.
- [ ] Verified dependency integrity:
  - `python scripts/check_imports.py src scripts --req requirements.in --ignore tiktoken`
  - `python scripts/check_imports.py tests --req requirements.in --req requirements-dev.in`
- [ ] Ran `pytest tests/` and all tests pass (with network, OAuth, and user data isolated).
- [ ] *(if applicable)* Added or updated regression tests for bug fixes / behavior changes.
- [ ] *(if applicable)* Updated `requirements.in` / `requirements-dev.in` and regenerated lockfiles via `pip-compile` (no manual edits to `.txt` lockfiles).
- [ ] *(if applicable)* Verified Markdown formatting against `.markdownlint-cli2.jsonc` (`docs-lint` job) and synced `src/pocket_agent/locales/*.json` for UI text changes.

### Architecture & security invariants

- [ ] Preserves relevant layered boundaries (`core/tool_registry.py`, `CliCommandRegistry`, `system/constants.py`, and Adapter / Strategy / Factory / Command patterns where applicable).
- [ ] Preserves security invariants: `on_confirm` allows writes only when `decision is True`, command allowlist remains fail-closed, and MCP tools keep `mcp__<server>__<tool>` isolation and read-only boundaries.
- [ ] PR diff contains no temporary files, `.env`, tokens, sessions, or `agent*.log`.
- [ ] *(if applicable)* Verified resource / execution path compatibility across source, Modern Portable (`build_scripts/build_portable.py`), and PyInstaller modes.

## Notes for the reviewer

<!-- Note breaking changes, config/data/permission migrations, rollback considerations, trade-offs, or areas needing extra eyes. Write "None" if N/A. -->
