# CloudWatch Log Analysis with VictoriaLogs

A lightweight, zero-code alternative for analyzing CloudWatch logs using VictoriaLogs and Grafana.

## Quick Start

```bash
# Start VictoriaLogs + Grafana
docker compose up -d

# Ingest logs from a CloudWatch log group (last 24h)
./ingest.sh /aws/lambda/my-function

# Open the UI
open http://localhost:9428/select/vmui/   # VictoriaLogs native UI
open http://localhost:3000                 # Grafana (admin/admin)
```

## Prerequisites

- Docker and Docker Compose
- AWS CLI configured with credentials (`aws configure`)
- `jq` installed (`brew install jq` on macOS)

## Usage

### Ingesting Logs

```bash
# Basic: last 24 hours
./ingest.sh /aws/lambda/my-function

# Custom time range: last 48 hours
./ingest.sh /aws/lambda/my-function 48

# With CloudWatch filter pattern
./ingest.sh /aws/lambda/my-function 24 "ERROR"

# Structured filter (CloudWatch JSON syntax)
./ingest.sh /aws/ecs/my-service 168 '{ $.level = "error" }'

# Multiple log groups
./ingest.sh /aws/lambda/service-a 24 &
./ingest.sh /aws/lambda/service-b 24 &
wait
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `VLOGS_URL` | `http://localhost:9428` | VictoriaLogs endpoint |
| `AWS_PROFILE` | (default) | AWS CLI profile to use |
| `AWS_REGION` | (from config) | AWS region for CloudWatch |

## LogsQL Query Examples

Query logs in VMUI (`http://localhost:9428/select/vmui/`) or Grafana Explore.

### Basic Queries

```logsql
# All logs from last hour
_time:1h

# Filter by log group
_time:24h log_group:"/aws/lambda/my-function"

# Search for errors
_time:1h error OR ERROR OR Error

# Exact phrase
_time:1h "connection refused"
```

### Trace Correlation

```logsql
# Find all logs with a specific trace ID
_time:24h trace_id:"abc123-def456"

# Count logs per trace
_time:1h | stats by (trace_id) count() as logs | sort by (logs) desc | limit 20

# Find traces with errors
_time:1h level:"error" | stats by (trace_id) count() as errors | filter errors > 0
```

### Structured Log Analysis

```logsql
# Parse JSON and filter by field
_time:1h | unpack_json | filter level:"error"

# Group by service/function
_time:1h | stats by (log_group) count() as total | sort by (total) desc

# Extract and analyze response times
_time:1h | unpack_json | filter duration:* | stats avg(duration), p99(duration)
```

### Context and Surrounding Logs

```logsql
# Get 5 lines before/after each error
_time:1h error | stream_context before 5 after 5
```

## Architecture

```
CloudWatch Logs
      │
      ▼ (aws logs filter-log-events)
  ingest.sh
      │
      ▼ (HTTP POST /insert/jsonline)
VictoriaLogs ◄──────► VMUI (built-in)
      │
      ▼ (victoriametrics-logs-datasource)
   Grafana
```

## Grafana Setup

Grafana is pre-configured with the VictoriaLogs datasource. To use it:

1. Open http://localhost:3000 (admin/admin)
2. Go to Explore → Select "VictoriaLogs"
3. Enter LogsQL queries

### Optional: Trace-to-Logs Correlation

If you have a Jaeger/Tempo datasource, configure derived fields in the VictoriaLogs datasource settings to link trace IDs.

## Cleanup

```bash
# Stop containers
docker compose down

# Remove all data
docker compose down -v
```

## Comparison with tail-cw

| Aspect | This Demo | tail-cw |
|--------|-----------|---------|
| Lines of code | ~100 (shell) | ~2000 (Python) |
| Dependencies | Docker only | pyarrow, duckdb, polars, textual |
| Query language | LogsQL | CloudWatch filters + SQL |
| UI | VMUI / Grafana | Custom Textual TUI |
| Persistence | VictoriaLogs (7 day retention) | Parquet cache |
| Setup time | ~2 minutes | Python environment |

## Troubleshooting

### No logs appearing

```bash
# Verify VictoriaLogs is running
curl http://localhost:9428/health

# Check ingested data
curl 'http://localhost:9428/select/logsql/query?query=_time:1h' | head
```

### AWS credential errors

```bash
# Verify AWS access
aws sts get-caller-identity
aws logs describe-log-groups --limit 5
```

### Large log volumes

For log groups with millions of events, use CloudWatch filter patterns to pre-filter:

```bash
# Only ingest errors
./ingest.sh /aws/lambda/high-volume 24 "ERROR"
```

## Resources

- [VictoriaLogs Documentation](https://docs.victoriametrics.com/victorialogs/)
- [LogsQL Reference](https://docs.victoriametrics.com/victorialogs/logsql/)
- [LogsQL Examples](https://docs.victoriametrics.com/victorialogs/logsql-examples/)
- [Grafana VictoriaLogs Plugin](https://grafana.com/grafana/plugins/victoriametrics-logs-datasource/)
