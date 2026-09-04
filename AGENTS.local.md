# AGENTS.local.md

Project-specific guidance for tail-cw, loaded from `AGENTS.md`.
Keep changes highly testable: prefer functions over classes, efficient data structures,
and the fewest dependencies practical.

## Quick context

- Language: Python >=3.13, developed and CI-gated on 3.14
- Package layout: `tail_cw/` with tests in `tests/`
- Dependency management with uv (`uv sync`)
- Package manager/config in repo: uv (via `pyproject.toml`)
- Linters/types: Ruff, Mypy, Pyright
- Docs: MkDocs in `docs/`
- AWS I/O is async over aiobotocore, and `botocore` is pinned to the window aiobotocore
    accepts.
    A `botocore` upgrade that Renovate cannot land alone is expected; check aiobotocore's
    supported range first ([ADR 0011](docs/docs/adr/0011-async-aws-io-and-blocking-work.md))

## Core commands

WARN: the `./run` commands (and nox) are currently broken.
Use the direct commands listed below instead

- Format: `uv run ruff format`
- Lint: `uv run ruff check --fix --unsafe-fixes`
- Types: `uv run mypy` and `uv run pyright`
- Tests: `uv run pytest -q -n auto` (about 7s; roughly 50s without `-n auto`, since
    Textual Pilot tests are the bulk).
    Drop `-n auto` when debugging one test or using `--pdb`, which xdist cannot support
- Pre-Commit: `prek run --all-files`
- Profile: `uv tool install py-spy`, then
    `py-spy record -o profile.svg -- uv run tail-cw <command>`
    for a flamegraph, or `py-spy dump --pid <pid>` against an already-running one.
    `benchmark_backends()` in `tail_cw/query/engine.py` times DuckDB against Polars for one
    Parquet file without needing py-spy at all
- Docs: `uv run python docs/gen_ref_nav.py && uv run mkdocs build --strict`.
    The generator must run first, because mkdocs collects files before any plugin does and a
    stub written during a build is a build late.
    `--strict` catches a relative markdown link in a docstring, which resolves from the
    source tree and not from the generated reference page

`-n auto` is not in `addopts` on purpose, so the default invocation stays debuggable.
Every fixture is per-test (`tmp_path`), so parallel runs are safe; do not introduce a
fixture that writes to a shared path.

Agents should run tests and lint/type checks before finishing a task and fix any
failures.

## Coding guidance (testable-first)

- Functions over classes
    - Default to pure functions for data transforms and business logic.
    - Keep side effects at the edges (I/O, environment).
        Pass dependencies as parameters (simple DI), not globals.
- Data structures (prefer stdlib)
    - Favor `list`, `dict`, `set`, and `tuple`; use `collections.deque` for FIFO/LRU, rolling
        windows, and log tails.
    - Use `heapq` for top-N, `bisect` for sorted inserts, `array`/`memoryview` when working
        with large numeric buffers.
    - Choose `dataclasses` for simple records; avoid heavy object hierarchies.
- Fewest dependencies
    - Reach for stdlib first. Add third-party libs only with clear benefit and small
        footprint; use `uv add <name>` for managing dependencies
    - If a new dependency duplicates existing functionality (e.g., Rich/Textual/Ruff/Mypy
        already present), don’t add it.
- Types and contracts
    - Add precise type hints. Prefer `collections.abc` for callables/iterables.
    - Document inputs/outputs and error modes in docstrings; raise specific exceptions.
    - Runtime type checking (beartype) enforces annotations exactly; use explicit unions or
        helper types when accepting multiple numeric kinds instead of relying on implicit
        coercion.
- Testing
    - Unit-test pure functions thoroughly. Add at least one edge/boundary test per function.
    - For I/O, isolate adapters and test with fakes; avoid network calls in tests.
    - Keep tests fast and deterministic; avoid sleeps and random without seeding.
        Where a TTL or timeout must really elapse, use a fractional value (`_SHORT_TTL` in
        `tests/test_cache.py`) rather than a whole second.
    - Every fixture must be per-test (`tmp_path`).
        A fixture writing to a shared path breaks parallel runs and lets tests delete each
        other's data.
    - `tests/test_async_invariants.py` guards the properties of
        [ADR 0011](docs/docs/adr/0011-async-aws-io-and-blocking-work.md) that a functional test
        cannot see: no thread workers or `call_from_thread`, no `boto3` import, no
        `asyncio.gather`, no module-level asyncio primitives, and fan-out that genuinely
        overlaps.
        Add to it when you add a concurrency rule, and check a new guard actually fails when the
        invariant is broken.
    - Assert concurrency with a barrier that times out (see `_Barrier` there), not with
        wall-clock thresholds, so a serialized regression fails with a clear message instead of
        flaking.
    - When synthesising datetimes, use `timedelta` arithmetic instead of `datetime.replace` to
        stay within valid ranges.
    - Make boolean parameters keyword-only in helpers/fixtures to avoid Ruff FBT warnings and
        improve readability.

## Module layering and startup cost

`tail-cw --help` must not load aiobotocore, Polars, DuckDB, or Textual.
Three rules keep it that way, and
`tests/test_main.py::test_the_entry_point_does_not_load_the_heavy_stack` fails if one is
broken:

- `tail_cw/parser.py` owns the argparse surface and imports only light modules.
    `tail_cw/cli.py` owns the pipelines and imports it, never the other way round
- `tail_cw/__main__.py` holds the one deliberate deferred import in the package
    (`from tail_cw.services import run`, inside `main`).
    The global "never lazy import" rule stands everywhere else; this is the single
    exception, and it is what buys 0.35s down to 0.07s per invocation
- the record and the client are separate: `tail_cw/aws/events.py` holds `LogEvent`, and
    `tail_cw/aws/client.py` holds the aiobotocore calls.
    Likewise `tail_cw/cache/records.py` answers "what does this row say" with no Polars
    behind it.
    `tail_cw/aws/__init__.py`, `cache/__init__.py`, and `query/__init__.py` re-export
    nothing on purpose: a facade there loads the whole subpackage for one import

## Textual-specific guidance (performance & architecture)

If you introduce or modify Textual UI code:

- Think in Segments (from Textual/Rich)
    - Avoid treating the terminal as a naive 2D char grid.
        Compose Rich `Segment`s, let Textual’s compositor handle overlaps.
- Minimize re-render work
    - Use `reactive` attrs judiciously; batch updates and prefer partial updates over
        full-screen redraws.
    - Avoid per-frame allocation of large Python objects; precompute immutable renderables
        where possible.
- Spatial locality
    - For large widget trees, avoid O(n) per frame visibility checks.
        Let Textual’s spatial map prune non-visible widgets.
- Async and workers (see [ADR 0011](docs/docs/adr/0011-async-aws-io-and-blocking-work.md))
    - AWS calls are natively async through aiobotocore, so use plain async workers
        (`run_worker(self._coro())`), never `thread=True`.
        An async worker already runs on the message loop, so it updates widgets directly with no
        `call_from_thread`.
    - A thread worker cannot be interrupted, so `exclusive=True` on one cancels the
        bookkeeping and leaves the request running.
        Async workers cancel the coroutine and close the connection, which is why the AWS paths
        must stay async.
    - Blocking work (DuckDB, Polars, Parquet) goes through `tail_cw.concurrency`:
        `run_blocking` for a one-shot call, `consume_in_thread` to feed a blocking consumer from
        an async source.
        Do not use bare `asyncio.to_thread`, which lands on asyncio's shared default executor.
    - Do not wrap the sync query layer in `async def`.
        A thread is still required underneath, and hiding the hop makes it easy to lose.
    - Concurrent work uses `asyncio.TaskGroup`, not `asyncio.gather`; `gather` leaves siblings
        running when one fails.
    - A fetch's fan-out is bounded by one semaphore per command, built inside the loop and
        shared across every log group, because each in-flight segment holds a blocking-pool
        thread until its Parquet write returns.
        `[fetch].max_concurrent_segments` defaults to `DEFAULT_FETCH_WORKERS`, the width of
        that pool.
    - There are two blocking pools, and the difference matters: `blocking_pool` is narrow
        because DuckDB and Polars are CPU work, `fetch_pool` is wider because a segment
        writer spends most of a cold hour waiting on the network.
        Do not put query work on the fetch pool in the TUI, where the two run at once.
        A busy segment's write tail is CPU work like a query once the network wait is over,
        so it still runs on `fetch_pool`'s thread but must acquire
        `cpu_budget.native_write_gate()` first, capping it at the query budget regardless of
        `fetch_pool`'s width
    - Never declare an `asyncio.Semaphore`, `Lock`, or `Event` at module level.
        They bind to the first event loop that touches them.
        Build them inside the running loop.
    - `async def` with no `await` is a bug unless it is an async generator or an adapter
        conforming to an awaitable signature.
    - Native engines get a CPU budget, not the whole machine: `tail_cw/cpu_budget.py` caps
        DuckDB and Polars at 40% of the CPU count (`TAIL_CW_CPU_FRACTION` /
        `TAIL_CW_MAX_THREADS` override).
        `POLARS_MAX_THREADS` is read at import, so `apply_native_thread_limits()` must stay the
        first thing `tail_cw/__init__.py` does after the type-check hook.
        Polars' pool is process-wide so it takes the whole budget; DuckDB sizes per connection,
        so it takes the budget divided by the blocking-pool width.
    - Before offloading a new blocking library to a thread, measure whether it releases the
        GIL.
        Threads are a real offload for DuckDB and Polars (3.09x on four threads, against 1.07x
        for pure-Python CPU work); a GIL-holding library needs a process pool instead.
- Efficient log views (common TUI pattern)
    - Use `deque(maxlen=...)` as a ring buffer for tailing logs.
    - Chunk incoming lines; coalesce updates to reduce render churn.
    - Consider backpressure when sources outpace UI frame rate.
- Prompts and one-row inputs
    - A prompt must not `dock`.
        A docked input lands on the same row as the docked `Footer` or breadcrumb and is
        painted over, so it reads and runs what you type while showing nothing
    - A one-row `Input` must restate `PROMPT_CSS` (`tail_cw/tui/command_bar.py`) in its own
        leaf selector, `border: none !important` included.
        `Input` sets a tall border and a height of 3, `Input:focus` outranks a plain type
        selector, and an inherited rule loses the tie
    - A hidden `Input` left in the focus chain takes the initial focus and swallows every
        keystroke the footer advertises, so `HiddenInput` flips `can_focus` with `display`
    - A long-running worker needs a visible clock and a key that stops it.
        Escape alone is not that key on a view that can be the opening view, because
        `nav_pop` has nothing to pop there, so the screen overrides it to cancel first and go
        back on the second press
    - A docked widget with no explicit `width` reserves only its own text, and the next
        widget flows into the rest of that row.
        The breadcrumb is docked, so the histogram row landed beside it until `#breadcrumb`
        got `width: 100%`.
        A screenshot caught this; a Pilot test asserting on rendered text never would
    - Two widgets docked to the same edge land on the same row and the later one paints
        over the earlier.
        `#dash_status` docked `bottom` under a `Footer` that also docks `bottom`, and never
        rendered at any size.
        A status line belongs in the flow above a `1fr` pane, not docked
- A `VerticalScroll` with `width: auto` measures 0x0 and scrolls nothing.
    Give it a percentage or a fixed width.
    A modal that has to scroll also needs its close hint *outside* the scroll container, or
    the hint is the first thing to go off screen
- Anything with a fixed height needs a floor, and a widget that cannot have its floor
    should say so rather than draw.
    plotext in four rows returns axis furniture, no data, and an inverted y axis; the
    dashboard grid now gives up whole rows (`grid_rows_that_fit`) so the stage keeps
    `STAGE_MIN_HEIGHT`.
    Give up *whole* rows: a grid height that does not divide into cells squeezes every row
    instead of dropping the last, which renders as a column of clipped borders
- A bar chart fills from the axis floor, so fitting the axis to data far from zero would
    misstate every magnitude.
    `fitted_ylim` decides, and a fitted series is drawn as a line instead.
    Without it a RequestCount between 950 and 1350 filled eighteen of twenty-one rows with
    solid block
- A nested `Message` subclass inside a widget breaks under `RUNTIME_TYPE_CHECKING_MODE`:
    the outer class's `__dict__` descriptor resolves to the inner class and every
    `cached_property` on the widget raises.
    Declare messages at module level (`FacetSelected`, `ProgressUpdate`)
- `Screen.active_bindings` is read by the `Footer` and nothing else that matters, so
    overriding it thins the hints without disabling a key.
    Textual gives every hint an equal grid column and clips inside it, which is how
    `/ Searc` reached the footer.
    Anything that recomposes the footer lays the screen out again, so guard the refit on
    the width or `on_resize` arrives back at itself
- Never clip a header, a breadcrumb, or a label to a fixed width without marking the cut.
    `Timestam` in the table's own header and `logs demo/web-api ·` in the breadcrumb both
    read as rendering bugs.
    `fit_breadcrumb` drops whole parts and `tail_cw/text.py:shorten` marks what it cuts;
    a header narrower than its own label gets a shorter label
- Dim is not a hierarchy on a row that already carries colour.
    Dimming the `key=value` remainder of an error row put its status code and latency at
    1.9:1 against 4.6:1 for an ordinary row, which made the one row worth reading the
    least legible thing on screen.
    Colouring it the theme's red reaches only 2.7:1, so the remainder gets plain text
    (7.7:1) and the glyph and the coloured phrase carry the signal
- A base style on a `Text` applies to every `append` too, so a per-span style of `''`
    inherits it rather than clearing it.
    Style the phrase as a span (`Text(); text.append(phrase, style=...)`) when the rest of
    the line must not take its colour
- VHS is not ground truth for colour.
    Under `NO_COLOR` a VHS capture rendered the faulted waterfall row as a blank line,
    while the same view in tmux showed it with its glyph and its bar.
    Screenshots are for layout and hierarchy; confirm anything colour-dependent in a real
    terminal before believing it
- Never let colour be the only carrier of meaning.
    Under `NO_COLOR` a red row renders *dim*, so the one span that failed became the
    quietest thing on screen.
    Pair it with a glyph in a column of its own, like the log table's severity mark, and
    not with a prefix on an indented name: a prefix displaces the indentation and a nested
    row then reads as a root
- Size a table's columns from the terminal, not from constants.
    The waterfall's fixed 38-wide name and 18-wide service left an 80-column terminal 12
    columns of timeline, which cannot tell two sibling calls apart.
    `column_widths` is a pure function so the split is testable at every width
- A status line takes `Text`, never a bare string.
    Error text is not ours: every Polars failure names its file as
    `[/path.parquet]`, which Rich parses as a closing tag and raises `MarkupError`
    from inside `update`, so the error handler took the app down on every failed
    search
- A debounced fetch must re-check its cache *at fire time*, not only when scheduled
    (`should_fetch` in `tail_cw/tui/picker.py`).
    The value can arrive between the two, and the browsers paid for a second API call per
    highlight on Windows because of it: four TUI tests failed there and nowhere else, each
    with the requested name twice
    - Never handle `on_descendant_blur` to restore focus.
        It fires when another control opens
        and steals the focus straight back; watch the one widget you mean
- Testing Textual
    - Structure UI logic so state transitions are driven by pure functions you can unit-test.
    - Use Textual’s test utilities (Pilot) for interaction tests; assert on widget state, not
        pixel-perfect frames.
    - A binding is not covered until a test *presses the key*.
        Three dead bindings shipped with passing tests that called the action directly
    - Pilot cannot see a control that renders nothing, so a prompt's test asserts its
        `content_region` has a row.
        Drive the app under tmux before believing a new control works

References:

- Algorithms for high-performance terminal apps (Textual):
    https://textual.textualize.io/blog/2024/12/12/algorithms-for-high-performance-terminal-apps
- Rich Segments: https://github.com/Textualize/rich/blob/master/rich/segment.py

## Repository conventions

- Style rules configured in `pyproject.toml` (Ruff/Mypy/Pyright).
    Match those; don’t override locally.
- Tests live in `tests/`. Add tests for any new behavior; prefer small, focused test
    modules.
- Docs live in `docs/`. If you add public API, include docstrings; mkdocstrings will
    surface them.
- Versioning managed via `commitizen` config in `pyproject.toml`; keep semantic,
    conventional commits.

## Acceptance criteria (what to make green)

- Ruff: no errors on changed files
- Types: `mypy` and `pyright` pass on changed files and related modules
- Tests: `pytest` suite passes locally
- If you added Textual code: basic interaction test(s) included; UI remains responsive
    under typical input rates
- No new runtime dependencies unless justified in request

## Notes for agents

- Prefer touching the smallest surface area; avoid broad refactors unless asked.
- If you’re unsure between feature breadth and testability, choose testability.
- When in doubt about performance in Textual, profile and reduce work per frame; batch and
    reuse renderables.
- The cache's NDJSON stage (`_log_events_to_ndjson_file`) always re-encodes through
    `msgspec.json.Encoder`, timestamps included as epoch-microsecond ints rather than
    `isoformat()` strings, because msgspec's own encoder beat splicing pre-encoded text
    even after accounting for the re-encode: 251k events/s against 166k, measured against
    1.5M synthetic prod-shaped events.
    `_normalized_columns` casts the int columns back to UTC datetimes with `pl.from_epoch`
- A payload shape Parquet cannot hold is repaired rather than refused
    (`_repair_payload_types`): an always-empty JSON object is dropped and a key logged as
    two JSON types is widened to text, on a second pass over the staged NDJSON that only
    runs when the first `sink_parquet` fails.
    Both sets come back on the write stats and reach stderr or a TUI notice, because
    retyping a payload key silently is the thing that would be worse than failing
- `export logs --parsed` emits the decoded payload instead of the raw line, and `--limit`
    fetches segments one at a time so the fetch itself stops.
    A literal log group name never lists the account; only a glob or an `@preset` pays for
    `DescribeLogGroups` (`_target_group_names`)
- Field counting lives in `tail_cw/query/facets.py` and serves both `export stats --by`
    and the log view's panel.
    A field carrying one value per record (`is_identifier_like`) is dropped from the panel:
    a trace id listed one value per record and crowded out the field that groups
- A record-field filter (`level:info`) is skipped for a Parquet file whose `parsed`
    struct lacks that field, because both engines raise on an absent struct field and one
    such group failed the search for every other group.
    A tree holding a `NOT` is never skipped: "not level:info" matches every record in a
    file with no `level`
- X-Ray is billed per trace *scanned*, and `TracesProcessedCount` counts the traces a
    filter expression rejected.
    So a narrow `--expression` over a wide window costs the same as no expression at all,
    and only a shorter window is cheaper
    ([ADR 0013](docs/docs/adr/0013-read-x-ray-directly-for-spans.md)).
    Any new X-Ray surface caps its paging by default and says what it scanned
- Half the spans in an X-Ray trace are `inferred`, synthesized per downstream resource and
    named after the call that reached them.
    Read the service from `metadata.default["otel.resource.service.name"]`, fall back to the
    parent's for a subsegment, and use `origin` for an inferred one; using `name` makes
    every
    SQL statement look like its own service
- Our services log a trace id in W3C form (`6a89ad51596c…`), and X-Ray only answers to the
    dashed form (`1-6a89ad51-596c…`).
    They are the same 32 digits, and the leading eight are the epoch, which is also how
    `as_xray_trace_id` tells an X-Ray id from any other 32-digit hex id.
    A pivot that checks for the dashes rejects every real log line
- A filter is local by default and portable only sometimes.
    `portable_filter_pattern` decides, and its refusals are load-bearing: CloudWatch
    *ignores* its `?` any-of terms when they are mixed with anything else instead of
    rejecting the pattern, so sending a mixed expression returns the wrong events with no
    error.
    Never widen what it translates without checking that CloudWatch can mean it exactly
- A native engine panic (`pyo3_runtime.PanicException`) derives from `BaseException`, so
    every `except Exception` in the tool looks past it.
    `query_parquet_file` converts it to `EnginePanicError`; keep new Polars and DuckDB
    calls behind that boundary, or convert them the same way
- Runtime type checking is opt-in through `RUNTIME_TYPE_CHECKING_MODE`, which pytest sets
    and the installed binary does not.
    So beartype catches an annotation violation (an `int` where a `float` is declared) in
    the suite and never in a real run: the test is the only place that check exists
- Trace heuristics should inspect structured fields (levels, status, message bodies)
    before falling back to free-text keyword scans to avoid misclassifying IDs like
    `trace-error` as failures.
- A trace spans every selected log group, so trace grouping reads all of `_parquet_paths`
    rather than the first, and goes through the `load_traces` service onto the blocking
    pool.
    Do not add a span-hierarchy feature keyed on `parent_span_id`: our logs do not carry it,
    and [ADR 0012](docs/docs/adr/0012-export-traces-instead-of-drawing-them.md) sends trace
    rendering out as OTLP JSON instead of drawing it here.
- Give Textual shortcuts a usable default focus (table/tree) so bindings like `t`, `e`,
    and `c` fire even before the user switches focus manually.

---

This file is living documentation. Update it as the toolchain or architecture evolves.
