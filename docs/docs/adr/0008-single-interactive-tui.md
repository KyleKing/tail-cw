# ADR 0008: One interactive TUI instead of four subcommands

Date: 2026-07-24 Status: Accepted (supersedes the entry-point and `--json` decisions in ADR 0002)

## Problem

Today you pick your view from the shell. `tail-cw dashboards` prints names, you copy one into `tail-cw dashboard <name>`, and when a chart looks wrong you quit, run `tail-cw fetch <group>`, and retype the time window the dashboard already knew. `tail-cw tail` is a third process with a fourth set of state. Each subcommand builds a fresh `App`, so nothing carries across: no window, no filter, no focus, no history.

Three things break because of it:

- you cannot see what you are looking at. `dashboard prod-overview` shows a grid with no name in it and no way to reach `api-latency` except quitting
- `dive` (ADR 0005) can only push a log screen inside the dashboard app. The full log surface, live tail, trace view, and search live in a different process
- discovery never happened. M2's group browser was scheduled as a fifth subcommand, which would have made the problem worse

ADR 0002 is not the thing that failed. The Textual-free core stands, and it is the reason this change is a wiring job rather than a rewrite. What failed is one process per view.

## Decision

One `App`. Views are screens inside it, navigation is a stack, and the shell keeps one namespaced command for machine-readable output.

```
tail-cw                     open the TUI at the group browser
tail-cw logs [pattern...]   open the TUI with those groups loaded
tail-cw tail [pattern...]   open the TUI streaming those groups
tail-cw dash [name]         open the TUI on that dashboard
tail-cw export <what> ...   NDJSON to stdout, never a TUI
```

Every `--json` flag goes away. `export logs`, `export tail`, `export groups`, `export dashboards`, and `export dashboard` carry the flags their interactive counterparts had. The agent surface ADR 0002 called load-bearing keeps its exit-code contract (0/1/2) and gains a name that says what it does. This is a breaking CLI change, taken before 1.0 on purpose.

`logs`, `tail`, and `dash` are seeds, not modes. They set the opening view and then get out of the way, so anything reachable from one is reachable from the others. Historical search and live tail are one screen with a toggle rather than two screens, so flipping a search to live keeps its filter, window, and group selection.

## Navigation: k9s stack, vim motions, and a jumplist

Not tabs. Tabs ask you to manage windows. A stack asks you where you want to go and remembers how you got there.

- a breadcrumb in the header is always the answer to "what am I looking at": `dashboards › prod-overview › dive /aws/lambda/api`
- `:` switches views from anywhere, with Tab completion over view names, group names, and dashboard names. The `CommandLine` widget written for the dashboard moves up to the app so every view shares its completion and history
- `Esc` pops one level. `Ctrl+O` and `Ctrl+I` walk a jumplist back and forward, which is what makes dive-and-return cheap: dive into logs, `Ctrl+O` back to the chart, `Ctrl+I` forward into the same log window without refetching
- `[` and `]` move to the previous and next sibling at the current level. In a dashboard they cycle dashboards, in a log view they cycle selected groups
- `g` prefixes a goto (`gd` dashboards, `gl` logs, `gg` top of list). `/` filters the focused list, matching the dashboard's existing `:filter`

The jumplist is the reason a stack beats tabs here. Tabs give you two windows and make you find the right one. A jumplist gives you one window and an undo for movement, which is the same motion vim uses for exactly this problem.

## Log search stays inside a group, and previews tell groups apart

Cross-account Insights search is not the bottleneck. Knowing which of forty `/aws/lambda/*` groups holds the request you want is. So search keeps CloudWatch filter-pattern semantics against one group (or a few, see below) and the browser does the harder job of showing you what is in each group before you commit a query.

The group browser has a preview pane. Selecting a group fetches a small recent window and shows its distinct message shapes with counts:

```
/aws/lambda/api-handler          412 events, last 15m
  318  INFO  request completed status=<n> duration=<n>ms
   71  INFO  cache <str> for key=<str>
   19  ERROR Timeout connecting to <str>:<n>
    4  ERROR Unhandled: KeyError <str>
```

Shapes come from normalizing each message (numbers, UUIDs, hex, quoted strings, and timestamps to placeholders) and counting identical keys. Levenshtein distance was the first instinct and is the wrong tool: it is O(n²) in messages, needs a similarity threshold nobody can pick correctly, and produces clusters that shift as data arrives. Normalization is O(n), gives a stable key you can cache and compare across fetches, and is the same idea behind Drain and CloudWatch Insights' own `pattern` command. If the shapes ever need to be exact rather than indicative, `pattern` runs server-side and this local version is the cheap preview in front of it.

Previews cache under the existing diskcache metadata store keyed by group, window length, and profile, with a short TTL, so walking a list of forty groups costs one small call per group once.

`Space` multi-selects up to ten groups, matching the Live Tail cap so the same selection drives both historical fetch and live tail. Historical multi-group fans out `FilterLogEvents` per group in parallel and merges by timestamp at read time, which keeps one Parquet file per group and leaves the cache key untouched. Ten is a cap rather than a target. Past that, the answer is an Insights query, and M3 can add it.

## Dive resolves candidates and asks

`resolve_log_group_for_widget` currently returns one group or nothing, and only for Lambda `FunctionName` metrics and Insights `SOURCE` clauses. It becomes a ranked candidate list from three signals:

1. dimensions mapped to conventional group names (`FunctionName` to `/aws/lambda/x`, `ClusterName` and `ServiceName` to `/ecs/x`, `ApiId` to `/aws/apigateway/x`, `DBInstanceIdentifier`, `LoadBalancer`)
1. `SOURCE '...'` in a log widget's query, which is exact and always ranks first
1. of the candidates that exist, which ones actually had events in the widget's window

Only two of those mappings are AWS defaults: `/aws/lambda/{name}`, and `/aws/rds/instance/{id}/postgresql`. API Gateway's real default carries a stage segment (`/aws/apigateway/{id}/{stage}` for WebSocket, `API-Gateway-Execution-Logs_{id}/{stage}` for REST, nothing at all for HTTP APIs), and ECS and ELB have no default whatsoever because the destination is whatever the user configured. So the ECS and ELB candidates are conventions, not facts, and the API Gateway one is a prefix that will often miss.

That is the argument for signal 3 rather than a caveat about it. A guessed name either exists in the account or it does not, and `DescribeLogGroups` settles it before the user ever sees a query. Guesses that miss stay in the list marked as not found, with the dimension that produced them named, because "tail-cw looked for `/ecs/web/api` and your account has no such group" is more useful than silence.

Signal 3 is the honest answer to whether guessing is possible: a name that maps cleanly but was silent during the spike is the wrong group, and a `DescribeLogGroups` call plus one small `FilterLogEvents` per candidate settles it for a few cents of API calls. Dive then shows the ranked list with event counts and waits for `Enter`. Confirming rather than jumping is deliberate: a wrong guess costs a fetch and a retype, and the list doubles as the explanation of why tail-cw thinks these groups are related. One candidate with a `SOURCE` clause still shows the confirmation, pre-selected, so the keystroke is uniform.

The widget's own filter or query text carries into the log view as the starting filter, which it does not today.

## Options considered

| Option                                                       | Why not                                                                                                               |
| ------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------- |
| Keep the subcommands, add a `start` that launches a launcher | Five entry points instead of four, and the launcher owns none of the state that needs to persist                      |
| Tabs, one per open dashboard or search                       | Window management the user has to do. A jumplist solves back-and-forth without it, and `[`/`]` covers switching       |
| Cross-group Insights search as the default                   | Async, priced per GB scanned, and no live tail. Solves a problem that ranks below telling groups apart                |
| Dive straight into the best guess                            | Cheap when right, and when wrong it hides the reasoning and costs a fetch. The candidate list is also the explanation |

## Consequences

- `LogTailApp` and `DashboardApp` stop being `App` subclasses and become screens under one shell. Their bindings, workers, and tests mostly survive. What moves is ownership of the command line, the header, and the quit binding
- state that was per-process becomes app state: time window, filter, profile, selected groups, current dashboard. A window set in a dashboard is the window a dive inherits
- an in-app profile switch looks reachable now that one app holds the state, and [ADR 0009](./0009-no-in-app-profile-switching.md) decides against it: nearly everything the shell holds is account-scoped, so a switch is a teardown rather than a setting change
- M2's group browser ships as this ADR's home view rather than as `tail-cw groups`, and M2's pattern resolution layer becomes a prerequisite rather than a follow-on
- three new pure modules carry the logic that needs testing without a terminal: pattern normalization, the navigation stack and jumplist, and dive candidate ranking. The shell itself stays thin enough to test with Pilot
- ADR 0007 is unaffected. Nothing here builds tracing, and a smaller CLI surface makes the hybrid option easier, not harder, because the terminal path gets sharper while heavy historical analysis stays out of scope
