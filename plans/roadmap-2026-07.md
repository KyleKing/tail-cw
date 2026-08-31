# Roadmap: make tail-cw a daily-driver CloudWatch tool

Written 2026-07-05 from a code capability review and a survey of the CloudWatch tooling
landscape.
Pruned three times since, most recently on 2026-08-22, each time by moving what shipped
into the ADR that records the decision and deleting the planning detail.
The numbers behind a shipped decision live in its ADR; this file holds what is next.

## Delivered

| Milestone                      | Outcome                                                                                                                                                  | Record                                                                                                                                        |
| ------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| M0 wire the drivetrain         | argparse dispatch, fetch through the Parquet cache into the TUI, profile and region threaded through                                                     | [0002](../docs/docs/adr/0002-cli-first-layered-architecture.md), [0003](../docs/docs/adr/0003-parquet-cache-and-local-query-engine.md)        |
| M1 live tail                   | `StartLiveTail` with reconnects and sampling, ring-buffered rendering, one filter model across live, historical, and cached                              | [0004](../docs/docs/adr/0004-live-tail-via-startlivetail.md)                                                                                  |
| M2 navigation-first discovery  | group browser as the home screen, resolution ladder, ten-group multi-select, content previews, recents and presets                                       | [0008](../docs/docs/adr/0008-single-interactive-tui.md)                                                                                       |
| M3 investigation tools         | X-Ray reader, the `:xray` waterfall, the `p` and `x` correlation pivots, the `h` histogram                                                               | [0012](../docs/docs/adr/0012-export-traces-instead-of-drawing-them.md), [0013](../docs/docs/adr/0013-read-x-ray-directly-for-spans.md)        |
| M4 dashboards and metrics      | `GetDashboard` import, `metrics[]` shorthand translated to `GetMetricData`, native plotext charts, dive from a chart into logs                           | [0005](../docs/docs/adr/0005-dashboards-metrics-and-terminal-charts.md), [0006](../docs/docs/adr/0006-dashboard-rendering-and-interaction.md) |
| M5 async AWS I/O               | aiobotocore throughout, session-scoped client pool, bounded pool for DuckDB/Polars, cancellation that actually stops requests                            | [0011](../docs/docs/adr/0011-async-aws-io-and-blocking-work.md)                                                                               |
| M6 aggregation surface         | `export summary` with fuzzy-merged rollups, `export insights` in CWLI, SQL, and PPL, `export alarms --history`, `export metrics`, CPU budget             | [the evaluation](evaluation-2026-08-21-vs-aws-cli.md)                                                                                         |
| M7 cache v2 and one front door | segmented cache windows, a schema at 25 bytes per event, rollup/alarms/Insights in the TUI behind a cost gate, one shared history, `cache status`        | [0003](../docs/docs/adr/0003-parquet-cache-and-local-query-engine.md), [0008](../docs/docs/adr/0008-single-interactive-tui.md)                |
| M8 the queue's last five       | Insights scan estimate and a confirmation ceiling, `export trace` as OTLP, a 0.07s CLI start, a log table budgeted by width, `export dimensions`         | [0008](../docs/docs/adr/0008-single-interactive-tui.md), [0012](../docs/docs/adr/0012-export-traces-instead-of-drawing-them.md)               |
| M9 the filter surface          | `AND`, `OR`, `NOT`, and parentheses, named filters, a parse error that names its fix, and a translator that refuses what CloudWatch would answer wrongly | [the filter guide](../docs/docs/FILTER_GUIDE.md)                                                                                              |

Two deviations from the original plans are worth carrying forward, because they change
what a reader should expect to find.
There is no `tail-cw groups` subcommand: the browser is the home view and
`tail-cw export groups` covers the NDJSON case.
And log groups do not sort by last-event time, because `DescribeLogGroups` does not
return it (that would be a `DescribeLogStreams` call per group); your own selection
recency sorts the list instead, which is what the console's "recently accessed" actually
gives you.

## Open

Nothing is scheduled. Ordered by value against effort.

**Payload dtypes Parquet cannot store abort the whole fetch.** `scan_ndjson` infers a
schema over arbitrary structlog payloads, and two shapes IRM logs in production make the
write fail outright rather than degrading.
An empty JSON object (`{"meta": {}}`) infers a
zero-field struct Parquet cannot represent, and one key logged as two scalar types
(`"n": 1` then `"n": true`) fails to parse into the inferred type.
Both reproduce from a
single line through `write_log_events_to_parquet`, and both killed
`export logs irm-prod-ecs-hatchet-workers --start 12h --filter '"lacks searchable text"'`
against `read-prod` while the same command at `--start 1h` succeeded, so it presents as
an intermittent window-size bug.
The failure now names the offending key
(`Empty JSON object at parsed.a.b`) instead of only the dtype, which is diagnosis, not a
fix.
Fetching the same window with `aws logs filter-log-events --filter-pattern` worked
and returned 57 matching events over 72h.
Seen again on 2026-08-31, same log group, key `parsed.outcomes`, on both
`export summary` and `export logs --filter` over a 12h window, so it blocks every read
path into that group rather than one command.

The fix is a real choice and wants deciding before coding.
Sanitizing at the NDJSON
writer is the natural place because the data is still Python there, but the payload text
is spliced in verbatim on purpose (0.39s to 0.22s over 72,767 events) and re-encoding
every record gives that back.
Repairing the inferred schema before `sink_parquet` keeps
the hot path, and stringifying a nested payload key breaks the struct-field references
`query/engine.py` builds from `collect_schema()`.
Dropping an empty object loses nothing;
reconciling a conflicting key means picking a winning type or widening it to text, and
the tool's stated position is that silently dropping a payload key is worse than
failing.

**A truncated NDJSON stream is indistinguishable from a complete one.** On 2026-08-31
`export alarms --profile read-prod` wrote 124 records over 105,342 bytes, and an agent
harness consuming it persisted 50 records over 42,486 bytes, cut on a line boundary, and
labelled the file "Full output saved to".
Nothing in the stream contradicted that label: every line parsed, the last line was
whole, and no record carries a total, so the loss read as tail-cw capping at 50 and the
session spent a turn proposing a pagination key the tool does not need.
`describe_alarms` (`tail_cw/aws/alarms.py:116`) already walks every page, so what failed
is detectability, not resumability.

The cheapest repair is a record count on stderr at the end of every export, which leaves
the NDJSON contract of ADR 0002 and 0008 untouched and runs to about one line per
command.
`_export_alarms` already writes to stderr when nothing matches (`cli.py:1287`), so this
is the same courtesy for the case that succeeds.
It is the least effort of anything on this list and could reasonably be promoted above
the Parquet item.

Open: whether it covers `export metrics`, which emits one self-describing object per
series and gains little from a count; what `export tail` reports, given it never
completes and would have to count on interrupt; and whether the line stays human prose,
which it should, because stdout is the machine surface.

**A trailer record would survive `2>/dev/null`, at the cost of a documented contract.**
The stderr count above dies to any redirect, and the 2026-08-31 session suppressed
stderr
on most of its own calls, which makes the signal that matters least likely to arrive.
A final `{"record":"summary","count":124}` travels with the data instead, and turns
completeness into one `jq -e`.

The cost is that every export today yields one homogeneous record type and consumers
filter on domain fields, so `jq -r '.name'` over an alarms export would start printing
`null` for the trailer.
That breaks the NDJSON surface ADR 0002 and 0008 specify, and
[ADR 0010](../docs/docs/adr/0010-keep-tail-cw-with-a-narrower-scope.md) calls that
surface
"nice-to-have rather than load-bearing", which is a live argument for declining this
outright rather than building it.

Open: whether it ships behind a flag, on by default, or not at all; what discriminator
field existing consumers could be expected to filter on, when none of them filters on
one
today; and whether `export tail` gets a trailer at all.
Settle the contract question before writing code, because the flag version and the
default version are different products.

**Per-width binding priority in the footer.** The mid-word garble is gone (the four
vim-conventional motions moved behind `?`, and the footer sheds its padding and the
palette hint below 100 columns), but a 60-column log view still truncates after
`t Trace View`.
Fixing the rest means ranking bindings and showing as many as fit, which is machinery
Textual does not provide.
`?` lists everything either way, so this is polish.

**Benchmark targets gated in CI.** ADR 0003's claim that the local engine is better at
re-filtering was unmeasured until 2026-08-21 and is now measured once.
A wall-clock threshold on a shared runner is a flake generator, which contradicts this
repo's own rule against wall-clock assertions, so decide what the gate asserts before
writing one.
Asserting the backend *choice* plus a generous ceiling is the shape most likely to catch
a
real regression without flaking.

**`--output PATH` to keep a large export out of the consumer's pipe.** The third answer
to
the truncation above: write the NDJSON to a file and print only a summary, so nothing
downstream can drop the tail without saying so.

The premise is weak and worth testing before building anything.
`tail-cw export alarms > alarms.ndjson` already does this with no new surface, and it is
what the 2026-08-31 session should have run.
A flag earns its keep only if it reports the path and the count in a way a shell
redirect
cannot, which is a thin margin against a new argument on every export command.

Open: whether it applies uniformly or only to the exports that can grow large; whether
stdout then carries the summary or nothing at all; and whether an existing file is
overwritten, appended to, or refused.

**Matcher hooks to auto-link an event to its Sentry or PostHog issue.** Carried since
2026-07-05 and still unscheduled.
It adds an outbound integration surface to a tool that has deliberately stayed
CloudWatch-only, so it is a scope decision before it is a coding task.

**Memory-aware backend selection** was proposed on the grounds that DuckDB spills and
Polars does not, but the Polars path already does `scan_parquet` into
`collect(engine='streaming')`, so the premise is weak.
If pursued, use a configured byte ceiling rather than adding `psutil`.

## Blocked application-side, not here

M3's cross-service pivot needs a key present on both sides of the Hatchet boundary, and
there is none: measured on 2026-07-25 over three hours, `irm-ecs-api-prod` and
`irm-prod-ecs-hatchet-workers` shared zero `trace_id` and zero `request_id` values, and
100 of 100 API trace ids are X-Ray style against 0 of 22 worker ids.
No `workflow_run_id` appears in worker logs at all.
Worker OTLP export is also failing outright, 334 errors an hour to
`hatchet.hatchet.local:7070` with `StatusCode.UNIMPLEMENTED`, so fix that before
measuring
the join again.

Both sides already log `trace_id`, `span_id`, and `request_id`, and none logs
`parent_span_id`, which is why span hierarchy comes from X-Ray rather than from log
lines
([ADR 0012](../docs/docs/adr/0012-export-traces-instead-of-drawing-them.md)).
The fixes belong in the application repo: enable Hatchet's OTel instrumentor on both
sides
so W3C `traceparent` flows through task metadata, and align both on
`AwsXRayIdGenerator`.
Stamping `Context.workflow_run_id` into worker log lines is the weaker fallback, because
it
does not reach the API.

Two more application-side gaps, measured 2026-08-22 and written up in
`irm-0-null/docs/investigations/`.
The account runs one X-Ray sampling rule, the AWS default, so `irm-api` is recorded at
14.5% while Hatchet's exporter ignores the rule and supplies 99% of the traces.
And `irm-api` emits no database spans, so a 59-second request arrives as five spans with
24ms accounted for.

## Declined

TOML stays the only config format, because a second format means a second parser, an
optional dependency, and a forked document for no capability gain.
That choice was never written down, which was the actual gap.

A config wizard prompting on stdin conflicts with one TUI and one NDJSON surface, and
`create_default_config_file` already scaffolds commented TOML.
A visual modal filter builder duplicates the `:` command line and the which-key
discovery
ADR 0008 chose.
Log group mappings in config are covered by `[presets]`.
Multi-backend log sources and generic in-TUI AI summarization stay rejected per
[ADR 0010](../docs/docs/adr/0010-keep-tail-cw-with-a-narrower-scope.md).

Reimplementing LogsQL in the local engine is dropped: Insights does this server-side,
and
maintaining a second query language is the cost ADR 0010 exists to avoid.
An in-terminal service map and a waterfall drawn from log timestamps are rejected in ADR
0012; the waterfall that shipped is drawn from X-Ray segments, which carry real
intervals.

## Landscape (why build this at all)

Dedicated CloudWatch tailers (awslogs, saw, cw, utern) are abandoned or dormant since
2019-2023 and predate the Live Tail API and the newer Insights query languages.
Gonzo is a strong log-analysis TUI with no native CloudWatch source.
The official AWS CloudWatch MCP server covers Insights and pattern analysis for agents
but
has no live tail and no human surface.
Grafana's CloudWatch data source is the honest answer for anyone who wants a web GUI.

What was unclaimed and worth our time: a real TUI over `StartLiveTail`, log group
discovery with metadata in the terminal, one filter model shared across live and
historical
and cached data, and a correlation-ID pivot across log groups.
All four have shipped, the fourth within one service's groups.

Measured against the AWS CLI on 2026-08-21, tail-cw wins on ranking recurring patterns
(199,967 events into 29 readable rows, one command), on alarm flapping as a number, and
on
repeat latency over a cached window (4.2s against 37.8s).
It loses on first-call latency and on raw Insights speed (4.3s against 3s), and it takes
on
interpretation, which is where all four of that day's correctness bugs lived.
Both directions belong here, because the losses are what the open list is for.

## Design principles

- Every feature lands CLI-first with NDJSON output under `tail-cw export`; the TUI is a
    view over the same functions, so agents and humans drive one code path
- Frugal by default: cache everything fetched as Parquet, re-filter locally for free, keep
    default time ranges tight, and show a scan estimate before any paid query.
    Where no estimate is possible, say so and require `--yes` rather than run blind
- Frugal locally too: native engines get a minority of the machine (`cpu_budget.py` caps
    DuckDB and Polars at 40% of the CPU count), because a background fetch that takes 7.6 of
    12 cores makes the laptop unusable and buys no wall time when the work is bound by
    CloudWatch's API
- Keep logic in pure functions with side effects at the edges (per AGENTS.md).
    The async migration reinforced this: `aws/` functions take an open client and the pure
    translation code stayed untouched
- Interpretation gets tests.
    Severity classification, pattern normalization, and bucketing were each wrong against
    real data and right against synthetic data, so every heuristic ships with a regression
    test built from a real log line
- Drive it before believing it.
    Every correctness bug found on 2026-08-21 and 2026-08-22 came from running the tool
    against production or from reading a screenshot, and none from a passing test suite
- Each milestone ships with ruff, mypy, pyright, and pytest green before the next starts

## The standing risk

Scope growth, as [ADR 0010](../docs/docs/adr/0010-keep-tail-cw-with-a-narrower-scope.md)
named it: `tail_cw/` grew 82% in the nineteen days to 2026-07-25, and thousands of lines
landed across 2026-08-21 and 2026-08-22.
Prefer wiring existing machinery over new subsystems, prefer sending query power to Logs
Insights over reimplementing it, and prefer deleting an open item to building it.
