# ruff: file-ignore[unused-async] - the fixtures are async generators, so `async def` is required
"""Tests for offloading blocking work and bridging async sources into it."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Iterator

import pytest

from tail_cw.concurrency import (
    DEFAULT_BLOCKING_WORKERS,
    BridgeCancelledError,
    blocking_pool,
    closing_stream,
    consume_in_thread,
    run_blocking,
    take,
)


async def _counting(limit: int) -> AsyncIterator[int]:
    for value in range(limit):
        yield value


class _ClosableStream:
    """Async iterator recording whether it was closed."""

    def __init__(self, limit: int) -> None:
        self._values = iter(range(limit))
        self.closed = False

    def __aiter__(self) -> _ClosableStream:
        return self

    async def __anext__(self) -> int:
        try:
            return next(self._values)
        except StopIteration as err:
            raise StopAsyncIteration from err

    async def aclose(self) -> None:
        self.closed = True


def test_blocking_pool_is_sized_and_named_for_its_purpose():
    with blocking_pool() as pool:
        assert pool._max_workers == DEFAULT_BLOCKING_WORKERS


def test_blocking_pool_shuts_down_on_exit():
    with blocking_pool(max_workers=1) as pool:
        assert pool.submit(lambda: 1).result() == 1
    with pytest.raises(RuntimeError):
        pool.submit(lambda: 1)


async def test_run_blocking_runs_off_the_event_loop_thread():
    loop_thread = threading.get_ident()
    with blocking_pool(max_workers=1) as pool:
        worker_thread = await run_blocking(pool, threading.get_ident)
    assert worker_thread != loop_thread


async def test_run_blocking_propagates_the_callables_error():
    def boom() -> int:
        msg = 'no'
        raise ValueError(msg)

    with blocking_pool(max_workers=1) as pool, pytest.raises(ValueError, match='no'):
        await run_blocking(pool, boom)


async def test_take_stops_at_the_limit():
    assert await take(_counting(10), 3) == [0, 1, 2]


async def test_take_returns_everything_when_under_the_limit():
    assert await take(_counting(2), 5) == [0, 1]


async def test_take_closes_the_source_it_abandons():
    stream = _ClosableStream(10)
    assert await take(stream, 2) == [0, 1]
    assert stream.closed


async def test_closing_stream_tolerates_a_source_without_aclose():
    async with closing_stream(_counting(2)) as stream:
        assert [value async for value in stream] == [0, 1]


async def test_consume_in_thread_streams_every_item_to_the_consumer():
    with blocking_pool(max_workers=1) as pool:
        total = await consume_in_thread(pool, _counting(2500), sum)
    assert total == sum(range(2500))


async def test_consume_in_thread_crosses_batch_boundaries():
    """More items than one batch proves the bridge loops rather than truncating."""
    with blocking_pool(max_workers=1) as pool:
        collected = await consume_in_thread(pool, _counting(2001), list)
    assert len(collected) == 2001
    assert collected[-1] == 2000


async def test_consume_in_thread_runs_the_consumer_off_the_loop_thread():
    loop_thread = threading.get_ident()

    def consume(items: Iterator[int]) -> int:
        list(items)
        return threading.get_ident()

    with blocking_pool(max_workers=1) as pool:
        worker_thread = await consume_in_thread(pool, _counting(5), consume)
    assert worker_thread != loop_thread


async def test_consume_in_thread_propagates_a_source_error():
    async def failing() -> AsyncIterator[int]:
        yield 1
        msg = 'stream died'
        raise RuntimeError(msg)

    with blocking_pool(max_workers=1) as pool, pytest.raises(RuntimeError, match='stream died'):
        await consume_in_thread(pool, failing(), list)


async def test_cancelling_raises_inside_the_consumer_rather_than_ending_it_short():
    """A cancelled consumer must fail, so a partial write is never mistaken for a whole one."""
    started = threading.Event()
    outcome: list[type[BaseException]] = []

    async def endless() -> AsyncIterator[int]:
        while True:
            yield 1
            await asyncio.sleep(0)

    def consume(items: Iterator[int]) -> None:
        try:
            for _ in items:
                started.set()
        except BaseException as err:  # Records which exception class actually reached the consumer
            outcome.append(type(err))
            raise

    with blocking_pool(max_workers=1) as pool:
        task = asyncio.create_task(consume_in_thread(pool, endless(), consume))
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert outcome == [BridgeCancelledError]


async def test_cancelling_frees_the_pool_slot():
    """The worker must unwind so the single-slot pool can accept new work."""
    started = threading.Event()

    async def endless() -> AsyncIterator[int]:
        while True:
            yield 1
            await asyncio.sleep(0)

    with blocking_pool(max_workers=1) as pool:
        task = asyncio.create_task(consume_in_thread(pool, endless(), lambda items: [*items]))
        await asyncio.to_thread(started.wait, 0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.wait_for(run_blocking(pool, lambda: 'free'), timeout=5) == 'free'


async def test_cancelling_before_the_first_batch_still_stops_the_consumer():
    reached: list[int] = []

    def consume(items: Iterator[int]) -> None:
        reached.extend(items)

    with blocking_pool(max_workers=1) as pool:
        task = asyncio.create_task(consume_in_thread(pool, _counting(10), consume))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
