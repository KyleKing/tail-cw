# Roadmap: make tail-cw a daily-driver CloudWatch tool

Written 2026-07-05 from a code capability review and a survey of the CloudWatch tooling
landscape.
Pruned 2026-07-25: M0, M1, M2, M4, and M5 are delivered, so their planning detail moved
into the ADRs that record the decisions.
Reordered 2026-08-21 after the tool was driven through six real production
investigations against Coverbase's prod and stage accounts; the queue below is that
audit's output, and the evidence for each item is in
[the evaluation](evaluation-2026-08-21-vs-aws-cli.md) and
[the TUI critique](tui-critique-2026-08-21.md).

## Delivered

| Milestone                      | Outcome                                                                                                                                                            | Record                                                                                                                                        |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------- |
| M0 wire the drivetrain         | argparse dispatch, fetch through the Parquet cache into the TUI, profile and region threaded through                                                               | [0002](../docs/docs/adr/0002-cli-first-layered-architecture.md), [0003](../docs/docs/adr/0003-parquet-cache-and-local-query-engine.md)        |
| M1 live tail                   | `StartLiveTail` with reconnects and sampling, ring-buffered rendering, one filter model across live, historical, and cached                                        | [0004](../docs/docs/adr/0004-live-tail-via-startlivetail.md)                                                                                  |
| M2 navigation-first discovery  | group browser as the home screen, resolution ladder, ten-group multi-select, content previews, recents and presets                                                 | [0008](../docs/docs/adr/0008-single-interactive-tui.md)                                                                                       |
| M4 dashboards and metrics      | `GetDashboard` import, `metrics[]` shorthand translated to `GetMetricData`, native plotext charts, dive from a chart into logs                                     | [0005](../docs/docs/adr/0005-dashboards-metrics-and-terminal-charts.md), [0006](../docs/docs/adr/0006-dashboard-rendering-and-interaction.md) |
| M5 async AWS I/O               | aiobotocore throughout, session-scoped client pool, bounded pool for DuckDB/Polars, cancellation that actually stops requests                                      | [0011](../docs/docs/adr/0011-async-aws-io-and-blocking-work.md)                                                                               |
| M6 aggregation surface         | `export summary` with fuzzy-merged pattern rollups, `export insights`, `export alarms --history`, `export metrics`, CPU budget                                     | this file's queue, plus [the evaluation](evaluation-2026-08-21-vs-aws-cli.md)                                                                 |
| M7 cache v2 and one front door | segmented cache windows, a schema at 25 bytes per event, rollup/alarms/Insights in the TUI behind a cost gate, one shared history                                  | [0003](../docs/docs/adr/0003-parquet-cache-and-local-query-engine.md), [0008](../docs/docs/adr/0008-single-interactive-tui.md)                |
| M8 the queue's last five       | Insights scan estimate and a confirmation ceiling, `:trace <id>` and `export trace` as OTLP, a 0.07s CLI start, a log table budgeted by width, `export dimensions` | [0008](../docs/docs/adr/0008-single-interactive-tui.md), [0012](../docs/docs/adr/0012-export-traces-instead-of-drawing-them.md)               |

Two deviations from the original plans are worth carrying forward, because they change
what a reader should expect to find.
There is no `tail-cw groups` subcommand: the browser is the home view and
`tail-cw export groups` covers the NDJSON case.
And log groups do not sort by last-event time, because `DescribeLogGroups` does not
return it (that would be a `DescribeLogStreams` call per group); your own selection
recency sorts the list instead, which is what the console's "recently accessed" actually
gives you.

## Next up

The 2026-08-21 audit's five remaining items all shipped on 2026-08-22 (M8 above).
What follows is what they measured, because half the numbers were wrong.

### What items 1 to 5 measured after shipping

- **the scan estimate is a scale, not a number.** Stored bytes over retention was
    predicted to read low, because CloudWatch stores compressed and Insights bills
    uncompressed.
    Measured against three production groups it was out by up to 8x in *both* directions:
    `irm-ecs-api-prod` estimated 0.002 GB against 0.017 GB actual, and
    `/aws/ecs/irm-metrics`
    estimated 0.007 GB against 0.001 GB.
    Averaging a group's whole life over its window is the larger error, so the label says so
    and the ceiling only catches the genuinely huge cases
- **72 spans sharing one id is not a trace.** The first real `export trace` against prod
    returned 72 spans that all carried the same `span_id`, because every line a service logs
    inside a span carries that span's id.
    They are the span's events, so that is what the export emits now: one span, 72 events,
    7.6s wide.
    An X-Ray id also needs its version prefix dropped rather than truncated off the far end,
    which would name a different trace
- **the startup cost was mostly structural, not lazy loading.** `tail-cw --help` went from
    0.35s to 0.07s, and most of it came from three layering fixes rather than from deferring
    imports: `LogEvent` moved out of the aiobotocore-importing client module, the pure
    record
    helpers moved out of the Polars-importing storage module, and three package `__init__`
    facades that re-exported everything (and so loaded everything) were emptied.
    Exactly one deferred import remains, in `tail_cw/__main__.py`
- **the log table's 12 characters were a symptom.** Budgeting the columns by width gets
    Message from 12 to about 60 characters at 80 columns, and dropping the date, the group,
    and the search box's border gets the row count from 17 to 19.
    Rendering a record as its phrase plus dim `key=value` pairs (the `tail-jsonl` shape)
    matters more than the width did
- **three more controls that worked while showing nothing.** The command line ran what you
    typed into it and displayed none of it, at every width, because a docked prompt lands on
    the footer's row and a one-row `Input` keeps the border `Input:focus` gives it.
    `tail_cw/tui/picker.py` had already solved this with `border: none !important` and a
    comment, and the fix was not reused.
    Look for the reference implementation first

### Shipped 2026-08-22, and what it measured

- **a window's segments now overlap.** The cold-fetch cost was assumed to be AWS and
    checked instead: of a 19.87s hour, 19.73s was spent awaiting `FilterLogEvents` pages,
    0.13s building records, and the loop never stalled past 28ms.
    `FilterLogEvents` paginates one page per round trip, and the segments the cache already
    plans were being walked one at a time under a comment claiming the per-account quota was
    already saturated.
    It is not, for a single group.
    Four concurrent segments take a cold hour from 21.6s to 8.7s end to end, byte for byte
    identical output, and the ceiling is `[fetch].max_concurrent_segments`
    ([ADR 0011](../docs/docs/adr/0011-async-aws-io-and-blocking-work.md))
- **the segment writers got their own pool, and the second speedup did not materialise.**
    Sharing one pool with the query layer meant a four-group fetch filled it and a search
    waited, which is the reason to split it.
    The raw-network probe suggested another third was available at eight segments (2.90s
    against 4.50s at four), and end to end with the Parquet writes included it was 12%
    (6.4s against 7.3s over three runs each), while event-loop lag p99 went from about 9ms
    to 13-22ms.
    Eight is the default anyway, because the worst lag stays inside a frame, but the number
    to quote is 12% rather than a third
- **the scan estimate is measured now, and it is close.** Three `FilterLogEvents` samples
    spread across the query's own window give a bytes-per-second rate per group, and the
    stored-bytes average is only the fallback for a group that logged nothing measurable.
    Against the same three production groups that were out by 8x, the estimate now reads
    0.033 GB against 0.032 actual, 0.070 against 0.069, and 0.006 against 0.008.
    One sample was not enough: a single group measured between 5,773 and 14,900 bytes a
    second inside one hour, so sampling one end of the window read it 1.68x high.
    The preflight costs three requests per group, about 6s for an 18-group account
- **a slow load counts out loud, and escape stops it.** A cold multi-group window took
    tens of seconds behind a status line reading "Loading events...", which is
    indistinguishable from a hang (heuristic 1 and 3 in
    [the critique](tui-critique-2026-08-21.md)).
    It now ticks the elapsed seconds and names its own way out, and escape cancels the
    worker instead of only popping the screen, which mattered because the log view is
    reachable as the opening view where there is no screen to pop.
    A second escape goes back as before
- **a field search across groups was broken, and its error crashed the app.** Driving the
    TUI against two prod groups found both: `level:info` raised
    `StructFieldNotFoundError` because `/aws/ecs/irm-metrics` records carry no `level`, and
    the handler then took the app down, because a status `Label` renders markup and every
    Polars failure names its file as `[/path.parquet]`, which Rich reads as a closing tag.
    A file whose `parsed` struct lacks the field is skipped now (it cannot hold a match),
    except under a `NOT`, where it matches everything.
    Both predate today's work; Pilot could not see either, because no test searched a
    mixed-schema set and no test failed a search
- **every surface emits UTC now.** The local-time bug was wider than `export metrics`:
    botocore stamps the machine's zone on every timestamp it parses, so alarm state changes
    and alarm history read local too, while everything derived from epoch milliseconds read
    UTC.
    `to_utc` at the three response boundaries fixes all of them, and
    `tests/test_aws_alarms.py` is new because that module's response parsing had no test at
    all

### Still open, and worth doing next

Nothing from the 2026-08-21 audit. The next work is M3 below.

## Then: M3 investigation tools

Rescoped on 2026-07-25 by
[ADR 0010](../docs/docs/adr/0010-keep-tail-cw-with-a-narrower-scope.md).
The rule is now: build what only a CloudWatch-native terminal tool can build, and send
the rest to Logs Insights, which grew roughly fifty new commands across June and July
2026 and got GA PPL, SQL, JOIN, and sub-queries.
Items 3, 5, and 6 above were the front of this milestone.
The X-Ray reader shipped on 2026-08-22
([ADR 0013](../docs/docs/adr/0013-read-x-ray-directly-for-spans.md)): `export xray`
writes summaries, `export xray-trace` writes segment documents as OTLP, and `:xray <id>`
draws the waterfall
[ADR 0012](../docs/docs/adr/0012-export-traces-instead-of-drawing-them.md)
deferred.
Three things it measured are worth carrying forward.
The hierarchy is real (one root, no orphans, durations from 0.0ms to 50ms), so the
picture is honest.
Half the spans are X-Ray's own inferred segments, and reading their `name` as a service
made one trace look like four services called `pool.acquire` and
`query SELECT PG_NOTIFY12`.
And a filter expression does not make a query cheaper, because `TracesProcessedCount`
counts the traces it rejected: an uncapped three-hour sweep spent 44% of the month's
free tier, so `--limit` defaults to 1,000.

What remains:

- **correlation-ID pivot.** Shipped 2026-08-22 for one service's groups: `p` searches
    every selected group for the row's own correlation id, and `x` opens the row's trace
    in X-Ray.
    Driving it against prod found the thing no test could: our API logs the W3C trace id
    form and X-Ray answers only to the dashed one, so the first version rejected every
    real log line.
    Still blocked cross-service by the instrumentation gap below, and a pivot onto
    `workflow_run_id` is pointless until worker logs carry one
- **what X-Ray does not cover, which is most of the application.** The reader is done and
    the data is thin, in two ways worth keeping apart.
    The account runs one sampling rule, the AWS default, so `irm-api` is recorded at 14.5%
    uniformly across routes, while Hatchet's OTel exporter never consults the rule and
    supplies 99% of the traces.
    And what is recorded is shallow: `irm-api` emits no database spans, so a 59-second
    request arrives as five spans with 24ms accounted for.
    Both are application-side, and they sit with the instrumentation prerequisites below.
    Separately, the X-Ray recording bill went from covered by credits to $866.86 in 21
    days, and 71% of it is three Hatchet polling loops; the 2026-08-22 spend write-up in
    `irm-0-null/docs/investigations/` has the numbers
- **time-bucketed histogram of the current view.** Shipped 2026-08-22 behind `h`, over
    whatever the current search left on screen rather than over the whole group, and
    coloured per column by the worst severity in it.
    It found a burst immediately: an ERROR-filtered half hour of `irm-ecs-api-prod` peaks
    at 742 events in one column, 77x the average, with 83 of 104 columns quiet.
    Two traps came out of building it.
    A `display: none` row has no content region, so measuring it drew the whole window as
    one column.
    And the shape of a capped load is the shape of the cap rather than of the window, so
    the row says so when the read stopped at the limit
- **dropped:** reimplementing LogsQL (`stream_context before N after N`, `unpack_json`) in
    the local engine.
    Logs Insights does this server-side now, and maintaining a second query language is the
    kind of cost ADR 0010 exists to avoid
- **dropped:** an in-terminal service map, and a waterfall drawn from log timestamps.
    Both rejected in ADR 0012
- later: matcher hooks to auto-link events to Sentry/PostHog issues

New AWS calls here are async and take an open client from the pool, per
[ADR 0011](../docs/docs/adr/0011-async-aws-io-and-blocking-work.md).
`StartQuery` is a poll loop, so it wants an async wait rather than a thread.

### Prerequisite: trace context does not cross the Hatchet boundary yet (measured 2026-07-25)

The correlation pivot can only join on a key present in both places, and today there is
none.
Measured against `read-prod` over a three-hour window:

| Key                   | `irm-ecs-api-prod` | `irm-prod-ecs-hatchet-workers` | Shared |
| --------------------- | ------------------ | ------------------------------ | ------ |
| distinct `trace_id`   | 100                | 22                             | **0**  |
| distinct `request_id` | 100                | 23                             | **0**  |

No `workflow_run_id` appears in worker logs at all.
The cause shows in the IDs: 100 of 100 API trace IDs are timestamp-prefixed X-Ray style
(`6a648fb4…`), and 0 of 22 worker IDs are, so the two sides run different ID generators
and produce disjoint trace-ID spaces.

The 2026-08-21 rollup found a second, harder fact about the same boundary: worker OTLP
export is failing outright, 334 errors an hour to `hatchet.hatchet.local:7070` with
`StatusCode.UNIMPLEMENTED`, so worker spans are not reaching any collector.
Fix that before measuring the join again, because the current disjointness may be partly
an artifact of spans never landing.

Both sides already log structured JSON carrying `trace_id`, `span_id`, and `request_id`,
and `trace_id` is already first in `DEFAULT_TRACE_ID_FIELDS`.
So the tooling side is ready and the gap is instrumentation, in the application repo
rather than here:

1. enable Hatchet's OTel instrumentor (`hatchet-sdk[otel]`,
    `HatchetInstrumentor().instrument()`) on both the triggering API and the consuming
    workers, which injects and reads W3C `traceparent` through task metadata automatically
1. align both sides on `AwsXRayIdGenerator` so the ID formats match and X-Ray keeps
    accepting them
1. failing that, stamp `Context.workflow_run_id` into worker log lines and join on that
    instead, which is weaker because it does not reach the API

A second missing key blocks more than the first one does.
Our services log `trace_id`, `span_id`, and `request_id`, and no `parent_span_id`
anywhere.
So span hierarchy is unavailable even inside one service, which is why ADR 0012 defers
the waterfall to X-Ray segments rather than to log lines.
Option 3 above unblocks the cross-service join without unblocking hierarchy; only
options 1 and 2 reach both.

## Backlog

Ordered within each group by value against effort.
Nothing here is scheduled.

**The filter surface.** Done on 2026-08-22, and the premise the roadmap recorded was
wrong in a way that made the answer better rather than worse.
`tail_cw/query/expression.py` now parses `AND`, `OR`, `NOT`, and parentheses over the
existing terms, with a space still meaning `AND` because that is what CloudWatch means
by
it, and uppercase keywords because a log line saying "timed out or retried" must stay a
text search.
`FilterParseError` carries the fix (unclosed paren, unbalanced quote, `$..`, a trailing
operator), and the two filters that parse cleanly yet can never match (`key=value` and
`/re/`) are suggested against in the zero-result hint instead.

The recorded premise was that CloudWatch has no `OR` for text terms.
It does: `?a ?b` is an any-of, and `a -b` excludes.
What it actually has is worse than absence, and it is what settles the design.
Combining `?` terms with anything else makes CloudWatch **ignore the `?` terms** rather
than reject the pattern, so a mixed expression sent as a `filterPattern` returns the
wrong
events with no error.
So `portable_filter_pattern` translates only what CloudWatch can mean exactly (a single
term, an AND of text, an OR of text, an AND with exclusions, a JSON-only tree with real
`&&` and `||`) and refuses the rest by name at the one boundary that sends a pattern,
which is live tail.
That refusal also fixed a live bug: `--filter level:error` used to reach AWS verbatim,
where it matched no JSON record at all, and is now translated to
`{ $.level = "error" }`.

`docs/docs/FILTER_GUIDE.md` is the reference, since the syntax previously lived only in
`parser.py` docstrings.
Named filters shipped too, as a `[filters]` table read through the same `@name`
convention `[presets]` uses, expanded once where the filter is set rather than at every
reader.
Only a whole filter can be a reference, because a filter is one expression rather than a
list and there is no position where half a substitution would be unambiguous.
Filter history reuses `history.py` as the roadmap required, as a new `HistoryKind`, so
`:history` shows filters beside the rollups and Insights queries with no second store.

**Cache and query performance.** Done on 2026-08-22, and the framing was wrong.
Parsing was already in Rust: Polars does the real decode, and the write path measured
0.39s of Python against 0.64s of Polars over 72,767 events.
What Python was doing twice was *encoding*, not parsing, so the fix was to splice a
message that decodes as a JSON object into the NDJSON line verbatim rather than
re-encoding the dict the check produced.
That took the Python half to 0.22s, about 13% of the whole write and 1 to 2% of a cold
fetch, which is the honest size of it.
Two things stayed: the decode that proves a line is a JSON object (dropping it saves
0.077s and lets one malformed brace-prefixed line make a whole file unreadable), and
`scan_ndjson(infer_schema_length=None)`, because `str.json_decode` now requires an
explicit dtype (Polars 1.33 deprecated inference) and nothing else infers the union of
keys across a file.
Neither the sort nor zstd is worth touching: unsorted measured 0.67s against 0.64s, and
uncompressed 0.60s.
A pretty-printed payload is the trap: it is valid JSON, so it takes the parsed path, and
its newlines would end the NDJSON line early and make the whole file unreadable.
`tests/test_cache.py` covers it.
`tail-cw cache status` shipped on 2026-08-22 and reports files, bytes against the limit,
the oldest and newest window, entries, stale entries, and orphan files as one JSON
object.
Not a hit rate, and not a per-group breakdown: nothing counts reads, and a cache key is
a
BLAKE2b hash of the query, so the group it came from cannot be recovered from it.
It earned itself on the first run by reporting 12 orphan files out of 91, which the next
write swept, so a non-zero orphan count reads as a fetch in flight rather than a leak.
Still open: benchmark targets gated in CI, because ADR 0003's claim that the local
engine
is better at re-filtering was unmeasured until 2026-08-21 and is now measured only once.
A wall-clock threshold on a shared runner is a flake generator, so decide what the gate
actually asserts before writing one.
Memory-aware backend selection was proposed on the grounds that DuckDB spills and Polars
does not, but the Polars path already does `scan_parquet` into
`collect(engine='streaming')`, so the premise is weak; if pursued, use a configured byte
ceiling rather than adding `psutil`.

**Discovery.** Done on 2026-08-22.
The preview pane now lists the group's JSON fields with the share of sampled events
carrying each, merged from the shapes it already computed, which answers "what can I
filter this on" in a way the shape list could not: the same field appears in several
shapes, and a field in every record looked no different from a rare one.
Driving it against `irm-ecs-api-prod` read
`event 100% level 100% logger 100% timestamp 100% request_id 30% span_id 30% trace_id 30%`,
which says at a glance that the
X-Ray pivot only works on a third of the records.
The roster sits above the shapes because the shapes are long enough to push anything
after them off the pane.

Last-write time shipped as the honest form: `saturated` when the capped sample filled up
(so the newest event is unknown), an exact time when the whole window fit, and `quiet`
when nothing landed.
Reporting the newest sampled timestamp instead would have ranked the busiest groups as
the stalest.

`logGroupClass` is read and carried on `LogGroupInfo` with a `supports_live_tail`
property, and an Infrequent Access group is marked on its name rather than in a column
of
its own: the table already loses Created at 160 columns beside the preview pane, and
every group in this account is Standard, so the column would have been blank in every
row.
That also means the Infrequent Access path is unverified against a real IA group.

Shell completion goes through argcomplete, so bash, zsh, and fish come from one place
and
stay in step with argparse, and the import is guarded on `_ARGCOMPLETE` so a normal
invocation still starts in 0.06s.
Group names complete from the recents file, because completion runs on every Tab and no
AWS call belongs at that latency, and CLI-typed names are now recorded there too (globs
are not: a pattern is not a group).
Matching is by prefix rather than the substring matching the pattern resolver does,
since
a shell replaces the word being completed and argcomplete filters non-prefix matches out
regardless.
Verified by driving argcomplete's own protocol rather than by reading the code:
`tail-cw logs irm<TAB>` returns `irm-ecs-api-prod`.

**Plumbing and tooling.** Mostly closed on 2026-08-22, and two of the four items were
already stale when read.

The two `ProgressCallback` definitions are now one, in `tail_cw/progress.py`, on the
superset signature `(current, total, status)` with `TOTAL_UNKNOWN` for a paginated read
that cannot know its total.
Nothing in production passed either of them, which is why the mismatch survived.
Still open: whether to thread it into the TUI at all.
`ProgressUpdate` exists with only DataTable insertion behind it, so a long fetch reports
no count, but the load clock shipped earlier the same day already fixed the part that
mattered (a status line that cannot be told from a hang), and `ResolveLogs` has 44 call
sites to widen for the rest.

Polars' `PanicException` derives from `BaseException`, so it walked past every
`except Exception` in the tool.
`query_parquet_file` now converts it to `EnginePanicError`, a `RuntimeError`, which puts
it back inside every existing handler including the TUI's; the guard spans the schema
read
as well as the rows, because reading a schema is a Polars call too.
The CLI entry point catches it by type name so that catching it costs no import.

CI now runs 3.11 and 3.13 on macOS, Linux, and Windows, plus 3.12 on Linux.
It already ran on Linux, and 3.13 was verified locally against the whole suite before
the
matrix claimed it.
Stdout and stderr are reconfigured to UTF-8 at the entry point, since JSON is UTF-8 by
definition and a legacy Windows code page failed an export on one accented character;
`write_ndjson` takes a stream rather than a path and every file write already named its
encoding, so that half of the item no longer existed.

**Declined.** YAML config: TOML stays the only format, because a second format means a
second parser, an optional dependency, and a forked document for no capability gain.
That choice was never written down, which is the actual gap.
A config wizard prompting on stdin conflicts with one TUI and one NDJSON surface, and
`create_default_config_file` already scaffolds commented TOML.
A visual modal filter builder is rejected: it duplicates the `:` command line and
which-key discovery ADR 0008 chose.
Log group mappings in config are already covered by `[presets]`.
Multi-backend log sources and generic in-TUI AI summarization stay rejected per ADR 0010.

## Landscape (why build this at all)

Dedicated CloudWatch tailers (awslogs, saw, cw, utern) are abandoned or dormant since
2019-2023 and predate the Live Tail API and the newer Insights query languages.
Gonzo is a strong log-analysis TUI with no native CloudWatch source.
The official AWS CloudWatch MCP server covers Insights and pattern analysis for agents
but has no live tail and no human surface.
Grafana's CloudWatch data source is the honest answer for anyone who wants a web GUI.

What is still unclaimed, and so still worth our time: a real TUI over `StartLiveTail`,
log group discovery with metadata in the terminal, one filter model shared across live
and historical and cached data, and a correlation-ID pivot across log groups.
The first three shipped; the fourth is M3.

The 2026-08-21 measurements add a fourth claim that is now evidenced rather than
asserted.
Against the AWS CLI, tail-cw wins on ranking recurring patterns (199,967 events into 29
readable rows, one command), on alarm flapping as a number, and on repeat latency over a
cached window (4.2s against 37.8s).
It loses on first-call latency and on raw Insights speed (4.3s against 3s), and it takes
on interpretation, which is where all four of the day's correctness bugs lived.
Both directions belong in this file, because the losses are what the queue above is for.

## Design principles

- Every feature lands CLI-first with NDJSON output under `tail-cw export`; the TUI is a
    view over the same functions, so agents and humans drive one code path
- Frugal by default: cache everything fetched as Parquet, re-filter locally for free, keep
    default time ranges tight, and show a scan estimate before any paid Logs Insights query
- Frugal locally too: native engines get a minority of the machine (`cpu_budget.py` caps
    DuckDB and Polars at 40% of the CPU count), because a background fetch that takes 7.6 of
    12 cores makes the laptop unusable and buys no wall time when the work is bound by
    CloudWatch's API
- Keep logic in pure functions with side effects at the edges (per AGENTS.md).
    The async migration reinforced this: `aws/` functions take an open client and the pure
    translation code stayed untouched
- Interpretation gets tests. Severity classification, pattern normalization, and bucketing
    were each wrong against real data and right against synthetic data, so every heuristic
    ships with a regression test built from a real log line
- Each milestone ships with ruff, mypy, pyright, and pytest green before the next starts

## Sequencing rationale

Items 1 and 2 come first because they are the same file and the same write path, and
doing them apart means rewriting `cache/storage.py` twice.
Item 1 also unblocks the log table work in item 8, since both need display text built
from `parsed`.
Item 3 is next because it is the largest capability gap and mostly wiring over functions
that already exist.
Items 4 and 5 are small guards that keep the tool honest about incompleteness and cost,
and 4 shares a predicate with 2.
Items 6 through 9 are independent of each other and can be picked up in any order.

Scope growth is the standing risk ADR 0010 named: `tail_cw/` grew 82% in the nineteen
days to 2026-07-25, and another 1,522 lines landed on 2026-08-21.
Two of the nine queue items delete code rather than add it, which is the balance to
keep.
Prefer wiring existing machinery over new subsystems, and prefer sending work to Logs
Insights over reimplementing it.
