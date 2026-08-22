"""One progress contract, because one worker has to feed both halves of a fetch.

A cold window is an AWS read followed by a Parquet write, and those lived in different
modules with different callback signatures: ``(count, message)`` in the client and
``(current, total, status)`` in the cache. Anything reporting on the whole operation had
to adapt between them, which is why nothing did.

Imports nothing, so both the aiobotocore side and the Polars side can depend on it.
"""

from __future__ import annotations

from collections.abc import Callable

TOTAL_UNKNOWN = -1
"""Passed as ``total`` while the size of the work is not yet known.

A paginated AWS read never knows how many events it will return until it ends, so a
progress bar over it can only show a count. A Parquet conversion knows.
"""

ProgressCallback = Callable[[int, int, str], None]
"""``(current, total, status)``: items done, items expected or :data:`TOTAL_UNKNOWN`, a label."""
