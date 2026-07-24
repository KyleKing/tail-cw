This is much more mature than I expected — there's already a query engine, trace viewer, live tail, and a roadmap. Let me look at the key parts.

Good — I cloned the repo and read the actual code (not just the README). This is further along than the roadmap doc's own gap analysis suggests: you already have a working Textual app (`LogTailApp`), a DuckDB/Polars-backed Parquet cache (`LogCache`), a trace pivot screen (`TraceViewerScreen` → `RecordDetailScreen`), and `live_tail.py`. That existing pivot pattern (select a span → push a detail screen) is exactly the mechanism to reuse for graph drill-down, so this is more "add a screen" than "invent an interaction model."

**Two tiers of "graph," both worth building — don't pick one**

1. **Derived, from what you already cache.** Your `LogCache` already gives you time-bucketed counts for free via a DuckDB `date_trunc` group-by over the Parquet you're already writing. Zero AWS API calls, zero cost, works offline, always in sync with whatever filter is active in the log table. This is your "close enough" dashboard — a sparkline/histogram of event volume (or error-rate, if you regex-count `ERROR|WARN`) for the current view.
2. **Real metrics, from GetMetricData.** For actual CloudWatch metrics (Lambda duration, ALB 5xx, ECS CPU) you need `get_metric_data` — it batches up to 500 metrics/request and supports metric-math expressions (e.g. `Errors / Invocations` for an error rate), unlike the older `GetMetricStatistics`. This costs money per metric per call, so it needs the same diskcache treatment your log fetches already get.

The genuinely unclaimed feature here: **`GetDashboard`** returns the *exact JSON* of an existing console dashboard — `widgets[]` with `type: metric|log`, each with `metrics`, `period`, `stat`, and x/y/width/height layout, or a Logs Insights `query` for log widgets. Nobody's terminal tool reads this. `tail-cw dashboard <name>` could pull a dashboard you've already built in the console, map its grid layout onto a Textual `Grid`, render each metric widget as a `PlotextPlot` panel and each log widget by running its query through your existing `query/engine.py`. You get dashboard parity with zero redefinition of what you're already monitoring.

**Rendering library**

`textual-plotext` (Textualize's own wrapper around Plotext) is the right call over the newer `textual-plot` — the latter has real zoom/pan but is early and single-purpose (no bars, only line/scatter), while plotext gives you bars, histograms, and braille/half-block rendering, which is what a metric panel and a log-volume histogram both need. Tradeoff worth flagging: plotext itself has had no meaningful updates since late 2023 — acceptable risk for a personal tool, less so if this becomes a dependency others rely on.

**Drill-down, using the pattern you've already built**

Add a `g` binding to `LogTailApp` that splits the log screen and shows a `PlotextPlot` histogram of the *currently filtered* result set, bucketed at whatever period fits the visible width. Then:
- Arrow-select a bar → `action_show_bucket_detail` narrows `_all_events`/re-queries the Parquet cache to that time window, same code path `action_refresh` already uses.
- For real metrics, a bar click resolves to a time window and log group (or correlation ID from `trace.py`), then pushes straight into the existing table filtered to it — identical mechanism to how `T` currently jumps into `TraceViewerScreen`.

This means no new interaction paradigm, just a new `Screen` subclass and one more `on_tree_node_selected`-style handler.

**Fits your own roadmap**

This slots as M3.5 in `roadmap-2026-07.md`, right after the trace pivot and before the pattern/diff work — it depends on the same query engine, and "time-bucketed histogram/sparkline" is literally already listed there as a stub goal. I'd sequence: (1) derived histogram from cache — cheapest, no AWS billing surface; (2) `GetMetricData` panel with disk-cached results; (3) `GetDashboard` import as the standalone feature that actually beats Grafana/console for your workflow, since it's terminal-native and pivots straight into the same log table.