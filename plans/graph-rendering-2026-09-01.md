# Graph and UI findings, read against linecast

Reference checkout: [ashuttl/linecast](https://github.com/ashuttl/linecast),
`src/linecast`.
It draws weather, tides, radar, and maps in the terminal with no third-party
dependencies, so every rendering decision is visible in plain Python.
Three of its
modules are directly relevant to us:
[`_braille.py`](https://github.com/ashuttl/linecast/blob/main/src/linecast/_braille.py),
[`_color.py`](https://github.com/ashuttl/linecast/blob/main/src/linecast/_color.py),
and [`_theme.py`](https://github.com/ashuttl/linecast/blob/main/src/linecast/_theme.py).

We are further along than linecast on layout (Textual does the compositing) and behind
it on two things it takes seriously: what a downsample is allowed to throw away, and
whether a colour survives the terminal it lands in.

## Two proven defects

### A compact cell drops the spike it exists to show

`_resample` in `tail_cw/charts/sparkline.py` picks one source value per output column
and
discards the rest, and `sparkline_blocks` then takes its scale from the resampled list
rather than from the source.
A single-bucket spike disappears twice over: once from the
data and once from the axis.

```
>>> vals = [1.0] * 1440
>>> vals[700] = 500.0
>>> max(vals), max(_resample(vals, 40))
(500.0, 1.0)
>>> sparkline_blocks(vals, width=40, bars=True, lo=0.0)
'████████████████████████████████████████'
```

A 500x spike over a flat baseline renders as a solid wall of full blocks, which reads as
saturation rather than as one bad minute.
At the default 300s period the loss is around
a quarter of the points; at `:period 60` over a day it is 97%.

The log histogram is safe, because `bucket_events` already builds exactly one bucket per
column and `_resample` is a no-op on it.
Only the dashboard cells are affected.

Two changes fix it. Aggregate per column instead of sampling (max for counts and errors,
mean for gauges, and a min-max band when the cell has two rows to spend), and compute
`lo`/`hi` from the source series before resampling so the axis always spans the real
extremes.
linecast interpolates in
[`interpolate`](https://github.com/ashuttl/linecast/blob/main/src/linecast/_braille.py)
rather than aggregating, which is right for a smooth temperature curve and wrong for
event counts, so this is a case where we should not copy it.

### The focused chart is dark whatever theme is configured

`PlotChart._replot` calls `plt.theme('dark')` on every replot.
`textual-plotext` defaults
its `theme` reactive to `"auto"` and builds a plotext theme from
`app.theme_variables` whenever the app theme changes, so that one line throws away the
only theme integration the widget has.
Under `catppuccin-latte` or `ansi-light`, both
documented in `CONFIGURATION.md`, the chart draws a dark plot inside a light app.

Deleting the line is the whole fix.

The same problem sits one level down in `tail_cw/charts/palette.py`, where the role
colours and the categorical ramp are fixed hex.
Textual's themes carry `error`,
`warning`, `success`, `accent`, and `secondary`, and a `dark` flag, so the five roles
can
come from the active theme instead:

| Role         | Theme variable |
| ------------ | -------------- |
| errors       | `$error`       |
| latency      | `$warning`     |
| availability | `$success`     |
| traffic      | `$accent`      |
| saturation   | `$secondary`   |

That keeps the cross-panel cohesion ADR 0006 asks for and makes `ansi-dark` mean what
its config comment promises, which is that the user's own sixteen colours win.

## Ideas worth taking from linecast

**Colour the curve by its own value.** `build_braille_curve` returns
`(char, avg_value)` per cell rather than a bare string, so the caller inks each cell
from
the value under it.
`_render_braille_rows` in `_weather_hourly.py` uses it to run a
temperature curve from blue to red along its length.
For us the natural mapping is
severity or a threshold: an error-rate sparkline that reddens only where it crosses the
line says more in one row than a monochrome sparkline plus a headline.
It composes with
the aggregation fix, because a per-column aggregate is exactly the value to key the
colour on.

**Label the extremes on the curve.** `_compute_extrema_overlays` places the peak and
trough values as text on the braille row above or below the curve, tracking occupied
columns so labels never collide.
Our compact cell shows the title and the latest value
and nothing about range, so a cell answers "what is it now" and not "how bad did it
get".
Peak and trough overlays would close that without spending a row.

**Shade the background to carry a second variable.** The tide and temperature charts
tint
each column's background by daylight, so time of day reads without a legend.
Our
equivalents are a deploy window, an alarm's in-alarm span, or the segment of a fetch
that
was capped.
Textual gives us per-cell background styles for free through `Text`.

**Braille at four times the vertical resolution.** A two-row braille curve carries eight
dot rows.
Where the grid gives a cell five rows and we currently spend one on a block
sparkline, two rows of braille would show shape a block row cannot.
This is the one that
needs a `NO_COLOR` and a non-Unicode check before committing, and it should stay behind
the same reasoning as the existing block path rather than replacing it.

**Degrade colour deliberately.** `_rgb_to_ansi16` refuses to let a muted colour gain
saturation on a 16-colour terminal, confining near-neutrals to the four neutral slots,
because the terminal paints ANSI navy far more vividly than a distance metric assumes.
We hand fixed hex to Rich and let it quantise.
Anywhere we keep a fixed colour, the
sanity check is the same one AGENTS.local.md already records for `NO_COLOR`: look at it
in a real terminal at 16 colours before believing it.

**Contrast as a function, not a constant.** `ensure_contrast` and `best_contrast` in
`_theme.py` walk a colour toward black or white until it clears a ratio against the
background.
AGENTS.local.md records us solving the same problem by hand once, for the
error row's dimmed remainder at 1.9:1.
A theme-aware `role_color` will hit it again on
every light theme, and a ten-line contrast helper answers it for good.

## Not worth taking

linecast's OSC theme probing, its framebuffer, its half-block compositor, and its live
loop all solve problems Textual already solves for us.
Its colour maths is the
transferable part, and the way it treats a downsample as a decision rather than a
detail.

## Suggested order

1. Fix `_resample` and the scale it derives, since the current behaviour is wrong rather
    than plain
1. Delete `plt.theme('dark')`
1. Derive role colours from the active theme, with a contrast floor against the theme
    background
1. Value-keyed colour and extrema labels on the compact cell
1. Braille curves, if the block row proves too coarse once the aggregation is honest
