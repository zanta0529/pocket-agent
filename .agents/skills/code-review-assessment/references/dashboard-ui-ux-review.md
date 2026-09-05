# Dashboard UI/UX Review Evidence Guide

Use this reference for read-only reviews of dense operational dashboards, real-time monitoring pages, or table-heavy admin interfaces. The goal is evidence that can be pasted directly into a remediation/factory plan.

## Evidence collection

1. Read semantic HTML, CSS cascade order, state/rendering code, transport code, and reusable table/chart components together.
2. Run the UI when feasible, including a deliberately failing API/static-host scenario. Record what the operator actually sees; distinguish runtime observation from source-inferred successful-path behavior.
3. Inspect live DOM/computed state for claims that snapshots can miss:
   - unresolved loading indicators;
   - status/ARIA attributes;
   - missing anchor targets;
   - hidden native controls;
   - computed table layout and overflow behavior.
4. Check worktree status before and after any non-mutating runtime verification.

## High-value traces

### State truthfulness

Trace every `loading -> success | empty | error | stale` transition. Check whether a status function actually consumes the message arguments passed by callers. A transport label is not a connection/error status. Verify panel-level retry, last-success timestamp, stale-data disclosure, and disabled/pending actions.

### Request and render fan-out

Trace initialization, global refresh, individual refresh, and stream updates separately. Flag patterns where:

- one snapshot is followed by all individual endpoint loaders;
- an individual panel refresh reconnects the whole stream;
- a stream event rerenders all panels regardless of scope;
- tables rebuild headers/body/pagination on each tick;
- chart instances are destroyed and recreated rather than updated;
- search rerenders on every keystroke without debounce;
- an unbounded “show all” option creates every row.

State source-proven fan-out as a code-path risk unless it was measured against a working backend.

### Accessibility and keyboard operation

Prioritize interactive semantics, not merely labels:

- sortable headers should contain buttons and expose `aria-sort`;
- custom radio/segmented controls must remain focusable (visually hidden, not `display:none`);
- drawers need `nav` semantics, `aria-expanded`, `aria-controls`, Escape handling, focus entry/return, and valid anchors;
- pagination/search/goto controls need explicit accessible names;
- live data, connection status, and errors need restrained `aria-live`/status/alert behavior;
- canvas charts need an accessible name plus a textual summary or equivalent data table;
- row states must not rely on background color alone.

### Responsive data density

Do not accept `overflow-x:auto` alone as proof of usable responsiveness. Check the table’s computed layout, width/min-width, column count, word breaking, sticky columns, and touch target size. For wide market tables, plan either priority columns with expandable details or a deliberate min-width horizontal-scroll model with a visible affordance.

### Information architecture

Count top-level workspaces and identify whether all sections are flattened into one long page. Recommend grouping by operator task (for example overview, browse, instrument detail, exception monitoring), with a global connection/status bar and section navigation. Verify that exposed controls have implemented behavior; dead settings are a UX defect.

### CSS and maintainability

Inspect inline CSS, external stylesheet order, `!important` usage, duplicated tokens, and broad global selectors. Conflicting style sources make visual acceptance brittle. Recommend layered tokens/layout/components/states/responsive CSS and module boundaries when a single HTML file combines thousands of lines of styles, markup, and behavior.

## Plan-ready finding format

For each finding provide:

1. Priority (`P0` blocking operator feedback/core interaction, `P1` important accessibility/responsiveness/performance, `P2` structure/polish).
2. Problem statement.
3. Exact file and line evidence.
4. Runtime observation, if any, explicitly labeled.
5. Operator/user impact.
6. Target interaction/state design, not just “improve UX.”
7. Given/When/Then acceptance criteria.
8. Verification method (keyboard/screen reader, viewport matrix, mocked state, request count, DOM mutations, Performance panel).

Preserve API, stream schema, domain calculations, and data freshness as explicit invariants unless the user authorizes backend changes.
