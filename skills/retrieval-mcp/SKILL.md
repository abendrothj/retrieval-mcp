---
name: retrieval-mcp
description: "Use when locating code in a repository: who calls a function, where a symbol or string lives, how a behavior is implemented, what a file contains, or which paths a change affects. Triggers on: find callers of, where is X defined, how does X work, explore the codebase, trace this behavior, which files handle, search the repo, read that source."
---

# retrieval-mcp

Four MCP tools from the `retrieval-mcp` server (registered as `retrieval` in
client configuration): `search_exact`, `search_concept`, `find_callers`,
`read_source`. They are cheaper and more precise than reading files at random,
and they are the first thing to reach for on any "where is this / who uses
this / how does this work" question.

Only these four tools exist. If a plan calls for a call graph, dead-code
sweep, impact analysis, or a query language, that capability is not available
here — compose it from these four and say what remains unverified.

## Route the question

| Question | Tool |
|---|---|
| Known identifier, string, error text, filename, syntax pattern | `search_exact` |
| Exhaustive list of occurrences of a literal | `search_exact` with pagination |
| Behavior or intent, exact name unknown | `search_concept` |
| Who calls or references this symbol | `find_callers` |
| Read the implementation, or verify any claim | `read_source` |

Discovery usually runs `search_concept` → `find_callers` → `read_source`. A
known name skips straight to `search_exact` or `find_callers`.

## search_exact

Literal by default; pass `regex: true` for a ripgrep pattern (`|` is
alternation, `\|` a literal pipe). `path` narrows to a file or directory,
`case_sensitive` tightens, `limit` is 1–100 (default 20).

Do not use it to answer "who calls X" or "how is X implemented" — it cannot
distinguish a call from a comment, and a name-shaped grep is not a caller
list. Use it for identifiers, literals, and exhaustive occurrence counts.

When the result reports `has_more`, either pass `next_offset` until the list
is exhausted or narrow `path`/`query`. An exhaustive claim requires the full
pagination, not the first page.

## search_concept

Phrase the query the way the *code* reads, not the way the question reads:
likely identifiers, API terms, constants, and implementation nouns, with
several spellings. "worktree lock owner pid stale release" beats "how does it
recover from a crash".

Rows name the enclosing definition with caller and callee counts, which is
usually enough to pick the next step. Request `fields: ["excerpt"]` only when
you actually need source text — otherwise keep rows identity-sized.

## find_callers

Takes an unqualified symbol `name`. `include_references: true` widens from
call sites to identifier references. `path` restricts where the *callers*
live, not where the definition is.

Results are conservative syntactic candidates, not proven bindings and not a
complete call graph. Shadowed names, dynamic dispatch, re-exports, and
cross-language boundaries produce both false positives and misses. Confirm
anything material with `read_source`, and never present the output as a
closed set.

## read_source

Repository-relative path only — absolute paths and symlinks are rejected.
Inclusive 1-based `start_line`/`end_line`, 100 lines by default, 500 maximum.
Follow `next_line` to continue rather than re-reading from the top.

Every claim that matters — a signature, a branch condition, an invariant, a
default — gets confirmed here before it is stated.

## Evidence discipline

- Say which tool produced each claim, and quote the path and line range for
  anything load-bearing.
- Positive findings are cheap; **negative and exhaustive claims are not**.
  "There are no other callers", "this is dead code", and "nothing else reads
  this flag" require exhausted pagination across the relevant scope, and they
  are still bounded by `find_callers` being syntactic. State that bound.
- Prefer one more targeted call over a guess. Prefer a narrower `path` over a
  bigger `limit`.
- When delegating to a subagent that may lack MCP access, pass the concrete
  evidence — paths, line ranges, symbol names, what was already searched and
  what remains open — not just the conclusion.
