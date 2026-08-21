"""tail_cw."""

from ._runtime_type_check_setup import configure_runtime_type_checking_mode
from .cpu_budget import apply_native_thread_limits

__version__ = '0.0.1'
__pkg_name__ = 'tail_cw'

configure_runtime_type_checking_mode()
# Polars reads POLARS_MAX_THREADS when it is imported, so this must precede any module
# that imports it.
apply_native_thread_limits()


# == Above code must always be first ==
