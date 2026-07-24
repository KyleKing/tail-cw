# Architecture Decision Records

Decisions are numbered in the order they were accepted. Each record states the problem, the options considered, the decision, and its tradeoffs. Amend by adding a new ADR that supersedes an old one rather than rewriting history.

| ADR                                                      | Title                                    | Scope                                                                                       |
| -------------------------------------------------------- | ---------------------------------------- | ------------------------------------------------------------------------------------------- |
| [0001](./0001-product-scope-and-milestone-sequencing.md) | Product scope and milestone sequencing   | Why live tail before discovery, landscape survey, design principles                         |
| [0002](./0002-cli-first-layered-architecture.md)         | CLI-first layered architecture           | argparse subcommands, injected TUI runner, NDJSON agent surface                             |
| [0003](./0003-parquet-cache-and-local-query-engine.md)   | Parquet cache and local query engine     | Cost model, cache keys, dual DuckDB/Polars backend                                          |
| [0004](./0004-live-tail-via-startlivetail.md)            | Live tail via StartLiveTail              | Streaming wrapper, reconnects, ring-buffered rendering                                      |
| [0005](./0005-dashboards-metrics-and-terminal-charts.md) | Dashboards, metrics, and terminal charts | GetDashboard import, GetMetricData translation, dive-to-logs (rendering superseded by 0006) |
| [0006](./0006-dashboard-rendering-and-interaction.md)    | Dashboard rendering and interaction      | Why matplotlib/TGP failed, native plotext charts, no-scroll grid, command bar and filter    |
