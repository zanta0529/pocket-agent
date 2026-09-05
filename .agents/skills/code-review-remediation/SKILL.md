---
name: code-review-remediation
description: Convert confirmed code-review findings into focused remediation strategy, implementation constraints, regression tests, and QA acceptance checks as a supporting skill under the software factory or another user-approved workflow.
---

# Code Review Remediation

Use this skill after a code review or governance audit has produced confirmed findings and the user has authorized fixes. It supplies remediation-specific engineering guidance to a planner, builder, and QA gate.

This is a **supporting skill, not a workflow orchestrator**. It does not replace `factory`, create factory artifacts, control human approval gates, or independently run a Planner → Builder → QA lifecycle. For coordinated multi-stage work, `factory` owns orchestration and either activates this skill through the current agent's native mechanism or applies its guidance manually when useful.

## Inputs

Before applying this skill, establish:

- Confirmed findings with severity, evidence, affected paths, and remediation direction
- Findings explicitly accepted, rejected, or deferred by the user
- Protected exclusions and user-owned changes
- Loaded repository governance and architectural invariants
- The active plan or task scope, when one exists

Do not promote unresolved review suspicions into implementation work. A finding marked **Needs confirmation** must be investigated and confirmed before it becomes a remediation item.

## Responsibility boundary

This skill owns:

- Mapping confirmed findings to root causes and repair clusters
- Ordering remediation work by risk and dependency
- Supplying implementation constraints for security, reliability, architecture, and testing fixes
- Defining regression tests and finding-specific QA evidence
- Detecting incomplete or cosmetic fixes

This skill does not own:

- General feature, bugfix, documentation, dependency, or refactor orchestration
- Full/Lite mode selection or overall risk classification
- Human approval gates
- The owning workflow's artifact-directory lifecycle or archive policy
- Builder or QA role switching
- Agent-specific skill installation, profile configuration, or cross-agent router design
- General local-service incident diagnosis
- Commit, push, history rewriting, dependency installation, or destructive cleanup

Use purpose-specific workflows for those concerns when they are available in the current agent. Otherwise apply the current agent's native configuration-management or local-service diagnostic capabilities without changing this skill's remediation boundary.

## 1. Normalize and cluster findings

1. Re-read each accepted finding and its cited evidence.
2. Confirm current line locations and call paths; review reports may be stale.
3. Preserve every explicit exclusion in the active plan and final verification.
4. Group manifestations that share one root cause. Avoid separate patches for symptoms that require the same invariant or abstraction.
5. Identify dependencies between findings. Fix the enabling root cause before dependent tests, callers, or documentation.
6. Keep rejected and deferred findings out of implementation scope.

A useful cluster records:

- Finding IDs or titles
- Root cause
- Affected production paths
- Required behavior after repair
- Invariants that must remain unchanged
- Regression-test obligations
- QA evidence required to close the cluster

## 2. Prioritize remediation

Default ordering:

1. Data loss, source-file destruction, secret exposure, authentication/authorization, injection, and unsafe command execution
2. Path validation, configuration safety, external-input validation, and public API contracts
3. Reliability, timeout, retry, resource lifecycle, concurrency, and error normalization
4. Architecture and duplicated production logic
5. Test gaps, coverage enforcement, observability, and low-risk maintainability findings

Severity alone does not determine implementation order. Resolve a lower-severity prerequisite first when a Critical fix depends on it, and document that dependency rather than changing severity.

## 3. Contributions to planning

When loaded by a planner, add the following to the plan without taking ownership of the plan artifact:

- Accepted finding clusters and traceability back to the review
- Explicitly rejected, deferred, and protected items
- Root-cause repair order
- Expected production and test files
- Public behavior and compatibility constraints
- Finding-specific acceptance criteria
- Regression-test matrix
- Required validation commands and quality gates
- Rollback or recovery notes for High/Critical changes

Stop and ask the user when remediation would require an unapproved dependency change, schema migration, authentication/authorization policy change, destructive operation, public API break, or expansion beyond accepted findings.

## 4. Implementation guidance

When loaded by a builder:

- Reconfirm that each edit maps to an accepted finding cluster and approved file scope.
- Fix root causes rather than suppressing errors, weakening validation, lowering thresholds, muting logs, skipping tests, or adding broad ignore rules.
- Preserve behavior-compatible public APIs unless the approved plan authorizes a breaking change.
- Keep changes narrow; do not bundle unrelated refactors or formatting.
- Apply validation before every side effect. For path-sensitive workflows, validate before reads, backups, writes, moves, deletes, or command execution—not only at the final write.
- Prefer argument-array process APIs over shell command strings. Tests must prove invalid input causes zero command invocations.
- Keep credential-bearing configuration untracked and provide a tracked sample only when within approved scope.
- Redact secrets recursively and consistently across normal logs, error logs, structured events, and exception paths.
- Enforce configured timeouts and make timeout/retry/fallback behavior explicit and testable.
- Do not change security-sensitive defaults from opt-in to automatic behavior.

### Production-module lifecycle

When remediation splits a module or creates new runtime files:

- Classify each new file as a production runtime module, test, generated artifact, or temporary artifact.
- Do not infer that an untracked or generically named file is temporary.
- Cross-check entrypoint imports, test imports, planned files, and the actual filesystem before completion.
- Run an entrypoint import or startup smoke test where safe.
- Keep unknown-origin and user-owned files untouched.

## 5. Regression-test requirements

Tests must exercise production behavior:

- Import production helpers or use public APIs; do not copy production logic into tests.
- Reproduce the original trigger condition before asserting the repaired behavior.
- Cover the successful path, original failure, relevant boundaries, and a negative safety assertion.
- Mock external services, command runners, filesystems, clocks, SDK calls, or credentials as appropriate.
- For command safety, verify exact executable/argument boundaries and zero execution for rejected input.
- For path safety, cover traversal, absolute paths, Windows drive/UNC forms when relevant, symlinks where supported, and allowed relative paths.
- For API/AI wrappers, test timeout, retry, parse/schema failure, fallback, and redacted errors without real credentials.
- For monitoring exceptions, distinguish an observable deployment constraint from a failed target; add regression evidence for both status and details.

Coverage is a gate when repository governance or the approved plan makes it one. Passing lint and unit tests is not completion when the required coverage command fails.

## 6. Finding-specific QA checks

When loaded by a QA gate, independently verify every accepted cluster:

1. Re-read the review evidence and current implementation.
2. Map each finding to changed production code and regression tests.
3. Confirm the trigger is blocked or the invariant is restored.
4. Confirm no excluded file or behavior changed.
5. Run the approved validation commands and report exact results.
6. Check that tests reach production code and do not merely restate the fix.
7. Verify no threshold, lint rule, test, security control, or error signal was weakened to obtain a pass.
8. Mark an item unresolved when evidence is missing, even if unrelated tests pass.

A finding is closed only when all are true:

- The root cause is addressed.
- Required regression tests pass.
- Relevant quality gates pass.
- Architectural and security invariants remain intact.
- No unapproved scope expansion occurred.

## 7. Fresh-verification rule

Run fresh verification after implementation and again after any subsequent write that can affect runtime, tests, configuration, or validation evidence.

If any runtime, test, configuration, or validation-relevant file changes after verification—including reports or summaries that affect workflow evidence—rerun the affected commands before claiming completion. Distinguish clearly between:

1. Implementation completed
2. Lint/test/coverage verification passed
3. QA gate passed

Never collapse these states into a generic “done” claim while a required gate remains red.

## Reporting expectations

When this skill contributes to a factory or another approved workflow, the final status should identify:

- Accepted finding clusters repaired
- Deferred, rejected, and protected items preserved
- Production and test files changed
- Exact verification commands and results
- Remaining blockers or gaps
- Whether the owning QA gate passed, conditionally passed, or failed

Do not create a separate remediation artifact unless the owning workflow or user explicitly requests one.

## Pitfalls

- Do not use this skill merely because a user asks for a general feature or bugfix with no prior review findings.
- Do not restart or duplicate the factory lifecycle from inside this skill.
- Do not turn advisory review suggestions into mandatory changes without user approval.
- Do not fix a single reported call site when sibling call paths share the same unsafe root cause.
- Do not describe passing unit tests as closure when coverage, integration, build, or acceptance gates still fail.
- Do not modify governance files excluded by the user, even when the original review recommended them.
- Do not delete untracked files or generated artifacts without explicit authorization.
