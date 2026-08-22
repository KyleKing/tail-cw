# ADR 0013: Read X-Ray directly for spans, and cap what a scan costs

Date: 2026-08-22 Status: Accepted (delivers the span source
[ADR 0012](./0012-export-traces-instead-of-drawing-them.md) deferred to)

## Problem

[ADR 0012](./0012-export-traces-instead-of-drawing-them.md) rejected a waterfall drawn
from log timestamps, because a log line carries one timestamp and our services log no
`parent_span_id` anywhere.
It named X-Ray segment documents as the source that would make an honest waterfall
possible, and left the reading of them to M3.
This record decides how they are read and what guards the reading needs.

Two questions had to be settled first.
Whether to enable Transaction Search, which duplicates every span into CloudWatch Logs
as `aws/spans` at ingest cost, or to read the X-Ray API.
And whether a filter expression makes a query cheap enough that the tool can leave the
window wide.

## What the evidence says

### The hierarchy is real, and half the spans are X-Ray's own invention

Measured against `read-prod` on 2026-08-22.
A 21-segment trace flattens to 42 spans with one root, zero orphans, and durations from
0.0ms to 50ms, so `parent_id` and the nested `subsegments` give a complete tree.

Twenty of those 42 spans are `inferred`: X-Ray synthesizes a segment for each downstream
resource it decides a call reached.
An inferred segment is named after the call that reached it, so reading `name` as the
service made the trace look like four services called `pool.acquire`,
`copy_from MessageQueueItem`, and `query SELECT PG_NOTIFY12`.
Its `origin` (`Database::SQL`) is the only honest swimlane it has.
The service name for a real segment is not `name` either: it sits in
`metadata.default["otel.resource.service.name"]`, and a subsegment carries none at all
and has to inherit its parent's.

Timings on an inferred span are the caller's view of the work rather than the work's own
account of itself, which is why the waterfall dims them and the OTLP export marks them
`aws.xray.inferred`.

### A filter expression narrows the result and not the bill

`GetTraceSummaries` returns `TracesProcessedCount`, documented as "the total number of
traces processed, including traces that did not match the specified filter expression".
Traces scanned and traces retrieved both cost $0.50 per million.
The first million each month is free.

So the intuition that a narrow expression makes a wide window safe is wrong, and it is
the expensive direction to be wrong in.
A three-hour window on this account holds 442,828 traces: one uncapped
`export xray` spent 44% of the month's free tier and took over two minutes, while the
same window capped at 1,000 traces scanned 3,825 and returned in seconds.

That is the whole case for the cap.
`--limit` defaults to 1,000 and stops paging when it is reached, and the command reports
what it scanned and what that costs on stderr, in the same shape the Insights scan
estimate already uses.
Only a shorter window is cheaper; the expression only saves bytes over the wire.

### Transaction Search stays off

Measured on 2026-07-25 and unchanged: `get-trace-segment-destination` returns
`Destination: XRay`, so `/aws/spans` does not exist in this account.
Enabling Transaction Search would duplicate every span into CloudWatch Logs at ingest
cost, and 148,000 traces an hour makes that the wrong direction.
`/aws/spans` and `BatchGetTraces` are mutually exclusive span sources anyway, so reading
the API costs nothing extra in complexity.

### What the account actually holds, which limits what this unlocks

97% of those 442,828 traces are Hatchet's own background loops (`hatchet.run/snapshot`,
`hatchet.run/concurrency-manager`, `hatchet.run/pgmq-read-messages`).
Only 3,554 carry an HTTP URL, and most of those are health checks.

`http.url CONTAINS "radar_event"` matches nothing over three hours, so the endpoint at
the centre of the open latency investigation is invisible here while `/v1/import`,
`/v1/work_queue_item`, and the health checks are traced.
The reader works; the coverage is an application-side gap, recorded with the rest of the
instrumentation prerequisites in
[the roadmap](../../../plans/roadmap-2026-07.md).

## Decision

Read `GetTraceSummaries` and `BatchGetTraces` directly, five trace ids per request with
the requests overlapping, and leave Transaction Search off.

Keep the records in `tail_cw/aws/xray.py` and the geometry in `tail_cw/waterfall.py`, so
neither imports Textual and both are unit-testable without a terminal.
`tail-cw export xray` writes summaries as NDJSON for pivoting, `export xray-trace`
writes segment documents as OTLP JSON, and `:xray <id>` draws the waterfall.

Cap the scan by default and price it out loud.

## Consequences

A waterfall exists now, and it earns
[ADR 0012](./0012-export-traces-instead-of-drawing-them.md)'s
distinction: the picture is the service's own account of what waited on what, not an
inference from when lines were written.
The first real trace it drew showed a 1.29s `POST /v1/import/slug` with 216ms of auth
and most of the rest unaccounted for.
That is a gap in instrumentation the picture makes obvious and a log search does not.

The slowest chain is marked, and it is a heuristic rather than a critical path: siblings
can run in parallel, so the chain names where the time went without proving nothing else
could have caused it.

The cap is a real limit rather than a formality.
An account with 148,000 traces an hour cannot be swept, so anything that wants a
population rather than a sample (latency percentiles
per route, for instance) needs the window narrow and the reading deliberate.
