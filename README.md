# tail-cw

Read and explore AWS CloudWatch from the terminal: tail logs live, open a dashboard you already built in the console, reshape a metric chart with the keyboard, and drop from any chart into the logs behind it.

The CloudWatch console answers "how is the service doing right now", but it pulls you out of the terminal, and every dedicated CloudWatch tailer (awslogs, saw, cw, utern) went dormant between 2019 and 2023 and predates the Live Tail API. tail-cw stays where the work happens. It wraps `StartLiveTail` for real streaming, caches every fetch as local Parquet so re-filtering costs nothing, and renders your console dashboards as native terminal charts (Unicode, no graphics protocol, so they work over SSH). If what you want is a full web GUI, Grafana's CloudWatch data source is the honest answer. tail-cw aims to be the best terminal tool for a narrow set of daily tasks.

One Textual-free core (`tail_cw/cli.py`) owns argument parsing, the cache, and the AWS pipelines. The TUI and the `--json` output sit on top of the same functions, so agents and humans drive one code path. See the [ADRs](docs/docs/adr) for the decisions, [plans/roadmap-2026-07.md](plans/roadmap-2026-07.md) for what is built and what is next, and [AGENTS.md](AGENTS.md) for where to start.

## What it does

- Live tail through `StartLiveTail` (up to 10 groups) with a ring buffer, pause and resume, and bounded reconnect
- One filter model that reads the same across the live stream, a historical fetch, and cached data
- Every fetch cached as ZSTD Parquet and queried locally with DuckDB or Polars, so re-filtering and trace grouping are free after the first pull
- Dashboard import by name via `GetDashboard`, or from a local JSON file in the same schema, rendering metric widgets as charts, log widgets as Logs Insights queries, and text widgets as markdown
- Metric charts drawn natively with plotext (braille curves plus real text axes and legend), so nothing depends on a graphics protocol and there are no rendering artifacts
- A no-scroll overview grid of color-coded sparklines (errors red, latency amber, traffic blue, saturation purple, availability green) with a focus stage; a multi-series metric compacts to a min-max band with a median line
- Keyboard-first exploration: `hjkl` to move, Enter/Space to focus one or two charts, a `:` command line (Tab completion, history) for `stat`, `period`, `range`, `filter`, and `focus`, and `,` for a which-key reference
- Dive from a chart or log widget straight into the log table for that window and log group, reusing the same search and record view as `fetch`
- CLI-first throughout, so every command has a `--json` mode for agents and pipes

## Demo

`tail-cw dashboard --demo` renders a synthetic service dashboard from seed data (a mid-window incident: a latency and error spike with a traffic dip), so it needs no AWS account. The clip shows the overview grid, focusing a chart on the stage, `:filter` and `:range` on the command line, the which-key reference, and diving into the logs behind the errors panel.

![tail-cw dashboard demo](docs/images/demo.gif)

Charts are Unicode, so they render the same in any terminal and over SSH. Regenerate the clip with `mise run gif`.

## Why this exists

Dedicated CloudWatch tailers solved log streaming years ago and then stopped. The gap now is everything around the logs: live streaming through the current API, dashboards and metrics without the console, and a fast path from a chart to the logs that explain it.

- Grafana or the CloudWatch console: richer and mouse-driven, and out of the terminal. Reach for them when you want a web GUI
- awslogs, saw, cw, utern: the dedicated tailers, all dormant and predating Live Tail and the newer Insights query languages
- `aws logs tail` / `start-live-tail`: native, but a bare pane with no structure, no caching, and no dashboards
- Gonzo: a strong log-analysis TUI with no native CloudWatch source, so you pipe `aws logs tail` into it
- AWS CloudWatch MCP server: Insights and pattern analysis for agents, with no live tail and no human surface

## Install

```sh
git clone https://github.com/kyleking/tail-cw && cd tail-cw
uv sync
```

`uv sync` installs the chart stack (textual-plotext) alongside the core.

## Usage

```sh
uv run tail-cw dashboard --demo                         # offline synthetic dashboard, no AWS
uv run tail-cw dashboards --region us-east-1            # list dashboards in the account
uv run tail-cw dashboard my-service --region us-east-1  # open a console dashboard
uv run tail-cw dashboard --file ./dash.json            # a local dashboard (console JSON schema)
uv run tail-cw fetch /aws/lambda/my-fn --start 2h      # fetch a window into the cache and TUI
uv run tail-cw tail /aws/lambda/my-fn --backfill 15m   # live tail after 15m of backfill
```

Add `--json` to any command to write NDJSON or JSON to stdout instead of opening the TUI.

In the dashboard, `hjkl` move the selection, Enter focuses a chart on the stage, Esc clears it, `d` dives into the logs, and `q` quits. Press `:` for the command line (`stat`, `period`, `range 6h`, `filter errors`, `focus latency`, `help`) and `,` for the which-key reference. Set `AWS_PROFILE` before launch to pick an account.

## Requirements

- Python 3.11 or newer
- AWS credentials through the standard chain (environment, profile, SSO, or role); `--profile`, `--region`, or `AWS_PROFILE` select them, and the profile is part of the cache key so accounts never collide
- Any terminal; charts are Unicode and need no graphics protocol

## Development

```sh
uv sync
uv run ruff format && uv run ruff check --fix --unsafe-fixes
uv run mypy && uv run pyright
uv run pytest -q
```

See [AGENTS.md](AGENTS.md) for the testable-first conventions this project follows.

## License

[MIT](LICENSE)
