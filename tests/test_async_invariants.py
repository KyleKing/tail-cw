"""Guards for the async properties of ADR 0011 that ordinary tests would not catch.

These assert *how* the code runs concurrently, not just what it returns, because the
migration's whole value is in the how. A serialized fan-out or a reintroduced
thread worker still passes every functional test while giving back the latency and
the uncancellable requests the migration removed.
"""

from __future__ import annotations

import ast
import asyncio
import threading
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tail_cw.aws.client import LogEvent
from tail_cw.cli import FetchRequest, resolve_parquet_paths
from tail_cw.concurrency import DEFAULT_BLOCKING_WORKERS, blocking_pool
from tail_cw.config import CacheConfig, TailCWConfig

PACKAGE = Path(__file__).resolve().parent.parent / 'tail_cw'
NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
_BARRIER_TIMEOUT = 5.0


def _python_files() -> list[Path]:
    return sorted(PACKAGE.rglob('*.py'))


def _module_name(path: Path) -> str:
    return '.'.join(path.relative_to(PACKAGE.parent).with_suffix('').parts)


def _called_name(node: ast.Call) -> str:
    return node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, 'id', '')


def _asks_for_a_thread(node: ast.Call) -> bool:
    if _called_name(node) not in {'run_worker', 'work'}:
        return False
    return any(
        keyword.arg == 'thread' and not (isinstance(keyword.value, ast.Constant) and keyword.value.value is False)
        for keyword in node.keywords
    )


def test_no_textual_thread_workers() -> None:
    """A thread worker cannot be cancelled mid-request, so the AWS paths must not use one."""
    offenders = [
        f'{_module_name(path)}:{node.lineno}'
        for path in _python_files()
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8')))
        if isinstance(node, ast.Call) and _asks_for_a_thread(node)
    ]
    assert offenders == [], f'thread workers reintroduced: {offenders}'


def test_no_call_from_thread() -> None:
    """An async worker already runs on the message loop, so this hop should never be needed again."""
    offenders = [
        f'{_module_name(path)}:{node.lineno}'
        for path in _python_files()
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8')))
        if isinstance(node, ast.Attribute) and node.attr == 'call_from_thread'
    ]
    assert offenders == [], f'call_from_thread reintroduced: {offenders}'


def test_boto3_is_not_imported() -> None:
    """boto3 is not a dependency; importing it would work locally and fail on a clean install."""
    offenders = []
    for path in _python_files():
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or '']
            else:
                continue
            if any(name == 'boto3' or name.startswith('boto3.') for name in names):
                offenders.append(f'{_module_name(path)}:{node.lineno}')
    assert offenders == [], f'boto3 imported: {offenders}'


def test_no_asyncio_gather() -> None:
    """TaskGroup cancels siblings on failure; gather leaves them running."""
    offenders = [
        f'{_module_name(path)}:{node.lineno}'
        for path in _python_files()
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8')))
        if isinstance(node, ast.Attribute) and node.attr == 'gather'
    ]
    assert offenders == [], f'use asyncio.TaskGroup instead of gather: {offenders}'


def test_no_module_level_asyncio_primitives() -> None:
    """These bind to the first event loop that touches them, so they cannot be module state."""
    bound_to_a_loop = {'Semaphore', 'Lock', 'Event', 'Condition', 'BoundedSemaphore', 'Queue'}
    offenders = []
    for path in _python_files():
        for node in ast.parse(path.read_text(encoding='utf-8')).body:
            if not isinstance(node, ast.Assign):
                continue
            value = node.value
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and value.func.attr in bound_to_a_loop
                and isinstance(value.func.value, ast.Name)
                and value.func.value.id == 'asyncio'
            ):
                offenders.append(f'{_module_name(path)}:{node.lineno}')
    assert offenders == [], f'module-level asyncio primitives are loop-bound: {offenders}'


def test_blocking_pool_does_not_borrow_the_default_executor() -> None:
    """Query work must not share asyncio's default pool, or unrelated to_thread callers can starve it."""
    with blocking_pool() as pool:
        assert pool.submit(lambda: threading.current_thread().name).result().startswith('tail-cw')
        assert pool._max_workers == DEFAULT_BLOCKING_WORKERS
        assert DEFAULT_BLOCKING_WORKERS <= 8, 'DuckDB and Polars parallelize internally; a wide pool stalls the loop'


class _Barrier:
    """Fails the test rather than hanging when the work under test serializes."""

    def __init__(self, parties: int) -> None:
        self._parties = parties
        self._arrived = 0
        self._all_here = asyncio.Event()

    async def wait(self) -> None:
        self._arrived += 1
        if self._arrived >= self._parties:
            self._all_here.set()
        try:
            await asyncio.wait_for(self._all_here.wait(), timeout=_BARRIER_TIMEOUT)
        except TimeoutError as err:
            msg = f'only {self._arrived} of {self._parties} calls started; the work ran serially'
            raise AssertionError(msg) from err


def _event(log_group: str) -> LogEvent:
    return LogEvent(
        log_group=log_group,
        log_stream='stream',
        timestamp=NOW,
        message='hello',
        event_id=f'event-{log_group}',
        ingestion_time=None,
    )


def _requests(count: int) -> list[FetchRequest]:
    return [
        FetchRequest(log_group=f'/group/{index}', start_time=NOW - timedelta(hours=1), end_time=NOW)
        for index in range(count)
    ]


async def test_resolve_parquet_paths_fetches_every_group_concurrently(tmp_path: Path) -> None:
    """Every fetch must be in flight before any completes, or a multi-group load costs N round trips."""
    count = 4
    barrier = _Barrier(count)

    async def fetch(_client: object, log_group: str, *_args: object, **_kwargs: object) -> AsyncIterator[LogEvent]:
        await barrier.wait()
        yield _event(log_group)

    config = TailCWConfig(cache=CacheConfig(cache_dir=tmp_path / 'cache'))
    with blocking_pool(max_workers=2) as pool:
        paths = await resolve_parquet_paths(object(), _requests(count), config, fetch_events=fetch, executor=pool)

    assert len(paths) == count


async def test_resolve_parquet_paths_cancels_siblings_when_one_fails(tmp_path: Path) -> None:
    """A TaskGroup stops the others; gather would leave them writing into a cache nobody reads."""
    finished: list[str] = []

    async def fetch(_client: object, log_group: str, *_args: object, **_kwargs: object) -> AsyncIterator[LogEvent]:
        if log_group == '/group/0':
            msg = 'fetch exploded'
            raise RuntimeError(msg)
        await asyncio.sleep(0.2)
        finished.append(log_group)
        yield _event(log_group)

    config = TailCWConfig(cache=CacheConfig(cache_dir=tmp_path / 'cache'))
    with blocking_pool(max_workers=2) as pool, pytest.raises(BaseExceptionGroup) as caught:
        await resolve_parquet_paths(object(), _requests(3), config, fetch_events=fetch, executor=pool)

    assert caught.group_contains(RuntimeError, match='fetch exploded')
    assert finished == [], 'siblings kept running after a failure'
