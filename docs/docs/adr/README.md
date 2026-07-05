# Architecture Decision Records

Decisions are numbered in the order they were accepted. Each record states the problem, the options considered, the decision, and its tradeoffs. Amend by adding a new ADR that supersedes an old one rather than rewriting history.

| ADR                                                      | Title                                  | Scope                                                               |
| -------------------------------------------------------- | -------------------------------------- | ------------------------------------------------------------------- |
| [0001](./0001-product-scope-and-milestone-sequencing.md) | Product scope and milestone sequencing | Why live tail before discovery, landscape survey, design principles |
| [0002](./0002-cli-first-layered-architecture.md)         | CLI-first layered architecture         | argparse subcommands, injected TUI runner, NDJSON agent surface     |
| [0003](./0003-parquet-cache-and-local-query-engine.md)   | Parquet cache and local query engine   | Cost model, cache keys, dual DuckDB/Polars backend                  |
| [0004](./0004-live-tail-via-startlivetail.md)            | Live tail via StartLiveTail            | Streaming wrapper, reconnects, ring-buffered rendering              |
