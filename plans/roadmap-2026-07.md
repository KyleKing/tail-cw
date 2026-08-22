# Roadmap: make tail-cw a daily-driver CloudWatch tool

Written 2026-07-05 from a code capability review and a survey of the CloudWatch tooling landscape. Pruned 2026-07-25: M0, M1, M2, M4, and M5 are delivered, so their planning detail moved into the ADRs that record the decisions. Reordered 2026-08-21 after the tool was driven through six real production investigations against Coverbase's prod and stage accounts; the queue below is that audit's output, and the evidence for each item is in [the evaluation](evaluation-2026-08-21-vs-aws-cli.md) and [the TUI critique](tui-critique-2026-08-21.md).

## Delivered

| Milestone                      | Outcome                                                                                                                           | Record                                                                                                                                        |
| ------------------------------ | --------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| M0 wire the drivetrain         | argparse dispatch, fetch through the Parquet cache into the TUI, profile and region threaded through                              | [0002](../docs/docs/adr/0002-cli-first-layered-architecture.md), [0003](../docs/docs/adr/0003-parquet-cache-and-local-query-engine.md)        |
| M1 live tail                   | `StartLiveTail` with reconnects and sampling, ring-buffered rendering, one filter model across live, historical, and cached       | [0004](../docs/docs/adr/0004-live-tail-via-startlivetail.md)                                                                                  |
| M2 navigation-first discovery  | group browser as the home screen, resolution ladder, ten-group multi-select, content previews, recents and presets                | [0008](../docs/docs/adr/0008-single-interactive-tui.md)                                                                                       |
| M4 dashboards and metrics      | `GetDashboard` import, `metrics[]` shorthand translated to `GetMetricData`, native plotext charts, dive from a chart into logs    | [0005](../docs/docs/adr/0005-dashboards-metrics-and-terminal-charts.md), [0006](../docs/docs/adr/0006-dashboard-rendering-and-interaction.md) |
| M5 async AWS I/O               | aiobotocore throughout, session-scoped client pool, bounded pool for DuckDB/Polars, cancellation that actually stops requests     | [0011](../docs/docs/adr/0011-async-aws-io-and-blocking-work.md)                                                                               |
| M6 aggregation surface         | `export summary` with fuzzy-merged pattern rollups, `export insights`, `export alarms --history`, `export metrics`, CPU budget    | this file's queue, plus [the evaluation](evaluation-2026-08-21-vs-aws-cli.md)                                                                 |
| M7 cache v2 and one front door | segmented cache windows, a schema at 25 bytes per event, rollup/alarms/Insights in the TUI behind a cost gate, one shared history | [0003](../docs/docs/adr/0003-parquet-cache-and-local-query-engine.md), [0008](../docs/docs/adr/0008-single-interactive-tui.md)                |

Two deviations from the original plans are worth carrying forward, because they change what a reader should expect to find. There is no `tail-cw groups` subcommand: the browser is the home view and `tail-cw export groups` covers the NDJSON case. And log groups do not sort by last-event time, because `DescribeLogGroups` does not return it (that would be a `DescribeLogStreams` call per group); your own selection recency sorts the list instead, which is what the console's "recently accessed" actually gives you.

## Next up, in priority order

Six investigations on 2026-08-21 produced five new commands and four correctness fixes, and a ranking of what the tool got wrong. Items 1 to 4 of that ranking shipped on 2026-08-22 (see Delivered above); what remains is below, still in order.

### 1. Cost preflight for Insights

[ADR 0010](../docs/docs/adr/0010-keep-tail-cw-with-a-narrower-scope.md) and this file both promised a scan estimate before any paid query. Today the size prints after the bill is incurred: `292,676 of 3,293,189 records matched, 1.149 GB scanned`. There is no AWS preflight API, and the console's own estimate is not exposed, so estimate it from what `DescribeLogGroups` already returns: `storedBytes` divided by the retention period gives bytes per day, multiplied by the requested window and the group count gives an honest order of magnitude. Show it, and the dollar figure at $0.005/GB, before `StartQuery`. Label it an estimate, because stored bytes are compressed and Insights bills uncompressed, so it reads low.

### 2. An entry point that takes a trace ID

`TraceViewerScreen` works and is reachable with `t` from the log view, and it was useless for the investigation that needed it, because the real workflow starts with an identifier pasted out of a Slack alarm and there is no way to hand the tool one. That gap cost four hand-written Insights queries on the Bedrock `ReadTimeoutError` investigation. Add `:trace <id>` in the TUI and `tail-cw export trace <id>` emitting OTLP JSON per [ADR 0012](../docs/docs/adr/0012-export-traces-instead-of-drawing-them.md). The fan-out and the timestamp-merged read already exist from the M2 browser work, so this is an entry point rather than a subsystem. An error summary over the trace (first service to error, total errors, services touched) is one status line in the same screen and needs no parent links.

### 3. Lazy imports so the CLI starts in a tenth of a second

`tail-cw --help` takes 0.34s against 0.03s for a bare interpreter, and 277ms of that is import: aiobotocore at 86ms, Polars at 34ms, and `beartype.typing` at 31ms, none of which argparse needs to reject a typo. Defer them behind the dispatch and the startup penalty against the AWS CLI (0.35s per invocation, measured) mostly disappears, which matters because the tool gets called in loops from shell scripts. One constraint: `apply_native_thread_limits()` must still run before anything imports Polars, and moving the Polars import later makes that easier rather than harder.

### 4. The log table, the most-looked-at and least-designed surface

Detailed in [the critique](tui-critique-2026-08-21.md). Responsive column widths, drop `log_group` when the view has one group, drop `log_stream` and `event_id` below roughly 120 columns since the detail pane shows both in full, truncate with `…` everywhere a fixed width can clip, and colour the message by `query.severity.event_severity` with a level glyph so it survives `NO_COLOR`. At 80x24 the Message column currently renders 12 characters wide, which makes the view unusable over SSH from a phone. The severity question worth settling first: colour only records carrying an explicit `level`, or colour inferred ones too. A wrong colour in a live table is worse than no colour.

### 5. Metric dimension discovery

The latency investigation could not name the slow endpoint from metrics, because `ApiRequestLatencyMs` carries only `Method` and `StatusClass` and no route, and there was no way to learn that without reading the emitter's source. `ListMetrics` returns the dimension sets a metric actually publishes. Surfacing them turns "which dimensions does this metric have" from a code-reading exercise into a command, and it is the blocker that recurred most across the six investigations.

### What items 1 to 4 measured after shipping

Worth keeping, because two of the four predictions were wrong in the useful direction:

- **disk.** The schema predicted 38% of v1. On the same log group it landed at 25 bytes per event against 72, so 35%, and the whole local cache fell from 119 MB to 3.2 MB once the superseded v1 entries were reclaimed
- **repeat latency.** A cold hour costs 24.6s. Repeating it with a window that has moved costs 6.3s, because the aligned interior is reused and only the ragged ends and the unsettled tail are refetched. The 13.8x figure needed identical ISO timestamps; 3.9x is what a relative window actually gets
- **the filter.** Dropping it from the key made a local filter mandatory, which exposed a real defect: Polars widens the `parsed` struct across the whole file, so re-encoding one record emits every field any record had. A search for `ERROR` matched all 100,292 events in an hour because some other event carried `error_type`. Null fields are stripped from the search text now, and the same query returns 125
- **two dead keybindings.** `q` never quit and Enter never opened the record detail. Both were bindings whose action the screen did not have or whose key a child widget had already claimed, and both had passing tests, because the tests called the action instead of pressing the key. A binding is not covered until a test presses the key

## Then: M3 investigation tools

Rescoped on 2026-07-25 by [ADR 0010](../docs/docs/adr/0010-keep-tail-cw-with-a-narrower-scope.md). The rule is now: build what only a CloudWatch-native terminal tool can build, and send the rest to Logs Insights, which grew roughly fifty new commands across June and July 2026 and got GA PPL, SQL, JOIN, and sub-queries. Items 3, 5, and 6 above were the front of this milestone; what remains:

- **correlation-ID pivot.** Select a request, trace, or Hatchet `workflow_run_id` in any event and fan out across related log groups, building on `query/trace.py`. Blocked cross-service by the instrumentation gap below; build it against a single service's groups first
- **spans from X-Ray, not from `aws/spans`.** Measured in the prod account on 2026-07-25: Transaction Search is off (`get-trace-segment-destination` returns `Destination: XRay`), so `aws/spans` does not exist, while X-Ray already carries about 3,800 traces an hour including `hatchet-server` and an `execution_loop.lag_spike` service. Read the X-Ray API directly rather than enabling Transaction Search, which would duplicate every span into CloudWatch Logs at ingest cost
- **X-Ray span reader,** `GetTraceSummaries` into `BatchGetTraces`. Segment documents carry `start_time`, `end_time`, and `parent_id`, so this is what makes an honest waterfall possible later. Note `/aws/spans` and `BatchGetTraces` are mutually exclusive span sources
- **time-bucketed histogram of the current view.** Partly built: `bucket_event_counts` in `tail_cw/preview.py` powers the dashboard log-volume sparklines, and `rollup.py` now owns the bucketing the histogram needs
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

The 2026-08-21 rollup found a second, harder fact about the same boundary: worker OTLP export is failing outright, 334 errors an hour to `hatchet.hatchet.local:7070` with `StatusCode.UNIMPLEMENTED`, so worker spans are not reaching any collector. Fix that before measuring the join again, because the current disjointness may be partly an artifact of spans never landing.

Both sides already log structured JSON carrying `trace_id`, `span_id`, and `request_id`, and `trace_id` is already first in `DEFAULT_TRACE_ID_FIELDS`. So the tooling side is ready and the gap is instrumentation, in the application repo rather than here:

1. enable Hatchet's OTel instrumentor (`hatchet-sdk[otel]`, `HatchetInstrumentor().instrument()`) on both the triggering API and the consuming workers, which injects and reads W3C `traceparent` through task metadata automatically
1. align both sides on `AwsXRayIdGenerator` so the ID formats match and X-Ray keeps accepting them
1. failing that, stamp `Context.workflow_run_id` into worker log lines and join on that instead, which is weaker because it does not reach the API

A second missing key blocks more than the first one does. Our services log `trace_id`, `span_id`, and `request_id`, and no `parent_span_id` anywhere. So span hierarchy is unavailable even inside one service, which is why ADR 0012 defers the waterfall to X-Ray segments rather than to log lines. Option 3 above unblocks the cross-service join without unblocking hierarchy; only options 1 and 2 reach both.

## Backlog

Ordered within each group by value against effort. Nothing here is scheduled.

**The filter surface.** The AST already holds `OR`, `NOT`, and `combine_filters`, and no surface syntax reaches them, so `ERROR OR WARNING` parses as three text terms including the literal `OR`. Completing that is the largest gap. It carries one real decision rather than a coding cost: CloudWatch's own filter pattern syntax has no `OR` for text terms, so a filter accepted locally would fail when sent as a server-side `filterPattern`, and a filter that works on cached data but not live data is worse than no `OR` at all. Settle the divergence before writing the parser, and note that queue item 2 changes the stakes, because dropping `filter_pattern` from the cache key moves filtering local by default and makes local-only syntax defensible. Smaller items, in order: a `FilterParseError` carrying suggestions (unbalanced brace, `$..`, odd quote count, and `/re/` where the delimiter is `%re%`), which today surfaces as terse bare `ValueError` text; named filter presets extending the `@name` convention `[presets]` already uses for group sets; persisted per-profile filter history, which should share whatever storage queue item 3 builds for query history rather than inventing a second one; and a `FILTER_GUIDE.md`, since the syntax currently lives only in `parser.py` docstrings.

**Cache and query performance.** Every JSON log line is decoded by Python, re-encoded by Python, then decoded again by Polars: `_log_events_to_ndjson_file` calls `json.loads` per event and `scan_ndjson` re-parses the same bytes. Moving the decode into the lazy pipeline as `str.json_decode` is the fix, and the original brief asked for parsing "not in Python", so this is the founding requirement going unmet rather than a nice-to-have. Do it while item 1 is already rewriting the write path. Two obstacles the obvious version misses: `jsonl_events` is a returned count feeding cache metadata and becomes a null count on the frame, and `is_jsonl_message` strips a leading timestamp prefix, so the expression needs a `str.replace` first and non-JSON lines need to null rather than error. Beyond that: `tail-cw cache status` for size and hit rate, since no cache introspection exists and the cache sits at 115 MB against a 1000 MB limit with no way to see either; and benchmark targets gated in CI, because ADR 0003's claim that the local engine is better at re-filtering was unmeasured until 2026-08-21 and is now measured only once. Memory-aware backend selection was proposed on the grounds that DuckDB spills and Polars does not, but the Polars path already does `scan_parquet` into `collect(engine='streaming')`, so the premise is weak; if pursued, use a configured byte ceiling rather than adding `psutil`.

**Discovery.** Group metadata is done: stored bytes, retention, and creation time are read and displayed, and the sampled preview clusters messages into distinct shapes. Three things are still open. The preview already computes per-shape skeletons with literal keys and placeholder values, so merging them into one field roster ("here are this group's JSON fields, and how many events carry each") is a pure function over `list[MessagePattern]` and would also feed filter-field completion. Last-write time has an honest cheap form and a dishonest one: the sample already receives timestamps and throws them away, but `FilterLogEvents` yields ascending and the sample is capped, so a busy group's newest sampled timestamp sits early in the window and the busiest groups would read as the stalest. Ship it as an activity indicator (saturated, an exact time, or quiet) rather than as a timestamp. `logGroupClass` is returned by `DescribeLogGroups` and dropped; it is worth a column because Infrequent Access groups cannot be live-tailed. Separately, there is no shell completion for the CLI at all, and with nine `export` subcommands and log group names that run past 40 characters, completing group names from the cached group list would save more typing than any other ergonomics change.

**Plumbing and tooling.** `ProgressCallback` is defined twice with different arity, in `cache/storage.py` as `(current, total, status)` and in `aws/client.py` as `(count, message)`. Unify those before wiring fetch and Parquet-convert progress into the TUI, because one worker has to feed both; today `ProgressUpdate` exists but its only producer is DataTable row insertion, so the two long operations run silent, and a two-minute multi-group fetch reports nothing. `write_ndjson` opens its output with `output_path.open('w')`, so a non-ASCII log message fails on a Windows locale that is not UTF-8, and no test covers it. CI runs the test suite on macOS and Windows only, never on Linux, and only on 3.11 despite the classifiers claiming 3.13. `PanicException` from Polars is not an `Exception` subclass, so nothing between `write_log_events_to_parquet` and the terminal catches it; the schema bug that triggered it is fixed but the failure mode is not, and how the TUI renders it is unverified.

**Declined.** YAML config: TOML stays the only format, because a second format means a second parser, an optional dependency, and a forked document for no capability gain. That choice was never written down, which is the actual gap. A config wizard prompting on stdin conflicts with one TUI and one NDJSON surface, and `create_default_config_file` already scaffolds commented TOML. A visual modal filter builder is rejected: it duplicates the `:` command line and which-key discovery ADR 0008 chose. Log group mappings in config are already covered by `[presets]`. Multi-backend log sources and generic in-TUI AI summarization stay rejected per ADR 0010.

## Landscape (why build this at all)

Dedicated CloudWatch tailers (awslogs, saw, cw, utern) are abandoned or dormant since 2019-2023 and predate the Live Tail API and the newer Insights query languages. Gonzo is a strong log-analysis TUI with no native CloudWatch source. The official AWS CloudWatch MCP server covers Insights and pattern analysis for agents but has no live tail and no human surface. Grafana's CloudWatch data source is the honest answer for anyone who wants a web GUI.

What is still unclaimed, and so still worth our time: a real TUI over `StartLiveTail`, log group discovery with metadata in the terminal, one filter model shared across live and historical and cached data, and a correlation-ID pivot across log groups. The first three shipped; the fourth is M3.

The 2026-08-21 measurements add a fourth claim that is now evidenced rather than asserted. Against the AWS CLI, tail-cw wins on ranking recurring patterns (199,967 events into 29 readable rows, one command), on alarm flapping as a number, and on repeat latency over a cached window (4.2s against 37.8s). It loses on first-call latency and on raw Insights speed (4.3s against 3s), and it takes on interpretation, which is where all four of the day's correctness bugs lived. Both directions belong in this file, because the losses are what the queue above is for.

## Design principles

- Every feature lands CLI-first with NDJSON output under `tail-cw export`; the TUI is a view over the same functions, so agents and humans drive one code path
- Frugal by default: cache everything fetched as Parquet, re-filter locally for free, keep default time ranges tight, and show a scan estimate before any paid Logs Insights query
- Frugal locally too: native engines get a minority of the machine (`cpu_budget.py` caps DuckDB and Polars at 40% of the CPU count), because a background fetch that takes 7.6 of 12 cores makes the laptop unusable and buys no wall time when the work is bound by CloudWatch's API
- Keep logic in pure functions with side effects at the edges (per AGENTS.md). The async migration reinforced this: `aws/` functions take an open client and the pure translation code stayed untouched
- Interpretation gets tests. Severity classification, pattern normalization, and bucketing were each wrong against real data and right against synthetic data, so every heuristic ships with a regression test built from a real log line
- Each milestone ships with ruff, mypy, pyright, and pytest green before the next starts

## Sequencing rationale

Items 1 and 2 come first because they are the same file and the same write path, and doing them apart means rewriting `cache/storage.py` twice. Item 1 also unblocks the log table work in item 8, since both need display text built from `parsed`. Item 3 is next because it is the largest capability gap and mostly wiring over functions that already exist. Items 4 and 5 are small guards that keep the tool honest about incompleteness and cost, and 4 shares a predicate with 2. Items 6 through 9 are independent of each other and can be picked up in any order.

Scope growth is the standing risk ADR 0010 named: `tail_cw/` grew 82% in the nineteen days to 2026-07-25, and another 1,522 lines landed on 2026-08-21. Two of the nine queue items delete code rather than add it, which is the balance to keep. Prefer wiring existing machinery over new subsystems, and prefer sending work to Logs Insights over reimplementing it.
