#!/usr/bin/env bash
set -euo pipefail

# CloudWatch → VictoriaLogs ingestion script
#
# Usage:
#   ./ingest.sh <log-group> [hours-back] [filter-pattern]
#
# Examples:
#   ./ingest.sh /aws/lambda/my-function
#   ./ingest.sh /aws/lambda/my-function 48
#   ./ingest.sh /aws/lambda/my-function 24 "ERROR"
#   ./ingest.sh /aws/ecs/my-service 168 '{ $.level = "error" }'

LOG_GROUP="${1:?Usage: $0 <log-group> [hours-back] [filter-pattern]}"
HOURS_BACK="${2:-24}"
FILTER_PATTERN="${3:-}"
VLOGS_URL="${VLOGS_URL:-http://localhost:9428}"

START_TIME=$(( ($(date +%s) - HOURS_BACK * 3600) * 1000 ))

echo "Fetching logs from: $LOG_GROUP"
echo "Time range: last ${HOURS_BACK}h (since $(date -r $((START_TIME / 1000))))"
echo "Target: $VLOGS_URL"
[ -n "$FILTER_PATTERN" ] && echo "Filter: $FILTER_PATTERN"
echo ""

build_aws_cmd() {
    local cmd="aws logs filter-log-events"
    cmd+=" --log-group-name \"$LOG_GROUP\""
    cmd+=" --start-time $START_TIME"
    cmd+=" --output json"
    [ -n "$FILTER_PATTERN" ] && cmd+=" --filter-pattern \"$FILTER_PATTERN\""
    echo "$cmd"
}

AWS_CMD=$(build_aws_cmd)

# Fetch and transform logs, then POST to VictoriaLogs
# The jq transform:
#   - Converts CloudWatch timestamp (ms) to ISO8601
#   - Preserves log_group and log_stream for filtering
#   - Attempts to parse JSON messages inline
eval "$AWS_CMD" | jq -c '
  .events[] |
  {
    _msg: .message,
    _time: (.timestamp / 1000 | strftime("%Y-%m-%dT%H:%M:%SZ")),
    log_group: "'"$LOG_GROUP"'",
    log_stream: .logStreamName
  } +
  (try (.message | fromjson) catch {})
' | {
    # Count lines while passing through
    count=0
    while IFS= read -r line; do
        echo "$line"
        ((count++))
    done
    echo "Processed $count log events" >&2
} | curl -s -X POST \
    "${VLOGS_URL}/insert/jsonline?_stream_fields=log_group,log_stream&_msg_field=_msg&_time_field=_time" \
    -H 'Content-Type: application/stream+json' \
    --data-binary @-

echo ""
echo "Done. Query logs at: ${VLOGS_URL}/select/vmui/"
echo "  Example: _time:${HOURS_BACK}h log_group:\"$LOG_GROUP\""
