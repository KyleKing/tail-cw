# ADR 0007: VictoriaLogs and Grafana as a replacement candidate

Date: 2026-07-24 Status: Proposed (open question, no code commitment yet)

## Problem

tail-cw is roughly 2000 lines of Python that depend on pyarrow, duckdb, polars, and textual, and the roadmap keeps adding surface: dashboards (M4), then discovery (M2), investigation (M3), and eventually tracing. A parallel exploration (originally on the `victorialogs` branch, now folded here) asked a fair question: does a mature open-source observability stack already do most of this with far less code to own?

Two facts sharpen the question:

- The use case that started this project is a root-cause task across services ("investigate the tasker logs during elevated queue size, build a timeline, explain the congestion with graphs"). That is a distributed-tracing problem across an API and Kafka-worker topology, and tail-cw has no tracing. The roadmap only reaches a correlation-ID pivot at M3, and true span-based tracing is not planned at all
- A pull-based demo (`demo/`) ingests CloudWatch logs into VictoriaLogs with about 100 lines of shell, then queries them in Grafana or the built-in VMUI. It runs in about two minutes with Docker as the only dependency

So before pouring more effort into custom code, we should decide honestly whether the custom tool is still the right vehicle.

## The candidate stack

- VictoriaLogs for log storage and query. LogsQL covers filtering, `stats by` aggregation, `unpack_json` field extraction, and `stream_context before N after N` for surrounding-log context. Single binary, fast, self-hosted, no managed option
- VictoriaTraces for spans. It ingests OTLP and exposes a Jaeger-compatible query API, so Grafana reads it through the Jaeger datasource. Actively released through July 2026
- Grafana for dashboards and multi-source correlation, with the official `victoriametrics-logs-datasource` plugin. Derived fields pivot from a `trace_id` or `request_id` in a log line to all related logs, which is the same pivot tail-cw plans to build by hand at M3
- Ingestion stays pull-based: `aws logs filter-log-events` for logs (see `demo/ingest.sh`) and `aws xray get-trace-summaries` / OTLP export for traces. No change to production ECS beyond adding an OTel SDK where tracing is wanted

## Options considered

1. Keep building tail-cw. Terminal-native, one CLI with NDJSON output that agents consume, no Docker or web browser, offline Parquet cache. Cost: every feature (dashboards, discovery, tracing) is ours to write and maintain, and tracing is a large new subsystem
1. Adopt the candidate stack and wind tail-cw down. Logs, traces, and dashboards come from maintained projects; our code shrinks to ingestion glue plus dashboards-as-config. Cost: a web UI instead of a terminal, a local Docker footprint, the loss of the CLI-first and agent-first surface that ADRs 0001 and 0002 were built around, and self-hosting VictoriaLogs/VictoriaTraces
1. Hybrid. tail-cw stays the fast terminal path for live tail and dev-loop work; the candidate stack handles heavy historical analysis and tracing. Cost: two things to run, and a blurred story about which tool to reach for

## Decision

Deferred. This ADR does not pick a direction. It records the candidate, keeps the runnable demo (`demo/`) on main so the comparison stays honest, and sets a gate: do not build tracing (OTel ingestion, a span store, a trace UI) into tail-cw until this question resolves, because that is the largest piece of net-new code and the piece the candidate stack most clearly already solves.

Evaluate against these criteria, then supersede this ADR with the decision:

- how often the real work is terminal-bound (over SSH, in an agent loop, no browser) versus fine in a local web UI
- whether the agent/NDJSON surface (ADR 0002) is load-bearing for the intended workflow or a nice-to-have
- the true operational cost of self-hosting VictoriaLogs and VictoriaTraces for the log and trace volume in play
- whether tracing is a firm requirement (if yes, that weighs heavily toward the candidate stack, which gets it for free)
- the maintenance cost trend of the custom stack as M2 and M3 land

Concrete next steps, from the folded proposal:

1. Run `demo/` against a real CloudWatch log group and judge the LogsQL and Grafana experience against the tail-cw TUI for an actual triage
1. Add an OTel SDK to one API container and validate Kafka `traceparent` propagation in staging, so end-to-end API-to-worker traces are real, not hypothetical
1. Pull X-Ray traces into VictoriaTraces locally and confirm the Jaeger-API query path in Grafana

## Consequences

- ADRs 0001 through 0006 still stand. This ADR sits above them as a scope question, not a change to any shipped decision
- The `demo/` directory is documentation and a benchmark, not a supported product path, until this resolves
- Regardless of the outcome, three ideas from the candidate feed the roadmap now: span-based tracing as a first-class gap, LogsQL-style investigation features (`stream_context`, `stats`, JSON field extraction) for the M3 query engine, and the derived-field trace-to-logs pivot as validation of the M3 correlation-ID design. See `plans/roadmap-2026-07.md`
- If option 2 wins, expect to deprecate most of `tail_cw/` and keep only ingestion glue; if option 1 or 3 wins, this ADR is superseded and tracing gets its own milestone
