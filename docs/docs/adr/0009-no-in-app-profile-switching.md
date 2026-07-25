# ADR 0009: No in-app profile switching

Date: 2026-07-25 Status: Accepted (closes the `:profile` deferral in ADR 0006)

## Problem

ADR 0006 deferred an in-app `:profile` switch and ADR 0008 said the shell was where it would land. Now that the shell exists, the cost is legible, and it is higher than the feature is worth. This record closes the question rather than carrying it forward a third time.

## What a profile switch would actually cost

Changing profile changes the account. Almost every piece of state the shell holds is scoped to an account, so a switch is not a setting change, it is a teardown:

- the log group list, the dashboard list, and every cached group preview belong to the old account and must be dropped
- `session.selected_groups` names groups that may not exist in the new account, so the current log view can become a view of nothing
- the navigation stack and the jumplist hold `NavTarget`s pointing at those groups and dashboards. Either they are invalidated, which throws away the history the jumplist exists to preserve, or they are kept and `Ctrl+O` walks you back into a view that no longer resolves
- in-flight workers (metric fetches, previews, a live tail session) are mid-call against the old account's clients and have to be cancelled and drained before anything new starts
- the Parquet cache is safe, because the profile is already part of the cache key (ADR 0003), so that one piece needs no work

The state that survives is the window and the filter. Everything else is discarded. A switch that discards nearly all state is a relaunch wearing a command's clothes.

## Decision

Do not build it. Profile and region are chosen when the process starts, through `--profile`, `--region`, or the standard AWS environment chain, and they stay fixed for the life of the app.

Two accounts means two `tail-cw` processes, which is what a second terminal or a tmux split already gives you, with the useful property that both stay open and neither invalidates the other's history.

## Options considered

| Option                                                         | Why not                                                                                                                                                                         |
| -------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `:profile <name>` that resets the app in place                 | The reset is the whole feature. It buys one keystroke over relaunching and costs the invalidation logic above, plus a new class of bug where a stale target survives the switch |
| Per-view profile, so one dashboard and one log view can differ | Worse. The breadcrumb stops describing an account, the shared window and filter stop meaning one thing, and dive can cross accounts silently                                    |
| Keep deferring                                                 | Three ADRs would then mention it as pending. Deciding against it is cheaper to read than an open question nobody closes                                                         |

## Consequences

- the shell's `Session` keeps `profile` and `region` as read-only-in-practice fields set from the CLI. Nothing in the TUI writes them
- `ShellServices` closes over one profile and region when it is built in `tail_cw.__main__`, which is why the views never pass credentials around. That stays the shape
- the profile remains part of the cache key, so running two processes against two accounts cannot collide
- if the need ever becomes real, the honest version is a process relaunch (rebuild `ShellServices`, reset `NavState` to a fresh root, drop the preview cache) rather than a mutation, and it should be argued on evidence of daily use, not on symmetry with the other `:` commands
