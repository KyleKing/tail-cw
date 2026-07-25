# ADR 0012: Export traces instead of drawing them

Date: 2026-07-25 Status: Accepted (narrows the M3 tracing scope set by [ADR 0010](./0010-keep-tail-cw-with-a-narrower-scope.md))

## Problem

Four `claude/*` branches opened in November 2025 proposed three trace features that were never wired into any view: a time-proportional waterfall widget, a service map of call counts and error rates, and a Jaeger JSON export. They predate [ADR 0010](./0010-keep-tail-cw-with-a-narrower-scope.md), which narrowed tail-cw to what only a CloudWatch-native terminal tool can build. This record decides which of the three tail-cw builds, and closes the branches.

The question matters beyond those branches because M3 is the remaining milestone and it is the tracing one. Honeycomb-style trace exploration is the founding requirement in the original brief, so "draw a waterfall" reads as obviously in scope until you check what data would feed it.

## What the evidence says

### A log line is a point, and a span is an interval

This is the finding that decides the record. A waterfall places each span by start time and draws it as wide as its duration, so it needs two numbers per span. A log line gives one timestamp, and services emit their "handled request in 123ms" line when the work finishes rather than when it starts.

The branch waterfall computed each bar's horizontal offset from the log event's timestamp and then extended the bar rightward by a duration read out of the message body. Every bar is therefore displaced right by its own duration. It also mixed two scales, taking the axis total from the difference between log emit timestamps while taking bar widths from the payload's duration field, so one slow span can be wider than the whole chart. Lines carrying no duration at all, which is most of them, each got a one-cell bar. The result is a scatter plot of log lines wearing a waterfall's clothes, and it is wrong in a way a reader cannot see.

X-Ray segment documents carry `start_time`, `end_time`, `parent_id`, and nested `subsegments`, which is exactly the interval and hierarchy data a waterfall needs. So the picture is not the hard part. The span source is.

### Our logs carry no parent links

The roadmap records that `trace_id` does not cross the Hatchet boundary, measured against `read-prod`. Reviewing these branches surfaced a second missing key, and it constrains more features than the first one does.

Every hierarchy feature on the branches keyed off `parent_span_id`. Our services log `trace_id`, `span_id`, and `request_id`, and no parent link. So the service map emits rows only for spans whose parent it can find, and against real data it emits none; its seven tests pass only because the fixtures set the field by hand. The error-propagation walk breaks on its first iteration and always returns a one-element path dressed as a graph traversal.

Worse, `SPAN_ID_FIELDS` read a bare `id` as a span identity, so any row carrying an unrelated entity key looked like a span that another row could claim as a parent. That would have made a future hierarchy feature silently wrong rather than empty, which is the harder failure to notice. It is now removed.

Cross-service joins and span hierarchy are blocked on the same OpenTelemetry instrumentation work in the application repo. "Unblocked" therefore means more than the roadmap's prerequisite currently implies.

### A service map reimplements what X-Ray computes

X-Ray's `GetServiceGraph` returns the topology with per-edge response-time histograms, fault counts, throttle counts, and error counts, computed server-side. Rebuilding a worse version from log parent links we do not have is the measurement ADR 0010 said to watch: whether new code goes into things only tail-cw can do, or into reimplementing what AWS ships.

### Export is portable where a widget is not

Jaeger accepts OTLP over HTTP as JSON, and so does every OTel-compatible backend, including Grafana Tempo and the CloudWatch OTLP endpoint. Jaeger UI has no file-upload path, so the branch's Jaeger v1 JSON file targeted a viewer that cannot open it; OTLP JSON posted to a collector endpoint reaches the same UI and several others. Browser-based OTLP viewers also accept pasted OTLP JSON with no service to run.

One constraint on reading spans back: traces sent to `/aws/spans` under Transaction Search are not retrievable through `BatchGetTraces`, and only the legacy segment store is. The two span sources are mutually exclusive, and which read path is correct depends on whether Transaction Search is on. It is off in our account, so `GetTraceSummaries` into `BatchGetTraces` is the path here, matching the correction already recorded in ADR 0010.

## Decision

tail-cw renders what a terminal is uniquely good at, and exports the rest.

The terminal is good at tailing, browsing, and chronological correlation across log groups. That is the correlation pivot, and it stays the heart of M3. For any view a mature tool already owns, tail-cw emits a standard document and hands off rather than reimplementing the renderer.

Concretely:

- the trace export is **OTLP JSON**, reachable from `tail-cw export trace`, writing to stdout like the other export subcommands. Jaeger v1 JSON is not the target, because it is narrower and its file form opens in nothing
- the **service map is rejected**, permanently rather than deferred. `GetServiceGraph` computes it better, and we lack the parent links to approximate it
- the **waterfall is deferred behind an X-Ray span reader**, not behind a widget. Once `GetTraceSummaries` and `BatchGetTraces` supply real intervals, the drawing is a small pure function; before then it cannot be drawn honestly at all
- the useful residue of the error-propagation feature is a **one-line summary** over the trace tree that already exists (first service to error, how many errors, how many services), which needs no new screen and no parent links

This generalizes ADR 0010 from query features to visualizations. That ADR sends query work to Logs Insights rather than reimplementing it; this one sends rendering work to OTel viewers on the same reasoning.

## Consequences

- the four `claude/*` branches are closed. `service_map.py`, `trace_waterfall.py`, `export/jaeger.py`, `aws/async_client.py`, and the four root-level planning documents are not merged. Their still-live ideas moved into the roadmap and this record
- `tail_cw/export/` is not created. `export` stays a CLI subcommand group with serialization in `cli.py`, per [ADR 0008](./0008-single-interactive-tui.md)
- a trace that crosses services is only whole when every log group it touched is read together. The trace view now reads all selected groups rather than the first, and grouping runs on the blocking pool per [ADR 0011](./0011-async-aws-io-and-blocking-work.md) rather than on the message loop
- the M3 prerequisite gains a second missing join key. Stamping `workflow_run_id` into worker log lines unblocks the cross-service join but not span hierarchy, so it does not unblock a waterfall
- reading X-Ray means a new client kind alongside `logs` and `cloudwatch`. Its read path is a two-call sequence rather than a paginator, so `GetTraceSummaries` must complete before `BatchGetTraces` can be batched
- if Transaction Search is ever enabled on the account, the span read path changes wholesale and this record's API choice must be revisited

## Sources

- [X-Ray GetTraceSummaries and BatchGetTraces](https://docs.aws.amazon.com/xray/latest/devguide/xray-api-gettingdata.html), [segment document schema](https://docs.aws.amazon.com/xray/latest/devguide/xray-api-segmentdocuments.html)
- [X-Ray OTLP endpoint](https://docs.aws.amazon.com/xray/latest/devguide/xray-opentelemetry.html), [X-Ray SDK migration to OpenTelemetry](https://docs.aws.amazon.com/xray/latest/devguide/xray-sdk-migration.html)
- [Jaeger APIs, including OTLP over HTTP as JSON](https://www.jaegertracing.io/docs/1.57/apis/)
- [X-Ray GetServiceGraph](https://docs.aws.amazon.com/xray/latest/api/API_GetServiceGraph.html)
