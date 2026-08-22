"""The event record every layer passes around, and the timestamp conversion behind it.

Separate from :mod:`tail_cw.aws.client` so that reading a cached Parquet file,
rendering a table, or classifying severity does not import aiobotocore. Only the
modules that actually talk to AWS pay that cost.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class LogEvent:
    """Represents a single CloudWatch Logs event.

    Attributes:
        log_group: The CloudWatch log group name.
        log_stream: The CloudWatch log stream name.
        timestamp: Event timestamp as timezone-aware datetime (UTC). Converted from
            epoch milliseconds returned by CloudWatch API.
        message: The log message content.
        ingestion_time: When CloudWatch ingested the event. May be None if not
            provided in the API response.
    """

    log_group: str
    log_stream: str
    timestamp: datetime
    message: str
    ingestion_time: datetime | None


def epoch_ms_to_datetime(epoch_ms: int) -> datetime:
    """Convert epoch milliseconds, which is what CloudWatch returns, to a UTC datetime."""
    return datetime.fromtimestamp(epoch_ms / 1000.0, tz=UTC)
