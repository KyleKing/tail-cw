"""Text fitting shared by every surface that has a fixed number of cells.

Light on purpose: the log table, the markdown reports, and the compact charts all
need this, and none of them should pull in another's dependencies to get it.
"""

from __future__ import annotations

ELLIPSIS = '…'


def shorten(text: str, limit: int) -> str:
    """Cut text to ``limit`` cells, marking the cut.

    A truncated name that looks like a whole name is a correctness problem rather
    than a cosmetic one: ``/aws/rds/cluster/irm`` is a plausible log group that
    does not exist.
    """
    return text if len(text) <= limit else text[: limit - 1] + ELLIPSIS
