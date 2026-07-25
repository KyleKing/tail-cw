# ruff: file-ignore[unused-async] - producing an awaitable from a value is the point of these helpers
"""Helpers for driving the awaitable ``ShellServices`` signatures from tests.

The shell's services are coroutines and async generators, so a plain lambda no
longer satisfies them. These wrappers keep the fakes in tests as short as they
were while returning what the service layer awaits.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from typing import Any, TypeVar

T = TypeVar('T')


async def ready(value: T) -> T:
    """Present an already-computed value as an awaitable."""
    return value


def returns(value: T) -> Callable[..., Awaitable[T]]:
    """Build a service that awaits to ``value``, whatever arguments it is given."""
    return lambda *_args, **_kwargs: ready(value)


def calls(work: Callable[..., T]) -> Callable[..., Awaitable[T]]:
    """Build a service that runs a sync callable and awaits to its result."""
    return lambda *args, **kwargs: ready(work(*args, **kwargs))


def streams(items: Iterable[T]) -> Callable[..., AsyncIterator[T]]:
    """Build a live-stream service that yields ``items`` then ends."""

    async def factory(*_args: Any, **_kwargs: Any) -> AsyncIterator[T]:
        for item in items:
            yield item

    return factory


def raises(error: BaseException) -> Callable[..., Awaitable[Any]]:
    """Build a service that fails with ``error`` when awaited."""

    async def factory(*_args: Any, **_kwargs: Any) -> Any:
        raise error

    return factory


def stream_raises(error: BaseException, *, after: Iterable[T] = ()) -> Callable[..., AsyncIterator[T]]:
    """Build a live-stream service that yields ``after`` and then fails."""

    async def factory(*_args: Any, **_kwargs: Any) -> AsyncIterator[T]:
        for item in after:
            yield item
        raise error

    return factory
