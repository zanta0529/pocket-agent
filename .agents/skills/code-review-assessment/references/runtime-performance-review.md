# Runtime Performance Review: Streaming Node Services

Use this reference for read-only reviews of Node/Express services that poll a database and broadcast REST/SSE/WebSocket dashboard data.

## Evidence chain

Trace one complete update cycle:

1. Scheduler cadence and reentrancy.
2. Subscription/query-key collection and deduplication.
3. Query normalization, especially defaults added regardless of client input.
4. Service orchestration: sequential versus bounded parallel awaits.
5. Repository operation count, projections, limits, indexes, and result cardinality.
6. Whole-payload comparison and serialization.
7. Broadcast fan-out, per-client serialization, and backpressure.
8. Browser parse and render granularity.

Do not stop at server request latency. A volatile timestamp can make a whole-payload comparator broadcast every cycle, and the client may rebuild every table and chart even when market/business data is unchanged.

## Safe, executable probes

Prefer in-memory probes that import production modules but replace mutable repository methods with deterministic async stubs. They can establish call count, ordering, and amplification without reading or mutating production data.

Useful probes:

- **Repository fan-out:** replace repository methods with call-recording delayed functions; invoke one aggregate service request and report call count/order plus elapsed time. Treat elapsed time as synthetic.
- **Volatile-field dedup failure:** execute identical normalized queries twice with stable repository outputs and a delay crossing the timestamp resolution; compare serialized payloads and identify differing fields.
- **Interval overlap:** replace `tick()` with a delayed function, schedule it faster than its duration, and record maximum concurrent executions.
- **Cache growth:** under `node --expose-gc`, compare heap before/after inserting many unique cache keys. Report entry count and heap delta, while noting real payloads may differ.
- **Serialization scaling:** benchmark `JSON.stringify` over several payload sizes and report bytes plus p50/p95. Never present synthetic results as production SLA.

Inline `node --input-type=module -e` probes avoid creating files during review-only work. Always re-check `git status --short` afterward.

## Amplification formulas

Show assumptions and formulas explicitly:

- `DB ops/sec = operations_per_query × unique_query_keys × (1000 / interval_ms)`
- `wire bytes/min = payload_bytes × updates_per_minute × clients`
- Initial-load operations should include bootstrap snapshot, transport subscription snapshot, panel-specific loaders, and reconnect-triggered snapshots.

Separate confirmed static counts from estimates. For example, “11 repository calls are confirmed by a stubbed execution; 5.5 ops/sec per unique key assumes a 2,000ms interval and one Mongo operation per repository call.”

## High-value findings checklist

- Async `setInterval` permits overlapping ticks; require single-flight, skip, or coalescing.
- Aggregate service serially awaits independent DB calls.
- Unbounded `Promise.all` fans unique query keys into a smaller DB pool.
- Normalization turns an empty query into expensive defaults or makes empty-query validation ineffective.
- Timestamps, random IDs, or generated versions participate in change detection.
- Snapshot maps retain inactive query/symbol keys indefinitely.
- `res.write()` return value is ignored; no `drain` handling.
- WebSocket `bufferedAmount` is ignored; no slow-client policy.
- Identical events are `JSON.stringify`-ed separately for every client.
- Initial page load fetches an aggregate snapshot and then repeats every panel request.
- Reconnect fetches before subscribing, while subscribe also sends a fresh snapshot.
- Every update recreates tables, pagination, or charts rather than applying section-level diffs.
- Repository `find(...).toArray()` has no limit and performance depends on unknown cardinality/indexes.

## Factory-plan-ready acceptance criteria

Prefer behavioral and measurable criteria over invented latency budgets:

- At most one update tick is active at a time.
- No full dashboard update occurs when business data is unchanged.
- Identical section/query work executes once per tick.
- Initial load performs one authoritative bootstrap snapshot.
- Slow clients cannot accumulate unbounded buffered data.
- Inactive cache entries are evicted within a defined TTL or size budget.
- Client updates redraw only changed sections.
- Integration benchmarks record tick p50/p95/p99, DB operation count, pool wait, event-loop lag, heap/cache size, payload bytes, and backpressure events.

## Reporting discipline

Clearly separate:

- **Static proof:** source paths, line numbers, control flow, operation counts.
- **Synthetic measurement:** mocks/stubs proving mechanics and growth direction.
- **Production unknowns:** real data cardinality, indexes, pool contention, concurrency, network, and client hardware.

A passing placeholder test script is not a performance gate. Report its exact output and identify the missing regression/benchmark coverage without treating exit code 0 as meaningful validation.
