"""Entry point for the tail-cw command line interface.

Argument parsing and the fetch pipeline live in ``tail_cw.cli``; this module
wires in the Textual TUI runner and handles top-level error reporting. It can
be invoked as ``python -m tail_cw``, via the ``tail-cw`` console script, or
with ``uv run tail-cw``.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

from tail_cw.cli import FetchRequest, resolve_parquet_path, run_cli
from tail_cw.config import TailCWConfig
from tail_cw.tui.app import LogTailApp


def _run_tui(config: TailCWConfig, parquet_path: Path, request: FetchRequest) -> None:
    app = LogTailApp(config=config, parquet_path=parquet_path)

    def refetch(updated: FetchRequest) -> Path | None:
        return resolve_parquet_path(updated, config)

    app.set_fetch_context(request, refetch)
    app.run()


def main(argv: Sequence[str] | None = None) -> int:
    """Run the tail-cw CLI and return the process exit code."""
    try:
        return run_cli(argv, _run_tui)
    except KeyboardInterrupt:
        return 0
    except Exception as err:
        sys.stderr.write(f'Error: {err}\n')
        return 1


if __name__ == '__main__':
    sys.exit(main())
