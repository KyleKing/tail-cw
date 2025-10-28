"""AWS CloudWatch Logs integration.

This module provides functions for downloading logs from AWS CloudWatch Logs
using boto3. It handles pagination, time range filtering, CloudWatch filter
patterns, and AWS credential chain resolution (profiles, environment variables,
IAM roles).
"""
