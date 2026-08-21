# TUI critique, 2026-08-21

**Method.** `uv run tail-cw --profile read-prod` at commit `697678e`, driven through VHS (reusing `docs/demo.tape`'s theme: FontSize 13, Padding 10) and independently through tmux to rule out capture artifacts. States captured: log group browser, `?` overlay, `:` command line, `/` filter, and the log view. Non-default conditions tested: `NO_COLOR=1` at 80x24, and the log view at 80x24. Every claim about a cause below was checked against source.

## Design specificity verdict

Authored for this domain, clearly. The preview pane is the proof: instead of listing forty `/aws/lambda/*` groups by name and leaving you to guess, it samples each group and shows its distinct message shapes with counts, so you tell groups apart by what they contain. Swap the data source and that panel makes no sense. The same is true of the retention and stored-bytes columns, the ten-group selection cap that mirrors `StartLiveTail`'s limit, and `L` flipping a historical search to live without losing the filter. A generic list-of-rows TUI could not use this layout unchanged.

Where it stops being domain-specific is the log table itself, which is a plain five-column grid with fixed widths. That is the weakest surface in the app and it is the one you spend the most time looking at.

## Health score: 30/36

| #   | Heuristic                       | Score | Key finding                                                                                                                                          |
| --- | ------------------------------- | ----: | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Visibility of System Status     |     3 | `Sampling …`, `Loading events…`, `Loaded 1000 events` all land, but a 2-minute multi-group fetch reports no progress and never says how much is left |
| 2   | Match System/Real World         |     4 | CloudWatch's own vocabulary throughout: log group, log stream, retention, live tail. No invented nouns                                               |
| 3   | User Control and Freedom        |     3 | `Esc` backs out everywhere, `^o`/`^i` jumplist works; cancelling an in-flight fetch is not offered in the footer                                     |
| 4   | Consistency and Standards       |     4 | `/ ? : Esc Enter Space q [ ]` all behave conventionally in every view                                                                                |
| 5   | Error Prevention                |     2 | Nothing warns before an expensive action. Opening a busy group for an hour pulls 460k events over two minutes with no size estimate or confirmation  |
| 6   | Recognition Rather Than Recall  |     4 | Contextual footer plus a `?` overlay that lists both keys and `:` commands. Best-in-class here                                                       |
| 7   | Flexibility and Efficiency      |     4 | Command line with completion, config presets, jumplist, and a full `export` surface for scripting                                                    |
| 8   | Aesthetic and Minimalist Design |     2 | The column budget is misallocated: 71 fixed characters go to timestamp, group, stream, and event id before Message gets any                          |
| 9   | Error Recovery                  |     2 | The shared fetch pipeline dumps a raw Rust panic and Python traceback (see below). Unverified how the TUI surfaces the same failure                  |
| 10  | Terminal Portability            |     2 | `NO_COLOR` and tmux are fine and the browser reflows, but the log view is unusable at 80 columns and the footer truncates mid-word                   |

## Overall impression

This is a well-built app with one badly under-designed surface. The browser is genuinely excellent, the keyboard model is consistent and discoverable, and the `?` overlay is better than most tools ship. The biggest opportunity is the log table: it spends most of its width on columns that carry little information and none on encoding severity, which is the one thing a person scanning logs is looking for. Fixing the column budget and colouring rows by level would raise the app's perceived quality more than any new feature.

## What's working

**The preview pane earns the screen it takes.** In the 80-column `NO_COLOR` capture it still shows `219 <ts> UTC:<n>.<n>(<n>):hatchet_admin@hatchet:[<n>]:LOG: execute stmtcache_<hex>` with a count. That is a normalized shape with its frequency, at 80 columns, in monochrome. Very few log tools give you that before you have opened anything.

**Non-colour selection indicators.** With `NO_COLOR=1` the current row is marked with a `·` in the left gutter rather than relying on a highlight, so meaning survives the loss of colour. That is principle 5 done properly and it is easy to get wrong.

**The `?` overlay covers both layers.** It lists single-key actions *and* the `:` command vocabulary in one panel, with `esc to close` stated at the bottom. A first-timer can get from zero to `:tail` without documentation.

## Priority issues

### P1 — The log view is unusable at 80 columns

`tail_cw/tui/logs_screen.py:243` hardcodes `timestamp: 23, log_group: 20, log_stream: 16, event_id: 12` and leaves `message: None`. Those four fixed widths total 71 characters before separators. Captured at 80x24, Message renders **12 characters** wide: every row reads `{"method":"G`. The column widths never consult the terminal width.

It is worse than the arithmetic suggests, because in a single-group log view the `Log Group` column shows the same value on all 1,000 rows (`irm-ecs-api-prod`), spending 20 columns on a constant.

Fix: make the widths responsive, and collapse `log_group` when the view has one distinct group. Below roughly 120 columns, drop `log_stream` and `event_id` entirely; both are already shown in full in the record detail pane (`tail_cw/tui/log_viewer.py:139-142`), so nothing is lost.

### P1 — `Event ID` is 12 characters of an identifier that is never distinguishing

CloudWatch event ids are ~35 digits sharing a long prefix. Truncated to 12 with no ellipsis, every visible row showed the identical string `39859061` in the wide capture. A column that renders the same value for every row is worse than absent: it reads as a rendering bug.

Fix: remove the column from the table (`tail_cw/tui/log_viewer.py:241`). The detail pane already shows the full id.

### P2 — No severity encoding anywhere in the log table

`batch_format_log_events` (`tail_cw/tui/log_viewer.py:92`) returns `tuple[RenderableType, str, str, str, str]`, and only the timestamp carries a style (`format_timestamp`, cyan). Message, group, and stream are plain strings, so an ERROR row is visually identical to an INFO row. In a thousand-row table that is the single most useful distinction and it is absent.

The machinery now exists: `tail_cw.query.severity.event_severity` classifies any event, and `tail_cw/charts/palette.py` already owns the semantic colour slots. Colouring the message cell by severity, with a level glyph so it survives `NO_COLOR`, is a small change against `batch_format_log_events`.

### P2 — Truncation is silent everywhere

`/aws/rds/cluster/irm-prod-hatchet-cluster/postgresql` renders as `/aws/rds/cluster/irm` with no ellipsis, in both the log table and the 80-column browser (`/aws/ecs/containerinsights/irm-ecs-cluste`). A truncated name that looks like a whole name is a correctness problem, not a cosmetic one: `/aws/rds/cluster/irm` is a plausible group name that does not exist.

Fix: truncate with `…` wherever a fixed width can clip, matching what the summary table already does in `tail_cw/query/report.py:_shorten`.

### P2 — A raw panic reaches the user

A multi-group fetch over a week crashed with a Rust panic and a full Python traceback in the pane:

```text
thread 'async-executor-3' panicked at crates/polars-core/src/frame/mod.rs:711:42:
should not fail: SchemaMismatch(ErrString("type String is incompatible with expected type Null"))
```

The underlying schema-inference bug is fixed (commit `5319962`), but the failure mode is the finding: `write_log_events_to_parquet` can raise `pyo3_runtime.PanicException`, which is not an `Exception` subclass, so nothing between it and the terminal catches it. The TUI shares that code path through `resolve_parquet_paths`. **I did not verify how the TUI renders it**, which is the first thing to check.

### P3 — The footer truncates mid-word

At 80 columns the footer ends `q Q▏^p palette`, cutting "Quit" in half. Better to drop whole hints on a priority order than to clip the last one.

## Persona red flags

**Casey (80-column SSH from a phone).** Cannot use the log view at all. The primary content column is 12 characters, so every row is `{"method":"G`. The browser is fine; the thing Casey opened the tool to read is not.

**Sam (`NO_COLOR`, 16-colour).** Mostly well served. The `·` gutter marker and the `▁▂▃` sparklines both survive. The gap is severity: with no colour and no level glyph in the table, there is no way at all to spot an error row, so for Sam the log table conveys strictly less than `grep`.

**Jordan (first-timer).** Served well by `?` and the footer, but tripped by the `Event ID` column repeating one value on every row, which reads as a broken table, and by `Loaded 1000 events (showing first 1000)` which does not say 1,000 *of how many*. Jordan cannot tell whether they are looking at everything or a slice.

**Reeve (simplify).** The 80-column log view does not re-prioritize, it clips. Three columns of low information survive at full width while the one column that matters is amputated. That is the exact failure the priority-collapse strategy exists to prevent.

## Minor observations

The window in the header (`2026-08-21 19:55->20:55 UTC`) is stated in UTC, which is right, but `export metrics` emits datapoint timestamps in local time with an offset. One tool, two conventions.

The `:` command line and `/` filter are separate affordances that both take text at the top of the screen and look nearly identical. They are consistent within themselves; a first-timer may not notice which is active.

In the log view the message body often begins with a timestamp the Timestamp column already shows (`2026-08-21 19:55:13 UTC:10.0.78.100…`), duplicating information inside the most space-constrained column. `patterns.py` already has `_LEADING_TIMESTAMP_RE` for exactly this shape and could strip it for display.

## Questions to consider

1. Should the log table's columns be responsive (collapse by priority under a width threshold), or should the narrow case get a different layout entirely — one line per event with the metadata on a second dim line? The second is more work but reads far better at 80 columns.
1. For severity colouring, is `event_severity`'s inference acceptable in the UI, or should the table only colour rows whose records carry an explicit `level` field, leaving inferred ones neutral? Inference is right for a report but a wrong colour in a live table is worse than no colour.
1. `Loaded 1000 events (showing first 1000)` hides the denominator. Is the cap worth surfacing as "1,000 of 199,967 — narrow the window or add a filter", which would also make the cost of the fetch visible before the next one?
