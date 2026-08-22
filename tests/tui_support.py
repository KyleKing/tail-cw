"""Run a shell app under Textual's Pilot, settled enough to assert on."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from textual.pilot import Pilot

from tail_cw.tui.shell import TailCWApp


@asynccontextmanager
async def running(app: TailCWApp, *, settled: bool = False) -> AsyncIterator[Pilot[None]]:
    """Run ``app`` with its opening view mounted.

    Args:
        app: The shell to run.
        settled: Also wait for every worker to finish, for a view that loads
            its content in one.

    Yields:
        The pilot driving the running app.
    """
    async with app.run_test() as pilot:
        await pilot.pause()
        if settled:
            await app.workers.wait_for_complete()
            await pilot.pause()
        yield pilot
