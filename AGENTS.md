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
7. **Author the instrument outside the loop.** A question set written by an agent session in this
   repository is written with this file and the operator's `~/AGENTS.md` — a routing guide for
   these four tools — already in its context. Golds survive that (the oracles are ripgrep and
   ctags), but which questions exist and how they are worded do not. Authoring sessions get the
   trial's own isolation: `--setting-sources "" --settings '{"autoMemoryEnabled": false,
   "claudeMdExcludes": ["/**"]}'` for Claude, `-c project_doc_max_bytes=0` for Codex, and no
   knowledge of which arm is expected to win. Every suite in `experiments/suites/` predates this
   rule.

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

- **The trials run inside this repository, so the client handed the agent this file.** Corpus
  copies live under `runs/`, inside the git tree, and Codex loads the nearest `AGENTS.md` up to
  the repository root into its first user message — the claim, the arms, the expected direction,
  and for one kernel question the answer. `--ignore-user-config` does not cover it and the
  shell-command detector cannot see it. Every Codex study in the record carries 1.5-4.9k tokens of
  it in *both* arms; the Claude held-out study does not, because that launch already passed
  `claudeMdExcludes`. The launch now passes `-c project_doc_max_bytes=0` (73,338 request bytes
  against 53,384, measured against a local sink) and `session_instructions()` flags a rollout that
  carries one anyway. Writing here is writing into the next Codex run's prompt until that run is
  re-measured under the flag. The operator's `~/AGENTS.md` is a different document and a worse
  one — it routes these four tools by name — but it never reached a published trial: Codex stops
  at the git root and every archived Claude launch passes `claudeMdExcludes`, both verified
  against a request sink. The OpenCode wrapper was passing the real `HOME` through and now does
  not.

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
| `cli_announcement.py` | What the CLI transport arm's prompt fragment says, generated from the binaries - the CLI's own `--help`, the server's own handshake instructions and tool descriptions, tool names rewritten as subcommands - so no one authors the text that tells an arm its tools exist. Prices it against what the protocol hands the MCP arm. |
| `prompt_prefix.py` | **Costs model calls.** What a condition pays before its first tool call, one trivial prompt, consecutive runs per condition, never interleaved - because alternating two configurations measures the cache and not the prefix. Refuses a block that has not converged, and proves an MCP condition actually attached before reporting what it cost. |
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

The claim the evidence supports is **quality ties, input tokens 33–42% cheaper, on corpora of a
few hundred files**: the held-out Django suite (30 mixed-shape questions, 29/29/28, −33.5% against
native) and an etcd client corpus (30 mixed-shape questions, three repetitions, 270 trials, 87/89/89,
−42.4% against native and −26.8% against zvec-grep). Those two etcd figures were −44.6% and −29.2%
until 2026-09-22, when the second token column turned out to add cache reads to a total that
already contained them; one definition survives and it is the one the kernel studies always used.
Every Codex figure here was also measured with this file in the agent's context (see the trap
above), which no archived byte can net out. Quality has never separated in either
direction across 468 scored trials at that scale; the token gap has never failed to replicate
there.

**The etcd leg of that claim no longer replicates, and the Django leg is now the only clean
one.** `runs/cli-transport-20260924` re-ran the same corpus (fingerprint `ffe2bf07`, 311 files)
and the same suite (sha `3f2f5cf6`) with each trial in its own HOME, and measured the four-tool
surface at **+96.3% input tokens against native** where the published figure is −42.4%. The
control fell from 193,492 tokens a trial to 83,106 and the MCP arm rose from 111,389 to 163,148.
Two things differ between the runs and not one - the fetched routing document is gone *and* the
client moved 0.154.0 to 0.155.1 - and a 57% fall in the control is far outside the 8.6%
same-configuration drift already on record, so **this does not price the leak and is published
as uninterpretable rather than as its cost**. What it does establish is within-run and same-day
on the published claim's own instrument: on this corpus, on this client, the MCP surface is
nearly twice a shell agent's cost. The −33.5% held-out Django figure is unaffected - a Claude
client, documents excluded, 0 of 90 trials touching any of this - and is now the only leg of the
headline that no audit has moved. Two studies missed their registered quality criterion by a single answer and both are
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

- **Semantics is not the gap, and three of the four remaining kernel losses are not retrieval.**
  The precision figure that motivated the search - gold at rank 1 for 8 of 21 - came from sending
  the question verbatim, which is not how agents use the tool. On the queries the archived runs
  actually issued the gold is on the page for **19 of 21**, and the described target of a caller
  question is on the page for 7 of 8 and rank 1 for five. Of the four losses in the best run,
  three had the right target served at rank 1 or 2 and failed on the agent's second hop; only
  `flush_sigqueue` is a genuine miss. The antonym question the negation argument was built on
  scored 3 of 3. The reverted co-occurrence change, re-measured on agent queries, is identical to
  HEAD. `runs/precision-20260922`.
- **Top-1 precision, measured on verbatim questions, is a within-file problem.**
  `runs/precision-20260922`: over 100 rows the gold is rank 1 for 8 of 21, ranks 2-4 for 5,
  absent for 6 - and in **10 of 21 the right file is on the page while the right definition is
  not at the top of it**, three of them with the file at rank 1 and the definition missing
  entirely. Kernel-doc is indexed and works when the query carries a rare word (`TIMER_PINNED`
  resolves `add_timer_local`; the same sentence without it returns noise), so the scan was taught
  co-occurrence - rarity times `ln(1 + tokens seen within six lines)` - which bought recall
  13 to 15 of 21 and cost top-1 8 to 7, unchanged on Django and etcd, and was reverted. What
  remains is matching a described behaviour to a function body, which BM25 over definition text
  cannot express.
- **Working around precision has failed three times.**
  `search_concept` puts the gold definition at rank 1 for 8 of 21 kernel questions. Rarity-
  weighted selection fixed *recall* - 4 of 21 to 13 - and never touched precision. Three
  mechanisms built to work around it all failed: candidate relations under a model
  (`runs/linux-relations-20260922`), discriminating excerpts offline, and a `find_callers` that
  resolves a description itself (`runs/composed-callers-20260922`, right 3 of 8 against an agent
  two-hop that is near ceiling). The model's two hops are error-correcting; removing the check
  removes the correction. Precision at rank 1 is the thing to attack.
- **Deliberation was closed as a diagnostic and `runs/cli-transport-20260924` reopened it.**
  `runs/deliberation-20260922` found flat pages correlating with deliberation across questions
  (21.8% against 12.5% top-1 score gap) and not within them (+0.08 requests, 10 of 21 - a coin
  flip), and concluded that a shell agent does not think less, it thinks by running another
  command the client truncates cheaply, so requests-that-call-nothing measures where thinking is
  stored as much as whether it is needed. That reasoning still stands and it is why the column
  was demoted. What reopens it is a comparison it could not make: two arms reaching **the same
  index from the same build** with the same operations - 2.60 MCP calls against 2.67 CLI
  invocations - and **3.50 against 0.83 requests that call nothing**, a 4.2x gap that is the
  whole 163,148-against-57,424 token difference between them. Where thinking is stored is now
  the largest measured term in this comparison, not a diagnostic beside it. Three mechanisms are
  candidates and none is tested: the client truncates shell output on the way in and forwards
  MCP results whole; a tool result may invite a deliberation turn where a command's stdout does
  not; and the CLI arm carries an announcement the protocol gives the MCP arm free.
- **Deliberation did not respond to better evidence on the page.** `runs/linux-relations-20260922`
  stated which candidate calls which - 10 of 21 pages, no byte cost, offline-proven - and under a
  model it missed every registered criterion: idle requests 1.95 to 2.03, quality 59 to 56 of 63,
  unevidenced answers 2 to 6. It also produced this project's first negative kernel token gap,
  −5.4%, bought with three answers, which is why the safety column is gated. Reverted. Seven
  post-freeze optimisations have now been rejected by their own runs.
- **The kernel gap is closed to parity, and what remains is deliberation, not retrieval.** Four
  studies: +16.4%, +8.3%, +10.1%, **+1.0%** input tokens against a shell agent, with quality
  56 → 59 of 63, calls 3.20 → 2.10 and unevidenced answers 7 → 2. Per model request this server
  adds 4,462 tokens against native's 5,796. The remaining term is that a shell agent's requests
  are its calls plus one - it thinks by running another command - while this arm spends about two
  requests per trial that call nothing. Any further token work should target response sufficiency
  and the handshake, not bytes: `runs/linux-diet-20260921`.
- **Payload weight per call was the deficit, and the instrument to see it is Codex's own
  session log.** A non-ephemeral run writes a `token_count` event per model request;
  `runs/per-request-20260921` used it on six kernel questions and found this server already
  ahead on requests (32 against 34) and calls (26 against 28), its four tools costing 42 tokens,
  and the entire gap in context added per call: **6,042 against a shell agent's 4,378**. Codex
  truncates shell output on the way in (coefficient 0.09) and forwards MCP results whole, so a
  client trims for the competitor and not for us. Any payload we do not trim ourselves is paid
  for again on every later request of the session.
- **The earlier round-trip framing was half right.** Three studies:
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
