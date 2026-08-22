# Filter syntax

One filter language runs over live, historical, and cached events. Most of it is
CloudWatch's own pattern syntax, so a filter you already know keeps working. The boolean
operators and the `field:value` shorthand are additions, and they run locally.

## Terms

| Filter                     | Matches                                                |
| -------------------------- | ------------------------------------------------------ |
| `ERROR`                    | Any event whose text contains `ERROR`. Case sensitive. |
| `"connection timeout"`      | That exact phrase, spaces included.                    |
| `%[Ee]rror%`                | A regex. The delimiter is `%`, not `/`.                |
| `level:error`               | A record field equal to a value.                       |
| `status:>=500`              | A numeric comparison on a field.                       |
| `user.id:*`                 | A field that exists, whatever its value.               |
| `{ $.level = "ERROR" }`     | CloudWatch's own JSON filter syntax.                   |
| `{ $.status >= 500 }`       | The same, numerically.                                 |

`level:error` and `{ $.level = "ERROR" }` mean the same thing. The short form exists
because it is what you type; the long form exists because it is what CloudWatch documents.

## Combining terms

| Filter                          | Means                                        |
| ------------------------------- | -------------------------------------------- |
| `ERROR ARGUMENTS`               | Both, because a space means `AND`.           |
| `ERROR AND ARGUMENTS`           | The same thing, said out loud.               |
| `ERROR OR WARNING`              | Either.                                      |
| `NOT level:debug`               | Every event except those.                    |
| `level:error AND NOT status:404` | Errors that are not 404s.                    |
| `a AND (b OR c)`                | `AND` binds tighter than `OR`, so group it.  |

Operators are uppercase. A log line that says "timed out or retried" is a text search for
four words, and making `or` an operator would quietly change what that search means.

Parentheses are grouping. To search for a literal parenthesis, quote it: `"(retry)"`.

## What can be sent to CloudWatch, and what cannot

A historical fetch always retrieves the whole window and filters locally, so every filter
above works on cached and historical events. The filter is not part of the cache key,
which is why one cached window serves every filter you ask of it.

Live tail is different: CloudWatch applies the filter, so the filter has to be something
CloudWatch can mean exactly. These translate:

| Yours                            | Sent as                             |
| -------------------------------- | ----------------------------------- |
| `ERROR`                          | `ERROR`                             |
| `ERROR ARGUMENTS`                | `ERROR ARGUMENTS`                   |
| `ERROR OR WARNING`               | `?ERROR ?WARNING`                   |
| `ERROR AND NOT ARGUMENTS`        | `ERROR -ARGUMENTS`                  |
| `level:error`                    | `{ $.level = "error" }`             |
| `level:error AND status:>=500`   | `{ ($.level = "error") && ($.status >= 500) }` |

And these are refused, by name, rather than sent:

- an `OR` mixing text with fields, such as `ERROR OR level:debug`
- an `AND` mixing text with fields, such as `ERROR AND level:debug`
- a `NOT` of a text term on its own, such as `NOT ERROR`

The refusals are not caution. CloudWatch documents that its `?` any-of terms are
**ignored** when combined with anything else, rather than rejected, so a mixed pattern
comes back with the wrong events and no error at all. Refusing is the only way to avoid
answering wrongly. The same filter still works on the historical view, where it runs
locally.

## When a search finds nothing

Two filters parse cleanly and can never match, so the status line suggests the fix:

- `level=info` is a text search, because the field operator is `:`. The table renders
  records as `key=value`, which is why this one is easy to type by accident.
- `/error/` is a text search for those slashes. Regex is delimited by `%`.

## Errors

A malformed filter names what to do about it:

```
Unclosed ( in filter. Add the matching )
Unbalanced quote in filter. Close the phrase with a second "
Mismatched braces in filter. A JSON filter looks like { $.level = "ERROR" }
Filter ends after an operator. Every AND, OR, and NOT needs a term after it
```

## Where filters come from

The `--filter` flag on `export logs`, `export tail`, and `export summary`; the `/` search
box in the log view; and `:filter <expression>` in the shell, which sets the filter every
view shares until you clear it with a bare `:filter`.
