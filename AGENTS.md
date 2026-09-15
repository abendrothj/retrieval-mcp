# Working notes for agents

This repository is a retrieval MCP server **and** the experiment that chose its shape. The research
record is as much the product as the binary, so the rules below are not style preferences: they are
what keeps the published numbers true.

## The law

1. **A change ships only with measurement behind it**, whenever it could logically affect a
   measurement. Plausible accounting is not evidence: four post-freeze optimisations had convincing
   arithmetic and all four were rejected by their own runs.
2. **Pre-register before running.** Claim, arms, corpus fingerprints, binary hashes, endpoints, pass
   criteria and decision rule go into a `preregistration.json` beside the run, written before the
   first trial. Criteria missed means the negative result is what gets published.
3. **Every surprising result triggers an evaluator audit before an architectural interpretation.**
   Twenty-three harness defects were found that way; several had already produced believable findings.
4. **A defect is not a feature.** A wrong output — a caller row naming a local `const` — is fixed on
   sight with a regression test. A capability is measured first.
5. **One variable per arm.** Systems files are asserted to differ only in `id` and `server`; a
   bundled change cannot be attributed and has to be re-run isolated.
6. **Model spend is gated.** `--allow-model-usage` costs real money. Ask first, record the cost.

## Traps that have already cost a day each

- **`tools/list` bytes are not model-facing tokens.** Claude Code does not forward `outputSchema` to
  the API. Measure the prompt prefix (`cache_creation_input_tokens`), not the serialised tool list.
- **"Input tokens" here means `input + cache_read + cache_creation`.** Raw `input_tokens` is ~5 per
  trial; everything else is cached prefix plus per-turn re-reads.
- **`experiments/stratified.json` pins this repository's own source**, so any `src/` edit breaks
  `test_stratified`. Repin only after checking every gold self-grades and every evidence anchor still
  resolves — the guard exists to force that review.
- **A three-repetition cell can swing 1/3 to 3/3** on identical binary, seed and questions. One cell
  plus a plausible mechanism is not a finding; re-run the exact configuration.
- **No measured corpus contains a single markdown file.** Any claim about markdown is unmeasurable
  on this evidence base, whatever the code does.
- **Caller golds exclude the helper's own defining module** (`audit_failures.true_callers`). A
  question that does not say so gets answered with in-module test callers included, and every arm
  loses credit for being right. That was `cu-callers-02`.
- **`acceptable_symbols` is forbidden** in question sets; the frozen grader cannot score any-of
  answers and `validate_suite.py` rejects it.
- **Graders judge identities, not containers.** Both directions have been defects — a keyed object
  against a list gold, and a bare list against a single-key gold. `quality_pass.credit` handles both,
  and `validate_suite.py` now compiles no suite whose gold identities fail in a different container.
- **A gold can only name what the grader can tell apart.** One Go file defines four methods called
  `GetRequestMetadata` on four receivers; the attribution is `path::name`, so the caller set names
  that identity once and an answer enumerating all four is scored as extras. Three arms lost credit
  for being exactly right. `validate_suite.py` now refuses any caller question whose verified
  callers collapse that way, so this cannot reach a run again.

## The instruments (offline unless noted)

| Script | What it answers |
|---|---|
| `validate_suite.py` | Is this question set usable? Gold, anchors and grader proven against the corpus. Nonzero exit means the suite does not exist. |
| `cut_corpora.py` | Cut a scoped corpus from `corpora/`, disjoint from spent suites, with a fingerprint. |
| `study_a.py` / `study_b.py` | Offline ranking. `study_b` takes several arms (`mcp:name=bin`, `zvec:name=zg`) and scores them through one enclosing-definition attribution, so the comparison is symmetric. |
| `lexical_oracle.py` | Does a bounded lexical crawl dissolve this question? If so it proves nothing about structural tools. |
| `language_audit.py` | Does the index tell the truth about one language? Samples symbols from a corpus, compares `find_symbol` and `find_callers` against an independent ripgrep reading, and cross-checks that reading against universal-ctags where it reports definition line ranges (C, C++, Go, Java, Python — never Rust or JS/TS). |
| `comparison_runner.py` | `prepare` (no model) then `run` (model, gated). A per-system `server` field pins a binary per arm and records its sha256. |
| `end_to_end.py` | Scores an archived run: quality, tokens, calls, calls-to-first-evidence, `context_token_turns`, safety columns, per bucket. |
| `regrade.py` | Applies a repaired grader to an archived run symmetrically and records what moved. |
| `check_docs.py` | Doc, test-count and link consistency. Run before every docs commit. |

`runs/` and `corpora/` are gitignored: the repository publishes the record, not the bytes.

## Verify before committing

```sh
cargo test --locked --all-targets
cargo clippy --locked --all-targets -- -D warnings
python3 -W error::ResourceWarning -m unittest discover -s experiments -p 'test_*.py'
python3 experiments/check_docs.py
```

## Where the project stands

Released `v0.1.5`; what shipped in each release is in `CHANGELOG.md`. The default surface is four tools —
`search_exact`, `read_source`, `find_callers`, `search_concept` — with the lexical ranker; the
other three are un-defaulted on replicated evidence. Shipped after the freeze: the TypeScript
caller-attribution guard (isolated: source reads −74%, calls −30%, total input −39.5%, worst trial
−81%, quality unchanged), doc comments in concept chunks (offline MRR 0.126 → 0.234, no agent-level
gain claimed), and five more languages — Go, Java, C, C++ and JavaScript — admitted only after a
pre-registered audit against an independent reading of Cobra, Gson, Redis, LevelDB and ESLint
(99/100 definitions, caller precision 0.92–1.00, recall 0.93–1.00). Rejected on evidence: markdown
chunking, macro-argument callers, the schema diet, container fields, and dropping call rows from
parser-error regions.

What the new languages cost, measured on an M4 Pro: the release binary grows 11.3 → 15.9 MB, a
clean release build 37 → 47 s, and a cold Django snapshot 3.9 → 5.9 s — the last because 112
JavaScript files are now indexed, of which four minified vendor bundles account for about 0.7 s.
No model has run on any of the five: there is no agent-level claim for them, and making one needs
a pre-registered suite and `--allow-model-usage`.

Three open threads, none urgent: confirm the repaired `cu-callers-02` wording (6 trials), build
a caller suite around the `vs-callers-enter-rule-resolution` ambiguity, whose instability (3/3, 3/3,
1/3, 0/3, 3/3, 3/3) is the only genuine decision boundary the current questions expose, and decide
whether `v0.1.3` ships before or after an end-to-end run in one of the new languages.

## The line worth remembering

**Correct evidence reduces recovery turns.** The largest efficiency win measured after the freeze
came from a row that started telling the truth about a call site — not from a smaller payload, a
richer result, or more retrieval machinery.
