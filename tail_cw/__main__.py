"""Entry point for the tail-cw command line interface.

Only the argparse surface is imported here. ``--help`` and a rejected typo both
exit inside ``parse_args``, and loading the pipelines to reach that point cost
278ms against 0.03s for a bare interpreter (measured 2026-08-22), which the tool
paid on every invocation from a shell loop.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence

from tail_cw.completion import install as install_completion
from tail_cw.concurrency import INTERRUPTED_EXIT, is_engine_panic
from tail_cw.parser import build_parser


def _write_utf8(stream: object) -> None:
    """Make a text stream emit UTF-8 whatever the console code page says.

    JSON is UTF-8 by definition, so an NDJSON line holding a non-ASCII log message is
    correct and a console encoder that cannot represent it is not. Without this, one
    accented character in a log line ends an export with ``UnicodeEncodeError`` on a
    Windows console running a legacy code page.
    """
    reconfigure = getattr(stream, 'reconfigure', None)
    if reconfigure is not None:
        reconfigure(encoding='utf-8')


def main(argv: Sequence[str] | None = None) -> int:
    """Run the tail-cw CLI and return the process exit code."""
    _write_utf8(sys.stdout)
    _write_utf8(sys.stderr)
    parser = build_parser()
    install_completion(parser)
    args = parser.parse_args(argv)
    # The one deliberate deferred import in the package: aiobotocore (88ms),
    # Polars (34ms), and Textual (36ms) must not load to answer --help.
    from tail_cw.services import run  # noqa: PLC0415

    try:
        return run(args, parser)
    except KeyboardInterrupt:
        sys.stderr.write('Interrupted before finishing\n')
        return INTERRUPTED_EXIT
    except Exception as err:
        message = _readable(err)
        if _needs_sso_login(err):
            profile = getattr(args, 'profile', None) or os.environ.get('AWS_PROFILE')
            login = f'aws sso login --profile {profile}' if profile else 'aws sso login'
            message = f'{message}. The SSO session needs a new login: run `{login}` and retry'
        sys.stderr.write(f'Error: {message}\n')
        return 1
    except BaseException as err:
        # Polars' Rust side raises outside the Exception hierarchy, so without this the
        # tool exits on a traceback rather than a message.
        if not is_engine_panic(err):
            raise
        sys.stderr.write(f'Error: the query engine panicked, which is a bug: {err}\n')
        return 1


_SSO_LOGIN_ERRORS = frozenset({'SSOTokenLoadError', 'TokenRetrievalError', 'UnauthorizedSSOTokenError'})


def _needs_sso_login(err: BaseException) -> bool:
    """Report whether botocore gave up on an SSO token that only a new login fixes."""
    if isinstance(err, BaseExceptionGroup):
        return any(_needs_sso_login(inner) for inner in err.exceptions)
    return type(err).__name__ in _SSO_LOGIN_ERRORS


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
