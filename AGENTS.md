# AGENTS.md

A concise, agent-focused guide for working on this Python project with uv tooling and Textual-based TUI code. Keep changes highly testable: prefer functions over classes, efficient data structures, and the fewest dependencies practical.

## Quick context

- Language: Python >=3.11
- Package layout: `tail_cw/` with tests in `tests/`
- Dependency management with uv (`uv sync`)
- Package manager/config in repo: uv (via `pyproject.toml`)
- Linters/types: Ruff, Mypy, Pyright
- Docs: MkDocs in `docs/`
- AWS I/O is async over aiobotocore, and `botocore` is pinned to the window aiobotocore accepts. A `botocore` upgrade that Renovate cannot land alone is expected; check aiobotocore's supported range first ([ADR 0011](docs/docs/adr/0011-async-aws-io-and-blocking-work.md))

## Core commands

WARN: the `./run` commands (and nox) are currently broken. Use the direct commands listed below instead

- Format: `uv run ruff format`
- Lint: `uv run ruff check --fix --unsafe-fixes`
- Types: `uv run mypy` and `uv run pyright`
- Tests: `uv run pytest -q -n auto` (about 7s; roughly 50s without `-n auto`, since Textual Pilot tests are the bulk). Drop `-n auto` when debugging one test or using `--pdb`, which xdist cannot support
- Pre-Commit: `prek run --all-files`

`-n auto` is not in `addopts` on purpose, so the default invocation stays debuggable. Every fixture is per-test (`tmp_path`), so parallel runs are safe; do not introduce a fixture that writes to a shared path.

Agents should run tests and lint/type checks before finishing a task and fix any failures.

## Coding guidance (testable-first)

- Functions over classes
    - Default to pure functions for data transforms and business logic.
    - Keep side effects at the edges (I/O, environment). Pass dependencies as parameters (simple DI), not globals.
- Data structures (prefer stdlib)
    - Favor `list`, `dict`, `set`, and `tuple`; use `collections.deque` for FIFO/LRU, rolling windows, and log tails.
    - Use `heapq` for top-N, `bisect` for sorted inserts, `array`/`memoryview` when working with large numeric buffers.
    - Choose `dataclasses` for simple records; avoid heavy object hierarchies.
- Fewest dependencies
    - Reach for stdlib first. Add third-party libs only with clear benefit and small footprint; use `uv add <name>` for managing dependencies
    - If a new dependency duplicates existing functionality (e.g., Rich/Textual/Ruff/Mypy already present), don’t add it.
- Types and contracts
    - Add precise type hints. Prefer `collections.abc` for callables/iterables.
    - Document inputs/outputs and error modes in docstrings; raise specific exceptions.
    - Runtime type checking (beartype) enforces annotations exactly; use explicit unions or helper types when accepting multiple numeric kinds instead of relying on implicit coercion.
- Testing
    - Unit-test pure functions thoroughly. Add at least one edge/boundary test per function.
    - For I/O, isolate adapters and test with fakes; avoid network calls in tests.
    - Keep tests fast and deterministic; avoid sleeps and random without seeding. Where a TTL or timeout must really elapse, use a fractional value (`_SHORT_TTL` in `tests/test_cache.py`) rather than a whole second.
    - Every fixture must be per-test (`tmp_path`). A fixture writing to a shared path breaks parallel runs and lets tests delete each other's data.
    - `tests/test_async_invariants.py` guards the properties of [ADR 0011](docs/docs/adr/0011-async-aws-io-and-blocking-work.md) that a functional test cannot see: no thread workers or `call_from_thread`, no `boto3` import, no `asyncio.gather`, no module-level asyncio primitives, and fan-out that genuinely overlaps. Add to it when you add a concurrency rule, and check a new guard actually fails when the invariant is broken.
    - Assert concurrency with a barrier that times out (see `_Barrier` there), not with wall-clock thresholds, so a serialized regression fails with a clear message instead of flaking.
    - When synthesising datetimes, use `timedelta` arithmetic instead of `datetime.replace` to stay within valid ranges.
    - Make boolean parameters keyword-only in helpers/fixtures to avoid Ruff FBT warnings and improve readability.

## Textual-specific guidance (performance & architecture)

If you introduce or modify Textual UI code:

- Think in Segments (from Textual/Rich)
    - Avoid treating the terminal as a naive 2D char grid. Compose Rich `Segment`s, let Textual’s compositor handle overlaps.
- Minimize re-render work
    - Use `reactive` attrs judiciously; batch updates and prefer partial updates over full-screen redraws.
    - Avoid per-frame allocation of large Python objects; precompute immutable renderables where possible.
- Spatial locality
    - For large widget trees, avoid O(n) per frame visibility checks. Let Textual’s spatial map prune non-visible widgets.
- Async and workers (see [ADR 0011](docs/docs/adr/0011-async-aws-io-and-blocking-work.md))
    - AWS calls are natively async through aiobotocore, so use plain async workers (`run_worker(self._coro())`), never `thread=True`. An async worker already runs on the message loop, so it updates widgets directly with no `call_from_thread`.
    - A thread worker cannot be interrupted, so `exclusive=True` on one cancels the bookkeeping and leaves the request running. Async workers cancel the coroutine and close the connection, which is why the AWS paths must stay async.
    - Blocking work (DuckDB, Polars, Parquet) goes through `tail_cw.concurrency`: `run_blocking` for a one-shot call, `consume_in_thread` to feed a blocking consumer from an async source. Do not use bare `asyncio.to_thread`, which lands on asyncio's shared default executor.
    - Do not wrap the sync query layer in `async def`. A thread is still required underneath, and hiding the hop makes it easy to lose.
    - Concurrent work uses `asyncio.TaskGroup`, not `asyncio.gather`; `gather` leaves siblings running when one fails.
    - Never declare an `asyncio.Semaphore`, `Lock`, or `Event` at module level. They bind to the first event loop that touches them. Build them inside the running loop.
    - `async def` with no `await` is a bug unless it is an async generator or an adapter conforming to an awaitable signature.
    - Before offloading a new blocking library to a thread, measure whether it releases the GIL. Threads are a real offload for DuckDB and Polars (3.09x on four threads, against 1.07x for pure-Python CPU work); a GIL-holding library needs a process pool instead.
- Efficient log views (common TUI pattern)
    - Use `deque(maxlen=...)` as a ring buffer for tailing logs.
    - Chunk incoming lines; coalesce updates to reduce render churn.
    - Consider backpressure when sources outpace UI frame rate.
- Testing Textual
    - Structure UI logic so state transitions are driven by pure functions you can unit-test.
    - Use Textual’s test utilities (Pilot) for interaction tests; assert on widget state, not pixel-perfect frames.

References:

- Algorithms for high-performance terminal apps (Textual): https://textual.textualize.io/blog/2024/12/12/algorithms-for-high-performance-terminal-apps
- Rich Segments: https://github.com/Textualize/rich/blob/master/rich/segment.py

## Repository conventions

- Style rules configured in `pyproject.toml` (Ruff/Mypy/Pyright). Match those; don’t override locally.
- Tests live in `tests/`. Add tests for any new behavior; prefer small, focused test modules.
- Docs live in `docs/`. If you add public API, include docstrings; mkdocstrings will surface them.
- Versioning managed via `commitizen` config in `pyproject.toml`; keep semantic, conventional commits.

## Acceptance criteria (what to make green)

- Ruff: no errors on changed files
- Types: `mypy` and `pyright` pass on changed files and related modules
- Tests: `pytest` suite passes locally
- If you added Textual code: basic interaction test(s) included; UI remains responsive under typical input rates
- No new runtime dependencies unless justified in request

## Notes for agents

- Prefer touching the smallest surface area; avoid broad refactors unless asked.
- If you’re unsure between feature breadth and testability, choose testability.
- When in doubt about performance in Textual, profile and reduce work per frame; batch and reuse renderables.
- Trace heuristics should inspect structured fields (levels, status, message bodies) before falling back to free-text keyword scans to avoid misclassifying IDs like `trace-error` as failures.
- Give Textual shortcuts a usable default focus (table/tree) so bindings like `t`, `e`, and `c` fire even before the user switches focus manually.

---

This AGENTS.md is living documentation. Update it as the toolchain or architecture evolves. Inspired by the community guidance at https://agents.md.
