# ADR 0006: Dashboard rendering and interaction

Date: 2026-07-24 Status: Accepted (supersedes the rendering decision in ADR 0005)

## Problem

ADR 0005 chose to render dashboard metric charts as matplotlib PNGs shown inline through the Kitty graphics protocol. Building it against a real terminal (WezTerm) showed that approach does not hold up, and the dashboard also needed a real interaction model, not the ad-hoc single keys of the first cut. This ADR records what changed.

## What failed about matplotlib and the graphics protocol

Rendering a raster image inside a Textual app breaks two ways, and we hit both:

- Kitty graphics protocol: the image is drawn with escape codes outside Textual's cell compositor, so it fought the compositor. A focused chart left a stray bright strip on the row above it, doubled the footer and panel borders, and overlapped axis labels. Sizing the image one cell shy of its box did not fix it
- Unicode half-block fallback: composits cleanly, but downsampling a matplotlib PNG (whose labels are small rasterized text) to half-blocks makes the text illegible

A structural problem sits underneath both: the failures only appear at the escape-code level in a real terminal, so a headless test driver cannot see them, which made every fix a blind round-trip.

## Decision

Draw charts with native terminal cells and no graphics protocol at all.

- compact overview cells: Rich block sparklines (already in use)
- focused charts: [plotext](https://github.com/piccolomo/plotext) via [textual-plotext](https://github.com/Textualize/textual-plotext), which renders braille curves plus real text title, axes, ticks, and legend

A survey of 2026 terminal-charting libraries (verified releases and commit dates) put textual-plot (davidfokkema) as the best-maintained native widget and textual-plotext as the fuller-featured runner-up. We chose textual-plotext because it renders the title, legend, multi-series, and labeled time axis a dashboard chart needs out of the box, and it is the official Textualize widget. A thin `PlotChart` wrapper keeps the renderer swappable if plotext's slow release cadence (v5.3.2, Sep 2024) ever bites. matplotlib, textual-image, and pillow were removed entirely.

The payoff beyond correctness: native rendering exports to SVG, so charts are verifiable from a headless test, ending the blind-iteration loop.

## Interaction model

- no scrolling: a fit-to-screen grid of short, color-coded compact cells on top, a reserved stage below that always holds space (one chart focused by default), so nothing reflows or bounces
- `hjkl` navigates the grid; Enter focuses one chart on the stage, Space toggles a second for a two-up, Esc clears
- a vim-style `:` command line (Tab completion over command names, argument values, and panel titles; Up/Down history) owns the stateful actions: `focus`, `add`, `reset`, `dive`, `stat`, `period`, `range`, `filter`, `help`
- `:filter <terms>` narrows the grid to panels matching a metric role (errors, latency, traffic, saturation, availability) or a title substring; `:filter all` resets
- `,` opens a which-key reference of every binding and command
- focused charts carry a caption with the resolved metric identity (namespace, metric name, dimensions, statistic, period, window), because a title like "API Latency" does not say which target group it measures

## Compaction and color (carried from the M4 decisions)

- shape-aware compaction: a bar widget compacts to a mini bar sparkline, a single-value widget to a big current value plus a trend, a line widget to a line sparkline
- a multi-series metric (for example CPU across containers) reduces to a min-max band with a median line by default, overridable to a chosen percentile
- semantic role colors (errors red, latency amber, traffic blue, saturation purple, availability green, else a stable hashed color) give cross-panel cohesion; series inside one focused chart use the categorical palette so they stay distinct

## Consequences

- ADR 0005 still stands for the dashboard import (`GetDashboard`), the metric-shorthand-to-`GetMetricData` translation, and the cost model; only its rendering decision is superseded here
- charts render over plain SSH and in any terminal, at lower curve fidelity than matplotlib but with legible labels and no artifacts
- deferred: an in-app `:profile` switch (setting `AWS_PROFILE` before launch works today), and full vim operator-plus-text-object composition beyond `:focus`/`:filter` (the two-chart stage limits its value)
