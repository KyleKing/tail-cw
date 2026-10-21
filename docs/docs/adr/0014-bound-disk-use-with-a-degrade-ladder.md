# ADR 0014: Bound disk use with a degrade ladder

Date: 2026-10-21
Status: Accepted

## Problem

The cache had no floor beneath it: it would keep writing Parquet until the volume was
full, which breaks tools that have nothing to do with tail-cw.
Its bounds were also blind to use — a segment read every day expired on the write's
clock, and an oversized fetch could silently fill the cache with bytes nobody asked
twice for.

## Decision

Every segment write walks a fixed ladder, and the rung that ran is named on stderr:

1. Fits: fetch, write, and persist.
1. Over the cap: evict the least-recently-read settled segments, skipping anything read
    in the last 24 hours and anything inflight, then continue.
1. Still over, or under the free-space floor: sparse mode.
    The segment is written to a
    staging file inside the cache directory, queried, and deleted when the command ends,
    so the answer is correct and nothing persists.
1. Anything that cannot fit even sparse exits 3, printing the estimate and the flag that
    raises the ceiling, because silently returning less would be wrong.

Supporting mechanisms:

- `[cache].min_free_mb` (default 5120) is the free-space floor.
- Staging lives in `staging/` inside the cache directory, so it counts against the same
    `size_limit_mb` the persisted files do and a full system temp cannot take a fetch
    down with it.
- `--max-disk MB` bounds what one command may write, persisted and staging together.
- `--cache-ttl` (and `TAIL_CW_CACHE_TTL`) expires settled entries from their last read,
    not their write: `0` bypasses persistence, `never` disables expiry.
- `[cache].max_group_mb` caps one log group's share; `[cache].max_segment_mb` answers an
    oversized segment sparse rather than storing it.

## Why a ladder, not a cap that fails

A cap that refuses forces the user to retry narrower; a ladder answers the question
first and degrades persistence, which is the part that is optional.
The answer never changes under degradation, only whether the next one is faster, so the
stderr line naming the rung is the contract a user or an agent can react to.

## Alternatives considered

- Route oversized aggregate commands (`summary`, `stats`) to Logs Insights and cache
    only the aggregate result.
    Deferred: it changes what is computed server-side and
    needs the read numbers from M13 to justify.
- Compact settled 5-minute segments into hourly files.
    Deferred for the same reason;
    the read log now records whether rereads justify it.
