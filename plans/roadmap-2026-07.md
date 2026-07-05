# Roadmap: make tail-cw a daily-driver CloudWatch tool

Written 2026-07-05, based on a code capability review and a survey of the CloudWatch tooling landscape.

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

### M2 (Avenue A): discovery and orientation

Goal: answer "what log groups exist, which are active, what shape are the logs" without the console.

- `tail-cw groups` (and TUI opening screen): `DescribeLogGroups` with fuzzy find, last-event time, stored bytes, retention, log class
- "sample this group" action: pull N recent events, show inferred JSON field schema from the existing JSONL detection
- group presets/favorites in config

### M3 (Avenue C): investigation tools

- correlation-ID pivot: select a request/trace ID in any event, fan out a search across related log groups (builds on `query/trace.py`)
- Logs Insights `pattern`/`diff` subcommand and TUI action for warning/error spelunking, with a scan-size estimate shown before running
- time-bucketed histogram/sparkline of the current view for quick activity graphs
- later: matcher hooks to auto-link events to Sentry/PostHog issues

## Sequencing rationale

M1 before M2 because dev-loop tailing is the daily driver. M2 before M3 because pivot and pattern tools need group discovery to be usable. Each milestone ships CLI + tests green (ruff, mypy, pyright, pytest) before the next starts.
