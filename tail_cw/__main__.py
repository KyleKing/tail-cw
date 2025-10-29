"""Entry point for running the tail-cw TUI application.

This module provides the main() function that serves as the entry point
for the CloudWatch Logs viewer TUI. It can be invoked in multiple ways:

    1. As a module: python -m tail_cw
    2. Via console script: tail-cw (after installation)
    3. With uv: uv run tail-cw

The entry point handles initialization of the LogTailApp, graceful shutdown
on keyboard interrupt, and error reporting to stderr.

Future enhancements:
    - CLI argument parsing for log group, stream, time range
    - Config file support for AWS credentials, preferences
    - Option to load from cache vs. fetch from AWS
"""

from __future__ import annotations

import sys

from tail_cw.tui.app import LogTailApp


def main() -> int:
    """Main entry point for the TUI application.

    Creates and runs a LogTailApp instance. Currently starts with no data
    loaded; future versions will support CLI arguments for specifying log
    sources and filters.

    Returns:
        Exit code: 0 for success, 1 for error

    Example:
        >>> # From command line
        >>> # python -m tail_cw
        >>> # or
        >>> # tail-cw
    """
    try:
        # Create the app with no initial data
        # TODO: Add CLI argument parsing for log group, time range, etc.
        app = LogTailApp()

        # Run the TUI
        app.run()

    except KeyboardInterrupt:
        # User pressed Ctrl+C, exit gracefully
        return 0

    except Exception as e:
        # Unexpected error, log and exit with error code
        sys.stderr.write(f'Error: {e}\n')
        return 1

    else:
        return 0


if __name__ == '__main__':
    sys.exit(main())
