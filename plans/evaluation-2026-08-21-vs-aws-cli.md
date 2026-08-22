# tail-cw against the AWS CLI, measured on real investigations

Six production questions were taken end to end against Coverbase's prod and stage
accounts on 2026-08-21: two CloudWatch alarms, an alarm's trace, a cross-service error
rollup, a stage database alarm, and a client-latency complaint.
Everything below is measured on those runs rather than estimated.
The write-ups live in `irm-0-null/docs/investigations/`.

Summary: tail-cw won decisively on capability and on the accuracy of what it reports,
won large on repeat latency, lost slightly on first-call latency, and is free where the
AWS CLI is free and billed where it is billed.
The interesting result is that the biggest wins came from features that did not exist
when the day started, and the biggest losses were bugs that only real data exposed.

## Latency

| Task                                 |               AWS CLI |                tail-cw | Notes                                  |
| ------------------------------------ | --------------------: | ---------------------: | -------------------------------------- |
| Process startup, no work             |                 0.28s |                  0.62s | tail-cw pays 0.35s more per invocation |
| One group, one hour, raw events      |    37.8s (87 MB JSON) |             57.8s cold | tail-cw also writes the Parquet cache  |
| Same, repeated                       |           37.8s again |               **4.2s** | 13.8x, and the repeat needs no network |
| 12 groups, one hour, ranked patterns | not directly possible | 2m 05s cold, 8.7s warm |                                        |
| One group, one week, counted per day |         3s (Insights) |        4.3s (Insights) | AWS CLI is faster here                 |
| Same week via FilterLogEvents        |                     — |     >20 min, abandoned | why Insights was added                 |

Two honest losses. First-call latency is worse: 57.8s against 37.8s for the same hour,
because tail-cw converts to Parquet on the way past.
Second, on Insights the AWS CLI is genuinely quicker — 3s against 4.3s — from tail-cw's
0.35s startup, its 0.5s result-polling granularity, and an extra `DescribeLogGroups`
call to resolve glob patterns into group names.
That last one is a deliberate trade: `'irm-prod-ecs-*'` costs one API round trip and
saves typing five names correctly.

The win is repetition. Every fetch lands in a ZSTD Parquet file, so the second question
about the same window costs 4.2s and no API call, against the AWS CLI re-downloading 87
MB.
Since real investigations are a dozen questions about one window, this dominates in
practice.

**A cache defect found while measuring:** the cache key is the exact window, so relative
windows never hit it.
Two consecutive `--start 1h` runs took 69s each and scanned different event counts,
because `now` moved between them.
Overlapping ranges do not reuse either: having cached 17:00-18:00 and 18:00-19:00,
asking for 17:00-19:00 refetches everything.
Fixing this means snapping windows to bucket boundaries and composing cached segments,
and it would turn the 13.8x repeat win into the common case rather than something you
get only by passing ISO timestamps.

## Capability

Five of the six investigations needed something tail-cw could not do at the start of the
day.

| Question                                  | Before                | Added                     |
| ----------------------------------------- | --------------------- | ------------------------- |
| Grouped errors by frequency, deduplicated | no aggregation at all | `export summary`          |
| Same, counted per day over a week         | no time bucketing     | `--by day`                |
| Alarm history, is it flapping             | nothing               | `export alarms --history` |
| A metric's values without a dashboard     | dashboards only       | `export metrics`          |
| Aggregate a week server-side              | nothing               | `export insights`         |

Against the AWS CLI, three capabilities have no equivalent short of writing a script.

**Ranking recurring patterns.** `aws logs filter-log-events` returns 87 MB of JSON for
one busy group-hour.
Turning that into "these are the 20 things production complains about" needs
normalization, clustering, and counting.
`export summary` does it in one command and collapsed 199,967 events into 29 readable
patterns.

**Alarm flapping as a number.** `describe-alarm-history` exists, but nobody runs it per
alarm and counts.
Ranking every stage alarm by transitions in 14 days took one command and immediately
surfaced `irm-stg-radar-daemon-high-cpu` at **52 transitions**, five times the next
worst.
That finding was free and nobody had asked for it.
The same command showed the ECS MCP memory alarm going OK → ALARM → OK → ALARM inside
three minutes on 19 Aug.

**Insights ergonomics.** The AWS CLI's Insights path is `start-query`, then a poll loop,
then `get-query-results`, then unpacking a nested `[{field, value}]` array per row.
tail-cw is one command that resolves globs to group names, polls, prints a markdown
table, and reports what it scanned.
It is 1.3s slower and about fifteen lines of shell script shorter.

Where the AWS CLI still wins: anything outside logs, metrics, alarms, and dashboards.
It also wins on writes, which tail-cw deliberately cannot do.

## Accuracy and precision

This is where measuring on real data paid for itself, because every accuracy problem
found was in tail-cw, not in the AWS CLI, and none of them would have shown up on
synthetic logs.

**Severity was wrong 25% of the time.** The first cross-service rollup reported 836
errors in an hour.
The real number is 625. Three separate defects:

- A record declaring `{"level": "info", "message": "error finding route"}` was classified
    ERROR because the body was keyword-scanned on top of the declared level.
    A declared level is now authoritative; a `status` field still escalates, because it is
    structured rather than prose.
    Removed 59 false errors an hour.
- `WARNING: Bedrock transient error` was an ERROR, because the body contains "error".
    Text that labels its own level is now read from the label.
- The CloudWatch agent's `<ts> I!` info lines were errors for the same reason.
    Level prefixes are now recognized behind a leading timestamp, and in the single-letter
    `E!`/`W!`/`I!` form.
    Removed a further 152 an hour.

**Pattern grouping over-split by 100x.** The first run turned 3,763 matched events into
1,733 distinct shapes, which is not a report.
Two causes, both proven before fixing:

- `_HEX_RE` anchored on `\b`, and `_` is a word character, so a hex run inside a prefixed
    identifier never matched: `cborg_b7deea1ad2a3ce4a5b6` shredded into
    `cborg_b<n>deea<n>ad<n>a<n>ce<n>a<n>b<n>`, keeping the id's letters and producing one
    shape per organization and per user.
    This also degraded the group browser's preview pane.
- Keying on the whole JSON record split one recurring event across every optional field
    and every entity id.
    Keying on level, logger, and the message body instead took 3,982 shapes to **39**, with
    the top 20 covering 98% of volume.

A fuzzy pass then merges shapes differing only in a literal phrase, folding nine
variants of `Skipping upsert for legacy custom field <*>` into one row of 832.
Structural keying first is what makes the quadratic comparison affordable: it runs on
tens of keys, not thousands.

**A crash, and silent data loss behind it.** A week-long multi-group fetch died with a
Rust panic from `sink_parquet`.
The cause was `infer_schema_length=1000`: a field that is null in the first 1,000 rows
and a string later panics the writer, an int-then-string field fails to parse, and —
worse, because it is invisible — a field first appearing after row 1,000 is **silently
dropped and becomes unqueryable**.
Sampling a schema over arbitrary log JSON cannot work, so the option is gone and the
whole file is scanned.
The guard for it was confirmed to fail without the fix.

**A bucketing off-by-one.** Every hourly trend rendered `█▁` because a window ending
exactly on a boundary invented a trailing empty bucket.
The range is now half-open.

By contrast, the AWS CLI has no accuracy surface to get wrong: it returns what
CloudWatch returns.
That is the fair reading of this section — tail-cw takes on interpretation, and
interpretation is where bugs live.
The value is that the interpretation is now tested against 875 tests including the four
regressions above, rather than living in a `jq` one-liner rewritten per investigation.

## Cost

`FilterLogEvents` and `DescribeAlarms` are not billed per call, so the raw-retrieval
path is free in both tools and the only cost is time and bandwidth.
tail-cw's Parquet cache reduces repeat bandwidth to zero; the AWS CLI re-downloads.

Insights bills per gigabyte scanned, about $0.005/GB.
The queries in these investigations
scanned 0.3-2.4 GB each, so roughly $0.002-$0.012 apiece, and the whole day's work was
well under a dollar.
Because that is real money, Insights is opt-in: nothing routes through it unless
`export insights` is invoked, and every run prints what it scanned to stderr —
`292,676 of 3,293,189 records matched, 1.149 GB scanned` — so cost is visible at the
moment it is incurred rather than on a bill.
The AWS CLI reports the same statistics but only if you ask for them.

The cost nobody was tracking was local. A backgrounded multi-group fetch was measured at
**906% CPU, 7.6 of 12 cores**, because DuckDB and Polars each size their thread pool
from the CPU count while the blocking pool runs several of their calls at once.
Both are now capped at 40% of the machine, overridable by `TAIL_CW_CPU_FRACTION` or
`TAIL_CW_MAX_THREADS`:

|          |      Peak CPU | Wall time |
| -------- | ------------: | --------: |
| Uncapped | 700% of 1200% |       41s |
| Capped   | 374% of 1200% |       38s |

The cap costs nothing measurable, because the work is bound by CloudWatch's API rather
than by local cores.
The AWS CLI never had this problem; it does no local processing.

## What the investigations actually found

Worth recording, because it is the real test of whether the tool is any good.
Two findings came from the tool noticing something nobody asked about:

- Worker OTLP spans are being thrown away, 334 errors an hour, exporting to Hatchet's gRPC
    port which does not implement the trace service.
    Surfaced by ranking errors by frequency.
- A stage alarm has changed state 52 times in 14 days.
    Surfaced by ranking alarms by transition count.

And the trace reconstruction is the clearest single argument for the tool.
Starting from a `bedrock_call_id` in a Slack message, four Insights queries produced:
five retries exactly ten minutes apart, two controls each burning 40 minutes and
returning nothing, and the fact that `ReadTimeoutError` did not exist in prod before 19
Aug, occurs only on `claude-sonnet-4-6` (102 of 102), and went 2 → 13 → 87 over three
days.
Doing that with the AWS CLI is possible and would have taken an afternoon of shell
scripting.

## Where this leaves the tool

Highest-value work next, in order:

1. Fix the cache window keying, so relative and overlapping windows reuse cached segments.
    It turns the best measured result (13.8x) from a special case into the default.
1. The log table's column budget and severity colouring, both detailed in
    `plans/tui-critique-2026-08-21.md`.
    It is the most-looked-at surface and the least designed.
1. `export trace <id>`: the Bedrock investigation took four hand-written queries to go from
    an identifier to a per-span timeline.
    `query/trace.py` already has the grouping; what is missing is the entry point.
1. Route-level dimensions are a recurring theme in what the investigations could not answer
    from metrics alone.
    Worth a look at whether tail-cw should help discover which dimensions a metric actually
    carries, since that was the blocker on the latency question.
