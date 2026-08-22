"""Shell completion for the CLI, over the group names you have actually opened.

Completion runs in a subshell on every Tab, so it can only read what is already on disk.
It reads the recents file, which holds the last :data:`~tail_cw.recents.DEFAULT_RECENTS_LIMIT`
group names per profile: the groups worth completing are the ones you keep going back to,
and no AWS call is defensible at that latency.

Wired through argcomplete rather than hand-written per shell, so bash, zsh, and fish come
from one place and stay in step with argparse.
"""

from __future__ import annotations

import os
from typing import Any

from tail_cw.recents import load_recents, profile_recents

ACTIVATION_VARIABLE = '_ARGCOMPLETE'
"""Set by the shell hook while completing, and by nothing else.

The import of argcomplete is guarded on it because the entry point's whole job is to stay
cheap, and a completion round trip is the only time the library does anything.
"""


def log_group_completer(prefix: str, parsed_args: Any = None, **_kwargs: Any) -> list[str]:
    """Complete a log group name from the recents file, filtered by ``prefix``.

    Prefix matching, not the substring matching the pattern resolver does. A shell
    replaces the word being completed, so offering ``/aws/lambda/api-handler`` for
    ``handler`` would make the typed word vanish, and argcomplete filters non-prefix
    matches out anyway. Substring resolution still happens at run time, so
    ``tail-cw logs handler`` opens that group whether or not Tab could complete it.
    """
    profile = getattr(parsed_args, 'profile', None) or os.environ.get('AWS_PROFILE')
    try:
        names = profile_recents(load_recents(), profile)
    except OSError:
        return []
    lowered = prefix.lower()
    return [name for name in names if name.lower().startswith(lowered)]


def install(parser: Any) -> None:
    """Attach the completers and hand the parser to argcomplete, if it is completing.

    Does nothing at all unless the shell set :data:`ACTIVATION_VARIABLE`, so a normal
    invocation neither imports argcomplete nor pays for it.
    """
    if ACTIVATION_VARIABLE not in os.environ:
        return
    import argcomplete  # noqa: PLC0415 - see ACTIVATION_VARIABLE

    argcomplete.autocomplete(parser)
