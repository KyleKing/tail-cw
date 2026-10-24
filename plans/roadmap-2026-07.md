# Roadmap: make tail-cw a daily-driver CloudWatch tool

Written 2026-07-05 from a code capability review and a survey of the CloudWatch tooling
landscape.
Pruned five times since, most recently on 2026-10-05, each time by moving what shipped
into the ADR that records the decision and deleting the planning detail.
Rewritten on 2026-10-21 after a critical review of how the tool is used on this machine,
which added the disk budget, the cache measurements, and milestones M13 to M19.
The numbers behind a shipped decision live in its ADR, and this file holds what is next.

## Delivered

| Milestone                                       | Outcome                                                                                                                                                                                                         | Record                                                                                                                                        |
| ----------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| M0 wire the drivetrain                          | argparse dispatch, fetch through the Parquet cache into the TUI, profile and region threaded through                                                                                                            | [0002](../docs/docs/adr/0002-cli-first-layered-architecture.md), [0003](../docs/docs/adr/0003-parquet-cache-and-local-query-engine.md)        |
| M1 live tail                                    | `StartLiveTail` with reconnects and sampling, ring-buffered rendering, one filter model across live, historical, and cached                                                                                     | [0004](../docs/docs/adr/0004-live-tail-via-startlivetail.md)                                                                                  |
| M2 navigation-first discovery                   | group browser as the home screen, resolution ladder, ten-group multi-select, content previews, recents and presets                                                                                              | [0008](../docs/docs/adr/0008-single-interactive-tui.md)                                                                                       |
| M3 investigation tools                          | X-Ray reader, the `:xray` waterfall, the `p` and `x` correlation pivots, the `h` histogram                                                                                                                      | [0012](../docs/docs/adr/0012-export-traces-instead-of-drawing-them.md), [0013](../docs/docs/adr/0013-read-x-ray-directly-for-spans.md)        |
| M4 dashboards and metrics                       | `GetDashboard` import, `metrics[]` shorthand translated to `GetMetricData`, native plotext charts, dive from a chart into logs                                                                                  | [0005](../docs/docs/adr/0005-dashboards-metrics-and-terminal-charts.md), [0006](../docs/docs/adr/0006-dashboard-rendering-and-interaction.md) |
| M5 async AWS I/O                                | aiobotocore throughout, session-scoped client pool, bounded pool for DuckDB/Polars, cancellation that actually stops requests                                                                                   | [0011](../docs/docs/adr/0011-async-aws-io-and-blocking-work.md)                                                                               |
| M6 aggregation surface                          | `export summary` with fuzzy-merged rollups, `export insights` in CWLI, SQL, and PPL, `export alarms --history`, `export metrics`, CPU budget                                                                    | [the evaluation](evaluation-2026-08-21-vs-aws-cli.md)                                                                                         |
| M7 cache v2 and one front door                  | segmented cache windows, a schema at 25 bytes per event, rollup/alarms/Insights in the TUI behind a cost gate, one shared history, `cache status`                                                               | [0003](../docs/docs/adr/0003-parquet-cache-and-local-query-engine.md), [0008](../docs/docs/adr/0008-single-interactive-tui.md)                |
| M8 the queue's last five                        | Insights scan estimate and a confirmation ceiling, `export trace` as OTLP, a 0.07s CLI start, a log table budgeted by width, `export dimensions`                                                                | [0008](../docs/docs/adr/0008-single-interactive-tui.md), [0012](../docs/docs/adr/0012-export-traces-instead-of-drawing-them.md)               |
| M9 the filter surface                           | `AND`, `OR`, `NOT`, and parentheses, named filters, a parse error that names its fix, and a translator that refuses what CloudWatch would answer wrongly                                                        | [the filter guide](../docs/docs/FILTER_GUIDE.md)                                                                                              |
| M10 what a week of use showed                   | `export logs --parsed` and `--limit`, `export stats --by`, globs and `--demo` across export, `[aws].profile` and per-preset profiles, the field panel, repaired payload dtypes, and the terminal fixes          | [the review](review-2026-08-31.md)                                                                                                            |
| M11 what a prod incident showed                 | `--filter` sent to CloudWatch on export, case-sensitive text everywhere, Insights with no window cap and with the query's own `limit`, `export xray-trace` reading a logged id, and an interrupt that exits 130 | [0003](../docs/docs/adr/0003-parquet-cache-and-local-query-engine.md), [0008](../docs/docs/adr/0008-single-interactive-tui.md)                |
| M12 a cold export that answers Ctrl+C           | a stderr notice before a segment's write begins, and SIGINT/SIGTERM exiting immediately instead of waiting on `ThreadPoolExecutor.shutdown` for an in-flight Parquet write to finish                            | [0011](../docs/docs/adr/0011-async-aws-io-and-blocking-work.md)                                                                               |
| M13 measure before changing anything            | an append-only read log (hits, misses, bytes served/fetched, per group), hit rate and top groups in `cache status`, a peak-RSS/CPU trailer on every export                                                      | —                                                                                                                                             |
| M14 a disk budget with a degrade ladder         | `min_free_mb` floor, staging inside the cache dir, `--max-disk`, `--cache-ttl`/`TAIL_CW_CACHE_TTL` (read-age TTL, `0` bypasses), fit → evict → sparse → refuse at exit 3                                        | [0014](../docs/docs/adr/0014-bound-disk-use-with-a-degrade-ladder.md)                                                                         |
| M15 eviction and layout that follow reads       | least-recently-read eviction with a per-group quota, a path-to-key index, and the sweeps moved from per-write to once per command                                                                               | [0014](../docs/docs/adr/0014-bound-disk-use-with-a-degrade-ladder.md)                                                                         |
| M16 CPU and peak memory on a constrained laptop | `TAIL_CW_MAX_MEMORY_MB`/`[resources].max_memory_mb`, `--low-impact`/`[resources].profile` (15% CPU fraction, one segment fetch), a 300ms debounce on TUI window refetches                                       | —                                                                                                                                             |
| M18 warm the windows people reread              | `tail-cw warm` with per-group high-water marks, skipping on battery, under the free-space floor, or while a command runs                                                                                        | —                                                                                                                                             |
| Carried operational fixes                       | `export logs --stream`/`--stream-prefix` sent to CloudWatch and keyed into the cache, and a case-sensitivity stderr hint when a quoted term matches nothing                                                     | —                                                                                                                                             |

Two deviations from the original plans are worth carrying forward, because they change
what a reader should expect to find.
There is no `tail-cw groups` subcommand: the browser is the home view and
`tail-cw export groups` covers the NDJSON case.
And log groups do not sort by last-event time, because `DescribeLogGroups` does not
return it (that would be a `DescribeLogStreams` call per group); your own selection
recency sorts the list instead, which is what the console's "recently accessed" actually
gives you.

## What the 2026-10-21 review found

Measured on this machine today, from files and commands, with no profiler run.

- The cache holds 3,357 Parquet files and 1,046,536,312 bytes against a limit of
    1,048,576,000, which is 99.8% full.
    `cache status` also reports 7 orphan files and 0 stale entries
- The data volume is 96% used with 19 GiB free, so a cache that fills its cap and a cold
    fetch that stages NDJSON beside it both draw on a scarce resource
- File sizes are skewed.
    The median file is 18 KB, 51% are under 20 KB, and the largest is 149 MB from one
    segment on 2026-09-16, which is 14% of the whole cache
- Real use is mostly agents running `export summary` and `export alarms` against
    `read-prod` through the skill, plus repeated `logs ... --start 15m` runs from the
    checkout
- A live `export summary --start 72h` sampled at 9.7% CPU and 78 MB RSS, so the warm and
    network-bound path is not the spike

The hypotheses this review raised are answered: reads are recorded (M13), the
per-write sweeps now run once per command and evictions read one index instead of every
key (M15), eviction follows last read rather than store time (M15), staging lives inside
the cache and counts against the cap (M14), and every export reports its own peak RSS
and CPU (M13).

The user's doubt that exports produce more than 100 MB of files is plausible.
Exports stream to stdout, so the whole-tool budget reduces to the cache plus the staging
files, and M14 measures staging before it adds a second budget.

## Next milestones

Sequenced so each one measures what the next one changes.
M13 through M18 shipped; what is left in each is the work the read log exists to decide.

### M13 measure before changing anything

Effort: small.

- Profile one cold 1-hour fetch with py-spy and `benchmark_backends()` and write down
    which
    of JSON encoding, `sink_parquet`, the per-command cache upkeep, or schema repair
    dominates.
    The CPU spike is not diagnosed until a trace says so
- Watch the read log for a week of normal use before spending more on the cache.
    If more than about 90% of cached bytes are never read again, the right answer to disk
    cost is to stop persisting wide windows, and compaction is not needed

### M14 a disk budget with a degrade ladder

Effort: medium.
Shipped except the deferred step below; the ladder, TTL controls, and staging layout are
in [ADR 0014](../docs/docs/adr/0014-bound-disk-use-with-a-degrade-ladder.md).

Deferred from the ladder's step 4: when the window alone exceeds the budget and the
command is an aggregate (`summary`, `stats`, `insights`), send it to Logs Insights and
cache only the aggregate result, behind the existing scan-estimate gate.

Cheaper local storage, in the order worth trying after M13 reports.

1. Admit on second read: a segment fetched once for a wide window stays in the temp area
    and is promoted into the cache only if a second command reads it.
    Only worth building if M13 shows most bytes are never re-read
1. Lower the ZSTD level for cold segments or raise it for old settled ones, measured on a
    real group, and drop columns no command reads
1. Compact small segments (M15)

**S3 as a remote cache is deferred, not planned.**
DuckDB can query Parquet in S3 directly, so it is technically cheap to try.
It still trades local disk for S3 storage and request cost, adds `s3:PutObject` to a
workflow that runs on a read-only profile (`read-prod`), and copies production log
content, which may hold customer data, into a second store with its own retention and
access rules.
Warm reads would also move from local files to network range reads, which gives up the
repeat-latency win the tool exists for.
Revisit only for a shared team cache, with a bucket in the same account, SSE-KMS, a
lifecycle expiry no longer than the local TTL, and write access held by a separate role.

### M15 eviction and layout that follow reads

Effort: medium.
Shipped except compaction, which waits on M13's read numbers.

- Compact settled 5-minute segments into one Parquet file per group per hour (and hour
    segments into a day), so a day of one group is a few files DuckDB can glob in place of
    hundreds.
    Run it during eviction passes, under the `nice` budget, and never on a segment
    that is unsettled or inflight.
    It changes the read path (a timestamp filter becomes necessary), so it waits for M13
    to show the rereads justify the complexity

### M16 CPU and peak memory on a constrained laptop

Effort: medium.
The ceiling, the low-impact profile, and the TUI refetch debounce shipped; the
tightening shipped too: a default ceiling derived from physical memory, fewer segment
writers under a tight budget, batched reads with bounded fetches, a `nice` on TUI
fetches under `low-impact` (named in the status line), and the 1.5M-event
`getrusage` test.
What remains:

- Push regex and numeric filters down (carried item below), because a narrower
    cold download is the largest single cut in CPU, memory, and disk at once

### M17 what changed since the last window

Effort: medium.
Needs only the segment cache, so it can ship before any warming.

`export summary --baseline <duration>` compares the window with the same-length window
that many durations earlier (`--baseline 1d` or `7d`) and labels each pattern `new`,
`gone`, `up`, `down`, or `same`, with both counts and the ratio.
This is the question CloudWatch and the AWS CLI cannot answer without a script, and it
is
where the `ReadTimeoutError` ramp (2, then 13, then 87 over three days) would have shown
up on day two.

- The first run pays a cold fetch for the baseline, so it respects the M14 budget and
    reports when the baseline was sparse or fetched through Insights
- Patterns merge across the two windows with the existing fuzzy merge, so a changed
    request id does not split a pattern into `new` and `gone`
- The TUI summary view marks new patterns with a glyph, because colour alone does not
    carry meaning
- Tests build the two windows from real log lines, because the earlier pattern and
    severity bugs appeared only against real data

### M19 a better surface for agents and humans

Effort: ongoing.
Each item needs a scope check against the standing risk below, and the run report and
the answer budget are the two to build first.

For agents, who are the heaviest users today.

- Settle the trailer record contract (carried item) and decide `--output PATH`, because
    both answer "did I get everything"
- A machine-readable run report: `--report FILE` writes one JSON object with the path
    taken
    (cache hit count, pushdown applied or not, sparse or Insights, bytes fetched, peak RSS,
    CPU seconds, events written, truncation).
    It survives `2>/dev/null`, adds no record to the NDJSON stream, and settles the
    completeness question without the trailer's contract change
- A budget on the answer: `export summary --max-events N --max-cost-usd X` that stops and
    says what it skipped, in place of running until the user interrupts
- `tail-cw export schema <command>` printing the record fields, so a consumer reads the
    contract from the tool and not from the docs
- Stable exit codes documented once: 0 complete, 2 usage, 3 partial because of a budget
    step, 130 interrupted

For humans.

- The TUI footer shows cache state for the current view
    (`hot 7 of 9 segments · disk 0.9 of 1.0 GB`), so the cost of a keystroke is visible
- `cache status` as a table by group with hit rate and bytes, in place of one JSON line,
    with `--json` kept for scripts
- `y` in the log view copies the equivalent `tail-cw export logs ...` command, so an
    investigation moves from the TUI to an agent or a script without retyping
- A first-run check (`tail-cw doctor`) that reports profile, region, cache location and
    free space, and which budgets apply

Considered and not planned: an MCP server in front of the export commands.
The skill plus the CLI already serves the agents that use the tool, and the official AWS
CloudWatch MCP server covers Insights.
Revisit if the skill's approval prompts or token cost show up as the bottleneck.

## Housekeeping

- `./run` is documented as broken, so the gate in `AGENTS.md` cannot be run as written.
    Fix the task runner or correct the document
- `err.log`, `err2.log`, `stderr.log`, `demo-aws`, `tui-eval`, and `node_modules` sit in
    the repo root.
    Delete what is stray and ignore what is not
- ADR 0010 said the codebase was about 2,000 lines when it was about 8,000 and is now
    18,800 plus 16,100 of tests.
    Record the true size where the scope argument reads it, because the argument rested on
    the smaller number

## Carried open items

Ordered by value against effort.

**`export metrics` cannot run a metric math SEARCH expression.**
On 2026-10-24 an IRM on-call agent needed `TavilyRequestCount` summed across every
dimension set (`SEARCH('{IRM,Origin} TavilyRequestCount', 'Sum', 60)`) and fell back to
`aws cloudwatch get-metric-data --metric-data-queries` because `export metrics` takes
one
namespace and metric name.
Accepting `--expression` (passed through as a `GetMetricData` expression) would cover
SEARCH and metric math.

**Push regex and numeric filters down once they are proven equal.**
`pushdown_filter_pattern` sends text, phrases, and field `=` and `!=`, each checked live
against CloudWatch first
([ADR 0003](../docs/docs/adr/0003-parquet-cache-and-local-query-engine.md)).
A `%regex%`, a numeric comparison, field existence, and a field regex still download the
whole window.
Each needs the same live check before it moves: a regex can span JSON keys, whose order
the cache does not keep, and `status:>=500` is unverified against a number logged as a
string.

**The scan estimate is the only cost gate on a wide Insights window.**
`[insights].confirm_above_gb` decides on the estimate alone, whatever the window.
It samples three five-minute slices however wide the window is, so over 30 days they sit
ten days apart and a burst between them reads low.
On 2026-09-30 it read high instead: 0.715 GB estimated against 0.014 GB scanned for a
30h query over two groups.
Measure the estimate against `bytesScanned` over a few real wide queries before trusting
it at 30 days.

**The `x` pivot refuses worker trace ids that X-Ray holds.**
`as_xray_trace_id`, behind the TUI's `x` pivot, refuses a 32-digit id whose leading
digits are not a recent epoch, on the theory that only X-Ray generates ids worth a
request.
That theory is wrong.
On 2026-10-06 an `irm-prod-ecs-hatchet-workers` line logged `trace_id`
`0e7ba36f55329f51255ad76f9759416b`, whose leading digits decode to 1977, and
`export xray-trace` returned it as three spans from `irm-workers` and
`hatchet.run_workflows`.
So every worker log line carries an id X-Ray can answer, and the pivot refuses all of
them.
The fix is to drop the epoch check and leave `xray_form` as the one conversion, then
correct the trace-id bullet in `AGENTS.local.md` that says the leading eight digits are
the epoch.
It does not unblock the API-to-worker join under "Blocked application-side", because the
two sides still share no id.

**A trailer record would survive `2>/dev/null`, at the cost of a documented contract.**
Every export now writes `Wrote N events` to stderr, which is the cheap half of this and
enough to tell a truncated read from a complete one.
It dies to any redirect, and the 2026-08-31 session suppressed stderr on most of its own
calls, which makes the signal that matters least likely to arrive.
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
`ai-output/archive/tail-cw-investigations/`.
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
- Bounded on disk too: the cache, its staging files, and the free space on its volume each
    have a limit, and a fetch that would break one steps down a stated ladder (evict, go
    sparse, ask Insights, refuse) and says which step ran
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
