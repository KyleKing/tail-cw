# tail-cw

Read and explore AWS CloudWatch from the terminal: tail logs live, open a dashboard you
already built in the console, reshape a metric chart with the keyboard, and drop from
any
chart into the logs behind it.

It wraps `StartLiveTail` for real streaming, caches every fetch as local Parquet so
re-filtering costs nothing, and renders console dashboards as Unicode charts that
survive
SSH.
Every feature lands as a CLI command with NDJSON output under `tail-cw export`, and the
Textual UI is a view over the same functions, so agents and humans drive one code path.

## Install

```sh
git clone https://github.com/kyleking/tail-cw && cd tail-cw
uv sync
```

## Start here

```sh
uv run tail-cw                                   # the log group browser, the home screen
uv run tail-cw dash --demo                       # synthetic dashboard, no AWS account
uv run tail-cw logs '/aws/lambda/api*' --start 2h
uv run tail-cw export logs '/aws/lambda/*' --parsed --start 1h   # NDJSON, payload decoded
```

`--demo` reaches `logs`, `tail`, and `dash` as well, so the whole app is drivable
without
credentials.
The [project README][readme] carries the full command list, the key bindings, and the
screenshots.

## Documentation

- [CONFIGURATION](docs/CONFIGURATION.md) for the TOML sections, presets, and per-account
    profiles
- [FILTER_GUIDE](docs/FILTER_GUIDE.md) for the filter syntax shared by live, historical,
    and cached data
- [Architecture decisions](docs/adr/README.md) for why the tool is shaped this way
- [DEVELOPER_GUIDE](docs/DEVELOPER_GUIDE.md) and [STYLE_GUIDE](docs/STYLE_GUIDE.md) for
    working on it
- [CHANGELOG](docs/CHANGELOG.md) for release history

## Contributing

We welcome pull requests! For your pull request to be accepted smoothly, we suggest that
you first open a GitHub issue to discuss your idea.

## Code of Conduct

We follow the [Contributor Covenant Code of Conduct][contributor-covenant].

### Open Source Status

We try to reasonably meet most aspects of the "OpenSSF scorecard" from
[Open Source Insights](https://deps.dev/pypi/tail-cw)

## Responsible Disclosure

If you have any security issue to report, please contact the project maintainers
privately.
You can reach us at [dev.act.kyle@gmail.com](mailto:dev.act.kyle@gmail.com).

## License

[LICENSE]

[contributor-covenant]: https://www.contributor-covenant.org
[license]: https://github.com/kyleking/tail-cw/blob/main/LICENSE
[readme]: https://github.com/kyleking/tail-cw/blob/main/README.md
