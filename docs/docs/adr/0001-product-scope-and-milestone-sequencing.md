# ADR 0001: Product scope and milestone sequencing

Date: 2026-07-05 Status: Accepted

## Problem

tail-cw at v0.0.1 had well-tested modules (filter DSL, Parquet cache, trace grouping, TUI viewers) that were not wired together. Running `tail-cw` opened an empty table. Before investing further, we needed to decide what the tool should become so that it is genuinely useful for day-to-day CloudWatch work: dev-loop tailing after a deploy, incident triage across Lambda/ECS/CodeBuild groups, exploratory search with quick activity graphs, and orientation ("what are we even logging, what is noisy"), all in one SSO account with two profiles.

## Landscape (mid-2026)

Every dedicated CloudWatch tailer predates 2023 and is abandoned or dormant.

| Tool                            | State                   | Notes                                                                      |
| ------------------------------- | ----------------------- | -------------------------------------------------------------------------- |
| awslogs                         | Unmaintained            | Most popular, predates Live Tail and Insights                              |
| saw                             | Dead (2019)             | `--expand` JSON pretty-print was its best idea                             |
| cw (lucagrulla)                 | Dormant (2023)          | Best pure tailer, JMESPath, SSO                                            |
| utern                           | Maintenance only (2022) | stern-inspired multi-stream tail                                           |
| Gonzo                           | Active                  | Strong analysis TUI, no native CloudWatch source (pipe `aws logs tail` in) |
| aws logs tail / start-live-tail | Native                  | Polling or bare streaming pane, no TUI                                     |
| AWS CloudWatch MCP server       | Active                  | Insights and pattern analysis for agents, no live tail, no human surface   |

Unclaimed territory: a real TUI over `StartLiveTail`, terminal log group discovery with metadata, one filter model across live/history/cache, correlation-ID pivot across groups, and surfacing the server-side `pattern`/`diff`/anomaly APIs.

Explicitly not worth building: plain multi-group colored tailing (solved several times over) and generic in-TUI AI summarization (Gonzo does this well over piped input).

## Options considered

1. Discovery first (M0 → discovery → live tail → investigation): discovery makes every later feature usable and is cheapest to build on the existing fetch path
1. Live tail first (M0 → live tail → discovery → investigation): dev-loop tailing is the daily driver, so ship the daily habit first
1. Investigation first (M0 → histograms/patterns): highest novelty but useless until data can actually be fetched and found

## Decision

Sequence M0 → M1 (live tail) → M2 (discovery) → M3 (investigation). Option 2 won because dev-loop tailing is the most frequent daily task, and the user typically already knows the log group names for the services they deploy. Discovery (M2) then unblocks triage and orientation, and investigation tools (M3: correlation-ID pivot, Insights `pattern`/`diff`, histograms, later Sentry/PostHog linking) build on both.

Design principles adopted for all milestones:

- every feature lands CLI-first with `--json` NDJSON output, and the TUI is a view over the same functions (AI agents consume the CLI)
- stdlib argparse subcommands: `fetch`, `tail`, then `groups` and `query`
- frugal by default: cache fetched data as Parquet, re-filter locally for free, keep default time ranges tight
- pure functions with side effects at the edges, per AGENTS.md

## Consequences

- Each milestone ships with ruff/mypy/pyright/pytest green before the next starts
- The roadmap lives in `plans/roadmap-2026-07.md`; this ADR records why the order is what it is
- Live tail shipped before discovery, so until M2 the user must know log group names (acceptable for the dev-loop persona)
- On 2026-07-24 a dashboards-and-metrics milestone (M4, see ADR 0005) was prioritized ahead of the remaining M2 discovery and M3 investigation work, because reading console dashboards and exploring metrics from the terminal became the current need. It depends only on the M0 cache and query engine, so the reorder costs nothing structurally
