# AGENTS.md

A concise, agent-focused guide for working on this Python project with uv tooling and Textual-based TUI code. Keep changes highly testable: prefer functions over classes, efficient data structures, and the fewest dependencies practical.

## Quick context

- Language: Python >=3.11
- Package layout: `tail_cw/` with tests in `tests/`
- Dependency management with uv (`uv sync`)
- Package manager/config in repo: uv (via `pyproject.toml`)
- Linters/types: Ruff, Mypy, Pyright
- Docs: MkDocs in `docs/`

## Core commands

WARN: the `./run` commands (and nox) are currently broken. Use the direct commands listed below instead

- Format: `uv run ruff format`
- Lint: `uv run ruff check --fix --unsafe-fixes`
- Types: `uv run mypy` and `uv run pyright`
- Tests: `uv run pytest -q --ff`
- Pre-Commit: `prek run --all-files`

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
    - Keep tests fast and deterministic; avoid sleeps and random without seeding.
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
- Async and workers
    - Offload blocking I/O (e.g., AWS calls) to Textual workers; keep the message loop responsive.
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
