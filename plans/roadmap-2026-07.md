# Roadmap: make tail-cw a daily-driver CloudWatch tool

Written 2026-07-05 from a code capability review and a survey of the CloudWatch tooling landscape. Pruned 2026-07-25: M0, M1, M2, M4, and M5 are delivered, so their planning detail moved into the ADRs that record the decisions, and what remains below is what is still ahead.

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
- **dropped:** reimplementing LogsQL (`stream_context before N after N`, `unpack_json`) in the local engine. Logs Insights does this server-side now, and maintaining a second query language is the kind of cost ADR 0010 exists to avoid
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
