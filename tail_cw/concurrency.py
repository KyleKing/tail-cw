"""Offloading blocking work from the event loop, and feeding it async data.

The AWS calls in :mod:`tail_cw.aws` are natively async, but the cache and query
layers are DuckDB and Polars, which have no asyncio interface. Both are native
extensions that release the GIL during execution, so a worker thread is a real
offload rather than a GIL-bound relay; a process pool is not needed.

Three rules shape this module.

Blocking work goes to a dedicated pool rather than asyncio's default executor,
which is shared with every other ``to_thread`` caller and sized
``min(32, cpu_count + 4)``.

The pool stays small, because DuckDB and Polars each parallelize across cores
internally: stacking many concurrent queries on top of that oversubscribes the
machine and the event loop pays for it in latency, measured at roughly 90ms of
stall for eight concurrent DuckDB queries against 1-2ms through a small pool.

Cancelling an async task does not interrupt a thread that task started, so a
thread pulling from a bridged async source would hold its slot forever once the
loop stopped feeding it. :func:`consume_in_thread` closes that hole from both
sides: it cancels the in-flight pull so a thread blocked on the network wakes,
and it raises :class:`BridgeCancelledError` inside the thread so the consumer unwinds
instead of finishing early on a truncated stream.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from typing import Generic, TypeVar

T = TypeVar('T')
R = TypeVar('R')

DEFAULT_BLOCKING_WORKERS = 4
"""Concurrent DuckDB/Polars calls to allow. Above this, oversubscription stalls the loop."""

DEFAULT_BRIDGE_BATCH = 1000
"""Items pulled per loop round-trip, and so the granularity of cancellation."""


class BridgeCancelledError(Exception):
    """Raised inside a worker thread when the awaiting task was cancelled.

    Callers that write to durable storage must let this propagate. Swallowing it
    turns a cancelled stream into a short one, which is indistinguishable from a
    complete stream to whatever the consumer writes.

    Raised in place of ``concurrent.futures.CancelledError``, which a worker thread
    would otherwise see. That one derives from ``Exception`` rather than
    ``BaseException``, so any ``except Exception`` between here and the caller would
    quietly absorb a cancellation.
    """


@contextmanager
def blocking_pool(max_workers: int = DEFAULT_BLOCKING_WORKERS) -> Iterator[ThreadPoolExecutor]:
    """Open a dedicated pool for blocking cache and query work.

    Kept separate from asyncio's default executor so query work cannot be starved
    by unrelated ``to_thread`` callers, and so its width is chosen rather than
    inherited from the CPU count. Exiting waits for running work, which is bounded
    because every bridged consumer stops within one batch of being cancelled.

    Yields:
        The pool, shut down when the block exits.
    """
    pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='tail-cw-blocking')
    try:
        yield pool
    finally:
        pool.shutdown(wait=True)


async def run_blocking(executor: ThreadPoolExecutor | None, work: Callable[[], T]) -> T:
    """Run a blocking callable on ``executor``, or the default pool when None.

    The thread runs to completion even if the awaiting task is cancelled. Use
    :func:`consume_in_thread` for work long enough that the leaked slot matters.
    """
    return await asyncio.get_running_loop().run_in_executor(executor, work)


@asynccontextmanager
async def closing_stream(source: AsyncIterator[T]) -> AsyncIterator[AsyncIterator[T]]:
    """Close an async source on the way out, when it supports closing.

    Closing matters for anything backed by a paginator: abandoning the generator
    mid-page leaves the HTTP response open until the interpreter finalizes it.
    Unlike :func:`contextlib.aclosing` this accepts any async iterator, so a test
    fake need not implement ``aclose``.

    Yields:
        The source, unchanged.
    """
    try:
        yield source
    finally:
        if (aclose := getattr(source, 'aclose', None)) is not None:
            await aclose()


async def take(source: AsyncIterator[T], limit: int) -> list[T]:
    """Read at most ``limit`` items, then close the source."""
    async with closing_stream(source) as stream:
        items: list[T] = []
        async for item in stream:
            items.append(item)
            if len(items) >= limit:
                break
        return items


async def _next_batch(source: AsyncIterator[T], size: int) -> list[T]:
    batch: list[T] = []
    async for item in source:
        batch.append(item)
        if len(batch) >= size:
            break
    return batch


class _Bridge(Generic[T]):
    """Pulls from an async iterator on ``loop`` for a consumer in another thread.

    Written as a plain class rather than a dataclass so the lock and the in-flight
    future stay internal state instead of constructor arguments.
    """

    def __init__(
        self,
        source: AsyncIterator[T],
        loop: asyncio.AbstractEventLoop,
        batch_size: int = DEFAULT_BRIDGE_BATCH,
    ) -> None:
        self.source = source
        self.loop = loop
        self.batch_size = batch_size
        self._lock = threading.Lock()
        self._pending: Future[list[T]] | None = None
        self._cancelled = False

    def __iter__(self) -> Iterator[T]:
        while True:
            with self._lock:
                if self._cancelled:
                    raise BridgeCancelledError
                pending = asyncio.run_coroutine_threadsafe(_next_batch(self.source, self.batch_size), self.loop)
                self._pending = pending
            try:
                batch = pending.result()
            except (CancelledError, asyncio.CancelledError) as err:
                raise BridgeCancelledError from err
            if not batch:
                return
            yield from batch

    def cancel(self) -> None:
        """Stop the consumer, waking it if it is blocked on the network."""
        with self._lock:
            self._cancelled = True
            if self._pending is not None:
                self._pending.cancel()


async def consume_in_thread(
    executor: ThreadPoolExecutor | None,
    source: AsyncIterator[T],
    consume: Callable[[Iterator[T]], R],
) -> R:
    """Run ``consume`` in a worker thread, feeding it from an async iterator.

    Lets blocking code that wants an iterator (``LogCache.write``, which streams
    events into Parquet) read from an async source without first materializing it.
    Batching costs one loop round-trip per :data:`DEFAULT_BRIDGE_BATCH` items.

    On cancellation the worker raises :class:`BridgeCancelledError` rather than ending
    its iterator, so ``consume`` fails instead of committing a partial result, and
    the pool slot frees within one batch.
    """
    loop = asyncio.get_running_loop()
    bridge: _Bridge[T] = _Bridge(source, loop)
    try:
        return await loop.run_in_executor(executor, lambda: consume(iter(bridge)))
    except asyncio.CancelledError:
        bridge.cancel()
        raise
