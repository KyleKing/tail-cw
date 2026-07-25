# Plan: one interactive TUI

Implements [ADR 0008](../docs/docs/adr/0008-single-interactive-tui.md). Ordered so each step lands green (ruff, mypy, pyright, pytest) and is committable on its own. Steps 1 through 4 add pure code behind no user-visible change. Step 5 is the switch.

## 1. Log group discovery and pattern resolution

New `tail_cw/aws/log_groups.py`.

```python
@dataclass(frozen=True)
class LogGroupInfo:
    name: str
    arn: str
    stored_bytes: int | None
    retention_days: int | None
    created: datetime | None

def describe_log_groups(*, prefix=None, profile_name=None, region_name=None) -> Iterator[LogGroupInfo]
def resolve_group_pattern(pattern: str, groups: Sequence[LogGroupInfo]) -> list[LogGroupInfo]  # pure
```

`resolve_group_pattern` is the M2 ladder, pure and fully unit-tested: exact name wins alone, then a `logGroupNamePrefix` match, then fnmatch over the full list. `describe_log_groups` paginates `DescribeLogGroups` and is the only part needing a stub.

Tests: `tests/test_aws_log_groups.py`. Ladder precedence, empty pattern, no match, glob with and without `/`, pagination via a stubbed client.

## 2. Message pattern clustering

New `tail_cw/query/patterns.py`, no AWS, no I/O.

```python
@dataclass(frozen=True)
class MessagePattern:
    key: str        # normalized shape
    count: int
    example: str    # first raw message matching the shape

def normalize_message(message: str) -> str
def cluster_messages(messages: Iterable[str], *, limit: int = 8) -> list[MessagePattern]
```

`normalize_message` replaces, in order: ISO and epoch timestamps, UUIDs, long hex runs, quoted strings, then bare integers and floats, with `<ts>`, `<uuid>`, `<hex>`, `<str>`, `<n>`. Order matters because a UUID contains hex runs which contain digits, and the tests pin it. JSONL messages normalize their `parsed` values rather than the raw line when the cache has already parsed them, so `{"level":"ERROR","ms":12}` and `{"level":"ERROR","ms":900}` share a shape.

`cluster_messages` counts normalized keys in a `Counter`, returns the top `limit` by count as `MessagePattern`, ties broken by first appearance so output is deterministic.

Tests: `tests/test_query_patterns.py`. Each placeholder class in isolation, the ordering hazard (UUID not shredded into hex and digits), a JSONL case, empty input, `limit` truncation, determinism on ties, and one realistic Lambda log sample asserting the shape count.

## 3. Group previews

New `tail_cw/preview.py` (core layer, Textual-free).

```python
@dataclass(frozen=True)
class GroupPreview:
    log_group: str
    event_count: int
    window_seconds: int
    patterns: list[MessagePattern]

def build_group_preview(log_group, *, window, now, config, profile_name=None, region_name=None,
                        fetch_events: FetchEvents | None = None, sample_limit: int = 500) -> GroupPreview
```

Fetches at most `sample_limit` events over `window` (default 15m) through the existing `fetch_log_events` seam, clusters them, and caches the result in the diskcache metadata store under `preview:v1:<blake2b of group, window, region, profile>` with a TTL of 5 minutes. Cache reads and writes go through a thin pair of functions in `cache/storage.py` so `LogCache` keeps owning the store.

Tests: `tests/test_preview.py`. Fake fetcher, assert sample cap honored, assert a second call does not refetch, assert TTL expiry refetches, assert an empty group returns `event_count=0` with no patterns.

## 4. Navigation stack and dive ranking

New `tail_cw/tui/navigation.py`, pure, no Textual import.

```python
class ViewKind(StrEnum):
    GROUPS = 'groups'; LOGS = 'logs'; DASHBOARDS = 'dashboards'; DASHBOARD = 'dashboard'

@dataclass(frozen=True)
class NavTarget:
    kind: ViewKind
    label: str
    payload: tuple[str, ...] = ()

@dataclass(frozen=True)
class NavState:
    stack: tuple[NavTarget, ...]
    jumps: tuple[tuple[NavTarget, ...], ...]
    jump_index: int

def initial(target) -> NavState
def current(state) -> NavTarget
def push(state, target) -> NavState
def pop(state) -> NavState
def replace_top(state, target) -> NavState
def jump_back(state) -> NavState
def jump_forward(state) -> NavState
def sibling(state, targets, *, offset) -> NavTarget | None
def breadcrumb(state) -> str
```

Jumplist semantics copy vim: `push` truncates any forward history, `jump_back` walks toward index 0 and stops, `jump_forward` stops at the end, and popping to the root leaves the jumplist intact so `Ctrl+I` still works after `Esc`.

Three details only fell out once the shell drove this code, and each is now pinned by a test:

A jumplist entry is a whole stack, not a single target. Recording just the target left the breadcrumb showing the depth you jumped from, because the stack never moved.

`[` and `]` need `replace_top`, not a pop followed by a push. A pop at the root is a no-op, so pop-then-push grew the stack instead of swapping the view.

The real root view is pushed over Textual's default screen rather than replacing it, because `switch_screen` empties the stack when the default screen is the only entry, which is exactly what `]` hits on the home view. Textual's depth therefore stays one greater than `NavState`'s, in lockstep.

One more trap, worth recording because it will bite any future annotation in `shell.py`: beartype resolves a function's annotations when the function is first called, so a name imported only under `if TYPE_CHECKING:` makes the call fail at runtime with an unimportable forward reference. `TailCWApp.dive` takes a `Widget`, so `Widget` must be a real runtime import. There is no cycle to justify hiding it, since `aws/dashboards.py` is core.

Dive ranking goes in `tail_cw/aws/dashboards.py` next to the widget model, since it reads widget shape:

```python
@dataclass(frozen=True)
class DiveCandidate:
    log_group: str
    reason: str          # 'SOURCE clause', 'FunctionName dimension', ...
    exists: bool
    event_count: int | None

def candidate_log_groups(widget: Widget) -> list[tuple[str, str]]   # pure, ranked, (group, reason)
def rank_dive_candidates(widget, *, known_groups, count_events) -> list[DiveCandidate]
```

`candidate_log_groups` replaces `resolve_log_group_for_widget` and covers `SOURCE` clauses first, then `FunctionName`, `ClusterName` + `ServiceName`, `ApiId`, `DBInstanceIdentifier`, and `LoadBalancer`. `rank_dive_candidates` takes both AWS touches as injected callables, marks non-existent names `exists=False` without dropping them (the name is still the explanation), and sorts existing-with-events first.

Tests: `tests/test_tui_navigation.py` (every transition, plus the truncate-on-push and stop-at-edge cases) and additions to `tests/test_aws_dashboards.py` for each dimension mapping, an unmapped widget, and ranking with a fake counter.

## 5. The shell

New `tail_cw/tui/shell.py` holding `TailCWApp(App[None])`, which owns the header breadcrumb, the app-level `CommandLine`, `NavState`, and shared session state (window, filter, profile, region, selected groups).

Two structural constraints the first cut got wrong, both worth stating because they shape the module boundaries:

`Session` lives in `cli.py`, not in `shell.py`. The CLI builds it from arguments before any Textual import happens, which is the same slot `FetchRequest` already occupies.

The views subclass `ShellScreen`, so `shell.py` cannot import them. Rather than reach for function-local imports (the repo bans lazy imports), `TailCWApp` takes a `build_screen: Callable[[NavTarget], ShellScreen]` constructor argument, and a thin `tui/views.py` sitting above both supplies it. `WhichKeyScreen` and `DiveConfirmScreen` are leaves that reference the app only under `TYPE_CHECKING`, so `shell.py` imports those directly.

```python
@dataclass(slots=True)
class Session:
    start: datetime
    end: datetime
    filter_pattern: str | None
    profile: str | None
    region: str | None
    selected_groups: list[str]
```

Conversions, smallest diff first:

- `LogResultsScreen` is deleted rather than reused. It existed only as a cut-down dive destination, and the full `LogsScreen` now takes that role, so a dive lands somewhere with search, the trace views, and the live toggle already available
- `LogTailApp` becomes `LogsScreen(Screen[None])`. Its `compose`, workers, live-tail plumbing, search, and trace bindings move unchanged. `q`/quit and the `App` CSS move to the shell
- `DashboardApp` becomes `DashboardScreen(Screen[None])`; its `CommandLine` mounting and `:` dispatch move to the shell, and its command table registers into a shared registry so `:stat` exists only while a dashboard is on top
- new `GroupsScreen` (home): filterable `DataTable` of `LogGroupInfo` on the left, `GroupPreview` pane on the right, `Space` multi-select capped at 10, `Enter` opens logs, `t` opens live tail
- new `DashboardsScreen`: the same list-plus-detail widget, previewing a dashboard's widget titles instead of log patterns

Shell bindings: `:` command, `Esc` pop, `Ctrl+O`/`Ctrl+I` jumps, `[`/`]` sibling, `g` prefix for `gl`/`gd`/`gg`, `?` and `,` help, `q` quit. Per-screen bindings stay on their screens so Textual's own resolution decides precedence.

Commands the shell registers: `groups`, `logs [pattern]`, `tail [pattern]`, `dash [name]`, `dashboards`, `range`, `filter`, `profile`, `region`, `help`, `quit`. Tab completion sources group names and dashboard names from the caches populated in steps 1 and 3.

Multi-group historical fetch: `cli.py` grows `resolve_parquet_paths(requests) -> list[Path]` fanning out over a `ThreadPoolExecutor` (`max_workers=4`), and `query/engine.py` grows a merge read over several Parquet paths ordered by timestamp. One file per group keeps step 3's cache keys and ADR 0003's scheme untouched.

Tests: `tests/test_tui_shell.py` with Pilot. Launch lands on groups, `:dash demo` switches and the breadcrumb updates, `Esc` pops, `Ctrl+O` then `Ctrl+I` round-trips, `]` cycles dashboards, dive pushes the confirmation list and `Enter` opens logs with the widget window applied. `tests/test_tui_dashboard_app.py` and `tests/test_tui_app.py` get mechanical updates from `App` to `Screen` mounting, and their behavior assertions stay.

## 6. CLI surface

`build_parser` becomes `logs`, `tail`, `dash`, and `export` with subcommands `logs`, `tail`, `groups`, `dashboards`, `dashboard`. Shared flags (`--config`, `--profile`, `--region`, `--start`, `--end`, `--filter`) move to a parent parser so the interactive and export halves cannot drift. `--json` is removed rather than deprecated. Bare `tail-cw` on a TTY launches the shell, and off a TTY it prints help and exits 2, keeping ADR 0002's contract.

`__main__.py` collapses its three runner injections into one `_run_shell(config, session, nav_target)`.

Tests: `tests/test_cli.py` gains coverage for each `export` path (the dashboard gaps the audit found are filled here), the TTY versus non-TTY bare invocation, and seed-to-`NavTarget` resolution for each of `logs`, `tail`, and `dash`.

## 7. Docs

README rewritten around one entry point, with the new key table and a group-browser screenshot. ADR 0008 moves from Proposed to Accepted and enters `docs/docs/adr/README.md`. The roadmap marks M2 as delivered by this work (browser, resolution ladder, multi-select) and notes that recents and named presets are the remaining M2 slice. `CONFIGURATION.md` documents the preview TTL and sample cap as new `[preview]` keys.

## Sequencing note

Steps 1 through 4 are independent of each other and of the Textual layer, so they can go in parallel. Step 5 depends on all four. Step 6 depends on step 5. Step 7 lands with step 6.

## Decided

`tail` folds into the logs view. One `LogsScreen` owns both a historical window and a live stream, with `L` toggling between them and the filter, window, and group selection carried across the switch. `tail-cw tail <pattern>` survives as a seed that opens that screen already streaming.

## Open questions

- whether the preview sample should reuse a cached Parquet window when one already covers it, trading freshness for zero API calls
- whether `:profile` lands here (ADR 0006 deferred it) or waits, given that switching profiles invalidates the group list, the preview cache, and the dashboard list at once
