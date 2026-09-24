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
- **Deliberation stays closed. `runs/cli-transport-20260924` first looked like it reopened the
  thread, and that reading was a counting defect.** `calls.jsonl` is gate-side and sees only
  what reaches the retrieval server; `command_execution` sees only shell. On codex-cli 0.155.1
  every arm drives one tool, `exec`, and writes code in it, so both counters missed most of what
  the MCP arm did - it looked like 2.60 calls against the CLI arm's 2.67 with 3.50 requests
  calling nothing, when counted from the rollout it is **5.10 `exec` calls a trial of which 2.93
  are tool discovery**. Those requests were the model searching for its own tools with
  `ALL_TOOLS.filter(...)`, and in **30 of 30 trials the MCP arm's first action is discovery**.
  Corpus work is equal across the two retrieval arms, 2.17 repository calls against 2.27 - the control makes more of them, 3.17, and is not part of that equality. The deliberation
  hypothesis was also tested directly and failed: payload size does not buy a thinking turn -
  within each arm the larger half of results is followed by no more requests than the smaller
  half, and matched on size the MCP arm deliberates least. **Count from the rollout, not from
  the gate log**, and audit before interpreting: this interpretation was published before the
  audit finished and had to be withdrawn the same day.
- **On this client MCP tools are not in the prompt, and that is paid for in round trips.**
  `runs/cli-transport-20260924/prefix.json`: attaching the four tools costs **0** prompt tokens,
  measured with four tools provably attached, because the schemas sit behind a tool-search
  interface rather than in the prefix. The CLI arm's generated announcement costs **1,203 tokens
  a request** and buys exactly what the MCP arm must go and find - 2.93 discovery round trips a
  trial. That is not a handicap to subtract from the CLI arm; on this client it is the cheaper
  side of the trade by a wide margin, 57,424 input tokens a trial against 163,148.
- **The discovery tax is not a setting, and it replicates.** `code_mode` and `tool_search` are
  real flags on this client - Codex validates names, `apply_patch_tool` is rejected - both on by
  default and set by nothing here. `runs/codemode-price-20260924` turned both off over 20 trials
  and moved nothing: discovery 3.10 to 3.00, present in 10 of 10 trials either way, requests
  6.10 against 6.10. The flags were verified to apply rather than assumed to, because
  `CODEX_DISABLE_FEATURES=definitely_not_a_feature` fails the launch with an empty usage record.
  Discovery also replicates across launches, 2.93 a trial over 30 and 3.0 over 10. So an MCP
  server here cannot configure its way out of the tax; the only remaining lever is
  `unified_exec`, which removes the tool altogether and is the no-shell arm that scored 10 of 25.
- **The fetched document was worth about 38% of the MCP arm's tokens, and the mechanism is that
  it made discovery unnecessary.** `runs/leak-price-20260924` recovered the operator skill file
  from the archive - the archived trials ran `sed` on it and Codex captured the output, so the
  bytes are the bytes they read, 4,253 of them - and put it back in one arm's trial-local HOME.
  Against a clean arm on the same ten questions: discovery calls **3.10 to 1.10**, requests 6.20
  to 5.00, input tokens **166,988 to 104,200**, quality unchanged at 9 of 10 both ways. 10 of 10
  seeded trials read it; 0 of 10 clean trials did. It names all four tools, so an agent that has
  read it does not need to search for them, and repository work barely moves while catalogue
  searching collapses. Seeded, today's arm lands within 7% of the archived 111,389 - so the
  document accounts for most of the MCP side of the −42.4%-to-+96.3% swing. The control side is
  untouched by that run and stays unexplained, and the published figure stays withdrawn rather
  than corrected, because this prices the document on 0.155.1 with a v0.1.6 server rather than
  reconstructing the archived configuration.
- **And the client version is not the culprit; the document is the whole of it.** codex-cli
  0.154.0 was twice called unobtainable here. It is published at `rust-v0.154.0` and runs
  through this harness unmodified. `runs/client-version-20260924` puts both clients on the same
  ten questions, same corpus, same server binary, no document: **native 80,320 on 0.154.0
  against 78,671 on 0.155.1, +2.1%**, with identical repository calls and requests. The
  registered bar for blaming the client was +40%. It is worth about 12% and only to the MCP
  arm, 137,348 against 155,689, through less catalogue searching - and **discovery is not new**:
  0.154.0 does it too, 2.20 calls a trial in 7 of 10 trials, which was declared unknowable
  twice and took one run to settle. The number that matters: clean on the archived study's own
  client the MCP surface costs **+71.0%** against native, where the published figure says
  −42.4%. The published result does not survive on its own client once the document is
  removed.
- **Confirmed over three repetitions, and two registered criteria failed.**
  `runs/cli-confirm-20260924`, 360 trials, four arms, same etcd corpus: native 80,331 input
  tokens a trial at 86/90, bare MCP 151,486 at 89/90, **MCP with a skill document shipped
  beside it 106,481 at 87/90**, and the four operations as a command **62,519 at 88/90**. The
  registered primary - the command at least 20% under the shell agent - is **met at −22.2%**,
  but thinner every repetition: −26.2, −24.7, −22.2. Quoting the single run's −30.9% would
  have been quoting the high-water mark. The product question is met decisively: shipping a
  skill document recovers a third of the MCP arm's cost and the command is still **41% cheaper
  than MCP-as-shipped**. Two criteria failed and are published as failures: the quality bar,
  missed **by the control arm** at three answers below the best while every retrieval arm sat
  within two; and the mechanism registered in advance, −21.6% round trips × −11.9% context,
  measured at **−10.3% × −13.2%** with the round-trip term shrinking at every repetition. The
  qualitative mechanism survives - fewer round trips *and* lighter context - and the split does
  not. Evidence volume is the stable figure: **−66.9%** against the control, −66.8% in the
  first run. The `read_outside_corpus` detector fired in 89 of 90 seeded trials and none of the
  other 270, which is the accidental-contamination detector catching the deliberate condition.
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
