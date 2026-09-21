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
- **`experiments/suites/stratified.json` pins this repository's own source**, so any `src/` edit breaks
  `test_stratified`. Repin only after checking every gold self-grades and every evidence anchor still
  resolves — the guard exists to force that review.
- **A three-repetition cell can swing 1/3 to 3/3** on identical binary, seed and questions. One cell
  plus a plausible mechanism is not a finding; re-run the exact configuration. Two repetitions are
  not enough either: `runs/instructions-ab-rerun-20260916` turned a two-repetition 78/78-against-75/78
  handshake win into 115/115/115 on the third, and the token delta changed sign, +3.2% to −1.8%.
- **Build an arm somewhere that survives a reboot.** The first `instructions-ab` treatment binaries
  lived in `/tmp`, the source edit was reverted after building, and the physical layout of one
  string literal is unrecoverable — so the pinned hashes could never be reproduced and all three
  repetitions had to be re-run. Arms now live in the run directory's own `bin/`, re-hashed after
  copying.
- **No measured corpus contains a single markdown file.** Any claim about markdown is unmeasurable
  on this evidence base, whatever the code does.
- **Caller golds exclude the helper's own defining module** (`audit_failures.true_callers`). A
  question that does not say so gets answered with in-module test callers included, and every arm
  loses credit for being right. That was `cu-callers-02`.
- **State a caller question's scope as a file, never as a "module".** `cu-callers-02`'s repair
  said "outside the parser's own module" while its gold counted a caller in the defining file's own
  directory, and in `runs/cu-callers-02-confirm-20260916` all three retrieval trials found that
  caller, excluded it by quoting the phrase back, and scored 0.667 against the native arm's 1.000 —
  a replicated false reading that the structural surface is worse at caller questions. A repaired
  question can carry the same defect one scope level up; `validate_suite.py` now refuses that
  wording. The second restatement names the defining *file* and says neighbours count, and it
  passed 6 of 6 in `runs/cu-callers-02-confirm-2-20260916`, so the question is back in the bucket.
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

- **A lost cell is not automatically a retrieval failure.** Three classes produce one: an
  evaluator defect, a server defect, and an arm that held the right rows and answered past them.
  Separate them before interpreting — `experiments/README.md` keeps the model-side incidents with
  the evidence that the rows were correct, and the ledger keeps the other two. In this project the
  first class has produced thirty entries, the second one, and the third five.

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
| `check_docs.py` | Doc, test-count and link consistency, plus the README shape contract: `README_SECTIONS` in order, the install line budget, no duplicated fenced block, one registration block, a contents list naming every section, a quickstart sample compared field by field against a live call, and every figure in the result section compared against `published_results.json`. Run before every docs commit. |
| `publish_numbers.py` | Derives `experiments/published_results.json` from the archived reports, so CI can check a published number without seeing `runs/`. Re-run it whenever a published study is added or re-graded; a percentage that is not a run comparison must be declared in it. |

`runs/` and `corpora/` are gitignored: the repository publishes the record, not the bytes.

## Verify before committing

```sh
cargo test --locked --all-targets
cargo clippy --locked --all-targets -- -D warnings
python3 -W error::ResourceWarning -m unittest discover -s experiments -p 'test_*.py'
python3 experiments/check_docs.py
```

## Where the project stands

Released `v0.1.6`: the routing filter, the Go qualified-call fix, optional `--root`, strict
`search_concept` arguments and the README shape contract. What shipped in each release is in
`CHANGELOG.md`. The default surface is four tools — `search_exact`, `read_source`, `find_callers`,
`search_concept` — with the lexical ranker; the other three stay un-defaulted on replicated evidence.

The claim the evidence supports is **quality ties, input tokens 34–51% cheaper, on corpora of a
few hundred files**: the held-out Django suite (30 mixed-shape questions, 29/29/28, −33.5% against
native) and an etcd client corpus (30 mixed-shape questions, three repetitions, 270 trials, 87/89/89,
−44.6% against native and −29.2% against zvec-grep). Quality has never separated in either
direction across 468 scored trials at that scale; the token gap has never failed to replicate
there. Two studies missed their registered quality criterion by a single answer and both are
published as misses. The handshake instructions are also settled: two candidate sentences were
measured over 351 trials in `runs/instructions-ab-rerun-20260916` and both came in at +0 against a
registered +3 bar, so the prompt side of this server has nothing left that measurement supports
changing.

**The size clause is new, it was paid for, and most of it turned out to be a defect.** `runs/linux-agent-20260919` put the same two arms
on Linux 6.12 — 86,602 files, 21 questions, 126 trials — and missed all three registered criteria:
56 resolved against native's 62, **+16.4% input tokens** where the bar was −20%, and seven answers
whose gold identity no payload contained. The mechanism is measured, not guessed: attaching the
four-tool surface costs zero prompt tokens (stub-server probe, 13,802 either way), only ~9% of what
ripgrep prints locally reaches the model against ~4× re-sending for an MCP payload, and a native
shell call chains a mean of 1.95 sub-commands while an MCP call answers one question per round
trip. At a few hundred files grep is a poor summary; at kernel scale a pipeline is a good one.

Then the identified experiment. `runs/scale-response-20260920` cuts four nested corpora from the
same tree - 928, 5,637, 18,652, 86,605 files, every gold in the smallest - and holds retrieval
behaviour fixed by replaying the archived run's own calls. The payload is scale-invariant at
73-75 KB; recall is not, falling 14, 9, 8, **4** of 21. It was never the page size, and 17 of the
21 losses were the gold file never entering the 400-file candidate set. `files_about` scored a
file by how many distinct query words it wrote, and 56,000 of 60,000 Linux source files write at
least one, so ties were broken by path order. Scoring by rarity - `ln(1 + eligible/df)`, document
frequency accumulated in the walk already running - plus a budget that stops sampling long files
and a word boundary that can see inside `quiesce_bucket` takes the kernel to **12 of 21** and
flattens the ladder to 15, 13, 13, 12, with Django and etcd unmoved.

`runs/linux-scanfix-20260920` re-ran the kernel plan with that one variable: **58 of 63 against
56, +8.3% input tokens against native where it was +16.4%, unevidenced answers 7 to 4.** Every
question the registration predicted would recover did, on the first attempt; the paired token test
and the safety bar were both missed and are published as misses. It also exposed the next failure:
rarity ranks the site where a rare word literally appears, which for a wrapper is the callee it
calls, so the agent asked `find_callers` about `tls_alert_send` rather than the
`tls_handshake_close` that the question described. All four lost trials were caller questions.

Everything measured lives under `runs/`, which is gitignored and therefore local: each study
directory holds its `preregistration.json`, per-repetition reports, and a note for any chunk that
was stopped or failed. `experiments/README.md` carries the prose record for anyone without those
bytes, and the corpora are re-cuttable from `corpora/` against the fingerprints in each
`corpus-manifest.json`.

Open threads:

- **The kernel deficit is round trips, not bytes, and it is not closed.** Three studies:
  +16.4%, +8.3%, +10.1% input tokens against a shell agent, with quality level at 56, 58, 58
  against 62, 61, 61. `runs/token-metric-20260921` prices the axis - a dependent hop is ~15,000
  tokens, a 20 KB payload ~2,400 - so payload efficiency, where this server wins 40x, is the
  wrong lever. `runs/linux-complete-20260921` removed a hop's worth of calls (3.20 to 2.50) and
  still lost ground, because native drifted 8.6% between two runs of an identical configuration.
  Any future token claim on this client must be same-day and within-run.
- **Wrapper against callee is the next failure, and it is measured.** Rarity-weighted selection
  ranks the site where a rare word appears, which for a thin wrapper is the function it calls.
  In `runs/linux-scanfix-20260920` that cost all four lost trials, every one a caller question:
  three trials asked `find_callers` about `tls_alert_send` where the question described
  `tls_handshake_close`, its caller. This is a ranking problem inside the candidate set, not a
  selection problem, and nothing in the record says yet whether it is best fixed by scoring, by
  the tool description, or by a disambiguation step the agent runs itself.
- **`runs/etcd-caller-20260915` is built, gated and deliberately unrun**: 39 questions, 27
  attribution-hard and 12 local controls, enriched 2.4× over its corpus's own 38% base rate. It
  answers "does attribution change answers, or only cost", and that question has not been worth
  $16.
- **No agent-level claim for the client-root work.** It is covered by an offline differential (28
  tool payloads identical across pinned, launch-directory and client-roots resolution), six stdio
  tests and one live Codex session. The benchmark pins `--root`, so the new path was never exercised
  under a model.
- **`cu-callers-01` does not compile and blocks both coreutils suites.**
  `src/uu/head/src/head.rs` defines `print_n_bytes` twice under opposing `#[cfg]` gates, so
  `path::name` attribution cannot say which variant calls `send_n_bytes`. `validate_suite.py` has
  refused it since the namesake guard landed. Repairing it means re-authoring the question or
  dropping it, which changes the suite's bucket counts, so it is a decision rather than a fix.

## The line worth remembering

**Correct evidence reduces recovery turns.** The largest efficiency win measured after the freeze
came from a row that started telling the truth about a call site — not from a smaller payload, a
richer result, or more retrieval machinery.
