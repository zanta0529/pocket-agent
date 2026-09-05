---
name: cross-agent-skill-portability
description: Audit and refactor reusable coding-agent skills into a platform-neutral core that can run across Hermes, Claude Code, Antigravity, Codex-style agents, and other tool runtimes without hard-coded tool APIs or product assumptions.
---

# Cross-Agent Skill Portability

Use this skill when a reusable agent workflow will be copied, synchronized, or maintained across multiple coding-agent products. It applies to code-review, planning, implementation, QA, remediation, documentation, and other multi-stage skill bundles.

The goal is one behaviorally consistent source of truth whose core instructions describe capabilities and workflow contracts, while product-specific installation and tool mappings remain optional adapters.

## 1. Establish the portability target

Identify:

- Which skill directories, main files, references, templates, and scripts belong to the bundle.
- Which agents must consume the bundle.
- Whether all agents should share the same repository artifacts.
- Which companion skills are guaranteed to be installed and which are optional.
- Whether the task is analysis-only or includes approved edits.

Do not equate content portability with installation portability. A neutral `SKILL.md` may still require product-specific registration or discovery paths.

## 2. Inspect the complete bundle

Review every main `SKILL.md` and linked file. Search for:

- Product names used as runtime requirements rather than examples.
- Exact tool identifiers and parameter syntax.
- Product-local profile, cache, skill, plugin, or configuration paths.
- Agent-specific instruction entrypoints or precedence assumptions.
- Harness, approval, stale-evidence, background-process, or delivery behavior that may not exist elsewhere.
- Hard references to companion skills that will not be distributed.
- Migration history or source-product provenance embedded in standalone native documentation.
- Artifact paths that may be interpreted as one product's private configuration directory.

Report exact file and line evidence. Separate actual platform coupling from generic shell commands, Git commands, programming-language tools, and repository paths that any agent can use.

## 3. Classify each finding

Use these classes:

### Portable as written

The instruction describes engineering behavior without depending on one agent's API.

Examples:

- Run the repository's configured tests.
- Preserve user-owned changes.
- Stop before destructive operations.
- Require Planner, Builder, and QA approval boundaries.

### Capability wording needed

The behavior is portable, but a product-specific tool name is hard-coded.

Replace exact APIs such as file-read, search, edit, write, or shell tool identifiers with capability descriptions. Let the current agent choose its native tool and parameter schema.

### Optional adapter material

The detail is useful only for one product, such as installation paths, skill-management commands, or exact tool mappings. Move it to an optional platform adapter or deployment guide rather than the common core.

### Workflow contract requiring a decision

The content is not inherently product-specific, but all agents need an agreed convention. Examples include artifact directories, approval phrases, archive behavior, and shared plan schemas. Keep a documented default and allow repository governance to override it.

### Broken dependency

A named companion skill or workflow will not be present in the distributed bundle. Convert the dependency to a soft handoff with fallback behavior, or include the companion skill.

## 4. Write platform-neutral core instructions

Prefer wording such as:

- Use the current agent's native file-reading capability.
- Search file names and contents with the available repository search tools.
- Apply targeted edits using the current agent's safest edit mechanism.
- Run validation commands with the available command-execution tool.
- Activate the named supporting workflow through the current agent's native skill mechanism when installed.

Avoid maintaining a large multi-product tool table in every workflow file. Product tool names and parameter schemas change independently and create duplicated maintenance.

## 5. Discover governance portably

Before analysis or writes, inspect applicable repository instruction entrypoints, including when present:

- `AGENTS.md`
- `CLAUDE.md`
- `.agents/AGENTS.md`
- `.cursorrules`
- `.github/copilot-instructions.md`
- Any files routed from those entrypoints

Follow documented precedence and directory scope. Do not wait until an artifact write reveals a nested instruction file that should have governed the write.

## 6. Handle companion workflows as soft dependencies

For each named skill:

1. Confirm whether it is part of the portable bundle.
2. If installed, activate it through the current agent's native mechanism.
3. If absent, use the documented fallback without failing the whole workflow.
4. Preserve responsibility boundaries: a supporting skill must not become a competing orchestrator.
5. Do not assume every agent recognizes the same phrase for loading or invoking a skill.

## 7. Define a shared artifact contract

Use the artifact directory specified by repository governance. If no override exists, a documented repository-local default may be used.

When `.agents/context/` is the default:

- Describe it as a shared workflow-artifact path, not as one product's private settings directory.
- Preserve overwrite-by-default behavior unless the user explicitly requests an archive.
- Keep Planner, Builder, and QA schemas stable across agents.
- Ensure each agent loads applicable nested governance before writing there.

## 8. Make verification freshness runtime-independent

Do not rely on one platform emitting a "stale evidence" signal. Rerun affected checks whenever runtime, test, configuration, build, or validation-relevant files change after the previous verification.

Report these states separately:

1. Implementation completed.
2. Required commands passed or failed.
3. Acceptance and invariant checks passed or failed.
4. Final QA gate passed, conditionally passed, or failed.

## 9. Package common core and adapters

Recommended shape:

```text
workflow-skill/
├── SKILL.md
├── references/
│   ├── domain-guidance.md
│   └── portability-notes.md
└── adapters/                 # only if the target skill system permits this layout
    ├── hermes.md
    ├── claude-code.md
    └── antigravity.md
```

If the skill manager only allows `references/`, `templates/`, `scripts/`, and `assets/`, store optional adapters under `references/platforms/`.

The common `SKILL.md` must remain authoritative. Adapters may explain installation, discovery, or exact tool mappings but must not redefine workflow semantics.

## 10. Verify the portable bundle

For each target agent:

- Confirm the skill is discoverable through that product's supported mechanism.
- Confirm all named companion skills resolve or documented fallbacks activate.
- Exercise a read-only smoke task that reads governance, inspects files, and reports without edits.
- Exercise an approved write task in a disposable fixture, including plan, implementation, validation, and QA boundaries.
- Verify shared artifacts use the intended path and schema.
- Confirm product-specific text remains only in adapters.

## Reporting format

Summarize:

- Files inspected.
- Portable files.
- Product-coupled locations with line evidence.
- Broken or soft skill dependencies.
- Artifact-path decisions.
- Recommended common-core edits.
- Optional adapter material.
- Verification still required per target agent.

## Pitfalls

- Do not blindly replace one product's tool name with another; abstract the capability first.
- Do not claim that copying a folder guarantees skill discovery on every agent.
- Do not preserve migration provenance in a standalone native skill when it does not help execute the workflow.
- Do not split workflow semantics across platform adapters.
- Do not make optional supporting skills hard dependencies.
- Do not treat `.agents/` as universally private or universally portable without documenting its role.
- Do not rewrite manually authored or protected skills when the active curator or skill manager forbids it; capture the reusable method in an editable class-level umbrella and report the blocked targets.

## References

- `references/code-review-factory-portability-audit.md` — concrete portability findings and remediation patterns for a paired code-review and software-factory skill suite.
