# ADR 0011: Async AWS I/O, and where blocking work goes

Date: 2026-07-25 Status: Accepted (supersedes the boto3 and worker decisions in ADR 0002)

## Problem

Every AWS call was synchronous boto3 run on a Textual thread worker.
That worked while the app made one call at a time.
The dashboard work in ADR 0005 and 0006 broke the assumption, because a dashboard fans
out one call per widget.

Three specific costs, all reproducible on the verified `irm-prod-main` dashboard (43
widgets, 28 of them metric):

- Textual thread workers run on asyncio's default executor (`run_in_executor(None, ...)`),
    capped at `min(32, cpu_count + 4)`.
    On a 12-core machine that is 16 slots, shared by every metric panel, every log-volume
    sparkline, every Parquet load, and the live tail worker, which holds one slot for the
    whole three-hour session.
    Twenty-eight metric widgets therefore fetch in two waves
- `exclusive=True` cancels the worker, not the HTTP request.
    A debounced reload or a time-range change abandons a thread mid-`GetMetricData`, so the
    old request still completes, still bills, and still occupies a slot
- Every function built its own client, so each call re-resolved SSO credentials and opened
    a fresh connection pool

## Options considered

1. Keep boto3 and fix only the fan-out: drive panels from one worker over an explicit
    `ThreadPoolExecutor`.
    Cheapest change, removes the wave behaviour, and leaves cancellation broken and
    credentials re-resolved per call
1. `aioboto3`: the familiar surface, but it wraps the boto3 resource and file-transfer
    layer this project never uses, and pulls `aiofiles` for the same reason
1. `aiobotocore` directly, with async Textual workers: clients only, which is all the
    project ever creates

## Decision

Option 3. `tail_cw/aws` is coroutines and async generators over `aiobotocore`, and the
ten Textual thread workers became async workers, which deleted twenty-two
`call_from_thread` hops.
`boto3` is gone from the dependency tree.

Two things had to be true before this was viable, and both were checked before any code
moved:

- `StartLiveTail` needs an async event stream.
    `aiobotocore` provides `AioEventStream` with `__aiter__`/`__anext__`, and its `__iter__`
    raises `NotImplementedError('Use async-for instead')`
- SSO with two profiles needs the async credential chain.
    `aiobotocore` ships `AioSSOProvider` over `AioSSOTokenProvider` and honours
    `profile_name`, so `sso-session` config resolves as before

Clients now come from a `ClientPool` held open for the session, and every function in
`aws/` takes an already-open client.
That keeps credential resolution at the edges, per the layering in ADR 0002, and removes
the per-call client construction.
Services depend on a `ClientProvider` Protocol rather than the concrete pool, so tests
wire a fake with no credentials at all.

### Where blocking work goes

The cache and query layers are DuckDB and Polars, neither of which has an asyncio
interface.
They cannot become coroutines, so the question is how to run them without stalling the
message loop.

Threads are the right offload here, and that is a measured claim rather than an
assumption.
Both libraries are native extensions that release the GIL during execution: four threads
of DuckDB queries scaled 3.09x, where four threads of pure-Python CPU work scaled 1.07x.
A process pool would only be needed if the hot work were pure Python.

The pool is deliberately small (four workers) and deliberately not asyncio's default
executor.
Both libraries already parallelize across cores internally, so stacking many concurrent
queries on top oversubscribes the machine and the event loop pays for it: eight
concurrent DuckDB queries through `asyncio.to_thread` stalled the loop for roughly 90ms,
against 1-2ms for the same queries through a small dedicated pool.
Owning the pool also means query work cannot be starved by unrelated `to_thread`
callers, and its width is chosen rather than inherited from the CPU count.

`polars.LazyFrame.collect_async()` is documented for exactly this use and does not work:
[pola-rs/polars#18718](https://github.com/pola-rs/polars/issues/18718) reports that it
blocks the event loop, and has been open since September 2024.

### How wide a fetch fans out (added 2026-08-22)

`FilterLogEvents` paginates serially: one `nextToken` at a time, one page per round
trip.
Instrumenting a cold hour of `irm-ecs-api-prod` showed where that lands, 108,491 events
over 33 pages: 19.73s of a 19.87s wall clock was spent awaiting pages, 0.13s building
records, and the loop never stalled longer than 28ms.
So the cost is round trips, and nothing about it is CPU or GIL bound.

The window is already cut into segments for caching, and running them concurrently is
what shortens the wall clock.
Measured against the same group, splitting one hour into concurrent sub-windows gave
16.69s serial, 7.90s at two, 4.50s at four, and 2.90s at eight, with the event count
identical every time and no throttling.
End to end through `export logs`, writes included, a cold hour went from 21.63s to 8.71s
at four.
Past eight it reverses (a second run measured 6.73s at 32 against 3.35s at 16), which
reads as retry backoff, so the ceiling stays configurable and low.

Segment writes run on their own pool (`fetch_pool`, eight threads), not the query pool.
The two are bounded by different things: a query is CPU work inside DuckDB or Polars and
its pool stays narrow, while a segment writer waits on the network for nearly all of its
life.
Sharing one pool made them compete, because four groups fetching filled it and a search
then waited for the fetch.
`[fetch].max_concurrent_segments` defaults to the fetch pool's width, since each
in-flight segment holds one of its threads from its first page until its Parquet write
returns.

Eight rather than four is worth less than the network probe implied.
Measured end to end with the writes included, a cold hour took 6.4s at eight against
7.3s
at four over three runs each, a 12% gain rather than the third the raw-network numbers
suggested, and event-loop lag p99 rose from about 9ms to between 13ms and 22ms.
The worst case stayed around 30ms at either setting, inside one frame, which is why
eight
stands.

The limiter is one semaphore per command, shared across every log group in it, so a
ten-group fetch cannot open ten times the ceiling.
It is built inside the running loop rather than at module level, per the rule below.

### The cancellation contract

Cancelling an async task does not interrupt a thread that task started.
A worker pulling events from a bridged async source would therefore hold its pool slot
forever once the loop stopped feeding it.
`consume_in_thread` closes that from both sides: it cancels the in-flight pull so a
thread blocked on the network wakes, and it raises `BridgeCancelledError` inside the
thread so the consumer unwinds.

Raising matters more than it looks. `LogCache.write` sets cache metadata only after the
Parquet write returns, so an exception from the iterator aborts cleanly and leaves an
orphan file the next write sweeps.
A bridge that merely *ended* its iterator early would let `write` finish normally and
register a truncated Parquet file as a complete cache entry, which is indistinguishable
from a complete one on the next read.

Segmented cache windows ([ADR 0003](0003-parquet-cache-and-local-query-engine.md))
inherit that contract per segment rather than per request: a cancelled fetch orphans the
segment it was writing, and the segments already written stay valid because each one is
keyed to its own bounds.
A cancelled multi-segment request therefore leaves the cache correct but partially
populated, which the next run completes.

`BridgeCancelledError` is a distinct type because `concurrent.futures.CancelledError`
derives from `Exception` rather than `BaseException`, so any `except Exception` between
the bridge and the caller would quietly absorb a cancellation.

Concurrent work uses `asyncio.TaskGroup`, not `asyncio.gather`, because `gather` leaves
siblings running when one fails.

## Tradeoffs

- The version pin is the standing cost. `aiobotocore` 3.8.0 requires
    `botocore>=1.43.3,<1.43.47`, so the lock sits at the 1.43.46 ceiling and every new AWS
    API or region update waits on an `aiobotocore` release.
    Renovate cannot bump `botocore` alone
- Dependency count moves the wrong way against the AGENTS.md rule: `boto3` and
    `s3transfer` leave, `aiobotocore` and `aiohttp` arrive with `aioitertools`, `multidict`,
    `yarl`, `frozenlist`, `aiosignal`, `propcache`, and `aiohappyeyeballs`
- Two async idioms now have to be held in mind at once: AWS calls are natively async,
    cache and query calls are blocking work behind an executor hop.
    Wrapping the query layer in `async def` would hide that rather than fix it, so it stays
    sync and the hop stays visible at the call site
- `aiobotocore` types are untyped, so `aws/` carries `type: ignore[import-untyped]` on its
    imports and clients are `Any` at the boundary

## Consequences

- Every function in `tail_cw.aws` takes an open client as its first argument and returns a
    coroutine or an async iterator; `ShellServices` callables are awaitable.
    This is a breaking change to the internal API
- A new blocking dependency (anything that does not release the GIL) cannot simply go on
    the shared pool.
    Check the GIL behaviour by measurement first, and reach for a process pool only when a
    thread is not a real offload
- Anything reading from a bridged stream must let `BridgeCancelledError` propagate.
    Swallowing it converts a cancelled write into a silently truncated one
- The Parquet write path is bounded by the four-worker pool rather than by AWS.
    Fetches all start together and the writes queue, which is where the real limit now sits
- Segment order no longer matches completion order, so anything that needs the window in
    order has to sort by segment rather than by arrival.
    `_resolve_into_cache` returns paths in window order for this reason, and a cold hour
    was checked byte for byte against a serial fetch (106,015 events, identical output)
