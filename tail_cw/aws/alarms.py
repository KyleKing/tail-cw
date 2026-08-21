"""Read CloudWatch alarms and their state history.

An alarm is usually where an investigation starts, and the two questions it raises are what
it watches and how often it has fired. ``DescribeAlarms`` answers the first,
``DescribeAlarmHistory`` filtered to state transitions answers the second.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

STATE_TRANSITION = 'StateUpdate'
ALARM_STATES = ('OK', 'ALARM', 'INSUFFICIENT_DATA')


@dataclass(frozen=True)
class AlarmSummary:
    """One metric alarm and the comparison it makes.

    Attributes:
        name: Alarm name.
        state: Current state, one of :data:`ALARM_STATES`.
        state_reason: The reason text CloudWatch recorded for the current state.
        state_updated: When the state last changed, or None when never reported.
        description: The alarm's configured description.
        namespace: Metric namespace, or None for a metric-math alarm.
        metric_name: Metric name, or None for a metric-math alarm.
        dimensions: Metric dimensions as name/value pairs.
        statistic: Statistic or extended statistic being compared.
        comparison: Comparison operator, such as ``GreaterThanThreshold``.
        threshold: The threshold compared against, or None for anomaly detection.
        period_seconds: Evaluation period, or None for a metric-math alarm.
        datapoints_to_alarm: Datapoints that must breach, defaulting to the evaluation count.
        evaluation_periods: Periods considered when evaluating.
        actions_enabled: Whether the alarm's actions fire.
    """

    name: str
    state: str
    state_reason: str
    state_updated: datetime | None
    description: str
    namespace: str | None
    metric_name: str | None
    dimensions: tuple[tuple[str, str], ...]
    statistic: str | None
    comparison: str | None
    threshold: float | None
    period_seconds: int | None
    datapoints_to_alarm: int | None
    evaluation_periods: int | None
    actions_enabled: bool


@dataclass(frozen=True)
class AlarmTransition:
    """One state change, from :func:`describe_alarm_history`."""

    alarm_name: str
    moment: datetime
    summary: str


def _to_alarm_summary(alarm: dict[str, Any]) -> AlarmSummary:
    dimensions = tuple((dimension['Name'], dimension['Value']) for dimension in alarm.get('Dimensions', []))
    return AlarmSummary(
        name=alarm['AlarmName'],
        state=alarm.get('StateValue', 'INSUFFICIENT_DATA'),
        state_reason=alarm.get('StateReason', ''),
        state_updated=alarm.get('StateUpdatedTimestamp'),
        description=alarm.get('AlarmDescription', ''),
        namespace=alarm.get('Namespace'),
        metric_name=alarm.get('MetricName'),
        dimensions=dimensions,
        statistic=alarm.get('Statistic') or alarm.get('ExtendedStatistic'),
        comparison=alarm.get('ComparisonOperator'),
        threshold=alarm.get('Threshold'),
        period_seconds=alarm.get('Period'),
        datapoints_to_alarm=alarm.get('DatapointsToAlarm'),
        evaluation_periods=alarm.get('EvaluationPeriods'),
        actions_enabled=bool(alarm.get('ActionsEnabled')),
    )


async def describe_alarms(
    client: Any,
    *,
    name_prefix: str | None = None,
    states: Sequence[str] | None = None,
) -> AsyncIterator[AlarmSummary]:
    """Stream metric alarms, paginating as needed.

    Composite alarms are not returned: they have no metric of their own, so nothing here
    can be dived into.

    Args:
        client: An open CloudWatch client, from :meth:`ClientPool.client`.
        states: Restrict to these states. A prefix and a state filter combine as an AND.
        name_prefix: Restrict to alarms whose name starts with this.

    Yields:
        AlarmSummary records in the order the API returns them.
    """
    kwargs: dict[str, Any] = {'AlarmTypes': ['MetricAlarm']}
    if name_prefix is not None:
        kwargs['AlarmNamePrefix'] = name_prefix
    if states is not None and len(states) == 1:
        kwargs['StateValue'] = states[0]

    wanted = set(states) if states else None
    paginator = client.get_paginator('describe_alarms')
    async for page in paginator.paginate(**kwargs):
        for alarm in page.get('MetricAlarms', []):
            summary = _to_alarm_summary(alarm)
            if wanted is None or summary.state in wanted:
                yield summary


async def describe_alarm_history(
    client: Any,
    alarm_name: str,
    *,
    start_time: datetime,
    end_time: datetime,
) -> AsyncIterator[AlarmTransition]:
    """Stream an alarm's state changes, newest first.

    Only state transitions are returned; configuration updates and action invocations are
    dropped, so the result counts how often the alarm actually fired.

    Yields:
        AlarmTransition records from newest to oldest.
    """
    paginator = client.get_paginator('describe_alarm_history')
    pages = paginator.paginate(
        AlarmName=alarm_name,
        HistoryItemType=STATE_TRANSITION,
        StartDate=start_time,
        EndDate=end_time,
        ScanBy='TimestampDescending',
    )
    async for page in pages:
        for item in page.get('AlarmHistoryItems', []):
            yield AlarmTransition(
                alarm_name=item.get('AlarmName', alarm_name),
                moment=item['Timestamp'],
                summary=item.get('HistorySummary', ''),
            )
