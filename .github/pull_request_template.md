<!-- Thanks for contributing! Fill this in so review goes quickly. -->

## What does this PR do?

<!-- A short description of the change and why. Link the issue it addresses. -->

<!-- Link the issue it addresses (e.g., Closes #123), or remove if N/A. -->

Closes #

## Base branch

<!-- CI runs on pull requests targeting `main` and `dev`. -->

- [ ] This PR targets `dev` (or it is a release/hotfix PR targeting `main`).

## Type of change

- [ ] Bug fix (regression test included)
- [ ] New feature (follows the Adapter / Strategy / Factory / Command architecture)
- [ ] Docs / comments / tests only
- [ ] Refactor (no behavior change)
- [ ] Security fix / boundary hardening

## Affected layers

- [ ] Core (`src/pocket_agent/core/`)
- [ ] LLM engine adapters (`src/pocket_agent/engines/`)
- [ ] CLI frontend (`src/pocket_agent/frontend/cli/`)
- [ ] System & security policies (`src/pocket_agent/system/`)
- [ ] Extensions (`src/pocket_agent/extensions/`: MCP / Skills / Hooks)
- [ ] Built-in tools & MCP servers (`src/pocket_agent/tools/`, `mcp_servers/`)
- [ ] Build scripts / docs / tests (`build_scripts/`, `scripts/`, `docs/`, `tests/`)

## Checklist

### Code quality & CI (`.github/workflows/standard-ci.yml`)

- [ ] Compatible with Python 3.14+, UTF-8, LF line endings, and the 120-character line length limit.
- [ ] Ran `ruff format --check .` and `ruff check .` with no remaining issues.
- [ ] Verified dependency integrity:
  - `python scripts/check_imports.py src scripts --req requirements.in --ignore tiktoken`
  - `python scripts/check_imports.py tests --req requirements.in --req requirements-dev.in`
- [ ] Ran `pytest tests/` and all tests pass (with network, OAuth, and user data isolated).
- [ ] If dependencies changed, updated `requirements.in` / `requirements-dev.in` and regenerated lockfiles via `pip-compile` (no manual edits to `.txt` lockfiles).

### Architecture & security invariants

- [ ] Preserves layered boundaries: tools dispatched via `core/tool_registry.py`, CLI commands registered via `CliCommandRegistry`, and constants centralized in `system/constants.py`.
- [ ] Preserves security invariants: `on_confirm` allows writes only when `decision is True`, command allowlist remains fail-closed, and MCP tools keep `mcp__<server>__<tool>` isolation and read-only boundaries.
- [ ] Keeps the workspace clean (no temporary files in the repo, and no `.env`, tokens, sessions, or `agent*.log` committed).
- [ ] If resource or execution paths changed, verified compatibility across source, Modern Portable (`build_scripts/build_portable.py`), and PyInstaller modes.
- [ ] Confirmed this contribution complies with the project's `LICENSE` (MIT License).

## Notes for the reviewer

<!-- Anything tricky, trade-offs, risk notes, or areas you want extra eyes on. -->
