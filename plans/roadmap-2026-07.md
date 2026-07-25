# Roadmap: make tail-cw a daily-driver CloudWatch tool

Written 2026-07-05 from a code capability review and a survey of the CloudWatch tooling landscape. Pruned 2026-07-25: M0, M1, M2, M4, and M5 are delivered, so their planning detail moved into the ADRs that record the decisions, and what remains below is what is still ahead. Also on 2026-07-25, four dormant `claude/*` branches from November 2025 were read for anything still live; what survived is folded in below, and [ADR 0012](../docs/docs/adr/0012-export-traces-instead-of-drawing-them.md) records what was rejected.

## Delivered

| Milestone                     | Outcome                                                                                                                        | Record                                                                                                                                        |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------- |
| M0 wire the drivetrain        | argparse dispatch, fetch through the Parquet cache into the TUI, profile and region threaded through                           | [0002](../docs/docs/adr/0002-cli-first-layered-architecture.md), [0003](../docs/docs/adr/0003-parquet-cache-and-local-query-engine.md)        |
| M1 live tail                  | `StartLiveTail` with reconnects and sampling, ring-buffered rendering, one filter model across live, historical, and cached    | [0004](../docs/docs/adr/0004-live-tail-via-startlivetail.md)                                                                                  |
| M2 navigation-first discovery | group browser as the home screen, resolution ladder, ten-group multi-select, content previews, recents and presets             | [0008](../docs/docs/adr/0008-single-interactive-tui.md)                                                                                       |
| M4 dashboards and metrics     | `GetDashboard` import, `metrics[]` shorthand translated to `GetMetricData`, native plotext charts, dive from a chart into logs | [0005](../docs/docs/adr/0005-dashboards-metrics-and-terminal-charts.md), [0006](../docs/docs/adr/0006-dashboard-rendering-and-interaction.md) |
| M5 async AWS I/O              | aiobotocore throughout, session-scoped client pool, bounded pool for DuckDB/Polars, cancellation that actually stops requests  | [0011](../docs/docs/adr/0011-async-aws-io-and-blocking-work.md)                                                                               |

Two deviations from the original plans are worth carrying forward, because they change what a reader should expect to find. There is no `tail-cw groups` subcommand: the browser is the home view and `tail-cw export groups` covers the NDJSON case. And log groups do not sort by last-event time, because `DescribeLogGroups` does not return it (that would be a `DescribeLogStreams` call per group); your own selection recency sorts the list instead, which is what the console's "recently accessed" actually gives you.

## Next: M3 investigation tools

Rescoped on 2026-07-25 by [ADR 0010](../docs/docs/adr/0010-keep-tail-cw-with-a-narrower-scope.md). The rule is now: build what only a CloudWatch-native terminal tool can build, and send the rest to Logs Insights, which grew roughly fifty new commands across June and July 2026 and got GA PPL, SQL, JOIN, and sub-queries.

- **correlation-ID pivot (the headline).** Select a request, trace, or Hatchet `workflow_run_id` in any event and fan out across related log groups, building on `query/trace.py`. Much cheaper than when this was scheduled: the multi-group fan-out and the timestamp-merged read already exist from the M2 browser work, so this is mostly wiring
- **spans from X-Ray, not from `aws/spans`.** Measured in the prod account on 2026-07-25: Transaction Search is off (`get-trace-segment-destination` returns `Destination: XRay`), so `aws/spans` does not exist, while X-Ray already carries about 3,800 traces an hour including `hatchet-server` and an `execution_loop.lag_spike` service. Read the X-Ray API directly rather than enabling Transaction Search, which would duplicate every span into CloudWatch Logs at ingest cost. This is what the lifted tracing gate now means
- **Logs Insights as a backend, not a reimplementation.** Surface `pattern`, `diff`, `stats by`, and context windows by sending a query to `StartQuery` with a scan-size estimate shown first, at $0.005/GB scanned. The local DuckDB and Polars engine keeps the job it is better at, which is free re-filtering over an already-cached Parquet window
- **time-bucketed histogram of the current view.** Partly built: `bucket_event_counts` in `tail_cw/preview.py` already powers the dashboard log-volume sparklines
- **TODO: easier-to-read JSON logs.** The log table shows raw JSON in the message column, so nested payloads read as one long escaped line. `format_log_event_detail_with_json` in `log_viewer.py` already pretty-prints the detail view; the table row itself is the gap. Worth checking `tail-jsonl` for field-selection and formatting logic to adapt rather than reinvent
- **`tail-cw export trace` as OTLP JSON.** Per [ADR 0012](../docs/docs/adr/0012-export-traces-instead-of-drawing-them.md), hand the trace to an OTel viewer rather than drawing it here. Cheapest if it takes a trace ID plus log groups and does the fan-out itself, which makes it the first M3 deliverable rather than a bolt-on
- **an error summary over the trace tree.** First service to error, total errors, services touched, as one status line in `TraceViewerScreen`. Needs no new screen and no parent links, unlike the propagation-path walk that ADR 0012 rejected
- **X-Ray span reader,** `GetTraceSummaries` into `BatchGetTraces`. Segment documents carry `start_time`, `end_time`, and `parent_id`, so this is what makes an honest waterfall possible later. Note `/aws/spans` and `BatchGetTraces` are mutually exclusive span sources
- **dropped:** reimplementing LogsQL (`stream_context before N after N`, `unpack_json`) in the local engine. Logs Insights does this server-side now, and maintaining a second query language is the kind of cost ADR 0010 exists to avoid
- **dropped:** an in-terminal service map, and a waterfall drawn from log timestamps. Both rejected in ADR 0012
- later: matcher hooks to auto-link events to Sentry/PostHog issues

New AWS calls here are async and take an open client from the pool, per [ADR 0011](../docs/docs/adr/0011-async-aws-io-and-blocking-work.md). `StartQuery` is a poll loop, so it wants an async wait rather than a thread.

### Prerequisite: trace context does not cross the Hatchet boundary yet (measured 2026-07-25)

The correlation pivot can only join on a key present in both places, and today there is none. Measured against `read-prod` over a three-hour window:

| Key                   | `irm-ecs-api-prod` | `irm-prod-ecs-hatchet-workers` | Shared |
| --------------------- | ------------------ | ------------------------------ | ------ |
| distinct `trace_id`   | 100                | 22                             | **0**  |
| distinct `request_id` | 100                | 23                             | **0**  |

No `workflow_run_id` appears in worker logs at all. The cause shows in the IDs: 100 of 100 API trace IDs are timestamp-prefixed X-Ray style (`6a648fb4…`), and 0 of 22 worker IDs are, so the two sides run different ID generators and produce disjoint trace-ID spaces.

The good news is that both sides already log structured JSON carrying `trace_id`, `span_id`, and `request_id`, and `trace_id` is already first in `DEFAULT_TRACE_ID_FIELDS`. So the tooling side is ready and the gap is instrumentation, in the application repo rather than here:

1. enable Hatchet's OTel instrumentor (`hatchet-sdk[otel]`, `HatchetInstrumentor().instrument()`) on both the triggering API and the consuming workers, which injects and reads W3C `traceparent` through task metadata automatically
1. align both sides on `AwsXRayIdGenerator` so the ID formats match and X-Ray keeps accepting them
1. failing that, stamp `Context.workflow_run_id` into worker log lines and join on that instead, which is weaker because it does not reach the API

Until one of those lands, build and test the pivot against a single service's log groups, where trace IDs do correlate.

A second missing key turned up on 2026-07-25 while reading the dormant branches, and it blocks more than the first one does. Our services log `trace_id`, `span_id`, and `request_id`, and no `parent_span_id` anywhere. So span hierarchy is unavailable even inside one service, which is why ADR 0012 defers the waterfall to X-Ray segments rather than to log lines. Option 3 above unblocks the cross-service join without unblocking hierarchy; only options 1 and 2 reach both.

## Harvested from the dormant branches (2026-07-25)

Four `claude/*` branches carried about 3,300 lines of planning documents written in November 2025. Their factual claims about the codebase are stale and their landscape analysis is superseded by the section below, so the documents are not merged. These are the items that survived the read, grouped by where they land. None is scheduled; they are ordered within each group by value against effort.

**The filter surface.** The AST already holds `OR`, `NOT`, and `combine_filters`, and no surface syntax reaches them, so `ERROR OR WARNING` parses as three text terms including the literal `OR`. Completing that is the largest gap. It carries one real decision rather than a coding cost: CloudWatch's own filter pattern syntax has no `OR` for text terms, so a filter accepted locally would fail when sent as a server-side `filterPattern`, and a filter that works on cached data but not live data is worse than no `OR` at all. Settle the divergence before writing the parser. Smaller items, in order: a `FilterParseError` carrying suggestions (unbalanced brace, `$..`, odd quote count, and `/re/` where the delimiter is `%re%`), which today surfaces as terse bare `ValueError` text; named filter presets extending the `@name` convention `[presets]` already uses for group sets, so the shipped patterns are ones the codebase owns and tests; persisted per-profile filter history written in `recents.py`'s idiom (frozen dataclass, pure record function, atomic replace, degrade on corruption); and a `FILTER_GUIDE.md`, since the syntax currently lives only in `parser.py` docstrings.

**Cache and query performance.** Every JSON log line is decoded by Python, re-encoded by Python, then decoded again by Polars: `_log_events_to_ndjson_file` calls `json.loads` per event and `scan_ndjson` re-parses the same bytes. Moving the decode into the lazy pipeline as `str.json_decode` is the fix, and the original brief asked for parsing "not in Python", so this is the founding requirement going unmet rather than a nice-to-have. Two obstacles the obvious version misses: `jsonl_events` is a returned count feeding cache metadata and becomes a null count on the frame, and `is_jsonl_message` strips a leading timestamp prefix, so the expression needs a `str.replace` first and non-JSON lines need to null rather than error. Beyond that: incremental cache extension and resumable fetch, which needs its own ADR because [ADR 0011](../docs/docs/adr/0011-async-aws-io-and-blocking-work.md) deliberately orphans truncated Parquet files on cancellation and "resume" has to mean something against that contract; a `tail-cw cache status` for size and hit rate, since no cache introspection exists; and benchmark targets gated in CI, because ADR 0003's claim that the local engine is better at re-filtering is currently unmeasured. Memory-aware backend selection was proposed on the grounds that DuckDB spills and Polars does not, but the Polars path already does `scan_parquet` into `collect(engine='streaming')`, so the premise is weak; if pursued, use a configured byte ceiling rather than adding `psutil`.

**Discovery.** Group metadata is done: stored bytes, retention, and creation time are read and displayed, and the sampled preview clusters messages into distinct shapes. Three things are still open. The preview already computes per-shape skeletons with literal keys and placeholder values, so merging them into one field roster ("here are this group's JSON fields, and how many events carry each") is a pure function over `list[MessagePattern]` and would also feed filter-field completion. Last-write time has an honest cheap form and a dishonest one: the sample already receives timestamps and throws them away, but `FilterLogEvents` yields ascending and the sample is capped, so a busy group's newest sampled timestamp sits early in the window and the busiest groups would read as the stalest. Ship it as an activity indicator (saturated, an exact time, or quiet) rather than as a timestamp, or record the newest timestamp per group as a `write_payload` entry so it survives a restart. `logGroupClass` is returned by `DescribeLogGroups` and dropped; it is worth a column because Infrequent Access groups cannot be live-tailed.

**Plumbing and tooling.** `ProgressCallback` is defined twice with different arity, in `cache/storage.py` as `(current, total, status)` and in `aws/client.py` as `(count, message)`. Unify those before wiring fetch and Parquet-convert progress into the TUI, because one worker has to feed both; today `ProgressUpdate` exists but its only producer is DataTable row insertion, so the two long operations run silent. Separately, CI runs the test suite on macOS and Windows only, never on Linux, and only on 3.11 despite the classifiers claiming 3.13.

**Declined.** YAML config: TOML stays the only format, because a second format means a second parser, an optional dependency, and a forked document for no capability gain. That choice was never written down, which is the actual gap. A config wizard prompting on stdin conflicts with one TUI and one NDJSON surface, and `create_default_config_file` already scaffolds commented TOML. A visual modal filter builder is rejected: it duplicates the `:` command line and which-key discovery ADR 0008 chose, and the branch's version crashed on compose because it passed filter patterns as Textual widget IDs. Log group mappings in config are already covered by `[presets]`. Multi-backend log sources and generic in-TUI AI summarization stay rejected per ADR 0010.

## Landscape (why build this at all)

Dedicated CloudWatch tailers (awslogs, saw, cw, utern) are abandoned or dormant since 2019-2023 and predate the Live Tail API and the newer Insights query languages. Gonzo is a strong log-analysis TUI with no native CloudWatch source. The official AWS CloudWatch MCP server covers Insights and pattern analysis for agents but has no live tail and no human surface. Grafana's CloudWatch data source is the honest answer for anyone who wants a web GUI.

What is still unclaimed, and so still worth our time: a real TUI over `StartLiveTail`, log group discovery with metadata in the terminal, one filter model shared across live and historical and cached data, and a correlation-ID pivot across log groups. The first three shipped; the fourth is M3.

Explicitly not worth building: plain multi-group colored tailing (solved by `aws logs tail`, cw, utern), generic in-TUI AI summarization (Gonzo does this over piped input), and a second query language (ADR 0010).

## Design principles

- Every feature lands CLI-first with NDJSON output under `tail-cw export`; the TUI is a view over the same functions, so agents and humans drive one code path
- Frugal by default: cache everything fetched as Parquet, re-filter locally for free, keep default time ranges tight, and show a scan estimate before any paid Logs Insights query
- Keep logic in pure functions with side effects at the edges (per AGENTS.md). The async migration reinforced this: `aws/` functions take an open client and the pure translation code stayed untouched
- Each milestone ships with ruff, mypy, pyright, and pytest green before the next starts

## Sequencing rationale

The remaining sequencing question is inside M3, not across milestones. Build the correlation pivot against a single service first, because the cross-service join is blocked on instrumentation in another repo and waiting on it would stall the whole milestone. X-Ray span reading is independent of that block, so it can proceed in parallel.

Scope growth is the standing risk ADR 0010 named: `tail_cw/` grew 82% in the nineteen days to 2026-07-25. Prefer wiring existing machinery over new subsystems, and prefer sending work to Logs Insights over reimplementing it.
