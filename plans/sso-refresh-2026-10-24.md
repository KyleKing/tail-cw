# SSO token refresh failure on `read-prod` (2026-10-24)

Status: open, not reproduced as a tail-cw-only failure yet.

## What happened

During an IRM on-call sweep, an agent reported that tail-cw failed on
`--profile read-prod` while
the `aws` CLI kept working, so it fell back to `aws logs` and `aws s3`.
Its exact error text was not
captured.

At 22:24Z the same day, both fail:

```text
$ uv run --project ~/Developer/kyleking/tail-cw tail-cw export groups '/aws/ecs/irm-prod*' --profile read-prod
  File ".../aiobotocore/tokens.py", line 91, in _protected_refresh
    raise TokenRetrievalError(
botocore.exceptions.TokenRetrievalError: Error when retrieving token from sso: Token has expired and refresh failed
Error: Error when retrieving token from sso: Token has expired and refresh failed

$ aws sts get-caller-identity --profile read-prod
aws: [ERROR]: Error when retrieving token from sso: Token has expired and refresh failed
```

Context:

- `read-prod` uses `sso_session = Coverbase` (refreshable SSO token, `sso:account:access`)
- The SSO cache entry for that start URL had `expiresAt` 21:49:58Z with a refresh token
    present.
    Its client registration expires 2026-12-01
- Versions: aws-cli 2.37.13, botocore 1.43.46, aiobotocore 3.9.0

## Open questions

1. Earlier in the session, did aiobotocore's refresh fail while aws-cli's succeeded on the
    same
    cache entry?
    To check: next time the access token is near expiry with a valid refresh token, run
    tail-cw first, then `aws sts get-caller-identity`, and compare.
    If aws-cli refreshes and writes the
    cache while aiobotocore cannot, compare `aiobotocore/tokens.py` with botocore's
    `SSOTokenProvider` refresh, and check whether tail-cw's session reads the refreshed
    cache file or
    holds a stale frozen token for the life of the process
1. Small fix in reach either way: tail-cw prints a full traceback for an expired SSO
    session.
    It
    should print one line naming the profile and the fix
    (`aws sso login --sso-session Coverbase`)
    and exit non-zero
