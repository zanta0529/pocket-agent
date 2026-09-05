# Software Factory Design Notes

Use this reference to preserve the software factory's cross-agent workflow semantics. The core workflow is platform-neutral: each agent uses its own native file, search, edit, shell, validation, and skill-activation capabilities.

## Governance discovery

Before planning or writing artifacts:

1. Discover applicable repository instruction entrypoints, such as `AGENTS.md`, `CLAUDE.md`, `.agents/AGENTS.md`, `.cursorrules`, and `.github/copilot-instructions.md`.
2. Follow their routing and precedence rules.
3. Read every routed document that actually exists.
4. Apply scoped or nested governance to the files under that scope.
5. If documents conflict and no precedence resolves the conflict, stop and ask the user.

Do not assume that one product's preferred instruction filename is the only governance source.

## Tool independence

Describe required capabilities rather than fixed tool API names:

- Read files.
- Search file names, directories, and file contents.
- Apply targeted edits to existing files.
- Create approved new files and artifacts.
- Execute tests, lint, type checks, builds, coverage, audits, and Git inspection.
- Activate optional skills or sub-workflows when the current agent supports them.

Never require a named tool unless the skill is intentionally platform-specific. If an optional skill is unavailable, use the documented fallback rather than stopping the factory.

## Intake and triage

Classify the task before selecting Full or Lite mode:

- `feature`
- `bugfix`
- `refactor`
- `test-only`
- `docs-only`
- `dependency`
- `review-only`

Classify risk independently from complexity:

- `Low`: small UI text, tests, simple pure functions.
- `Medium`: service logic, state management, form validation.
- `High`: authentication, database, dependency upgrades, migrations, or security-sensitive behavior.
- `Critical`: data-loss risk, secrets/authentication boundaries, destructive actions, or tenant isolation.

High/Critical risk requires stronger planning and QA even when the file count appears Lite.

## Planner contract

The plan must include:

- Task, project, complexity, and risk classification
- Relevant repository evidence and reusable patterns
- User story and Given/When/Then acceptance criteria
- Explicit exclusions
- Architectural and security invariants
- Backend/frontend/non-app specifications as applicable
- Regression and verification strategy
- Expected changed files
- Risk and rollback notes
- Unresolved questions

The planner remains read-only except for an explicitly authorized plan artifact. It must stop when requirements, acceptance criteria, architecture, dependency changes, schema/auth scope, or security boundaries are unclear.

## Builder contract

The builder implements only an approved plan:

- Reconfirm invariants, expected files, and verification strategy before editing.
- Fix root causes without unrelated refactors or formatting.
- Add regression tests for bug fixes.
- Do not add dependencies, change schema/auth policy, or expand scope without approval.
- Preserve public contracts unless a breaking change is explicitly authorized.
- Run the repository's actual quality commands after edits.

For full-stack work, complete and verify the backend contract before implementing the frontend. Record the effective API contract in the shared artifact root.

## QA gate contract

Separate QA into four layers:

1. Mechanical Verification — typecheck, lint, tests, coverage, audit, and build as required.
2. Acceptance Verification — map each acceptance criterion to test or direct evidence.
3. Invariant and Code Review — scope, security, architecture, maintainability, and contract consistency.
4. Final Gate — `Pass`, `Conditional Pass`, or `Fail`.

Suggested semantics:

- `Pass`: all required verification and acceptance criteria pass, invariants hold, and no Critical/Important findings remain.
- `Conditional Pass`: required verification and acceptance criteria pass, invariants hold, and no Critical findings remain, but Important risks require human judgment.
- `Fail`: a required command, acceptance criterion, or invariant fails, or a Critical finding exists.

QA reports problems but does not silently switch into implementation mode.

## Artifact strategy

Repository governance may define the artifact root. If it does not, use `.agents/context/` as the cross-agent default:

```text
.agents/context/
├── plan.md
├── api-contract.md
├── build-summary.md
└── qa-report.md
```

All factory phases and all participating agents must resolve the same effective artifact root. Default behavior overwrites the fixed artifacts above. Create a timestamped archive only when the user explicitly requests one:

```text
.agents/context/archive/YYYYMMDD-HHMM-task-slug/
```

Do not create or clean archives automatically.

## Fresh verification

Verification evidence becomes stale when a later write can affect runtime, tests, configuration, or validation behavior. Rerun the affected checks before claiming completion. Artifact-only writes that cannot affect runtime do not require pretending the code changed, but the final report must clearly relate them to the latest runtime verification.

Keep these states distinct:

1. Implementation completed.
2. Required verification passed.
3. QA gate passed.

## Stop conditions

Stop and ask the user before:

- Adding, removing, or upgrading dependencies.
- Changing database schema, migrations, ORM configuration, authentication, authorization, secrets handling, or tenant isolation without approved scope.
- Running destructive or irreversible operations.
- Expanding beyond approved files or requirements.
- Committing, pushing, rewriting history, or deleting data without explicit authorization.
- Continuing when the approved plan conflicts with observed repository architecture.
- Overriding repository governance with an optional skill or agent-specific convention.
