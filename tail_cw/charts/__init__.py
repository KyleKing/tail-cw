"""Chart rendering for the dashboard TUI.

Renders CloudWatch metric series to PNG bytes with matplotlib (Agg backend, no
display server) for inline display via a terminal graphics protocol, plus a
pure-text braille fallback for terminals without image support.
"""
