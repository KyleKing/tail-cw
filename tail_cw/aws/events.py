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


def to_utc(moment: datetime) -> datetime:
    """Normalize a datetime botocore parsed for us.

    botocore attaches the machine's local zone to every timestamp it parses, so a
    response field used as-is prints a local offset while every epoch-derived
    field in tail-cw prints UTC: one tool, two conventions, and only under a shell
    whose ``TZ`` is not UTC.
    """
    return moment.astimezone(UTC)


def to_utc_or_none(moment: datetime | None) -> datetime | None:
    """Normalize a botocore timestamp that the API may omit."""
    return None if moment is None else to_utc(moment)
