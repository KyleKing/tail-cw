## Unreleased

### BREAKING CHANGE

- every function in tail_cw.aws takes an open client as its
first argument and returns a coroutine or async iterator. ShellServices
callables are awaitable.
- `fetch`, `dashboards`, and the `dashboard` subcommand are gone,
and every `--json` flag moved under `tail-cw export`.

### Feat

- **tui**: count the payload fields beside the log table
- **export**: emit the parsed payload, count by field, and stop a fetch at a limit
- **tui**: theme from config, and fit the chrome to the terminal
- **insights**: run SQL and PPL queries, and gate what cannot be estimated
- **query**: name a filter in config and record the ones you set
- **cli**: complete log group names from the groups you have opened
- **discovery**: list a group's fields and say what its sample knows about recency
- **query**: give OR, NOT, and parentheses a surface syntax
- **cache**: report what the cache holds against its limit
- **tui**: show when the events on screen happened behind h
- **tui**: pivot a log row onto its correlation id and its X-Ray trace
- **tui**: draw an X-Ray trace as a waterfall behind :xray
- **xray**: read traces from X-Ray, where the span hierarchy exists
- **insights**: measure the scan estimate instead of averaging it
- **metrics**: list the dimension sets a namespace publishes
- **tui**: budget the log table by width and show severity
- **trace**: open or export a trace by id
- **insights**: estimate the scan before a query bills
- **tui**: rank patterns and alarms, run Insights, and keep one history
- **alarms**: read alarms with their firing history, and any metric without a dashboard
- **insights**: add an opt-in Logs Insights query surface that reports what it scanned
- **summary**: roll log groups up into recurring error and warning patterns
- **demo-aws**: add ephemeral OpenTofu stack that generates real CloudWatch data
- **discovery**: match log groups by substring when no prefix hits
- finish m2 with recents and presets, and close the deferred gaps
- replace the cli subcommands with one interactive tui
- **dashboard**: single-key Enter focus and a visible command prompt
- **dashboard**: add metric captions, predicate filtering, and a which-key leader
- **dashboard**: add a vim-style command bar with completion and history
- **dashboard**: draw focused charts with plotext and drop matplotlib/TGP
- **dashboard**: no-scroll overview grid with sparkline compaction and semantic colors
- **dashboard**: add offline demo mode with seed data
- **dashboard**: render dashboards in the TUI with metric charts and log dive
- **dashboard**: import dashboards and translate metrics to GetMetricData
- add live tail streaming via StartLiveTail (roadmap M1)
- wire fetch CLI through cache into the TUI (roadmap M0)
- implement Config
- implement tracing/chrono view
- add search and filter capabilities
- add Textual UI
- add storage cache
- add boto3 client
- implement initial project skeleton
- initialize from calcipy template

### Fix

- **tui**: create the facet-count coroutine only when its worker starts
- **tui**: re-check the cache when a debounced fetch fires, not only when it is scheduled
- **tui**: give an error row's detail the most readable colour available
- **tui**: drop whole hints and whole breadcrumb parts instead of clipping
- **tui**: make the reference panel, the record detail, and a focused chart readable
- **cache**: name the payload key behind a Parquet dtype failure
- **tui**: correct what the screenshots found in the new views
- put a native engine panic back inside the handlers that catch Exception
- **xray**: cap and price a trace scan, which a filter expression does not reduce
- **query**: make a field search work across groups that lack the field
- **tui**: count out a slow load and let escape stop it
- **aws**: emit UTC for timestamps botocore parsed locally
- **tui**: show what is typed into the command line
- **trace**: render lines sharing a span id as one span
- **cache**: write NDJSON as UTF-8 and run tests on Linux in CI
- **tui**: open the record detail on Enter
- **tui**: let the filter box render the text typed into it
- **cache**: infer the Parquet schema from every row, not a 1000-row sample
- **rollup**: treat the bucket window as half-open so a trailing empty period is not invented
- **severity**: trust a record's declared level over keywords in its body
- **tests**: read source with utf-8 so the async invariant guards run on Windows
- **tests**: isolate the suite from the machine's AWS configuration
- **trace**: read every selected group off the message loop
- **config**: honor preview.sample_limit when sampling a group
- **cache**: stop pickling metadata, and close the fan-out race properly
- declare the real runtime dependencies so an install works
- **dashboard**: contain chart image, add render-mode toggle, log-volume sparklines
- **dashboard**: dense grid, reserved stage, hjkl nav, non-scroll rendering
- address ruff 0.15 lint findings from calcipy_template migration
- resolve test failures
- resolve failures and try to write better code moving forward
- finish migrating to uv

### Refactor

- share the test harnesses and drop the pyarrow dependency
- replace boto3 with aiobotocore and make every aws call async
- run prek

### Perf

- **cache**: splice a JSON payload into the cache line instead of re-encoding it
- **fetch**: give segment writes their own thread pool
- **fetch**: run a window's cache segments concurrently
- **cli**: keep the heavy stack out of the entry path
- **cache**: segment windows and drop the bytes stored twice
- **cpu**: cap DuckDB and Polars at 40% of the machine
