# ADR 0010: Keep tail-cw, and narrow what it builds

Date: 2026-07-25 Status: Accepted (supersedes [ADR 0007](./0007-victorialogs-grafana-replacement-evaluation.md))

## Problem

ADR 0007 asked whether VictoriaLogs, VictoriaTraces, and Grafana should replace this tool, and deferred the answer behind five criteria and a gate on tracing. This record answers it.

Two of that ADR's premises turned out to be false, which is most of why the answer differs from what it expected.

It describes tail-cw as "roughly 2000 lines of Python". On the day it was written the figure was 7,965 lines under `tail_cw/`, and it is 10,451 today, or 21,901 counting tests. The ADR understated the thing it was weighing by about four times.

It frames the founding use case as a timeline across an API and a Kafka-worker topology. That topology is gone, replaced by Hatchet. The underlying need is unchanged and still firm: follow one request across the log groups of several services and explain what happened.

## What the evidence says

Verified 2026-07-25. Sources are listed at the end.

### Tracing stopped being a reason to switch

This is the finding that decides the ADR. Its strongest argument for the candidate stack was that tracing is a large net-new subsystem for us and free for them. Both halves have changed.

X-Ray now accepts OTLP natively, and it is GA. With Transaction Search enabled, every span lands in an ordinary CloudWatch log group named `aws/spans`, in OpenTelemetry semantic-convention format carrying W3C trace IDs. AWS ended support for the X-Ray SDKs and Daemon on 2026-02-27 and standardized on OpenTelemetry. Spans are a log group, so a CloudWatch-native tool reaches them with the query machinery it already has. No span store, no Jaeger API, no new subsystem.

Hatchet supplies the other half already. Its Python, TypeScript, and Go SDKs ship OpenTelemetry instrumentors that inject W3C `traceparent` into task metadata automatically, so a request that triggers a task produces one connected parent-and-child trace. Its dashboard has rendered a per-run trace waterfall since v0.82.0 in March 2026, and `Context.workflow_run_id` is available inside task code, which is the join key for stamping log lines.

What neither covers is the gap that remains: Hatchet's waterfall is scoped to a single workflow run, so the API layer and every non-Hatchet service are absent from it, and Hatchet's own log store is a bounded slice (1,000 lines per task, 3 to 7 days retention on Cloud). Joining across CloudWatch log groups by a correlation ID is the piece nobody hands us, and it is exactly M3.

### The candidate stack is not ready where it would matter

VictoriaTraces is v0.10.0, twelve months old. Its README says the project is a work in progress and that "on-disk data structures and API endpoints may change and may not be backward compatible", and its roadmap lists finalizing the data structure and committing to backward compatibility as open blockers for GA. A store that may require re-ingesting its history on upgrade is not a system of record.

The pivot ADR 0007 cites as the main draw works in one direction only. Grafana's Jaeger datasource accepts Loki or Splunk as a trace-to-logs target, and VictoriaLogs is not on that list. Log to trace works through derived fields, and trace back to logs needs a hand-built data link.

Our own demo never exercised it. `demo/provisioning/datasources/victorialogs.yml` has the derived-fields block commented out with an empty URL, so the capability the ADR rests on was never actually tried here.

VictoriaLogs itself is healthy (v1.52.0, Apache-2.0, monthly releases) and its footprint at our volume is trivial. It publishes no AWS ingestion path at all, though. The OpenTelemetry Collector's `aws_cloudwatch` receiver is alpha and marked "seeking new contributors", and it polls rather than streams.

### The criteria ADR 0007 set

Terminal-bound work: mixed, and a browser is acceptable. This weakens the terminal-native argument for keeping tail-cw, and it is the criterion that cuts hardest against this decision.

The NDJSON agent surface: nice-to-have rather than load-bearing. AWS now publishes a first-party `awslabs.cloudwatch-mcp-server`, which serves that need better than we can.

Operational cost of self-hosting: low in resources, but it is two more processes to run and upgrade, one of them pre-GA, for a problem that no longer requires them.

Tracing as a firm requirement: yes, and it now points at AWS rather than at VictoriaTraces.

Maintenance cost trend: steeply upward. The codebase grew 82% in the nineteen days to 2026-07-25. That is the strongest argument in this record for restraint, and it shapes the second half of the decision.

## Decision

Keep tail-cw. Do not adopt the candidate stack. Narrow what tail-cw builds.

Rejecting the replacement is the easy half, because the stack is immature exactly where we would depend on it, cannot ingest our logs without work nobody maintains, and no longer holds the tracing advantage that justified the disruption.

The harder half is that "keep building" is not the same as "build everything". Two things changed under us while the roadmap stood still, and both mean writing less code.

Tracing arrives by reading `aws/spans`, not by building a span store. The gate ADR 0007 set is lifted and redirected: we may build tracing, and the way we build it is to point existing query code at a log group. Note that the gate was already breached in fact, since `query/trace.py` and `tui/trace_viewer.py` together are 1,100 lines added on 2025-11-01, eight months before the gate existed.

Query features go to Logs Insights where it now wins. PPL and SQL are GA, JOIN and sub-queries landed in April 2026, and roughly fifty commands arrived across June and July including `sessionize`, `logcompare`, `outlier`, `topk`, and `filldown`. M3 planned to hand-write `stream_context`, `stats by`, and `unpack_json` in the local engine. Sending a query to `StartQuery` costs $0.005 per GB scanned and no maintenance, so the local engine keeps the job it is genuinely better at, which is free re-filtering over an already-cached Parquet window.

What tail-cw keeps building is what nothing else does: live tail through `StartLiveTail`, the log group browser with content previews, native terminal charts over console dashboards, and the correlation pivot across log groups. That last one is the answer to the founding use case and the reason this decision is not simply "use the console".

## Consequences

- ADR 0007 is superseded. The tracing gate is lifted, and tracing becomes a milestone scoped as "read `aws/spans`", not "build a span store"
- `demo/` is deleted. It existed to keep the comparison honest, the comparison is resolved, and the code misleads: `ingest.sh` is a one-shot backfill rather than a tail, it truncates CloudWatch's millisecond timestamps to whole seconds with `strftime("%Y-%m-%dT%H:%M:%SZ")` and so destroys intra-second ordering, and it uses BSD-only `date -r`. Its pinned versions (VictoriaLogs v1.43.1, Grafana 11.4.0) are far behind current
- M3 is rescoped in the roadmap: keep the correlation-ID pivot, delegate `pattern`, `stats`, and context-window features to Logs Insights behind a scan-size estimate, and drop the plan to reimplement LogsQL
- `pyarrow` (125 MB installed) and `numpy` (25 MB) are declared dependencies that nothing under `tail_cw/` imports. Parquet I/O goes through polars natively. They should be dropped
- A separate packaging defect is now blocking any release: the real runtime dependencies live in `[dependency-groups]`, which consumers never install, so a clean install of the built wheel fails on `import boto3`. This is unrelated to the scope question but must be fixed before tagging
- The decision rests partly on `aws/spans` being queryable the way AWS documents. Confirm with one real query before committing a milestone to it
- If the maintenance trend keeps climbing after M3 without the tool earning it in daily use, reopen this question. The measurement to watch is whether new code goes into things only tail-cw can do, or into reimplementing what AWS ships

## Sources

- [CloudWatch OTLP endpoint](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-OTLPEndpoint.html), [Transaction Search](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-Transaction-Search.html), [X-Ray SDK end of support](https://aws.amazon.com/blogs/mt/announcing-aws-x-ray-sdks-daemon-end-of-support-and-opentelemetry-migration)
- [Logs Insights JOIN and sub-queries](https://aws.amazon.com/about-aws/whats-new/2026/04/cloudwatch-logs-insights-join-sub-query/), [new query commands](https://aws.amazon.com/about-aws/whats-new/2026/7/amazon-cloudwatch-logs-insights-ql/)
- [Hatchet OpenTelemetry](https://docs.hatchet.run/v1/opentelemetry), [Hatchet logging limits](https://docs.hatchet.run/v1/logging), [Hatchet Python context](https://docs.hatchet.run/sdks/python/context)
- [VictoriaTraces roadmap](https://docs.victoriametrics.com/victoriatraces/roadmap/) and [repository](https://github.com/VictoriaMetrics/VictoriaTraces)
- [VictoriaLogs changelog](https://docs.victoriametrics.com/victorialogs/changelog/), [VictoriaLogs data ingestion](https://docs.victoriametrics.com/victorialogs/data-ingestion/)
- [Grafana Jaeger datasource trace-to-logs targets](https://grafana.com/docs/grafana/latest/datasources/jaeger/configure/)
