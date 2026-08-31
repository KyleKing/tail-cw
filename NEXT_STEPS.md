# Next steps

## The TUI preview fetches twice on Windows

Four tests fail on `windows-latest` and pass everywhere else, and each one fails the
same way: the list of requested names carries a duplicate.
`test_preview_renders_for_the_highlighted_group` expects `['/aws/lambda/api']` and
gets that name twice.

Failing tests:

- `tests/test_tui_groups_screen.py::test_preview_renders_for_the_highlighted_group`
- `tests/test_tui_groups_screen.py::test_a_cached_preview_is_not_refetched`
- `tests/test_tui_dashboards_screen.py::test_a_cached_body_is_not_refetched`
- `tests/test_tui_dashboards_screen.py` line 216

Not a flake. It reproduces on every Windows run and predates the calcipy_template
5.4.0 update: same four assertions, same duplicate shape, on
[run 32596225445](https://github.com/KyleKing/tail-cw/actions/runs/32596225445)
from 2026-08-22 under Python 3.14, and again on 3.13 today.

Two of the four names say `is_not_refetched`, so the cache guard is what is not
holding. Something dispatches the fetch twice before the first result populates the
cache, and Windows event ordering is what exposes the window. Read it as a real
double-dispatch in the preview path rather than a platform quirk in the test: a user
on Windows pays for two API calls per highlight.
