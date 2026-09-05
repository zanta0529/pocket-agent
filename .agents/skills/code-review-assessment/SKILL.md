---
name: code-review-assessment
description: Conduct comprehensive, read-only, evidence-based repository code reviews with governance loading, technology-stack detection, safe verification, severity-ranked findings, and prioritized remediation guidance.
---

# Code Review Assessment

Use this skill when the user requests a full code review, architecture review, security review, performance review, readiness assessment, or another repository-wide review. The workflow is read-only, technology-agnostic, evidence-based, and designed to avoid generic checklist findings.

## 1. Establish the review boundary

- Treat the task as analysis-only unless the user explicitly authorizes implementation.
- Do not modify source code, tests, documentation, configuration, or tracked generated artifacts.
- Do not create a report or remediation-plan file in the repository unless explicitly requested; return the report in the conversation by default.
- Do not commit, push, reset, restore, clean, rewrite history, install or upgrade dependencies, or delete existing files.
- Read-only tool usage and safe validation commands are allowed.

## 2. Load governance before reviewing

1. Read the repository entry instructions, such as `AGENTS.md`, `CLAUDE.md`, `.cursorrules`, or `.github/copilot-instructions.md`.
2. Follow their routing and precedence rules.
3. Read every referenced governance document that actually exists before judging implementation.
4. If a referenced document is missing, do not invent or infer its contents. Mark it as missing or skipped in the report.
5. Apply project-specific rules before universal engineering defaults.

## 3. Detect the actual technology stack

Inspect root feature files and relevant subdirectories before choosing review rules. Examples include:

- `package.json`, lockfiles, workspace manifests
- `requirements.txt`, `requirements-dev.txt`, `pyproject.toml`, `setup.cfg`
- `go.mod`, `Cargo.toml`, `pom.xml`, `build.gradle`
- API, database, ORM, migration, frontend, mobile, test, lint, type-check, coverage, and build configurations

Identify:

- Primary languages and runtime versions
- CLI and application entrypoints
- Backend APIs and external services
- Databases, LDAP, queues, filesystems, or other integrations
- Frontend type, if any
- Test, lint, formatting, type-check, coverage, and security tools

Prefer repository evidence over stale, copied, or template-like governance claims. If the user requests a detection summary, place it at the beginning of the final report; do not pause for confirmation unless explicitly requested.

If an expected area does not exist—such as a frontend, database, ORM, mobile app, or queue—skip it and mark it in the summary rather than inventing concerns.

### Monorepo detection

If multiple independent sets of stack-feature files are found in different subtrees (for example, more than one `package.json`/`pyproject.toml`/`go.mod` outside of nested dependency directories), treat the repository as a monorepo:

- Enumerate each sub-project separately with its own detected stack, entrypoints, and test tooling.
- Determine whether governance documents are repository-wide, per-package, or both, and apply the correct precedence for each sub-project.
- Scope and findings in later sections must be attributed to the specific sub-project, not the repository as a whole.

## 4. Select and disclose the review scope

### 4.1 Review mode

Determine the mode before selecting files:

- **Incremental (diff) review** — use when the user specifies a base branch, a PR/commit range, or explicitly asks to review recent changes (e.g., "review my latest changes", `--diff`). Scope the primary review to files changed relative to the specified base (via `git diff --name-only <base>...HEAD` or equivalent). For each changed file, also read its direct callers and callees and any governance-relevant context needed to judge correctness — a changed file must not be assessed in isolation from what depends on it or what it depends on.
- **Full review** — the default when no base/range is specified. Review the whole detected scope per section 4.2.

State the chosen mode and, for incremental review, the exact base/range used, at the top of the report.

### 4.2 Scope selection

Choose the scope from the detected repository shape. Review in this order when applicable:

1. Core business logic and domain models
2. External-system, network, database, LDAP, and filesystem access
3. API, CLI, worker, scheduler, or application entrypoints
4. Configuration, logging, authentication, and error handling
5. Frontend and user interactions
6. Tests, CI, and engineering-governance configuration

List included and excluded directories in the report.

Exclude generated or irrelevant content by default:

- `.git/`
- Virtual environments
- `node_modules/`
- `__pycache__/`, `.pytest_cache/`
- `build/`, `dist/`, coverage output
- Binary or generated files
- Previous code-review reports

Review excluded content only when it creates a version-control, release, supply-chain, or security risk. Previous reports may be compared only after completing an independent review and must not be treated as current evidence by themselves.

## 5. Gather evidence before reporting

- Read the relevant files and trace functions, classes, data flows, and call sites before making a finding.
- Locate definitions and usages rather than inferring behavior from names.
- Do not report a defect based only on a filename, TODO, isolated snippet, theoretical possibility, or generic best practice.
- For each confirmed finding, establish the trigger condition, affected path, actual or credible impact, and remediation direction.
- Mark unresolved suspicions as **Needs confirmation** rather than presenting them as confirmed defects.
- Consolidate multiple manifestations of the same root cause into one finding and list all affected locations.
- Batch independent reads and searches.

### Secret-handling rules

- Never read or print `.env` contents, private keys, passwords, tokens, certificates, or other real credential values.
- Inspect `.gitignore`, sample configuration, Git tracking state, and secret-loading logic instead.
- To assess a possibly credential-bearing file, prefer `git ls-files` and ignore-rule checks over opening the file.
- If a suspected secret is discovered incidentally, report only its location, category, and risk—never reproduce the value.

## 6. Review dimensions

### Architecture and governance

- Compliance with loaded governance and explicit architectural invariants
- Module boundaries, responsibility allocation, layering, and dependency direction
- Oversized modules, hidden coupling, duplicated production logic, and dead configuration
- Every governance-noncompliance finding must cite the specific document and section/rule it violates (e.g., `AGENTS.md §3.2`). Do not assert noncompliance without a locatable source citation; if no specific rule is violated but the pattern is still a concern, classify it under code quality or reliability instead, not governance.

### Security

- Authentication and authorization
- Credential and sensitive-data handling
- Input validation and output encoding
- SQL, LDAP, command, template, and path injection
- Logging and exception redaction
- TLS, timeout, retry, external connection, and failure behavior
- Dependency, CI artifact, and supply-chain risks

#### Dependency and supply-chain verification

- Identify the dependency manifest and lockfile for each detected stack (e.g., `package.json` + lockfile, `requirements.txt`/`poetry.lock`, `go.sum`, `Cargo.lock`).
- Where a safe, read-only audit command exists for the ecosystem (e.g., `npm audit --omit=dev`, `pip-audit`, `cargo audit`, `govulncheck`), run it per section 7 and report exact findings; do not guess at CVEs from memory.
- If no audit tool is available or network access is restricted, state this explicitly rather than omitting the dimension, and flag unpinned or wildcard version ranges as a finding on their own.
- Note dependencies with no lockfile entry, dependencies pinned to a moved/deleted source, and license terms that conflict with the project's stated distribution model, when license metadata is present in the manifest.

### Code quality and reliability

- Type safety and runtime validation
- Error wrapping and exception consistency
- Resource cleanup and partial-failure behavior
- Defensive handling of edge cases
- Platform, timezone, encoding, and filesystem assumptions
- Appropriate modular or object-oriented design without unnecessary abstraction

### Runtime performance

Trace complete hot paths rather than reporting local micro-optimizations. Check:

- N+1 operations and request/tick fan-out
- Repeated network, filesystem, database, serialization, or rendering work
- Cache correctness, bounds, eviction, and invalidation
- Unbounded input, recursion, collections, concurrency, and queues
- Async reentrancy, overlap, backpressure, and slow-client policy
- Memory, CPU, payload, and UI update amplification

When projecting rates, state the fan-out formula and assumptions. Label in-memory probes and synthetic measurements as directional evidence rather than production latency or SLA data. For Node/Express/Mongo/streaming systems, consult `references/runtime-performance-review.md`.

### Testing

- Coverage of core success, failure, boundary, and security paths
- Whether tests import and exercise production code rather than copied helpers
- Correct isolation of networks, databases, directories, clocks, and external services
- Skipped, flaky, over-mocked, or assertion-light tests
- Coverage thresholds and whether commands actually enforce them

### Frontend, when present

- Loading, error, empty, stale, and success states
- Responsive behavior and accessibility
- Keyboard and semantic interactions
- Safe DOM rendering and untrusted metadata handling
- API timeout and failure behavior
- Request/render fan-out and truthful connection status

For dense operational dashboards, consult `references/dashboard-ui-ux-review.md`. For browser cryptography or secret-sharing applications, consult `references/client-side-crypto-review.md`.

## 7. Run safe, read-only verification

When feasible, run the repository's relevant tests, lint, type-check, coverage, audit, or security commands.

1. Run `git status --short` before verification and preserve pre-existing state.
2. Do not use auto-fix options.
3. Do not install, upgrade, or remove dependencies.
4. Skip build or validation commands known to overwrite tracked files; explain why they were skipped.
5. Do not delete caches or temporary files, including ones that predated the review.
6. Run `git status --short` again after verification.
7. Distinguish environment/setup failures from repository defects.
8. Report exact commands and their actual Pass, Fail, or Blocked results.

A successful command is not automatically a meaningful quality gate. Detect placeholder scripts, skipped suites, missing configuration, and configured-but-unenforced thresholds.

## 8. Severity rules

Use the user's taxonomy if supplied. Otherwise use:

- 🔴 **Critical — must fix:** exploitable security vulnerability, data loss, sensitive-data disclosure, broken core workflow, or violation of an explicit high-risk invariant.
- 🟡 **Important — should fix:** meaningful reliability, error-handling, testing, performance-amplification, maintainability, or defense-in-depth issue.
- 🔵 **Minor — optional:** cleanup, naming, documentation consistency, low-risk simplification, or future-proofing that does not currently impair correctness.

Do not inflate or reduce severity to populate every category. Explicitly state when no confirmed findings exist at a severity level.

## 9. Reporting format

### 1. Detection summary

Include:

- Review mode (full or incremental) and, if incremental, the exact base/range used
- Whether the repository is a monorepo, and if so, each sub-project detected
- Languages and runtimes
- Backend, APIs, and external systems
- Frontend
- Database or ORM
- Test and quality tools
- Primary entrypoints
- Missing or skipped expected areas

### 2. Review scope

Include:

- Reviewed directories and important files (for incremental review, the changed-file list plus traced callers/callees)
- Excluded content
- Skipped areas and reasons

### 3. Verification results

For each command include:

- Exact command
- Pass, Fail, or Blocked
- Relevant result
- Skipped commands and reasons
- Whether pre/post-verification worktree status remained consistent

### 4. Findings

Order findings Critical → Important → Minor. Each finding must include:

- Severity and concise title
- Problem description
- Evidence and trigger condition
- Why it matters / impact
- File path and exact line number or range
- Concrete remediation direction
- Missing test case, when applicable

Use repository-relative `path:line` references. Do not paste secrets or large code blocks.

### 5. Reviewed areas with no confirmed findings

List important areas that were actually inspected but lacked enough evidence for a finding. This makes the review's negative evidence and coverage explicit.

### 6. Prioritized remediation actions

Order actions Critical → Important → Minor. Every action must map to a preceding finding; do not introduce unreviewed issues in the action list.

## 10. Completion checks

Before finalizing, verify that:

- All routed governance files were read or marked missing.
- Technology-stack detection preceded framework-specific judgments, and monorepo sub-projects (if any) were individually attributed.
- The review mode (full or incremental) was stated, and incremental reviews traced changed-file callers/callees rather than assessing diffs in isolation.
- Dependency/supply-chain risk was addressed with an actual audit command or an explicit statement that none was available — not silently skipped.
- Every architecture/governance finding cites a specific document and rule.
- Findings are line-numbered and evidence-backed.
- Environment blockers are not misreported as code defects.
- Secrets were not read or disclosed.
- The worktree was not intentionally modified.
- No report file, plan file, commit, or cleanup action was created without authorization.

## Handoff to remediation

If the user later approves fixes, stop using review-only mode and start the coordinated planning, implementation, and QA workflow. If the current agent can activate named skills and both `factory` and `code-review-remediation` are available, use the agent's native skill mechanism to activate them; otherwise preserve the same orchestration and remediation boundaries manually. Convert only confirmed findings into repair clusters, regression-test obligations, and finding-specific acceptance checks. Preserve explicit exclusions and do not treat unresolved suspicions as approved work.

## Common pitfalls and review heuristics

- Do not stop after manifests; inspect core implementation and call paths.
- Do not treat passing unit tests as sufficient when coverage gates fail or tests duplicate implementation.
- CLI automation tools deserve special scrutiny around shell wrappers, user-provided arguments, path traversal, generated file paths, timeout enforcement, and log redaction.
- For desktop media or file-processing systems, trace source/destination identity through overwrite, rename, copy, and fallback branches. A missing `source != destination` invariant may cause irreversible deletion.
- Inspect CI artifact paths as well as repository secrets. Generated private signing keys and their backups must never be uploaded as artifacts.
- In browser image-processing pipelines, trace `FileReader`, canvas, worker, ZIP, and Promise lifecycles; require bounded dimensions and guaranteed exit from running states.
- For client-side encrypted metadata, validate decoded schemas and KDF bounds, reject incompatible metadata, and distinguish checksums from authenticated integrity.
