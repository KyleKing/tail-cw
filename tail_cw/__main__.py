"""Entry point for the tail-cw command line interface.

Only the argparse surface is imported here. ``--help`` and a rejected typo both
exit inside ``parse_args``, and loading the pipelines to reach that point cost
278ms against 0.03s for a bare interpreter (measured 2026-08-22), which the tool
paid on every invocation from a shell loop.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from tail_cw.parser import build_parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the tail-cw CLI and return the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    # The one deliberate deferred import in the package: aiobotocore (88ms),
    # Polars (34ms), and Textual (36ms) must not load to answer --help.
    from tail_cw.services import run  # noqa: PLC0415

    try:
        return run(args, parser)
    except KeyboardInterrupt:
        return 0
    except Exception as err:
        sys.stderr.write(f'Error: {_readable(err)}\n')
        return 1


def _readable(err: BaseException) -> str:
    """Name what actually failed, looking through the TaskGroups that carried it.

    Concurrent fetches raise an ``ExceptionGroup`` whose own message is
    "unhandled errors in a TaskGroup (1 sub-exception)", which says nothing about
    the expired token or missing permission underneath it.
    """
    while isinstance(err, BaseExceptionGroup) and len(err.exceptions) == 1:
        err = err.exceptions[0]
    if isinstance(err, BaseExceptionGroup):
        return '; '.join(_readable(inner) for inner in err.exceptions)
    return str(err)


if __name__ == '__main__':
    sys.exit(main())
