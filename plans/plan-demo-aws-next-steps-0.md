# demo-aws next steps

Staged for your review. Delete what you don't want; `[AI: ...]` marks an open question I need answered, `[TODO: ...]` is yours to add.

Committed in `c7770ff`. The stack passes `tofu fmt -check`, `tofu validate`, Ruff, and the 802-test suite, and it has never been applied to an AWS account. Everything under "First apply" is unverified against real AWS.

## First apply

The one thing that matters. Nothing below it is worth doing until a run has happened.

```sh
cd demo-aws && tofu init && tofu apply
```

- [ ] Fix the shell first: `AWS_PROFILE=PowerUserAccess-760682031284` does not resolve, and no default region is set. Both break `tofu apply` and tail-cw

- [ ] Confirm CloudWatch accepts the dashboard body. The widget schema is written from the documented shape and never round-tripped through `PutDashboard`. The riskiest parts are the invisible metric rows feeding the availability expression and the `alarm` widget's ARN list

- [ ] Confirm percentile stats resolve on the EMF metric. `RequestLatencyMs` is charted at p50/p90/p99 and alarmed at p99, and a custom metric needs enough samples per period before percentiles return anything

- [ ] Check whether the Python runtime prefixes plain `print` output. tail-cw tolerates an ISO timestamp before the `{`, so either way parses, but confirm which shape lands in the group

- [ ] Watch the gateway's own duration. It paces 90 requests against a 55s timeout, and during the incident a single `POST /v1/orders` can spend up to `MAX_SLEEP_SECONDS` in `payments`. If the loop falls behind, traffic thins exactly when the demo is most interesting. Either lower `MAX_SLEEP_SECONDS`, raise `requests_per_minute`, or accept the dip

    [AI: if it does fall behind, do you want the incident to show as fewer, slower requests (realistic) or hold throughput flat so the latency spike reads cleanly on the chart (better demo)?]

- [ ] Confirm `tofu destroy` leaves nothing. Log groups are the usual survivor; these are declared explicitly rather than created by Lambda, so they should go, but verify in the console

## Drive tail-cw against it

- [ ] `tail-cw dash tail-cw-demo` renders every widget type: metric, log, text, and alarm
- [ ] `t` in a log view groups by `trace_id` across all five groups, and a trace shows the gateway leg plus its downstream legs
- [ ] `d` from a Lambda chart dives into `/aws/lambda/tail-cw-demo-*`. This is the `FunctionName` dimension mapping, the one dashboard-to-logs path worth proving
- [ ] The group browser preview clusters distinct message shapes per service. If it shows one shape per group, the success and failure message catalogs in `scenario.py` need widening
- [ ] `tail-cw tail @demo` streams all five groups under the ten-group Live Tail cap

This is also the first real exercise of `doing.txt`'s "Need to test against AWS for real!". Anything that breaks here is a tail-cw bug, not a demo bug, and belongs in its own commit.

## Known gaps I chose not to close

Both are written up in `demo-aws/README.md` under "Known limits".

The X-Ray root is per invocation, so one trace covers a minute of traffic rather than one request. `trace_id` is minted per request and `xray_trace_id` carries the root separately. Closing this means an HTTP API in front of the gateway and a separate load-generator Lambda, roughly five more resources.

The saturation widget uses account-wide `AWS/Lambda ConcurrentExecutions`, because per-function concurrency is only published when reserved concurrency is set. Setting a small reserved concurrency on each function would fix the metric and also cap runaway cost, at the price of throttling during the incident.

[AI: worth setting reserved concurrency? It buys a real per-function saturation metric and a cost ceiling, but Throttles would then show up as a failure mode the scenario does not model.]

## Smaller follow-ups

- [ ] Point the root `README.md` at `demo-aws/`, next to the existing `dash --demo` paragraph, so the offline and live demos sit together
- [ ] An ADR recording the log-field contract. `scenario.build_log_record` and `tests/test_scenario.py` already encode it, and an ADR would say why those names and why `parent_span_id` stays out, which ties back to ADR 0012
- [ ] Use this data to build `tail-cw export trace` as OTLP JSON, the first M3 item in `roadmap-2026-07.md`. It needs a trace that genuinely spans services, which is what this produces
- [ ] Consider an AWS Budgets alert as a second guardrail behind the schedule's `end_date`

[TODO: ]
