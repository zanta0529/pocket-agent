# Code Review + Software Factory Portability Audit

This reference captures a representative audit of a paired read-only code-review workflow, a remediation helper, and a Planner → Builder → QA factory suite.

## Assessment skill

A well-factored assessment skill can be mostly portable when it describes governance loading, stack detection, Git diff scope, evidence collection, tests, severity, and reporting without naming agent APIs.

Remaining coupling commonly appears in the handoff:

- `start the factory workflow`;
- `load code-review-remediation`.

Keep those names only when the companion skills ship in the same bundle. Describe activation through the current agent's native skill mechanism and provide a manual fallback.

## Remediation helper

Typical portability leaks:

- Product profile/skill-management concepts in responsibility exclusions.
- Direct handoff to a product-specific configuration skill.
- A named diagnostic skill that is not included in the bundle.
- "If the harness marks evidence stale" wording.

Replace product-specific referrals with capability-based optional handoffs. Replace harness-dependent freshness with a file-change rule: rerun affected validation after any validation-relevant write.

## Factory suite

The strongest coupling usually appears in tool sections in every phase:

- exact read/search/edit/write/shell tool names;
- product names in the orchestrator introduction;
- product-specific parameter syntax such as a file-search target mode.

Replace these lists with native-capability wording in the orchestrator, Planner, Builder, and QA Gate. Keep workflow boundaries and commands such as Git, npm, pytest, or build scripts when they are selected from the detected repository stack.

## Governance discovery

Do not limit Factory initialization to one product's router. Discover `AGENTS.md`, `CLAUDE.md`, `.agents/AGENTS.md`, `.cursorrules`, `.github/copilot-instructions.md`, and routed documents before planning or writing artifacts. A nested router discovered only after writing a plan is too late if it contains substantive constraints.

## Artifact path

`.agents/context/` can remain a shared cross-agent contract when documented as a repository workflow directory rather than a private Antigravity configuration directory. Prefer:

1. Repository governance override.
2. `.agents/context/` as the default.
3. Overwrite by default.
4. Archive only on explicit request.

## Platform migration notes

A reference containing Hermes profile paths, `skill_manage`, `skills_list`, Antigravity `agy` commands, or explicit mappings such as `run_command → terminal` is adapter material, not portable workflow documentation. Extract durable orchestration lessons into neutral references and leave exact installation/tool mappings in optional platform-specific notes.

## Practical migration order

1. Inventory main files and all references.
2. Abstract exact tool APIs in every phase.
3. Generalize governance discovery.
4. Convert companion skills to soft dependencies.
5. Make artifact location overrideable.
6. Remove harness-specific freshness assumptions.
7. Separate platform migration notes from the common core.
8. Smoke-test discovery, read-only review, approved edits, and QA on each target agent.
