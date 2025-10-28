"""Textual-based terminal user interface.

This module provides a high-performance TUI for viewing and searching CloudWatch
logs. It includes a DataTable widget for columnar log display, modal screens for
full record inspection, trace viewer with waterfall-style grouping across multiple
sources, and search input with live filtering. The implementation follows Textual
performance best practices including batch updates, streaming, and efficient
rendering using Rich Segments.
"""
