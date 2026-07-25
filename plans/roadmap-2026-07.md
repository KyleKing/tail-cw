# Roadmap: make tail-cw a daily-driver CloudWatch tool

Written 2026-07-05, based on a code capability review and a survey of the CloudWatch tooling landscape. Updated 2026-07-06 to prioritize navigation-first UX (M2) after M0/M1 shipped.

## Problem

The package is at 0.0.1 with well-tested modules that are not wired together. Running `tail-cw` opens an empty table:

- `fetch_log_events` (`tail_cw/aws/client.py`) calls only `FilterLogEvents` and is never invoked outside tests
- `LogCache` (Parquet + DuckDB/Polars engine) works but the TUI never reads or writes it
- The TUI data entry points (`load_events`, `set_parquet_source`) are only called from tests
- `__main__.py` has no argument parsing; refresh and copy-to-clipboard are stubs
- No `StartLiveTail`, no `DescribeLogGroups`, no Logs Insights, no profile selection

Day-to-day needs this must serve: dev-loop tailing after a deploy, incident triage across Lambda/ECS/CodeBuild groups, exploratory search and quick ad-hoc activity graphs, orientation ("what are we even logging, what's noisy"), all against one SSO account with two profiles.

## Landscape (why build this at all)

Dedicated CloudWatch tailers (awslogs, saw, cw, utern) are abandoned or dormant since 2019-2023 and predate the Live Tail API and the newer Insights query languages (PPL, SQL). Gonzo is a strong log-analysis TUI but has no native CloudWatch source. The official AWS CloudWatch MCP server covers Insights and pattern analysis but has no live tail and no human surface. Unclaimed in 2026:

- a real TUI over `StartLiveTail` (WebSocket, up to 10 groups, 3h sessions)
- log group discovery with metadata (last write, size, retention) in the terminal
- one filter model that works identically across live tail, historical fetch, and cached data
- correlation-ID pivot across log groups
- surfacing server-side `pattern`/`diff`/anomaly-detection APIs outside the console

Explicitly not worth building: plain multi-group colored tailing (solved by `aws logs tail`, cw, utern) and generic in-TUI AI summarization (Gonzo does this over piped input).

## Design principles

- Every feature lands CLI-first with `--json` NDJSON output; the TUI is a view over the same functions. AI agents consume the CLI
- Subcommand CLI via stdlib argparse: `tail-cw tail`, `tail-cw fetch`, `tail-cw groups`, `tail-cw query`
- Frugal by default: cache everything fetched as Parquet, re-filter locally for free, keep default time ranges tight
- Keep logic in pure functions with side effects at the edges (per AGENTS.md)

## Milestones

### M0: wire the drivetrain

Goal: `tail-cw fetch <group> --start 2h --profile X` pulls events through cache into the TUI.

- argparse subcommand skeleton in `__main__.py` (`fetch` first; `tail`, `groups`, `query` reserved)
- flags: log group, `--start`/`--end` (relative like `2h` and absolute), `--filter`, `--profile`, `--region`, `--config`, `--json` (NDJSON to stdout, no TUI)
- thread `profile_name` through a `boto3.Session` in `aws/client.py`
- pipeline: cache-key lookup → `fetch_log_events` on miss → `LogCache` write → `set_parquet_source` → `app.run()`
- make `action_refresh` re-fetch the tail of the current range; implement clipboard copy or remove the binding

### M1 (Avenue B): live tail with a unified filter model

Goal: `tail-cw tail <group...>` streams via `StartLiveTail`; the existing filter DSL applies identically to the live stream, historical backfill, and cached Parquet.

- `StartLiveTail` client wrapper (handle 3h session expiry, >500 events/sec sampling, reconnect)
- ring buffer (`deque`) feeding coalesced TUI updates; periodic flush into the Parquet cache so scrollback past tail-start and post-hoc re-filtering are free
- one filter expression evaluated three ways: pushed down to Live Tail's server filter where expressible, `FilterLogEvents` pattern for backfill, query engine for cached data
- `--json` streaming NDJSON mode for agents and piping

### M2 (Avenue A): navigation-first discovery

Goal: the user should never need to know an exact log group name. Every entry point either resolves a loose pattern or drops into an interactive browser.

**Delivered.** The bulk landed on 2026-07-24 with the single-TUI work ([ADR 0008](../docs/docs/adr/0008-single-interactive-tui.md)), which took the browser as the app's home view rather than as a fifth subcommand: the resolution ladder (`resolve_group_pattern`), `DescribeLogGroups` metadata, `/` filtering, ten-group multi-select, and a preview pane that goes further than this plan asked by clustering each group's distinct message shapes instead of dumping the last N lines. Recents and named presets followed on 2026-07-25, closing the milestone.

Two deviations from the text below, both deliberate. There is no `tail-cw groups` subcommand, because the browser is the home view and `tail-cw export groups` covers the NDJSON case. Sorting by last-event time is not implemented, because `DescribeLogGroups` does not return it (that is a `DescribeLogStreams` call per group); recency of your own selections sorts the list instead, which is the behaviour the console's "recently accessed" actually gives you.

How comparable tools handle this, and what we take from each:

| Tool                 | Group selection UX                                                         | Takeaway                                                     |
| -------------------- | -------------------------------------------------------------------------- | ------------------------------------------------------------ |
| `aws logs tail`      | Exact name required; community wraps it in `describe-log-groups \| fzf`    | The gap to close; don't require exact names                  |
| utern / cw / awslogs | Group argument is a regex/prefix resolved against all groups               | Accept patterns everywhere a group is accepted               |
| CloudWatch console   | Multi-select up to 10, surfaces recently accessed groups first             | Keep local recents and sort them to the top                  |
| k9s                  | Never type a resource name: `/` fuzzy-filters a live list, Enter drills in | The browser is the home screen, not a subcommand you look up |
| stern (k8s)          | Pod query is a regex; new matches join the tail automatically              | Pattern selection composes with live tail                    |

Deliverables, in order:

1. Group resolution layer (pure function, CLI-first)
    - anywhere a `<group>` is accepted (`tail`, `fetch`), resolve in stages: exact match, then server-side `logGroupNamePrefix`, then server-side `logGroupNamePattern` (substring, verified in botocore), then client-side fnmatch for `*` globs
    - unambiguous single match proceeds silently; multiple matches for `tail` expand up to the 10-group Live Tail cap; multiple matches for `fetch` or >10 for `tail` open the picker pre-filtered (TTY) or list candidates to stderr and exit 1 (non-TTY/`--json`)
1. `tail-cw groups [pattern]` subcommand
    - `DescribeLogGroups` metadata table: name, last-event time, stored bytes, retention, log class; `--json` NDJSON for agents and for piping into fzf
    - sorted by last-event time descending by default so active groups surface first
1. Recents and presets
    - record each resolved group selection in local state (XDG data dir, not config); picker and `groups` sort recents first, mirroring the console's recently-accessed behavior
    - named presets in config (`[presets] api = ["/aws/lambda/api-a", "/ecs/api-b"]`), usable as `tail-cw tail @api`
1. Interactive group browser as the TUI home screen
    - bare `tail-cw` (TTY) opens the browser instead of exiting 2: fuzzy find over the metadata table, space multi-selects up to 10, Enter starts live tail, `f` fetches a recent window (supersedes the M0 bare-command behavior; non-TTY keeps help + exit 2)
    - preview pane for the highlighted group: last N events via a small `FilterLogEvents` call plus inferred JSON field schema from the existing JSONL detection, so groups are identifiable by content, not just name

### M3 (Avenue C): investigation tools

- correlation-ID pivot: select a request/trace ID in any event, fan out a search across related log groups (builds on `query/trace.py`). VictoriaLogs proves this pattern with Grafana derived fields that jump from a `trace_id` in a log line to all related logs, so the design is sound; see [ADR 0007](../docs/docs/adr/0007-victorialogs-grafana-replacement-evaluation.md)
- Logs Insights `pattern`/`diff` subcommand and TUI action for warning/error spelunking, with a scan-size estimate shown before running
- time-bucketed histogram/sparkline of the current view for quick activity graphs
- LogsQL-inspired query-engine features worth stealing (seen in the VictoriaLogs demo): surrounding-log context (`stream_context before N after N`), `stats by (field)` aggregation, and inline JSON field extraction (`unpack_json`) so structured fields are filterable without a regex
- later: matcher hooks to auto-link events to Sentry/PostHog issues

### Open question: is the custom stack the right vehicle? (added 2026-07-24)

A parallel exploration weighed replacing tail-cw with VictoriaLogs, VictoriaTraces, and Grafana fed by a pull-based CloudWatch ingest (see `demo/` and [ADR 0007](../docs/docs/adr/0007-victorialogs-grafana-replacement-evaluation.md)). Two points bear on the roadmap:

- Distributed tracing is a real gap. The founding use case (build a timeline across an API and its Kafka workers, explain a congestion root cause) is a span-based tracing problem, and tail-cw has none. The correlation-ID pivot at M3 is the closest we get. True tracing would be a large new subsystem
- ADR 0007 sets a gate: do not build tracing into tail-cw until the replacement question resolves, because tracing is the largest piece of net-new code and the piece the candidate stack (VictoriaTraces over OTLP, Jaeger-compatible) most clearly already solves

So no tracing milestone is scheduled yet. If the evaluation keeps tail-cw (ADR 0007 options 1 or 3), tracing becomes its own milestone then.

### M4: CloudWatch dashboards and metric exploration (prioritized next, added 2026-07-24)

Goal: read the dashboards and metrics you already keep in the console from the terminal, reshape a chart with keyboard-driven inputs, and jump from any chart straight into the logs behind it.

Built next, ahead of finishing M2 and M3, because bringing dashboard insight into the terminal is the current need. It depends only on the cache and query engine (both shipped in M0), not on discovery.

Three features:

- import and render: `tail-cw dashboards` lists dashboards via `ListDashboards`, `tail-cw dashboard <name>` pulls the exact console JSON via `GetDashboard` and lays its widget grid (x/y/w/h) onto a Textual `Grid`. Metric widgets render as charts, log widgets run their Logs Insights query through the existing engine, text widgets render markdown. A tail-cw-native TOML dashboard produces the same typed model, so saved explorations and console imports share one render path
- metric panels: translate the console `metrics[]` shorthand (positional `.` ditto references, trailing option objects, and metric-math `expression` rows) into `GetMetricData` `MetricDataQueries`, one batched call per panel. Results cache to disk keyed by query hash and window, which cuts latency and API-request count on refresh (the dollar cost is already negligible, see the cost note below)
- explore and dive: the focused chart takes keyboard inputs to change period, statistic, and time range (vim-style range motions, no mouse), re-rendering only that chart. From a chart or log widget, one key opens the existing log table filtered to the widget's time window and log group (or its Insights query), reusing the M3 pivot mechanism rather than inventing a new one

Charts render natively with plotext (braille curves plus real text axes and legend), after an image-protocol approach (matplotlib over Kitty TGP) proved unstable in Textual; see [ADR 0006](../docs/docs/adr/0006-dashboard-rendering-and-interaction.md). Verified against the account on 2026-07-24: `GetDashboard` on `irm-prod-main` returned 43 widgets (28 metric, 8 text, 6 log, 1 alarm) with metric-math and Logs Insights bodies intact, and a `GetMetricData` call with a metric-math availability expression returned in about 0.7s.

Cost note: `GetMetricData` and `GetDashboard` bill at $0.01 per 1,000 requests, so a full 28-widget dashboard refresh costs well under a tenth of a cent. Caching metric results is for latency and request-quota headroom, not for saving money. Logs Insights (used by log widgets) stays the one paid path to watch at ~$0.005 per GB scanned, so log-widget queries show a scan estimate before running, same as the M3 rule.

### M5: async AWS I/O over aiobotocore (added 2026-07-25)

Goal: every CloudWatch call is a coroutine, the Textual thread workers become async workers, and a cancelled request stops sending bytes instead of running to completion in a thread we no longer read.

Three problems in the shipped code motivate this, all visible once a dashboard has a few dozen widgets:

- Textual thread workers run on asyncio's default executor (`run_in_executor(None, ...)`, `textual/worker.py:326`), which caps at `min(32, cpu_count + 4)`. On a 12-core machine that is 16 slots shared by every metric panel, log-volume sparkline, Parquet load, and the live tail worker, which holds one slot for the life of a three-hour session. The verified `irm-prod-main` dashboard has 28 metric widgets, so a refresh fetches in two waves
- `exclusive=True` cancels the worker, not the HTTP request. A debounced dashboard reload or a time-range change abandons the thread mid-`GetMetricData`, so the old request still completes, still bills, and still burns a slot. Async workers cancel the coroutine and close the connection
- Every AWS function builds its own client (`build_client`, `_create_logs_client`), which re-resolves SSO credentials and opens a fresh connection pool per call. One session-scoped client fixes this and is a prerequisite for the async version, where clients are context managers with real lifetimes

Feasibility was checked before scheduling, and the two things that could have blocked it do not:

- `StartLiveTail` needs an async event stream. `aiobotocore` provides `AioEventStream` with `__aiter__`/`__anext__`, and its `__iter__` raises `NotImplementedError('Use async-for instead')`, so `live_tail.py` becomes `async for chunk in response['responseStream']`
- SSO with two profiles needs the async credential chain. `aiobotocore` ships `AioSSOProvider` over `AioSSOTokenProvider` and honours `profile_name`, so `sso-session` config resolves the same way

Take `aiobotocore` directly, not `aioboto3`. The project only ever creates clients, so the `boto3`-shaped resource and file-transfer layer that `aioboto3` wraps is unused, and skipping it drops `aioboto3` plus `aiofiles` from the tree. That still adds `aiohttp`, `aioitertools`, `multidict`, and `wrapt`, which is the dependency cost to accept under the AGENTS.md "fewest dependencies" rule.

The standing cost is the version pin. `aiobotocore` 2.25.1 requires `botocore>=1.40.46,<1.40.62`, a sixteen-patch window, and the lock currently sits at 1.40.61. Every new AWS API or region update waits on an `aiobotocore` release, so `boto3>=1.35.0` in the `aws` dependency group gets replaced by a pin that Renovate cannot bump alone.

Deliverables, in order:

1. Session-scoped client, still sync. Build one client per service per session at startup, thread it through `ShellServices`, and delete the per-call `build_client` hops. Land this alone first, because it is the only piece that carries a benefit without the async migration and it defines the seam the rest of the work moves through
1. Fan-out fix, still sync. Drive the metric panels from one worker over an explicit `ThreadPoolExecutor` instead of 28 `run_worker(thread=True)` calls, so the wave behaviour goes away whether or not the async work lands
1. Async producers in `tail_cw/aws/`. `fetch_log_events`, `describe_log_groups`, and `stream_live_tail` become `AsyncIterator[...]`; `fetch_metric_data`, `list_dashboards`, and `get_dashboard` become coroutines. The pure translation code (`build_metric_data_queries`, `resolve_group_pattern`, the dashboard parsers, `_live_event_to_log_event`) does not change, which is most of the line count in those modules
1. Async consumers. The six `run_worker(..., thread=True)` sites become async workers and roughly twenty `call_from_thread` hops disappear, since an async worker already runs on the message loop. The CLI keeps its sync signature with one `asyncio.run` at the dispatch boundary so `--json` NDJSON streaming stays a plain pipe
1. Tests. The five `tests/test_aws_*.py` modules fake boto clients with `get_paginator` stubs and need async equivalents, plus `anyio` or `pytest-asyncio` as a dev dependency. Annotate async generators as `AsyncIterator[X]` so beartype's exact-annotation checking passes

Client lifetime is the trap to design around. An async client is an `async with` context manager, so an async generator that yields events has to hold the client open across its `yield` and every consumer has to exhaust it or call `aclose()`, or aiohttp logs unclosed-connector warnings. Deliverable 1 avoids most of this by owning the client at the session level instead of inside each generator.

## Sequencing rationale

M1 before M2 because dev-loop tailing is the daily driver. M2 before M3 because pivot and pattern tools need group discovery to be usable. M4 (dashboards) jumped ahead of M2 and M3 on 2026-07-24 because terminal dashboard insight became the priority, and it needs only the M0 cache and query engine. Each milestone ships CLI + tests green (ruff, mypy, pyright, pytest) before the next starts.

M5 (async I/O) comes after M4 rather than before it, even though M4's 28-panel fan-out is the case that most wants async, because M4's render and interaction code is already built and migrating it now would mean rewriting worker plumbing that works. Its first two deliverables (session-scoped client, explicit executor for the fan-out) stay useful on their own, so they can land during M3 without committing to the dependency pin.

M0 and M1 shipped 2026-07-05. M2 was then elevated from "a groups listing" to navigation-first UX: requiring exact group names is the single largest friction left (it is also the gap every fzf-wrapper workaround exists to paper over), and pattern resolution plus the browser make `tail` and `fetch` usable from a cold start. `DescribeLogGroups` is free, so none of this adds cost pressure.
