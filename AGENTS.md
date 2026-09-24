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
7. **The control has a shell.** A comparison against `Read`/`Grep`/`Glob` is not a comparison
   against what an agent actually has, and the difference is not small: a shell agent chains 2.03
   operations per call against this surface's 1.02, and composition is the term that decides these
   comparisons at ~15,000 tokens a dependent hop. The held-out −33.5% was measured against a
   shell-less control and is reported that way, not as a headline: **against a shell-bearing agent
   this server has never won at any scale** — +96.3% and +71.0% on etcd, +1.0% at kernel scale on
   its best run. Any future win is claimed against a control with a shell or it is not claimed.
8. **Author the instrument outside the loop.** A question set written by an agent session in this
   repository is written with this file and the operator's `~/AGENTS.md` — a routing guide for
   these four tools — already in its context. Golds survive that (the oracles are ripgrep and
   ctags), but which questions exist and how they are worded do not. Authoring sessions get the
   trial's own isolation: `--setting-sources "" --settings '{"autoMemoryEnabled": false,
   "claudeMdExcludes": ["/**"]}'` for Claude, `-c project_doc_max_bytes=0` for Codex, and no
   knowledge of which arm is expected to win. Every suite in `experiments/suites/` predates this
   rule.

9. **A suite's quality ties are quotable only after a degraded arm has scored worse on that
   suite.** `runs/degrade-control-20260924` ran the held-out Django set against itself with the
   row order scrambled - same binary, same bytes, `initialize` and `tools/list` untouched - and
   scored **29/29 against 29/29**, with the gold pushed to rank 10 in six trials and no answer
   changing. That suite measures whether the gold was on the page, not retrieval quality, so its
   ties are not evidence that anything lost nothing. **No suite in this repository has yet
   demonstrated discrimination**, which puts every "quality did not separate" reading - five of
   eight published studies - out of bounds until one does. `page-position` says etcd-mixed is the
   suite with the power to try (9-10 of 30 questions at risk under `--page-fraction`), and
   `degrade_server.py` already has the knob. Token figures and mechanism measurements are
   unaffected.

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

- **`unretrieved_identities` and `first_hit` are visibility columns, not retrieval columns.**
  They scan every tool *request* and *body*, so they are generous by design - `first_hit`'s own
  docstring says it proves visibility, not use - and they are asymmetric between an arm that reads
  whole files and one that returns rows: a `Read` drags a whole file's identities in, a
  `search_concept` page names definitions. Two consequences, both paid for on 2026-09-24. Scanning
  requests alongside bodies counts paths **the agent typed itself** as evidence that retrieval
  delivered them, which produced a confident "6 of 15 losses are a fixable selection failure" that
  was an artifact. And the cross-arm *rate* is not like-for-like, so only the within-arm statement
  survives. Both readings were also downstream of an arm searching the wrong repository, which is
  the real lesson: audit the payloads before classifying the losses.

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

## Answering the operator

How replies to the operator are written. Same discipline as the rest of this file: the point is
that a reply be decidable, not that it be complete.

- **Lead with the decision or the number.** Not the method, not the caveats, not what was
  attempted. Detail belongs in the run directory and in `experiments/README.md`; the reply carries
  the figure and what it settles.
- **A question about direction wants a goal and an order of work, not a survey.** Recommend one
  path. Say what it costs, what it would settle, and what gets dropped to do it. Mapping the
  option space reads as thorough and decides nothing.
- **Cost every step and put the cheapest decisive one first.** An offline check that could kill
  the idea for $0 outranks a study that would confirm it for $16.
- **Name what would falsify it, before running it.** A step whose failure condition is not stated
  in advance is not an experiment, and the registration will not accept it either.
- **Say what not to do, and until when.** A plan that only adds work is not a plan.
- **State a preference when asked to choose.** "I can argue it either way" is worth saying only
  with the preference attached.
- **Report a miss as plainly as a hit.** A criterion missed is the finding, not a setback to
  soften — four withdrawn figures in this record came from taking that seriously.
- **Don't re-narrate what the record already holds.** Cite the section and move on.
- **Flag a limit before the run, not after the number.** A caveat produced once a result is
  unwelcome is indistinguishable from an excuse, and this project cancelled a study on a limit
  that was stated in advance and still not taken seriously enough.

## Where the project stands

Released `v0.1.6`, and **releases are paused there** - see `CHANGELOG.md` for why, and do not
cut another. The default surface is four tools — `search_exact`, `read_source`,
`find_callers`, `search_concept` — with the lexical ranker; the other three stay un-defaulted on
replicated evidence. `CHANGELOG.md` says what shipped in each release.

**The record is `experiments/README.md`, not this file.** Every study, its registration, its
misses and its numbers are there; `runs/` holds the bytes and is gitignored, so the prose record
is what survives for anyone without them. What follows is the shortest thing an agent needs
before it changes something, and a pointer to where the evidence is written down.

### The claim as it stands

Quality has never separated in either direction across 468 scored trials on corpora of a few
hundred files. The token half was cut back twice in one week and now reads:

- **At the bar that matters — a control with a shell — there is no win, at any scale.** etcd,
  cleanly re-measured on its own client with documents removed: **+96.3%** and **+71.0%** input
  tokens. Kernel scale, four studies: +16.4% → +8.3% → +10.1% → **+1.0%**, parity at best, with
  quality 59 against the control's 62. [record](experiments/README.md#where-the-tokens-actually-go-per-request-accounting)
- **The held-out Django −33.5% stands as measured and does not meet that bar.** Its control had
  `Read`, `Grep` and `Glob` and no shell, so it could not compose; law 7. Its questions are also
  in-loop, and whether the figure survives out-of-loop questions is **unmeasured** — the run that
  tried, `runs/client-cell-20260924`, was voided by a harness defect that rooted the retrieval
  server at the arm-wide placeholder corpus instead of the question's own tree.
  [record](experiments/README.md#the-held-out-comparison),
  [the defect](experiments/README.md#harness-defect-ledger)
- **Index amortization has never been measured and is the one advantage grep cannot copy.** Every
  trial in this record is one question in a fresh session, so an index is built, used once and
  discarded — the architecture's worst case, measured exclusively. N questions per session against
  the same repository is untested.
- **The etcd leg, −42.4%, is withdrawn, and not to a corrected number.** Every archived trial in
  all three arms fetched an operator routing document that names these four tools and calls them
  cheap and precise. Clean re-runs measure **+96.3%** on the current client and **+71.0% on the
  study's own client**; seeding the document back into one arm moves it 166,988 → 104,200 input
  tokens. The document accounts for the published result rather than contributing to it.
  [record](experiments/README.md#the-client-version-is-not-the-culprit)
- **At kernel scale the gap is parity**, +16.4% → +8.3% → +10.1% → **+1.0%** across four studies,
  with quality 56 → 59 of 63 and unevidenced answers 7 → 2. What remains is deliberation, not
  retrieval. [record](experiments/README.md#the-response-diet-say-each-fact-once)
- **The same four operations as a command** beat a shell agent by 22.2% on etcd over 360 trials
  and by **0.2% — nothing — on 25 questions this project did not write**. What generalises is that
  the MCP channel is expensive and why; the index beating grep does not.
  [record](experiments/README.md#the-first-externally-authored-test-splits-the-finding-in-two)
- **On a Codex client MCP tools are not in the prompt.** Attaching four tools costs 0 prompt
  tokens and is paid for in round trips instead: 2.4–3.1 catalogue searches a trial, in 21 of 25
  and 30 of 30 trials, replicating across two client versions and unavoidable by configuration.
  A skill document shipped beside the server halves it. [record](experiments/README.md#the-same-four-operations-as-a-command-and-the-deliberation-thread-reopening)

Two rules came out of that and outlive it. **Any token claim on this client must be same-day and
within-run** — native drifted 8.6% between two runs of an identical configuration. And **count
round trips from the rollout, not from the gate log**: `calls.jsonl` sees only what reaches the
retrieval server, `command_execution` sees only shell, and on this client every arm drives one
`exec` tool, so both counters miss most of what an MCP arm does.

### Open threads

- **Precision at rank 1 is the thing to attack, and it is a within-file problem.** On verbatim
  questions the gold definition is rank 1 for 8 of 21 kernel questions, and in 10 of 21 the right
  file is on the page while the right definition is not at the top of it. On the queries agents
  actually issue the gold is on the page for 19 of 21, so recall is not the gap.
  [record](experiments/README.md#where-the-gold-sits-when-it-is-not-rank-1),
  [correction](experiments/README.md#correction-semantics-is-not-the-gap-and-three-of-four-losses-are-not-retrieval)
- **Working around precision has failed three times** — candidate relations under a model,
  discriminating excerpts offline, and a `find_callers` that resolves a description itself. The
  model's two hops are error-correcting; removing the check removes the correction.
  [record](experiments/README.md#which-candidate-calls-which-and-an-excerpt-idea-that-did-not-survive),
  [record](experiments/README.md#collapsing-the-two-hop-and-what-it-reveals-about-all-of-this)
- **Wrapper against callee is the live ranking failure.** Rarity ranks the site where a rare word
  appears, which for a thin wrapper is the function it calls, and it cost all four lost trials of
  the best kernel run — every one a caller question. Nothing says yet whether the fix is scoring,
  the tool description, or a disambiguation step the agent runs itself.
  [record](experiments/README.md#the-repair-measured-under-a-model-every-predicted-answer-and-a-new-failure-one-level-down)
- **The handshake is settled.** Two candidate sentences over 351 trials both came in at +0
  against a registered +3 bar, so the prompt side has nothing left that measurement supports
  changing. [record](experiments/README.md#two-handshake-sentences-measured-and-rejected)
- **Payload weight per call, not payload size, was the deficit.** A dependent hop is ~15,000
  tokens and a 20 KB payload ~2,400, and this client truncates shell output on the way in while
  forwarding MCP results whole — so any payload we do not trim ourselves is paid for again on
  every later request of the session.
  [record](experiments/README.md#where-the-tokens-actually-go-per-request-accounting)
- **Seven post-freeze optimisations have been rejected by their own runs.** Plausible accounting
  is not evidence. [record](experiments/README.md#findings-at-a-glance)

### Decisions waiting, not measurements

- **`runs/etcd-caller-20260915` is built, gated and deliberately unrun**: 39 questions, 27
  attribution-hard and 12 local controls, enriched 2.4× over its corpus's own 38% base rate. It
  answers "does attribution change answers, or only cost", and that question has not been worth
  $16.
- **`cu-callers-01` does not compile and blocks both coreutils suites.**
  `src/uu/head/src/head.rs` defines `print_n_bytes` twice under opposing `#[cfg]` gates, so
  `path::name` attribution cannot say which variant calls `send_n_bytes`. `validate_suite.py` has
  refused it since the namesake guard landed. Repairing it means re-authoring the question or
  dropping it, which changes the suite's bucket counts, so it is a decision rather than a fix.
- **No agent-level claim for the client-root work.** It is covered by an offline differential (28
  tool payloads identical across pinned, launch-directory and client-roots resolution), six stdio
  tests and one live Codex session. The benchmark pins `--root`, so the new path was never
  exercised under a model.

## The line worth remembering

**Correct evidence reduces recovery turns.** The largest efficiency win measured after the freeze
came from a row that started telling the truth about a call site — not from a smaller payload, a
richer result, or more retrieval machinery.
