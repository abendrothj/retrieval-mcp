# Routing experiments

The question is whether giving a model separate retrieval tools makes it choose better evidence, and which tools earn their place. Everything here is Python 3.11+ standard library only; the server and semantic backend remain Rust.

Start at [End-to-end context efficiency](#end-to-end-context-efficiency-three-arms-one-corpus) — `end_to_end.py`, `validate_suite.py`, `lexical_oracle.py` and `closure_audit.py` are the live harness, and the findings from [Compiling a suite before it is used](#compiling-a-suite-before-it-is-used) onward are the current ones. `benchmark.py` and `analyze.py` are the **frozen v1 availability harness**: they run sessions under the `--profile A/B/C/D` letters and are kept so the original runs replay, not because profiles are how new work is configured. Sections below them are retained as provenance for results already recorded.

## Findings at a glance

Chronological detail is below; this is the spine. Every row links to the section holding its run
artifacts, caveats, and the commands that produced it.

| # | Question asked | What the run showed |
|---|---|---|
| 1 | Does a retrieval MCP beat grep-and-read? | [Not on a saturating suite](#end-to-end-context-efficiency-three-arms-one-corpus): every arm answered nearly everything, so nothing could be claimed |
| 2 | Were the "hard" questions hard? | [Mostly broken, not hard](#audit-of-the-never-solved-questions) — two golds were wrong about the corpus, three failed on answer shape |
| 3 | Can a suite be trusted before it is run? | [Only if compiled](#compiling-a-suite-before-it-is-used): gold, evidence anchors and grader all verified against the corpus, no model needed |
| 4 | What actually drives context cost? | [Turns, not payload bytes](#closure-a-stopping-rule-is-worth-more-than-a-better-ranker) — r = 0.74 with calls, 0.09 with bytes |
| 5 | Can a stopping rule cut cost safely? | [Yes](#closure-a-stopping-rule-is-worth-more-than-a-better-ranker): −44% calls, −22% tokens at unchanged correctness, and it [survived a suite built to punish it](#does-closure-survive-uncertainty-the-hard-suite) |
| 6 | What was the one closure loss? | [Ambiguity, not premature stopping](#the-regression-was-ambiguity-not-stopping-one-clause-re-run) — one clause restored it with fewer calls |
| 7 | Do seven tool schemas pay for themselves? | [No](#schema-surface-the-tax-is-real-the-capability-pressure-is-not): the full surface was the most expensive arm on every axis at equal quality |
| 8 | Why did that ablation prove nothing about capability? | [Reachability is not necessity](#necessity-not-reachability-a-gate-a-suite-and-the-first-tool-that-pays) — a bounded lexical crawl dissolved half the suite |
| 9 | Does any structural tool earn its schema? | [`find_callers` does](#necessity-not-reachability-a-gate-a-suite-and-the-first-tool-that-pays): net +234.8 k tokens on exhaustive caller questions |
| 10 | Do the other three? | [No](#every-remaining-tool-on-trial), and [replication confirmed it](#the-replication-and-the-frozen-surface) — `trace_dependencies` was never called even on questions authored for it |
| 11 | Does the frozen system beat the alternatives? | [Yes on context, tie on quality](#the-held-out-comparison): 29/30 each against zvec, −24% input tokens, −34% persistent context |
| 12 | Do post-freeze index changes earn their place? | [One of three did](#three-index-changes-one-survivor): doc comments in the chunk nearly doubled offline MRR; markdown chunks and macro-argument calls were reverted on their own evidence |
| 13 | Does the v0.2.0 ranking gain reach the agent? | [No, and it still beats zvec](#layer-2-four-arms-120-trials-claude-sonnet-4-6): offline v0.2.0 goes from behind zvec to ahead (MRR 0.126 → 0.234 vs 0.165), but end to end on 120 trials it ties v0.1.1 while both beat zvec-grep 27–28/30 against 25 at 36% fewer total tokens |

**The result in one line.** On a sealed 30-question held-out set, this server matched zvec-grep at
29/30 on the same 78 tool calls while spending 24% fewer input tokens and carrying 34% less
persistent context, with no unsupported answers by any arm.

**Why, in one line.** Not a better ranker: model-side vocabulary translation, cheap lexical
retrieval, bounded source verification, one relational primitive, and a stopping rule — everything
else was removed after it failed to pay for itself.

## How it converged

The project began as a ranker question — dense against BM25 — and the first offline bake-off said
dense won clearly. Then the same rankers were run on questions rewritten into code vocabulary, and
BM25 overtook dense on every metric. The expensive part was never the ranking; it was translating a
human sentence into repository words, and the model already does that. Dense retrieval stopped being
the default and became an optional backend.

The next three hypotheses each looked like an architectural finding and each turned out to be
arithmetic about turns. Input tokens correlate with *call count* at r = 0.74 and with retrieval
bytes at 0.09, because every call re-sends the transcript; so payload optimisations were costed and
abandoned before being built, and a stopping rule written in the completeness signals the server
already emitted cut calls by 44% at unchanged correctness. Tool schemas turned out to be the same
kind of cost — re-sent every turn whether called or not — which made the seven-tool surface the most
expensive arm on a suite where it answered no better.

Cutting tools on that evidence would have been wrong, and the reason is the most useful thing the
project learned: **reachability is not necessity.** A suite can require a structural route and still
be dissolved by two ordinary searches, which is what a deterministic bounded crawl showed for half
the questions written to be hard. Only after questions were built around a relation lexical hits
genuinely do not encode — the enclosing definition of a call site — did one structural tool,
`find_callers`, return a positive ledger; the other three failed their own claimed classes under
replication and left the default surface.

Running alongside all of it: sixteen defects in the measuring apparatus, several of which had already
produced convincing results. A stopping rule that looked like a free 22% saving, a treatment loss
that looked like a premature stop, an arm whose payload metrics read zero, a 0.40 that looked like a
noisy tool, and — in the held-out run itself — this server's only loss, which had actually named both
correct symbols. The habit that caught them is the one methodological claim worth repeating: **every
surprising result triggers an evaluator audit before an architectural interpretation.**

What survived is what was left after each hypothesis and each tool was made to earn its place.

## Harness defect ledger

The harness is the second experimental subject. Sixteen defects in it have produced or nearly
produced believable false findings, and they run in both directions: some flattered this server,
some penalised it, one was found inside its own single held-out loss, and the last would have made
every brace-language caller question unauthorable. Each is pinned by a test. This table is the
authoritative list; prose below refers to it rather than to ordinals.

| Defect | Would have shown |
|---|---|
| Grader accepted a payload only when the whole reply was the JSON object | Correct answers preceded by one sentence scored zero; an envelope-discipline ranking read as retrieval quality |
| Vector cache written inside the corpus under test | A contaminated corpus whose fingerprint drifted mid-run |
| MCP tool errors swallowed by the runner | 36 successful calls scored as failures |
| Output-file check ran after the model spend | Runs discovered to be unusable only after they were paid for |
| Gold resolver indexed only Rust and Python | Every TypeScript answer failed on spelling, not on retrieval |
| Set-valued grader returned zero for any prose answer | 13 of 42 audited trials scored wrong while naming every gold symbol |
| TypeScript indexer dropped methods with more than one modifier | 359 definitions invisible, including golds that named them |
| Python indexer saw `def` but not `class` | A gold naming a class rejected as undefined |
| Caller verifier globbed the wrong tree | Gold caller sets verified against the wrong corpus |
| Symbol resolver would not split `file.py:Class.method` on a single colon | A correct answer in editor notation scored zero, manufacturing a treatment loss |
| Context scorer knew OpenCode and Codex streams but not Claude's | Every payload metric for a Claude arm silently read zero, making MCP arms look free |
| `enclosing()` credited a closed nested helper for later calls | Exhaustive golds demanding callers that do not exist |
| `enclosing()` credited the class when a decorator or wrapped signature intervened | The same, one scope too high |
| `true_callers()` counted a call written inside a comment | An exhaustive gold demanding a caller that is documentation |
| Grader scored a keyed object naming the gold identities as zero | This server's only held-out loss, which had named both correct symbols |
| `enclosing()` implemented only its Python branch, and its first brace-language replacement named frames from call syntax | On Rust and TypeScript corpora `true_callers()` returned nothing, so every correct caller gold failed validation as unverifiable; the first fix then attributed call sites to `Ok`, `Err` and, worst, to a real function defined elsewhere — a caller set that looks plausible and is fiction |

The habit that found them is in [The habit that made the numbers trustworthy](#the-habit-that-made-the-numbers-trustworthy).

## Corpora

No suite validates against an upstream checkout. `corpora/` holds the three checkouts the scoped corpora were cut from; the pinned corpus each question set was compiled against lives beside its run artifacts, and pointing a tool at the wrong one produces confusing `gold is declared exhaustive but omits N callers` failures rather than a clean error.

| Question set | Pinned corpus | Files |
|---|---|---|
| `django_*_questions.json` (all seven) | `../runs/django-suite/corpus` | 276, scoped to `django/db`, `core`, `utils`, `dispatch`, `apps` |
| `comparison_questions.json`, `v2_questions_draft.json` | `../runs/projects-v2-suite/coreutils/corpus` | 673 |
| `../runs/vscode-platform-suite/authored-questions*.json` | `../runs/vscode-platform-suite/corpus` | 472 |
| `stratified.json` | `../runs/stratified-v1-suite/corpus` | 15 |
| v1 ModelShare / pig / Sigil | `../runs/projects-v1-suite/*/corpus` | — |

Scope, upstream commit, and corpus hash for the Django suite are in `django_suite_manifest.json`; for coreutils, in [CORPUS_V2.md](CORPUS_V2.md). `comparison_questions.json` predates `validate_suite.py` and does not pass it — it was frozen under the earlier human-review gate described in [Freeze gate](#freeze-gate), and is kept as recorded rather than retrofitted.

## Offline work: no Claude calls

The next study's [factor-separation protocol](FACTOR_PROTOCOL.md) separates tool availability, first-tool policy, and syntax help. `plan_factors.py` generates balanced prompts and one-factor contrasts offline; it is not a model runner and does not resume historical trials.

```sh
cargo build --offline --locked --bin retrieval-mcp
cargo test --offline --locked --all-targets
python3 -W error::ResourceWarning -m unittest discover -s experiments -p 'test_*.py' -v
python3 experiments/audit_answers.py /path/to/project-results --output /path/to/new-answer-audit.json
```

The fake agent and semantic fixture in `test_experiments.py` exercise real MCP transport and all four profiles without inference. Rust tests cover bounded reads, path escapes, lexical pagination, structural ambiguity, subprocess failures, and embedding-cache persistence with synthetic vectors. The live Ollama test is ignored by default. Offline builds require dependencies already cached.

`audit_answers.py` reads saved project answers and frozen gold, records input hashes, and writes a separate post-hoc report without overwriting existing files. It compares exactly one JSON payload even when preceded by prose; multiple objects, duplicate keys, malformed JSON, and unsupported shapes require manual review. Typed values, set uniqueness, and ordered call chains keep the original comparison rules. This is a sensitivity check, not a replacement for frozen scores or a review of contradictory prose. It excludes unfinished trials and never invokes a model. Resuming model trials remains separate work; these commands do not resume them.

## Offline retrieval and navigation studies

Four scripts isolate one layer each, upstream of any agent loop. All are deterministic given their
inputs; only `study_a1.py` and `study_a2.py` call a model, and only with `--allow-model-usage`.

| Script | Question | Model calls |
|---|---|---|
| `study_a.py` | Given one query, which ranker puts gold in the candidate set? | none |
| `study_a1.py` | Does rewriting the question into code vocabulary change that? | one per question |
| `study_a2.py` | Given retrieved rows, which next retrieval action does the model choose? | one per question |
| `study_a3.py` | When a structural action fails, which part of the navigation was wrong? | none |

`study_a.py` runs the same questions and chunks through `lexical`, `semantic`, and `hybrid`,
scoring recall@1/3/5/10 and MRR against symbol-level gold. It refuses to run with a vector cache
inside the corpus and fingerprints the corpus before and after, aborting if ranking changed it.
`make_reformulations.py` freezes one model rewrite per question so `study_a1.py` can re-rank
without a live model; `make_mechanical_suite.py` derives description-to-symbol questions from doc
comments for cheap breadth across languages.

`study_a3.py` classifies each structural decision rather than only scoring it: `wrong_reach` (right
seed, wrong direction), `wrong_level` (right neighbourhood, wrong container/member), `unchosen` (a
workable seed was visible and something else was invented), and `unavailable` (no workable seed was
on screen). It also reports how far the chosen seed sits from gold, and replays the recorded action
to measure what that surface actually returned in bytes.

### The `inspect_symbol` A/B

The primitive in the server exists because of this contrast. Identical corpus, chunks, gold, ranker,
prompt, and seed; the only change is whether `inspect_symbol` appears in the action menu:

```sh
for surface in directed neighbourhood; do
  python3 experiments/study_a2.py --root /path/to/corpus \
    --questions /path/to/authored-questions.json \
    --reformulations /path/to/reformulations.json \
    --cache /path/to/cache-outside-the-corpus \
    --semantic-command '["/path/to/target/release/examples/ollama_backend"]' \
    --surface $surface --repetitions 3 --allow-model-usage \
    --output /path/to/study-$surface.json
  python3 experiments/study_a3.py --root /path/to/corpus \
    --questions /path/to/authored-questions.json \
    --decisions /path/to/study-$surface.json \
    --reformulations /path/to/reformulations.json \
    --output /path/to/study-$surface-nav.json
done
```

Over 45 decisions per surface (15 questions × 3 repetitions), certified solves went 17 → 27,
`wrong_reach` 15 → 4, and `wrong_level` 9 → 8. Six questions improved, none regressed. Median bytes
per structural call rose 322 → 1,235, so the primitive buys direction with bytes: better per
decision, worse per byte in isolation. Whether that trade survives a full agent loop is the
[end-to-end study](#end-to-end-context-efficiency-three-arms-one-corpus), where it currently does
not show.

## End-to-end context efficiency: three arms, one corpus

The question this study answers is not "which ranker retrieves better" but "how much model context
does an agent consume before it reaches a correct answer". Three arms — OpenCode's own
grep/read/glob/bash tools, zvec-grep 0.2.2, and `retrieval-mcp` profile D — run the same authored
questions over one byte-identical corpus, each until it answers or exhausts its call budget.

`comparison_systems_three.json` is that three-arm subset of `comparison_systems.json`, with the
zvec install pinned to a local package file so preparation needs no registry. `codex_wrapper.py`
drives Codex CLI and `opencode_wrapper.py` drives OpenCode; both translate their client's event
stream into the harness transcript shape and keep the raw stream beside the trial for scoring.

```sh
# 1. Prepare one corpus copy per arm, with warm indexes and recorded versions. No model calls.
python3 experiments/comparison_runner.py prepare \
  --source-root /path/to/corpus --workspace /path/to/workspace \
  --systems experiments/comparison_systems_three.json \
  --semantic-command '["/path/to/target/release/examples/ollama_backend"]'

# 2. Run the matrix. Requires explicit model approval; writes one directory per trial.
python3 experiments/comparison_runner.py run \
  --workspace /path/to/workspace --systems experiments/comparison_systems_three.json \
  --questions /path/to/authored-questions.json \
  --semantic-command '["/path/to/target/release/examples/ollama_backend"]' \
  --client command --model gpt-5.6-luna --allow-model-usage \
  --agent-command '["python3","experiments/codex_wrapper.py","gpt-5.6-luna","{mcp_config}","{prompt_file}","{run_dir}"]' \
  --repetitions 2 --seed 11 --max-calls 25 --timeout 600 --output /path/to/run

# 3. Score it offline from the raw event streams. No model calls.
python3 experiments/end_to_end.py --run /path/to/run \
  --questions /path/to/authored-questions.json --output /path/to/report.json
```

`end_to_end.py` recomputes every metric identically for all arms, because an MCP tool result and a
native `read` result are both just bytes a tool put into the context window: correctness, input,
output and reasoning tokens, retrieval bytes, calls, source reads, latency where the client reports
it, wall time, cost, and the two that matter most — `calls_to_first_hit` and everything spent
*after* the gold path or symbol first appeared in a tool result. A system that surfaces the answer
in call one and then needs six more reads to commit is not a good retrieval layer. Codex reports
usage once per turn rather than per step, so for that client the post-hit figures are calls and
bytes rather than tokens; the report states this in its `limitations`.

Two harness rules exist because their absence produced believable false results. A provider failure
— 402, quota, auth, rate limit — is classified from the client's error payload, marks that trial
`provider_error`, aborts the entire run, writes `status.json` with `complete:false`, and exits
non-zero. It never counts against an arm's failure budget, because a billing outage that silently
disables arms one at a time yields a partial matrix that still looks scoreable. Separately, the
grader indexes definitions for Rust, Python, and TypeScript with ripgrep over the corpus, never by
asking any system under test; a prose answer resolves only when it names exactly one indexed
definition, so listing candidates earns nothing.

### Result, `gpt-5.6-luna` via Codex CLI, 2026-09-10

Fifteen authored VS Code platform questions × 2 repetitions × 3 arms = 90 trials, 64 minutes,
all completed, no provider aborts.

| | native-control | zvec-grep | retrieval-mcp |
|---|---:|---:|---:|
| Resolved correct / 30 | 14 | 15 | 16 |
| Mean graded credit | 0.467 | 0.500 | 0.533 |
| Median input tokens | 206,054 | 213,646 | 227,955 |
| Median output / reasoning tokens | 1,040 / 411 | 1,123 / 339 | 906 / 370 |
| Median retrieval bytes | 92,449 | 61,756 | 85,231 |
| Median calls / source reads | 4.5 / 4.0 | 5.0 / 3.0 | 5.5 / 3.0 |
| Median calls to first hit | 2 | 3 | 2 |
| Median bytes after first hit | 22,330 | 14,146 | 46,020 |
| Input tokens, resolved trials only | 177,513 | 190,156 | 162,862 |
| Retrieval bytes, resolved trials only | 92,665 | 60,188 | 52,879 |

The target was quality at least matching zvec-grep while consuming less total context. Quality is a
tie — paired discordance is one to two question-repetitions, with no arm ever losing one it would
otherwise win — and total input tokens are about 10% *above* the native baseline, so the target is
not met. Conditioned on success this server is the cheapest arm on both tokens and bytes; it is the
most wasteful on failure. Strict envelope grading scored 0/90 for every arm because this model
answers in prose around its JSON, so all quality figures come from the graded resolver.

The suite itself is the limiting instrument: 6 of 15 questions were solved by every arm in both
repetitions and 7 by none, leaving 2 that discriminated at all. Interpreting arm differences on
this suite beyond "indistinguishable" is not supported.

### Audit of the never-solved questions

A question every system fails is either hard or broken, and a score table cannot tell those apart.
`audit_failures.py` separates them mechanically over the 42 trials of the 7 never-solved questions:
it checks whether the answer names every gold symbol, whether the gold's shape (list or object)
matches what the model returned, whether a named symbol has namesakes that leave it unqualified,
and — where the question names a helper — whether the gold's claimed callers agree with an
independent ripgrep enumeration of the corpus.

```sh
python3 experiments/audit_failures.py --run /path/to/run \
  --questions /path/to/authored-questions.json --corpus /path/to/corpus \
  --helper 'vsc-callers-as-text-or-error=src/vs/platform/request/common/request.ts::asTextOrError' \
  --output /path/to/audit.json
```

`--helper` is required for a caller question whose gold does not name the helper it is about;
without it the audit would read a gold list's first entry as the target, and a common name such as
`initialize` produces nonsense. Verdicts over the 42 trials: 21 `unqualified`, 17 `wrong`,
4 `shape`. Reading each question against the corpus gives:

| Question | Diagnosis | Evidence |
|---|---|---|
| `vsc-trace-shell-env-consumers` | Evaluator. All 6 answers name both gold classes; the gold is a list and the answers are prose, which scores 0 by shape | 6/6 name all gold symbols |
| `vsc-mixed-1223-owner` | **Gold is wrong.** It claims one caller, `UserDataSyncStoreClient::request`, which does not call `isSuccess`; the two real external callers are `queryRawGalleryExtensions` and `getActivityData`, which is what the models answered | 1 claimed caller absent, 2 real callers unclaimed |
| `vsc-callers-as-text-or-error` | **Gold is wrong.** It names 3 callers of `asTextOrError`, one of which (`getAllCollections`) does not call it; the corpus has 11 | 1 claimed caller absent, 9 real callers unclaimed. One trial listed exactly all 11 and scored 0 |
| `vsc-mixed-empty-window-guard` | Evaluator plus granularity. 4 of 6 name both gold parts in prose against an object gold; the gold consumer is a class while the call sits in its method `hasBackups` | 4 `shape`, 2 genuinely wrong |
| `vsc-trace-backup-restore-chain` | Half evaluator. 3 of 6 name all three gold symbols in prose; the other 3 answer from an unrelated subsystem | 3 `unqualified`, 3 `wrong` |
| `vsc-generic-service-shell-consumer` | Unanswerable as scored. Every arm answered `RequestService`, which is defined twice in the corpus, and no resolver may accept an ambiguous name | 6/6 name the gold, none qualify it |
| `vsc-backup-uri-parse-tolerance` | Question is ambiguous. All six answered `restoreRecentlyOpened`, which also rebuilds sessions from disk and continues past unparseable entries; only "silently" and "multi-root" favour the gold, and the gold appeared in just 2 of 6 trials | 0/6 name the gold |

So **none of the seven is a tool-surface gap, and at most one is a retrieval failure.** Five are
defects in the benchmark: two wrong golds, three answer-shape or ambiguity artifacts.

Repairing the two wrong golds and rescoring with the repaired grader — a sensitivity analysis, not
a change to the frozen score — gives 19/30 for `retrieval-mcp`, 18/30 for `zvec-grep`, and 17/30
for `native-control` (mean graded credit 0.747, 0.670, 0.681), with paired discordance of 3:1 and
2:1 in this server's favour. An earlier hand-written rule that credited any answer naming the gold
leaf reported 25/22/22; that rule accepted bare ambiguous names such as `RequestService`, which no
resolver may accept, and it overstated the gap. The grader-derived numbers above supersede it.

The correction does not rescue the instrument. Under corrected gold, 6 of 15 questions are solved
by every arm and 5 by none, leaving 4 that discriminate. The suite must be rebuilt before any
competitive claim, with a development set for tuning difficulty and a held-out set frozen before
the final comparison.

### Compiling a suite before it is used

A suite is not usable until it proves its own gold and its grader, with no model and no network.
`validate_suite.py` is that build step, and it exits nonzero when anything fails:

```sh
python3 experiments/validate_suite.py --questions /path/to/questions.json --corpus /path/to/corpus

# Every pinned suite passes; see Corpora above for which corpus each set requires.
python3 experiments/validate_suite.py \
  --questions experiments/django_heldout_questions.json --corpus ../runs/django-suite/corpus
```

It checks four things, tagged by severity in the output.

**Schema and anchors.** Every question carries `id`, `category`, `set`, `question`,
`expected_json`, a nonempty `rejected_alternates`, nonempty `evidence`, and `author_notes`; ids are
unique; and each evidence anchor is an exact substring of the corpus-relative file it names. An
author who paraphrases a snippet or renames a file fails the build rather than the run.

**Gold truth.** Every gold identity must be a real definition at the path it claims. Where a
question names the helper it is about, in a `helper` field, every claimed caller must really call
it by an independent ripgrep enumeration, and a gold marked `"exhaustive": true` must contain every
caller found that way. Run against the frozen v2 suite with the two caller questions given their
helpers, this reports exactly the defects the audit found by hand: one claimed caller of
`asTextOrError` that calls nothing plus 9 real callers unclaimed, and one claimed caller of
`isSuccess` that calls nothing plus 2 unclaimed. The check that would have blocked the suite is
four lines of set arithmetic against the corpus.

**Answerability.** A gold whose leaf name has namesakes cannot be credited from a bare name, so
the question must ask for a qualified symbol. On the v2 suite this flags 9 questions — including
`vsc-generic-service-shell-consumer`, where all six trials answered `RequestService`, a name the
corpus defines twice, against a question that never asked for a path. A question that spells its
own answer's leaf name is flagged too, and so is a caller question without a `helper` field,
because its gold cannot be verified.

**Grader behaviour**, against synthetic answers only, per question: the canonical gold scores 1,
prose naming every identity scores 1, naming one of *n* scores 1/*n*, a wrong symbol scores 0, a
bare ambiguous name scores 0, and anything listed in `rejected_alternates` scores 0.

Three grader repairs came out of writing this. Prose against a set-valued gold now scores by
recall over the asserted identities instead of returning zero on a type check — that alone was
worth 13 of 42 audited trials. The TypeScript definition index silently dropped every method with
more than one modifier, so `private async request(` was no definition at all; 359 definitions in
the VS Code corpus were missing, including golds that named them. And the Python pass indexed
`def` but not `class`, so a gold naming a class was rejected as undefined. All three are pinned by
tests.

New question fields the validator understands: `helper` (the symbol a caller question is about),
`exhaustive` (the caller set must be complete), and `rejected_alternates` (plausible answers that
must score zero). `acceptable_symbols` is rejected: the frozen grader cannot score an any-of
answer, so a question that would need it is mis-specified.

Two limits of the caller check are worth stating, because they shape how questions must be
written. It enumerates call sites syntactically by name, exactly as the systems under test do, so
a same-named method on another class counts; and it excludes the helper's own defining module, so
a helper whose only callers are its file neighbours cannot be verified and must not be asked as a
caller question.

### The Django suite

`django_development_questions.json` and `django_heldout_questions.json` are the rebuilt instrument
the VS Code audit demanded: 30 development and 30 held-out questions over a 276-file, five-package
subset of Django 5.1.4 (`django/db`, `django/core`, `django/utils`, `django/dispatch`,
`django/apps`), pinned with corpus and suite hashes in `django_suite_manifest.json`. Both sets
compile clean. Each question was authored from source by a separate agent per package, with
evidence anchors and author notes recording why each distractor is wrong; the two sets are
disjoint and balanced across the same seven categories, one null-answer question each.

### Django development pilot: the notation bug that nearly shipped as a win

The development set exists to tune difficulty before the held-out set is spent. Its pilot —
30 questions × 3 arms × 1 repetition, Codex CLI with `gpt-5.6-luna` — aborted at trial 67 of 90 on
a provider usage limit, so the arms are comparable only on the 14 questions where all three
completed. On that subset the frozen score read:

| | native-control | zvec-grep | retrieval-mcp |
|---|---:|---:|---:|
| Frozen resolved correct / 14 | 3 | 2 | **12** |
| Regraded resolved correct / 14 | 14 | 14 | 14 |

A four-fold win for this server, and it was entirely notation. On 11 of the 14 questions every arm
named the same symbol; the arms differed only in how they spelled it. This server's tools print
bare leaf names, so it answered `django/db/models/base.py::from_db`, while grep-and-read arms
answered `django/db/models/base.py::Model.from_db` — the spelling a Python reader writes. The
resolver split written symbols on `::` and `/` but not `.`, so `Model.from_db` failed the
identifier check, parsed as no symbol at all, and scored 0. The grader was measuring which tool
surface the answer came from. `quality_pass.context_segments` now splits on all three separators
and a test pins it.

Under the repaired grader all 67 completed trials of all three arms are correct. The development
suite does not discriminate for this model, so it is not yet a usable instrument, and the held-out
set stays unrun at zero model runs. With quality saturated, the cost columns are correctness-
controlled by construction:

| | native-control | zvec-grep | retrieval-mcp |
|---|---:|---:|---:|
| Input tokens, 14 questions | 2.10 M | 2.14 M | 2.94 M |
| Retrieval bytes | 418 KB | 715 KB | 595 KB |
| Bytes after first hit | 100 KB | 111 KB | 275 KB |
| Tool calls | 45 | 55 | 67 |
| Median calls to first hit | 2 | 2 | 2 |

Every arm reaches the evidence in the same two calls; this server then spends 40% more input
tokens and 2.75× the bytes after first hit to commit to the answer all three already had. That is
the same stopping-criterion problem the VS Code run showed, now visible without a quality
difference to argue about. `runs/.../decision.json` records the run, the defect, and the decision.

Two process notes. One ledger defect was found by disbelieving a result in
this project's own favour, which is the only reason it was found before the held-out set was
spent. And zvec-grep's semantic tool was never called once in 21 trials: offered both, the model
took the managed `rg` every time, as DeepSeek did on the coreutils suite. That is a valid
ecological result — the question is what happens when an agent is handed zvec-grep, not whether
dense retrieval works — and it sharpens the comparison rather than weakening it. This server is
not losing to a better embedding; it is losing to a cheap lexical interface the model knows when
to stop using.

### Closure: a stopping rule is worth more than a better ranker

A suite no arm fails is a poor quality benchmark and an excellent efficiency laboratory: with
accuracy pinned at the ceiling, any context a change saves is context that was never needed. The
saturated Django development set was used that way.

**What the tokens actually buy.** Across the pilot's 67 completed trials, input tokens correlate
with call count at r = 0.74 and with retrieval bytes at r = 0.09. Every arm pays 40–45 k input
tokens *per call*, because the whole transcript is re-sent each turn. Payload size is nearly free;
turns are the currency. This server's overhead was never its 595 KB of tool output — it was making
5.71 calls per question against the native control's 3.18.

**Where the extra turns went.** Reading the ten traces with the largest post-hit consumption and
labelling all 50 post-hit calls by hand:

| Verdict | Calls | Bytes |
|---|---:|---:|
| redundant verification — re-reading source for a row a structural tool already named | 16 | 57 KB |
| repeated retrieval — the same lookup again, or one tool reconfirming another | 13 | 52 KB |
| structural expansion — 90–180-line reads where a symbol's own range would do | 5 | 98 KB |
| rejected call — schema error, see below | 5 | 1 KB |
| necessary | 11 | 131 KB |

39 of 50 post-hit calls were not required to answer the question. The dominant shape is concrete:
`find_callers` returns every caller with its file and enclosing symbol, and the model then opens
each of those files anyway. It does not trust the compact representation enough to close.

The 5 rejected calls are the harness's own fault and are counted here for honesty: the shared
setup prompt tells the model to pass an indexed project name, which zvec-grep and codebase-memory
need and this server's schema rejects, so 2 of 24 trials burned four calls each rediscovering
that. The prompt is identical across arms, so it is not an asymmetry in wording — but it is an
asymmetry in cost, and it should be fixed before the held-out run.

**The intervention.** One `prompt_policy` string, no protocol change, no ranking change. The two
arms in `comparison_systems_closure.json` are byte-identical apart from `id` and this text:

> Stopping rule. Every row this server returns is a verified source fact: it names a real
> definition with its file, and `counts`, `has_more` and the relation labels state how complete the
> listing is. When the rows you already hold contain every fact the question asks for, answer from
> them. Do not read a definition's source to confirm a row that already named it, do not repeat a
> lookup you have already made, and do not reconfirm one tool's rows with another tool. Retrieve
> again only when a required fact is missing, ambiguous, contradicted, or truncated — `has_more`
> true, or a count larger than the rows shown. When you do read source, read the narrowest range
> that carries the missing fact.

It is not "be less curious": every clause names a completeness signal the server already emits.

**Result.** 30 questions × 2 arms × 1 repetition, 60 trials, none aborted:

| | baseline | + stopping rule |
|---|---:|---:|
| Regraded correct / 30 | 29 | **30** |
| Input tokens | 6.73 M | **5.27 M** (−22%) |
| Tool calls | 155 | **87** (−44%) |
| Calls after first hit | 84 | **24** (−71%) |
| Bytes after first hit | 574 KB | **212 KB** (−63%) |
| Median wall time | 30.4 s | **27.3 s** |

Per question the rule is cheaper on 22 of 30 and makes fewer calls on 25 of 30, median −57 k input
tokens. Correctness did not pay for it: the one question the baseline missed
(`dj-fields-instance-store-key-overrides`, where it answered the two *classes* instead of the two
`cache_name` properties) the closure arm got right.

On the 14 questions where the pilot compared all three arms:

| | native | zvec | ours, baseline | ours, + rule |
|---|---:|---:|---:|---:|
| Input tokens | 2.10 M | 2.14 M | 2.80 M | 2.27 M |
| Calls | 45 | 55 | 64 | **33** |
| Bytes after first hit | 100 KB | 111 KB | 234 KB | **82 KB** |

This server now makes the fewest calls and carries the least post-hit context of any arm, and its
token overhead against plain grep-and-read falls from +40% to +8%. Re-running the baseline
reproduced the pilot within 5% (2.80 M vs 2.94 M tokens, 64 vs 67 calls), so the effect is several
times the run-to-run noise.

One caution stands: the rule was measured on a suite where no arm fails, which is exactly where a
stopping rule cannot cost an answer. On a discriminating suite it may, and that is the test the
architecture still has to pass.

### What the residual 8% is, and what it is not

The obvious next move was to shrink `search_concept`, which returns 14–33 KB per call. Measurement
says that would optimise the one term where this server is already ahead.

`end_to_end.py` now reports `context_token_turns`: a payload's tokens multiplied by the number of
model turns that must carry it, computed identically for MCP and native arms. A transcript agent
re-sends every earlier result with every later turn, so a 20 KB result followed by four turns is
not a 20 KB result. On the 14-question frontier:

| | native | zvec | ours, baseline | ours, + rule |
|---|---:|---:|---:|---:|
| Persistent payload load (tok·turns) | 226 k | 390 k | 481 k | **140 k** |

The closure arm carries 38% *less* persistent payload than the native control while still spending
168 k more input tokens. The gap is therefore not the payload; it is the tool surface. Seven tool
schemas are 23,144 B ≈ 5,786 tokens, re-sent on each of the arm's 47 turns ≈ 272 k tokens, against
roughly nothing for a control with no MCP server. That single term over-explains the measured gap:

```text
  tool schemas carried every turn   +271,942
  persistent payload advantage       -85,671
  predicted net                     +186,271
  measured                          +168,098
```

The progressive-disclosure idea was costed before it was built, from the 51 archived
`search_concept` responses. Excerpts are 49% of payload bytes, symbol blocks 27%, and 76% of row
bytes sit in rows 4 and beyond — so withholding them looks attractive until the turn economics are
applied. Withholding rows 4+ saves 1.72% of input tokens; the first gold-path row ranked below 3
in 10 of those 51 responses, so roughly one call in five would need an expansion turn, at 1.98%.
Predicted net: slightly worse, and an order of magnitude inside the ~5% run-to-run floor. No A/B
can resolve that, so none was run and the response shape was left alone. Deduplicating the
identity fields that appear in both the row and its `symbol` block — 9.4% of row bytes — is
likewise real and worth 0.43% of input tokens, which does not justify changing a response
contract.

What does have headroom is the surface itself. `trace_dependencies` was called zero times in 60
trials and costs 1,157 tokens on every turn; only 14% of the schema bytes are prose, the rest is
JSON Schema structure. Removing a tool is not a cleanup, though — it changes a declared treatment
that the navigation study showed mattered for transitive questions — so it is the next
*experiment*, not the next commit. `runs/.../payload-analysis.json` holds the full accounting.

### Does closure survive uncertainty? The hard suite

A stopping rule tested where nothing can be lost is not tested. `django_hard_questions.json` is
30 further development questions authored against an explicit difficulty contract: the first
plausible symbol must be a distractor, or the answer must combine two definitions, or need a
two-hop traversal, or discriminate between namesakes. Difficulty had to come from code structure,
not from vocabulary — the VS Code suite already showed that obfuscated phrasing produces a floor
rather than a frontier. The suite compiles clean and is disjoint from the first development set;
the held-out set was never opened by any author.

`closure_audit.py` is the instrument the run exists for. For every question the treatment loses
and the control wins, it asks whether a gold identity ever appeared in *any* tool result of the
treatment trial:

* `premature_stop` — a required fact was never on screen, so the model answered without
  retrieving it. Where the control did retrieve it, the skipped expansion was demonstrably
  available (`expansion_skipped`).
* `evidence_seen` — everything required was on screen and the answer is still wrong. The stopping
  rule did not cause that.

Result over 60 trials, none aborted:

| | baseline | + stopping rule |
|---|---:|---:|
| Regraded correct / 30 | 30 | 29 |
| Input tokens | 7.15 M | **5.70 M** (−20%) |
| Tool calls | 187 | **101** (−46%) |
| Calls after first hit | 123 | **32** (−74%) |
| Persistent context load | 1.60 M | **0.42 M** tok·turns (−74%) |
| Median wall time | 31.8 s | **27.8 s** |

Fewer calls on 29 of 30 questions, cheaper on 21, median −43.8 k input tokens. **No premature
stops and no skipped expansions.** The single regression is
`djh-orm-internal-manager-fallback-chain`: the closure arm answered `options.py::default_manager`
after one concept search where the control read the source and answered `options.py::base_manager`.
Both gold markers were already on screen, so the rule did not stop it seeing the fact — it stopped
it checking which of two neighbouring properties the question described. That is the honest cost:
roughly 3% of answers for 20% of tokens and 46% of calls, on a suite where one distractor sits
directly beside the answer.

The suite has one clear shortcoming: it does not discriminate the control, which scored 30/30. It
is harder than the first development set and it does separate stopping policies, which is what
this run needed, but a suite that separates *retrieval strategies* still does not exist.

Another ledger defect surfaced here, again by disbelieving a result — this time one against the
treatment. The closure arm appeared to lose a second question by answering
`query.py:Query.combine` with a single colon, which the resolver did not split, though the answer
named both correct symbols. `quality_pass.context_segments` now treats a single colon as a
separator, exactly as editors and grep write it, while a line reference such as `query.py:1234`
still fails to parse rather than resolving to something wrong.

### The regression was ambiguity, not stopping: one clause, re-run

The hard suite's single closure loss was not a missing fact — both candidate properties were on
screen — so the repair had to be narrow enough to leave the closure win intact. One sentence was
added to the policy, and nothing else in either arm changed:

> If multiple visible symbols plausibly satisfy a requested fact, disambiguate between those
> candidates before answering. Do not expand merely to reconfirm a single unambiguous candidate.

Criterion set before the run: restore the lost answer without materially increasing turns.
30 questions × 2 arms × 1 repetition, 60 trials, none aborted, `gpt-5.6-luna`:

| | closure | + ambiguity clause |
|---|---:|---:|
| Regraded correct / 30 | 28 | **30** |
| Tool calls | 100 | **96** |
| Calls after first hit | 33 | **30** |
| Input tokens | 5.62 M | 6.07 M (+8%) |
| `closure_audit.py` regressions | — | **0** |

Both losses came back: `djh-orm-internal-manager-fallback-chain` (`default_manager` → `base_manager`)
and `djh-backends-literal-default-hook-override` (`quote_value` → `prepare_default`), each in the
same number of calls as before. Calls fell by four while quality rose, so the clause is not buying
correctness with turns. Input tokens nevertheless rose 8%, dominated by one trial that spent eight
extra `read_source` calls confirming a caller list; that is reported as a caveat, not as a saving.
The clause is now frozen into `comparison_systems_closure.json` and every later arm carries it.

### A suite that was supposed to separate retrieval strategies, and a validator for it

`django_retrieval_strategy_questions.json` is 12 development questions aimed at capability
boundaries rather than stopping behaviour: caller/callee orientation, two-hop traversal,
container/member distinction, same-name definitions where the path decides, an interface whose two
overrides are textually identical, evidence combined from distant definitions, and a lexical
distractor that outranks the answer.

The earlier suites showed that authoring "hard" questions is how impossible ones get in, so
`tool_reachability.py` adds a compilation property no earlier validator had: **there exists a
supported tool path from an initial retrievable seed to the complete gold, and it executes.** Each
question carries a `tool_path`; the checker runs it against the pinned server and fails when a step
errors, when a declared marker is absent from that step's own output, or when the complete gold
never appears. It also refuses two ways of cheating the property: step 0 may not query a gold
identifier directly, and every later step's arguments must consume a marker an earlier step
produced. All 12 questions pass in 26 tool calls; the first draft did not, which is the point.

### Schema surface: the tax is real, the capability pressure is not

`trace_dependencies` costs 1,158 schema tokens on every turn and had been called zero times in 60
trials, so the open question was whether a rarely used tool earns its schema. Six arms —
the full surface, one leave-one-out arm per structural tool, and a lexical-only arm — over the
12 retrieval-strategy questions, identical corpora, identical frozen policy, a shared five-call
ceiling, `claude-sonnet-4-6`, 72 trials, none aborted:

| arm | correct / 12 | schema tok/turn | schema tok·turns | input tokens | persistent payload |
|---|---:|---:|---:|---:|---:|
| full surface | 11 | 5,786 | 208 k | 341 k | 72.3 k |
| − `inspect_symbol` | 11 | 5,032 | 171 k | 306 k | 64.4 k |
| − `find_symbol` | 11 | 5,008 | 180 k | 315 k | 58.4 k |
| − `find_callers` | 11 | 4,406 | 150 k | 294 k | 55.1 k |
| − `trace_dependencies` | 11 | 4,628 | 162 k | 299 k | 60.0 k |
| lexical only | 11 | **1,717** | **62 k** | **277 k** | **52.0 k** |

No structural tool enabled a single unique solve. The full surface was the most expensive arm on
every axis, and the one question every arm missed is the same one in all six — the ambiguous
manager property, whose gold was re-checked against source and is correct — so that failure
measures commitment, not tool surface. `trace_dependencies` was never called even on the six
questions authored to require two caller hops: the model reached those golds with paired
`search_concept` calls, or `search_exact` plus a narrow `read_source`.

The tempting conclusion is to cut the surface, and it is not supported. What the run actually shows
is that **tool reachability is not tool necessity**. The validator proves a structural route to the
gold exists; it says nothing about whether a lexical seed plus reads also reaches it inside the
budget, and here it did, 11 times out of 12. Removing a tool on this evidence would be justified by
an absence of pressure, not an absence of capability. The next compilation property is necessity:
admit a question only when no lexical-seed-plus-read path reaches its complete gold within the call
budget. `runs/django-schema-ablation-claude-20260911/decision.json` holds the full accounting.

Two harness notes. `end_to_end.py` could not read a Claude transcript at all — it knew the OpenCode
and Codex event files, and the Claude client has no side channel, its stream-json output *is* the
transcript — so every payload metric for these arms would have silently read zero and made the
MCP arms look free. It now parses that stream, matching `tool_result` payloads back to the
`tool_use` that requested them and deduplicating the usage object Claude repeats across the blocks
of one API response; `schema_ablation.py` takes tokens from that normalised stream rather than from
a client-specific usage envelope, because Claude reports fresh, newly cached, and replayed prefix
separately while Codex reports one total. That is another entry in the [defect ledger](#harness-defect-ledger), found by disbelieving a zero. Separately, the first attempt at this matrix aborted at trial 27 of 72 on a
provider usage limit; `runs/django-schema-ablation-20260910/partial-state.json` records why those
trials must not be compared.

### Necessity, not reachability: a gate, a suite, and the first tool that pays

The schema ablation left one question: is a structural tool ever *needed*, or merely available?
`lexical_oracle.py` is the gate that question needs. It runs a deterministic, model-free crawl over
one question using only `search_concept`, `search_exact` and `read_source`, starting from the
question text, forbidden from ever querying a gold identifier directly, harvesting definition names
and paths from each result and following them, and stopping the moment every gold identity is
visible. Nothing about it is a proof — given enough calls a crawl reads the whole repository — so
the claim is budgeted and operational: within a budget at least as large as the treatment arm's,
this crawl did or did not recover the complete gold.

Pointed at the existing suites at a budget of 8, it explains the ablation result directly:

| suite | dissolved by the lexical crawl |
|---|---:|
| Django development (30) | 12 |
| Django hard (30) | 14 |
| retrieval-strategy (12) | **6** |

Most of those fall to a *single* `search_concept` call on the question text. Half of the suite
built to separate retrieval strategies was never about retrieval strategy at all.

**What lexical results do not encode.** A grep hit carries a path and a line; it does not carry the
definition that encloses the line. `django_necessity_questions.json` is ten questions built on that
asymmetry: each names a helper behaviourally and demands *every* enclosing definition outside its
module that calls it, as `path::name`, exhaustively. Five to six callers spread over four to five
files means a search-and-read arm pays one read per file to name them, while one structural call
certifies the set with its enclosing symbols and a complete count. All ten compile clean, all ten
are structurally reachable in two calls, and all ten survive the oracle at budget 8.

**The pilot.** Three arms, identical corpora and frozen policy, ten-call ceiling,
`claude-sonnet-4-6`, 30 trials, none aborted:

| arm | correct / 10 | graded credit | turns | calls | input tokens | persistent payload |
|---|---:|---:|---:|---:|---:|---:|
| full surface | 8 | 9.20 | 36 | 41 | 368 k | 121 k |
| lexical only | 7 | 9.27 | 64 | 77 | 679 k | 256 k |
| lexical + `find_callers` | 8 | 9.23 | **33** | **29** | **298 k** | **71 k** |

Adding one relational primitive to the lexical surface cut calls by 62% and input tokens by 56%.
Priced properly — schema tokens times turns against turns avoided times the cost of a turn —
`find_callers` pays 45,556 tokens of schema and returns 280,356 in avoided turns, a net of
**+234,800**. That is the first structural tool in this project to clear its own schema tax on
measured evidence, and `schema_ablation.break_even` now computes that ledger for any arm pair.

Two honest qualifications. Graded credit is a wash (9.20 / 9.27 / 9.23), so the win is cost, not
quality; the correct count moved by one question, which n=10 cannot support. And the full surface is
*not* the best arm — lexical plus `find_callers` beats it on every cost axis at the same correct
count, so the other three structural tools still do not pay, even on a suite about relations.

One question looked like a cost of over-listing and was written up that way; it was the wrong
diagnosis and the correction is the more useful result. On `djn-field-cache-writer-callers` both
structural arms scored 0.40 against the lexical arm's 1.00, which was attributed to `find_callers`
returning thirteen candidate rows across two descriptor classes. Auditing the tool sequences shows
**neither failing arm called `find_callers` on that question at all** — both stopped after two
source reads and enumerated what those two files happened to show. Pooling every exhaustive caller
trial in this pilot and the tool trial, 42 in total, splits cleanly:

| | trials | trials with an omission | mean calls | mean credit |
|---|---:|---:|---:|---:|
| used `find_callers` | 26 | **0** | 2.9 | 0.97 |
| did not | 16 | **5** | 7.5 | 0.86 |

Every omission in the pooled set came from skipping the tool, not from reading its output badly,
and `answered_without_evidence` flagged all five. So the indicated fix is not a grouped or
checksummed response shape — that hypothesis is retired — it is that an exhaustive question
answered from partial file coverage is a closure failure, which the safety column already catches.

**Three defects in the gold verifier, found while building the suite.** Candidate golds were
cross-checked against the structural index and disagreed on five of ten helpers; the index was
right every time. `audit_failures.enclosing` returned the nearest preceding `def` indented less
than the call, which credits a *closed* nested helper for any call that follows it; tightening the
indentation bound on every shallower statement fixed that, and then credited the enclosing class
whenever a decorator or a wrapped signature sat between the call and its `def`, because those lines
are not statements in the parent block. Third, `true_callers` counted `Field.set_cached_value()`
written inside a comment as a call site, which would have demanded that an exhaustive gold name a
caller that does not exist. All three are pinned by `test_audit_failures.py`, and all four existing
suites still compile clean under the corrected verifier, so no shipped gold depended on the bugs.
Those are three more [ledger](#harness-defect-ledger) entries, found by disbelieving agreement between
two tools rather than a single result.

### Safety first: a permanent premature-stop line

Closure buys context by answering sooner, and the dangerous failure is not a wasted turn but a
confident answer given while a required fact was never on screen. `end_to_end.py` now reports two
columns for every arm, on every run, forever: `answered_without_evidence` counts trials where some
gold identity appeared in no tool result at all, and `wrong_without_evidence` is the subset that
was also graded wrong. Visibility is textual, so the count can understate but never invent.

Applied backwards over the closure work, it agrees with `closure_audit.py`: 0 on every arm of both
the hard-suite and ambiguity runs. On the necessity suite it immediately earned its place — the
lexical-only arm answered without evidence three times (two of them wrong) against one for each
structural arm. An efficiency change that moves this number is a regression whatever the token
columns say.

### Every remaining tool on trial

The rule is now explicit: **a tool stays on the default surface only if it has an oracle-resistant
task class and saves more context than its schema costs.** `find_callers` earned that. The other
three were put through the same procedure, which needed three additions to `validate_suite.py`
first: `hops: 2` verifies a transitive caller gold one independent ripgrep hop at a time,
`caller_key` lets a dict gold assert a container without that container being demanded to call
anything, and `definition_set` checks a namesake enumeration against the definition index rather
than trusting it. The oracle also learned `--with-tool find_callers`, so a class claimed for
another tool must survive a crawl that can already enumerate callers.

`django_tool_trial_questions.json` is ten questions in three claimed classes — container plus
exhaustive callers for `inspect_symbol`, two-hop transitive callers for `trace_dependencies`,
namesake enumeration for `find_symbol`. All ten compile, all ten are reachable, and all ten survive
the baseline-surface crawl. Four arms: the frozen baseline, and the baseline plus one tool.
40 trials, `claude-sonnet-4-6`, none aborted:

| arm | correct / 10 | turns | calls | input tokens | payload |
|---|---:|---:|---:|---:|---:|
| baseline | 7 | 34 | 37 | 317 k | 139 k |
| + `inspect_symbol` | 8 | 40 | 40 | 458 k | 242 k |
| + `trace_dependencies` | 8 | 35 | 38 | 375 k | 202 k |
| + `find_symbol` | 9 | **31** | **31** | **285 k** | **105 k** |

The ledgers, priced the same way `find_callers` was:

| tool | calls made | turns saved | schema cost | turn saving | net | own class |
|---|---:|---:|---:|---:|---:|---|
| `inspect_symbol` | 5 | −6 | 30,140 | −68,639 | **−98,779** | 2/3 → 3/3 |
| `trace_dependencies` | **0** | −1 | 40,523 | −10,720 | **−51,243** | 1/3 → 2/3 |
| `find_symbol` | 6 | +3 | 24,102 | +27,534 | **+3,431** | 4/4 → 4/4 |

The decisive line is the first column. `trace_dependencies` was never called — on a suite whose
two-hop questions were authored for it, which survive a crawl that already has `find_callers`, with
the tool sitting in the schema. The model answered them by issuing `find_callers` repeatedly. That
is the third experiment in a row in which this tool was offered and not used, and it carries the
largest schema of any tool at 1,158 tokens per turn. It comes off the default surface provisionally;
`--tools` still exposes it.

The other two were left explicitly undecided, because the honest reading of n=10 at one repetition
is that a one-question quality difference is not a result. The follow-up was narrow and it was run.

### The replication, and the frozen surface

Three repetitions, three arms, only the seven questions in the two claimed classes, 63 trials:

| arm | correct / 21 | credit | calls | input tokens | payload |
|---|---:|---:|---:|---:|---:|
| baseline | **21** | 1.000 | 44 | 412 k | 69 k |
| + `inspect_symbol` | 21 | 1.000 | 42 | 472 k | 84 k |
| + `find_symbol` | **18** | 0.929 | 40 | 407 k | 56 k |

| tool | calls made | turns saved | schema cost | net | unique solves | regressions |
|---|---:|---:|---:|---:|---:|---:|
| `inspect_symbol` | 1 / 21 | −3 | 42,950 | **−67,782** | 0 | 0 |
| `find_symbol` | 14 / 21 | +2 | 40,430 | **−24,781** | 0 | 1, in all three repetitions |

The baseline answers both claimed classes perfectly, so the earlier signal favouring these tools
(baseline 7/10 at one repetition) was noise. `inspect_symbol` was called once in 21 trials and
enabled nothing. `find_symbol` was genuinely used, fourteen times, and still lost: a negative
ledger and a repeatable regression on a question the baseline got right every time. Neither
produced a unique solve on the class authored specifically for it.

So the surface is frozen, and for the first time the study changes the product rather than
describing it. With no `--tools` and no `--profile`, the server now exposes
`search_exact`, `read_source`, `find_callers`, `search_concept` — `config::DEFAULT_SURFACE`, pinned
by a stdio test. The other three are un-defaulted, not removed: naming them in `--tools` works, and
`--profile D` is unchanged so every command recorded in this study still replays exactly.

## The held-out comparison

The set stayed sealed through every development cycle in this file: 30 questions, zero model runs,
its hash pinned in `django_suite_manifest.json` and absent from all 40 prior run manifests. It was
opened once, after the retrieval architecture, the closure policy and the default tool surface were
all frozen, and it will not be used again.

Three arms, one corpus copy each, `claude-sonnet-4-6`, 30 questions × 3 arms, 90 trials, none
aborted: the client's own `Read`/`Grep`/`Glob` as the native control, zvec-grep 0.2.2 with its
managed `rg`, and this server on its compiled four-tool default with the frozen policy.

| | native | zvec-grep | retrieval-mcp |
|---|---:|---:|---:|
| Correct / 30 (frozen grader) | 28 | **29** | **29** |
| Graded credit | 0.933 | 0.994 | 0.967 |
| Input tokens | 1.15 M | 1.00 M | **764 k** |
| Output tokens | 1,572 | 1,068 | **621** |
| Tool calls | 147 | 78 | **78** |
| Calls after first hit | 96 | 39 | 42 |
| Calls to first hit | 1.80 | 1.30 | **1.20** |
| Persistent payload (tok·turns) | 175 k | 227 k | **150 k** |
| Retrieval bytes | 223 k | 364 k | 254 k |
| Median wall time | 14.5 s | 11.7 s | **10.6 s** |
| `answered_without_evidence` | **0** | **0** | **0** |

The pre-registered outcome was quality non-inferior to zvec with less context, and that is what the
run shows: the same 29 of 30 and the same 78 calls as zvec, for 24% fewer input tokens, 34% less
persistent payload and a second less per question; against the native control, 34% fewer input
tokens on half the calls. Paired against zvec the two arms are discordant on exactly two questions,
one each way. That is a tie, and it should be read as a tie — this design cannot resolve a
one-question quality difference. The cost differences are large and consistent: cheaper on 22 of 30
questions, equal or fewer calls on 20 of 30.

**One more harness defect, found in our own loss.** The one question this server missed,
`dj-fields-pickle-reducer-callables`, was answered with both correct symbols — as a keyed object
rather than a list. The grader scored that 0.0 while scoring free prose naming the same two symbols
1.0, which is grading notation rather than retrieval, the oldest defect in this project. It is now
repaired and pinned by a test, and the repair was applied symmetrically and re-run over all three
arms: retrieval-mcp 29 → 30, native 28 → 29, zvec unchanged at 29.

**Post-hoc sensitivity analysis, not a second result.** The frozen-grader row above is the result.
Repairing the notation defect and regrading all three arms identically gives retrieval-mcp 30/30,
native 29/30, zvec 29/30 — reported because concealing a known measurement defect is worse than
disclosing one found late, and labelled as sensitivity because it was computed after the seal broke.
It does not change the conclusion either way: quality indistinguishable from zvec, and the context
saving is the result. The set is spent; nothing further is tuned against it.

### Reproducing it

No corpus is shipped, so `reproduce_heldout.py` rebuilds it: a blobless sparse clone of the pinned
Django revision, the five scoped packages, and a hard failure unless the result matches the
fingerprint in `django_suite_manifest.json` byte for byte. It was run against a fresh clone and
returns `038e9fdd…` over 276 files, the same corpus every number in this file was measured on, and
it then prints the exact validate/prepare/run/score commands. It calls no model.

### The habit that made the numbers trustworthy

The sixteen entries in the [defect ledger](#harness-defect-ledger) run in both directions — some
flattered this server, some penalised it, and the last was found inside its own single held-out loss.
None was found by auditing on a schedule. Every one came from the same rule, which is the methodological
claim this project would actually defend:

> **Every surprising result triggers an evaluator audit before an architectural interpretation.**

A stopping rule that looked like a free 22% saving, a treatment loss that looked like a premature
stop, an arm whose payload metrics read zero, two tools that agreed on nine helpers and disagreed on
five, a 0.40 that looked like over-listing: each was a measurement bug, and each would have become a
published architectural finding under the opposite habit.

## Three index changes, one survivor

After the surface was frozen, driving the server against an awkward repository suggested three
index changes: put the comment block above a definition into its concept chunk, chunk markdown
files by heading into the same index, and record calls written inside Rust macro arguments
(`assert!(target())`), which parse as token trees and had been invisible. All three are cheap,
plausible, and were implemented before anything was measured. Two of them were wrong.

`study_a.py` settles the first two offline: identical corpora, symbol-level gold, lexical ranker,
raw question text, no model calls. Three binaries — `v0.1.1`, the three-change build, and a
candidate with markdown and macro calls removed — over six question sets:

| Suite | n | recall@5 before | recall@5 after | MRR before | MRR after |
|---|---:|---:|---:|---:|---:|
| coreutils authored | 18 | 7 | **10** | 0.246 | **0.474** |
| VS Code mechanical | 200 | 52 | **108** | 0.137 | **0.456** |
| VS Code authored | 15 | 3 | 3 | 0.167 | 0.173 |
| Django held-out | 29 | 17 | 17 | 0.510 | 0.510 |
| Django development | 29 | 13 | 13 | 0.343 | 0.343 |
| Django hard | 30 | 11 | 11 | 0.317 | 0.317 |

The gain is entirely the doc-comment chunk, and its shape is exactly what the project's first
finding predicts: a description question is a vocabulary problem, and the vocabulary sits in the
comment, not the body. Django does not move because Python docstrings already sit *inside* the
definition and were always in the chunk; the mechanical suite is derived from doc comments, so it
is partly circular and the authored coreutils suite is the honest number.

Markdown chunking could not be measured on any suite, because **not one of the eleven measured
corpora contains a single markdown file** — a fact worth knowing before believing any claim about
it. The upstream coreutils checkout does: 662 Rust files and 76 markdown files, with the same 18
graded questions. There, markdown chunks took 14 of 180 result slots and pushed a gold out of the
top ten — `diag-char-span-callers` rank 7 → absent, `diag-floor-same-name` 2 → 3, MRR 0.474 →
0.457, recall@10 12 → 11. No question improved. Prose about code outranks the code.

Macro-argument calls are a genuine recall gain and still a loss. Over 40 sampled symbols on the
coreutils corpus the rule added 361 caller rows, of which **347 were assertions inside test
functions** and 14 were production call sites. Both graded Rust caller questions exclude test code
by their own wording, so on the only questions that grade this relation the change adds nothing but
bytes: `find_callers("char_span")` went from 2 rows and 5,956 bytes to 5 rows and 8,734 bytes, and
`find_callers` is the one structural tool with a positive token ledger to protect.

So the doc-comment chunk stayed and the other two were reverted. Artifacts, binary hashes, corpus
fingerprints and per-question ranks are in `runs/concept-chunk-ab-20260912/`. The measurement costs
about 30 seconds of CPU and no model calls, which is the whole point: two of three changes that
looked obviously good were negative, and nothing in a code review would have said so.

## The v0.2.0 performance study

The doc-comment chunk was measured on the corpora and questions that were already lying around,
which is enough to accept or reject a change and not enough to claim an advantage. This study asks
the harder question — does a ranking gain become an agent-level advantage against zvec-grep — on
material neither the change nor the tuning ever saw. The Django held-out set is spent and is not
reopened.

Everything is declared before anything runs. `runs/perf-v020-20260912/preregistration.json` holds
the arms, the corpus fingerprints, the binary hashes, the buckets, the success target and the
decision rules, written before the first arm was run.

**Three fresh corpora**, cut by `cut_corpora.py` from the upstream checkouts, each scope disjoint
from every spent suite: `cu-text` (173 Rust files: coreutils text utilities and uucore features,
with the files the authored coreutils suites already ask about removed), `dj-forms` (88 Python
files: forms, template, views, http, urls, middleware, conf — disjoint from the held-out scope of
db/core/utils/dispatch/apps) and `vs-editor` (333 TypeScript files: `src/vs/editor/{common,browser}`,
disjoint from the platform corpus).

**Thirty authored questions**, ten per corpus, compiled clean by `validate_suite.py`, in seven
buckets chosen so that four of them are shapes zvec-grep ought to win: `vague_conceptual`,
`terminology_mismatch`, `api_semantics`, `generic_name`, `cross_file_ownership`,
`direct_caller_lookup` (exhaustive, gold verified by independent enumeration) and `exact_control`.
Conceptual questions are written from behaviour rather than paraphrased from a doc comment, which
would have flattered the very change under test; each question's `author_notes` records that.

**The success target, fixed in advance**: quality at or above zvec-grep, input tokens at least 20%
below it, fewer calls to first sufficient evidence, and no arm-wide or per-bucket increase in
`answered_without_evidence` or `wrong_without_evidence`. v0.2.0 must additionally not be worse than
v0.1.1 on any primary measure. If it is not better end to end, that is the published result.

### Layer 1: offline ranking, no model calls

`study_b.py` is study_a's multi-arm sibling: several arms, one corpus, symbol-level gold, per-bucket
scoring. Its one methodological requirement is symmetry — an arm is never scored on the `symbol`
block this server happens to return while the competitor is scored on bare paths. Every returned
`(path, line)` from every arm, including zvec's, is resolved to its enclosing definition by the same
hardened attribution function the caller golds are verified with, and identity is scored against it.
zvec-grep 0.2.2 has no JSON output mode, so its agent-markdown hit headers are parsed and the parser
is pinned by a test against captured output; its index is built on a copy outside the corpus under
test, and the corpus is fingerprinted before and after.

Thirty questions, raw question text, `limit` 10:

| Arm | MRR | recall@1 | recall@3 | recall@5 | recall@10 |
|---|---:|---:|---:|---:|---:|
| retrieval-mcp v0.1.1 | 0.126 | 2 | 6 | 6 | 8 |
| **retrieval-mcp v0.2.0** | **0.234** | **6** | **7** | **8** | 10 |
| zvec-grep 0.2.2 | 0.165 | 3 | 6 | 7 | 10 |

The coreutils result generalises: v0.2.0 beats v0.1.1 on `cu-text` (MRR 0.337 against 0.100) and on
`vs-editor` (0.114 against 0.029), and is identical on `dj-forms`, exactly as the mechanism predicts
— Python docstrings already sat inside the definition. The comparison that matters is the third row:
**v0.1.1 was behind zvec-grep on this suite and v0.2.0 is ahead of it**, by 42% on MRR and twice as
often at rank 1, while tying at recall@10. On economics the two are not comparable in kind:
retrieval-mcp answers warm in 1.3–2.6 ms with no index build, zvec-grep needs a 4.0–7.6 s index and
395–443 ms per warm query, and returns leaner rows (1.7–2.2 KB against 3.2–3.9 KB).

One bucket result is worth more than the table. `terminology_mismatch` scores **zero for every arm**,
including zvec: on raw question text, no ranker here bridges a deliberate vocabulary gap. That is the
project's first finding restated — the expensive work is query formation, and only an agent loop does
it — and it is why this layer is a screen, not the claim. Pooled bucket n is 3–6, so only the three
buckets with n = 6 support any statement at all.

### Layer 2: four arms, 120 trials, `claude-sonnet-4-6`

`comparison_systems_perf_v020.json` declares `native-control`, `zvec-grep`, `retrieval-v011` and
`retrieval-v020`. The two retrieval arms are byte-identical except for the pinned binary — same four
visible tools, same closure stopping rule copied verbatim from the held-out systems file, no semantic
backend on either, so the only difference in the treatment is the doc-comment chunk. `comparison_runner.py`
takes a per-system `server` binary and records its sha256 in the prepared manifest, which is what
makes a two-version comparison possible at all; `end_to_end.py` reports every primary and safety
measure per bucket as well as per arm. Every arm's corpus copy fingerprints identically to its
source, and the zvec install comes from the cached 0.2.2 tree because this run had no registry access.

30 questions × 4 arms × 1 repetition, 30-call ceiling, $6.38 of model spend:

| | native | zvec-grep | v0.1.1 | **v0.2.0** |
|---|---:|---:|---:|---:|
| Correct / 30 | 27 | 25 | **28** | 27 |
| Graded credit | 0.92 | 0.85 | **0.93** | 0.92 |
| Input tokens, total | 1.286 M | 1.215 M | 783 k | **779 k** |
| Input tokens, median trial | 34.5 k | **21.2 k** | 24.1 k | 23.0 k |
| Tool calls, median | 4 | 2 | 2 | 2 |
| Calls to first sufficient evidence | 1.63 | 2.48 | **1.20** | 1.37 |
| Persistent context, tok·turns median | 3,996 | 3,154 | 3,133 | **2,726** |
| Answered without evidence | 1 | 2 | **0** | **0** |

**Against zvec-grep the pre-registered target is met on every criterion.** Quality 27 against 25 with
paired discordance 3:1, input tokens 36% lower in total, first sufficient evidence in 1.37 calls
against 2.48, and zero unsupported answers against two. One honest qualification: zvec's *median*
trial is the cheapest of the four at 21.2 k tokens — its total is carried by a heavy tail, so the
context advantage is about failure modes, not about the typical question.

**Against v0.1.1 the pre-registered target is not met, and that is the result.** v0.2.0 loses one
question (27 against 28) and reaches first evidence in 1.37 calls against 1.20; it carries 13% less
persistent context and spends 5% fewer tokens at the median, both inside the run-to-run floor. The
offline ranking gain — MRR 0.126 → 0.234, recall@1 2 → 6 — did **not** convert into an agent-level
advantage. The most economical explanation is the one this project already measured: the model
rewrites the question into code vocabulary before it searches, and a ranker improvement measured on
raw question text is partly redundant with work the model was already doing. `terminology_mismatch`
is the sharpest version of that: every arm scored zero on it offline, and every arm answered all six
of those questions in the agent loop.

The single v0.2.0 loss was audited before it was interpreted, per the habit. Every gold identity was
retrieved — `unretrieved_identities` is empty — and the model named the third caller as
`EnterOperation::_goodIndentForLine` where the corpus defines `TabOperation::_goodIndentForLine`.
That is `wrong_level`, the container-versus-member error the `inspect_symbol` A/B could not fix
either, scored at 2/3 credit. It is not a retrieval failure and not a grader defect.

Two of the 120 trials failed on infrastructure — an unreachable API after ten client retries with
zero tokens spent, and one 600 s client timeout — and were re-run with identical settings; the
originals are quarantined under `run-vs-editor/failed-trials/` and the substitution is recorded in
`run-vs-editor/repair.json`.

What survives: **on three corpora and a suite none of the systems had seen, both versions of this
server beat zvec-grep 0.2.2 on quality, total context and speed to first evidence, and the
doc-comment change is a ranking improvement that an agent loop does not need.** Artifacts in
`runs/perf-v020-20260912/`.


## Native-system comparison

`comparison_runner.py` holds OpenCode and DeepSeek V4 Flash constant across six arms: a native
codebase-tool control, `retrieval-mcp` profile D, zvec-grep 0.2.2 search plus managed rg,
codebase-memory 0.10.2's analysis profile, and two bundles that attach zvec-grep and
codebase-memory together — one with no added guidance, one with an explicit routing policy in the
prompt. This estimates the marginal effect of adding each MCP bundle to a real agent environment;
it is not a common-schema retriever comparison. Native OpenCode tools, MCP tool descriptions, and
system strategies are part of the declared treatments.

`comparison-systems-v2` gives every arm an `upstreams` array, so one arm can attach several MCP
servers behind a single gate process. The gate routes each tool name to exactly one upstream,
refuses a name exposed by two servers, concatenates every upstream's `instructions` so the model
receives each server's own policy, and meters one shared call and response-byte budget across all
of them. Per-upstream call counts land in `upstream_calls`. `prompt_policy` is the routing-guidance
channel: it is prepended to the question, so it is covered by `prompt_sha256` and never written
into the corpus under test, which must stay byte-identical across arms.

The bundle arms ask whether a purpose-built retrieval server beats two off-the-shelf servers plus a
paragraph of routing policy. `bundle-unguided` versus `bundle-routed` isolates the paragraph;
`bundle-routed` versus `retrieval-mcp` is bought-and-glued versus built.

Two vendor constraints shape the arms. zvec-grep binds one daemon per listen address, so every
upstream that needs one receives its own ephemeral loopback port. codebase-memory allows exactly
one daemon per account, bound to one cache directory: all codebase-memory arms therefore share
`{shared}/cbm-cache` and separate their graphs by project name, which is the arm id and is stated
in each arm's prompt. No other codebase-memory session, including an editor or agent with the MCP
server attached, may run during preparation or trials; a foreign daemon holding a different cache
fails the arm with an explicit cache-mismatch error rather than silently degrading. Never point a
run at the operator's default cache: a trial that calls `list_projects` would see unrelated indexed
repositories and could retrieve outside the corpus under test.

The first four-arm run exposed two routing defects rather than a ranking: the model never called
`find_callers`, answered mixed discovery/structure questions with exact search alone, and no arm
solved transitive questions. `retrieval-mcp` therefore gained an explicit server-level routing
table, tool descriptions that name the tool to prefer instead, and `trace_dependencies` for bounded
multi-hop callers/callees. Runs before and after that change use different arm definitions and are
not matched trials; compare them as separate generations, not repetitions.

Preparation is model-free. It copies the pinned corpus once per arm, installs the pinned zvec-grep
package inside the workspace, builds warm indexes, records versions and hashes, and checks that all
four source copies have the same state-excluding fingerprint. At execution, zvec-grep receives an
isolated ephemeral loopback endpoint so an unrelated daemon cannot occupy its default port. The
control receives an empty MCP configuration. Administrative zvec-grep tools are filtered from model
sessions. The generic gate
records every attempted MCP call and full response while enforcing identical call and response-byte
budgets.

```sh
python3 experiments/comparison_runner.py prepare \
  --source-root /path/to/pinned-corpus \
  --workspace /path/to/new-comparison-workspace \
  --semantic-command '[\"/path/to/ollama_backend\"]'

# Plan only: 22 questions × 4 arms × 1 repetition = 88 trials.
python3 experiments/comparison_runner.py run \
  --workspace /path/to/new-comparison-workspace \
  --output /path/to/new-comparison-plan \
  --semantic-command '[\"/path/to/ollama_backend\"]' \
  --client command \
  --agent-command '[\"python3\",\"{experiments}/opencode_wrapper.py\",\"{model}\",\"{mcp_config}\",\"{prompt_file}\",\"{run_dir}\"]' \
  --model deepseek/deepseek-v4-flash --variant high --dry-run

# Charges/quota begin here. Use another new output directory and a valid DEEPSEEK_API_KEY.
python3 experiments/comparison_runner.py run \
  --workspace /path/to/new-comparison-workspace \
  --output /path/to/new-comparison-results \
  --semantic-command '[\"/path/to/ollama_backend\"]' \
  --client command \
  --agent-command '[\"python3\",\"{experiments}/opencode_wrapper.py\",\"{model}\",\"{mcp_config}\",\"{prompt_file}\",\"{run_dir}\"]' \
  --model deepseek/deepseek-v4-flash --variant high --allow-model-usage

python3 experiments/analyze_comparison.py /path/to/new-comparison-results \
  --output /path/to/new-comparison-analysis.json
```

The system-specific absolute root is necessarily different because indexes and the native control
are isolated; pairing uses question ID, question hash, and repetition rather than the full prompt
hash. OpenCode's native codebase tools remain available in every arm; web/external retrieval and
source modification are prohibited and recorded as contamination. Analysis retains strict JSON/path
correctness, the source-resolved quality score, graded credit, failures, calls, bytes, and latency.
The manifest pins `deepseek/deepseek-v4-flash`, the `high` reasoning variant, and usage approval.

### Question set

`comparison_questions.json` is the frozen, pre-registered workload: the twelve reviewed
`v2_questions_draft.json` tasks plus ten authored in `comparison_questions_new.json` (ids prefixed
`comp-`). Every gold answer is verified against the pinned corpus by reading source and by
independent ripgrep; `test_comparison_questions.py` re-checks each anchor, stratum, and null answer
without touching the indexes under test.

Distribution by category and stratum:

| Category | Count | What it tests |
|---|---:|---|
| conceptual_lookup | 7 | behavior described without the target identifier |
| symbol_resolution | 4 | same-named items; two are absent-target (`null`) answers |
| exact_lookup | 3 | literal diagnostic origin and named definitions |
| direct_caller_lookup | 2 | distinct callers |
| transitive_blast_radius | 3 | ordered chains, one crossing two files |
| mixed_discovery_structure | 3 | discover from behavior, then resolve or trace |
| **pre_cutoff** | 6 | files a model may have memorized |
| **post_cutoff** | 16 | files a model cannot know; retrieval is required |

The expansion deliberately rebalances toward description-led and absent-target cells, which the
prior set under-represented, and adds a cross-file chain. Sixteen of twenty-two targets are
post-cutoff, so correctness provably requires retrieval rather than priors. Efficiency is scored
per-answer: token and call savings are only meaningful gated on resolved correctness, because the
prior study found the dominant failure is ~27% cheaper in calls than success.

### Freeze gate

Before any model run, review the answer key, not the model's behavior. `test_comparison_questions.py`
proves mechanical grounding (anchors exist, paths and strata match, no answer leakage, null answers
have no candidate definition), but a human still owns semantic correctness:

1. Each gold symbol names the intended target, and each question's wording is unambiguous without
   naming it.
2. Each `null` answer genuinely has no candidate in the file, under a reading that includes tests.
3. The category and stratum labels are the ones the analysis will report.
4. The description-led questions are solvable from the description alone — not only by guessing the
   module.

Only after that review is the set frozen for a comparison run.

## Three-project study

For post-hoc within-ModelShare comparisons, run `python3 experiments/research_review.py /path/to/project-results --output /path/to/new-research-analysis.json`. This preserves frozen scores, reports supplemental payload matches, retains every paired observation, separates all-eligible and both-matching subsets, and includes per-question/category mean/median savings and leave-one-question-out checks. Input run records, transcripts, server logs, and questions are hashed. It makes no model calls and does not infer verification from source-read overlap. Repetitions remain repeated observations of questions, not independent tasks.

`project_questions.json` freezes 36 questions over committed ModelShare, pig, and Sigil source: two per category per repository. Each question has a source-evidence checklist and a typed gold answer. `snapshot_projects.py` exports committed `.rs`/`.py` blobs only; uncommitted edits, untracked files, and non-source files such as credential files, databases, datasets, and documentation are not copied. This extension allowlist is not a secret scanner; review committed source for embedded credentials before sharing it with a model provider. The snapshot manifests retain revisions and per-file hashes. Corpus contents are never executed.

```sh
python3 experiments/snapshot_projects.py --output /path/to/new-project-snapshots
python3 experiments/run_projects.py --snapshots /path/to/new-project-snapshots \
  --output /path/to/new-project-plan --phase full --dry-run

# Charges/quota begin here; use separate new directories.
python3 experiments/run_projects.py --snapshots /path/to/new-project-snapshots \
  --output /path/to/new-project-pilot --phase pilot
python3 experiments/run_projects.py --snapshots /path/to/new-project-snapshots \
  --output /path/to/new-project-results --phase full
python3 experiments/analyze_projects.py /path/to/new-project-results
```

The pilot is 12 trials and excluded from the main results. The full study is 432 trials: 36 questions × A/B/C/D × 3 repetitions. It uses Claude Code subscription login, `claude-sonnet-4-6`, medium effort, $1 CLI limit per trial, 300-second trial timeout, and warm semantic caches. There is no batch-level billing cap: nominal per-trial limits total $432 for the main study, plus $12 for its pilot. Repositories run sequentially (ModelShare, pig, Sigil); each repository's task/profile/repetition order is shuffled with seed 42. No concurrent model sessions are introduced. Three consecutive unsuccessful trials stop the runner; retained results are not overwritten or automatically retried.

The first project pilot used `json-answer-v1` and exposed correct fenced JSON preceded by prose. Before the main study, grading was frozen at `json-answer-v2`: one fenced JSON answer may have surrounding prose, while `format_correct` remains false. Gold answers were not changed based on model output. Pilot scores stay under their original grader and are not pooled with the main study. Only the structured answer is graded; contradictions in surrounding prose need manual review. There are no partial-credit or LLM-judge scores.

`analyze_projects.py` writes `analysis.json` alongside the run directories, with repository/profile totals, category/profile summaries, matched category comparisons, raw per-repository analysis, model IDs, initialization contamination checks, and client-reported API-equivalent cost. Inspect failure/exclusion counts before accuracy. Macro category summaries have equal task counts when complete, but repetitions and questions sharing function families are not independent observations. Cross-repository latency comparisons are particularly sensitive to time-of-day and cache state; prioritize within-repository paired results. This study tests task-dependent utility, not causal superiority or a universal ideal first tool.

## Stratified suite

`stratified.json` contains 24 source-checked questions, four in each category:

| Category | What it tests |
|---|---|
| Exact lookup | Literal diagnostics and a named configuration default |
| Conceptual lookup | Behavior descriptions without the target identifier |
| Symbol resolution | Same-named methods, concrete implementations versus trait declarations |
| Direct caller lookup | Distinct callers, not occurrence counts or transitive ancestors |
| Transitive dependency / blast radius | Bounded multi-hop paths, tool impact sets, and a destructor path |
| Mixed discovery + structure | Discover a routine from behavior, then identify callers or a dispatch path |

The corpus is the real retrieval server and persistent adapter, not the two-file sample. `prepare_stratified.py` copies only 13 hash-pinned Rust files and Cargo manifests. Inline tests remain as distractors; prompts specify when to exclude them. README files, experiments, questions, and gold answers are not included. File hashes and source evidence anchors guard against silent answer-key drift. Changing the corpus requires reviewing answers and explicitly repinning the suite, not automatically accepting new hashes.

```sh
python3 experiments/prepare_stratified.py --output /path/to/new-stratified-suite

# Plan only: 24 questions × 4 profiles × 3 repetitions = 288 trials.
python3 experiments/benchmark.py \
  --root /path/to/new-stratified-suite/corpus \
  --questions /path/to/new-stratified-suite/questions.json \
  --server /path/to/retrieval-mcp/target/release/retrieval-mcp \
  --semantic-command '["/path/to/retrieval-mcp/target/release/examples/ollama_backend"]' \
  --client claude --claude-auth subscription --model claude-sonnet-4-6 \
  --profiles A B C D --repetitions 3 --seed 42 --semantic-cache warm \
  --timeout 300 --tool-timeout 120 --max-budget-usd 1 \
  --output /path/to/new-stratified-plan --dry-run
```

Review the plan and quota/budget before executing. Remove `--dry-run` and use a different new output directory to run. A $1 per-trial setting across 288 trials is **not a $1 batch limit**; nominal aggregate limits sum to $288. It is not a hard billing guarantee, and subscription usage follows the client's quota rules. There is no automatic large-run launch or retry. One repetition is a 96-trial pilot if desired.

The subscription preset preserves keychain login without `--bare`, disables ordinary settings, hooks, auto memory, discovered CLAUDE.md files and skills, and retains the strict MCP configuration and per-profile allowlist. Managed policies can still apply. Verify each transcript's initialization tool list and authentication/model before accepting a run. The API preset remains the default and still uses `--bare`.

### Scoring and interpretation

Each new question requests a single `{"answer": ...}` JSON object. `expected_json` compares typed values, object key order is ignored, and explicit `answer_set` tasks compare unique set members without regard to order. Ordered call chains stay ordered. Under `json-answer-v2`, one fenced JSON object can pass content correctness while failing `format_correct`, including when surrounded by prose. The v2 study runs keep that grader. Runs on the v2 corpus use `json-answer-v3`, frozen before the first comparative trial there: a model answering questions about code quotes source, and under v2 a quoted `rust` block made the reply unparseable and discarded a correct answer. V3 reads the one fenced block that is an `{"answer": ...}` object, ignores blocks that are not, fails when two such blocks appear, and still requires a bare object for `format_correct`. The prompt asks for exactly that object. No score under an earlier grader is recomputed. Prose without a fenced structured answer, multiple fenced answers, duplicate keys, missing or extra members, and unsupported output shapes fail. Legacy `expected` questions keep their original strict text grading. The old smoke-run scores are not rewritten.

Category labels, evidence, and expected answers are evaluator-only; all profiles receive identical question text. Neither the prompt nor correctness grader mandates a tool. In particular, a mixed task **invites** semantic discovery followed by structure but can be solved through grep/read. The observed `semantic_to_structural` pairs require a successful, nonempty semantic response before the structural request, with separate overlap flags. This is a temporal proxy, not proof that one result caused the next call or resolved the same symbol.

Analysis now includes `category_profiles` (correctness and format counts, first-tool histograms, call/latency/volume/token distributions, fallback/redundancy counts and uncertain-source verification) and `category_comparisons` (paired differences, separately restricted to both-correct pairs). Category, grading version, corpus, model/client and prompt must match for a paired comparison. Failures/exclusions are counted explicitly; do not interpret completed-only accuracy without its denominator. Cache token fields are kept separate, never synthesized when absent.

Pre-registered questions to inspect: does exact-first dominate literal tasks; does semantic-first help conceptual discovery; do structural queries help caller tasks but hurt simple lookups; can the agent compose methods on mixed tasks; and does it verify uncertain structural evidence? These are hypotheses, not labels defining a universally correct route. Include total calls as well as reduced grep/read counts: substituting several graph calls for one grep is not automatically an improvement.

This is a larger **single-repository** study, still with only four distinct tasks per category. Some tasks share implementation families across categories. Repetitions measure route variability, not independent repository/task samples. No significance tests or generalization claims are produced. Add independently authored tasks and a second repository after this pilot, and review the answer key before running models rather than fitting it to their responses.

## Run the harness

Build the server and persistent semantic adapter first. Start Ollama with `nomic-embed-text` installed. Choose an explicit Claude model identifier; the harness passes it through unchanged. Claude's preset uses `--bare`, which requires API/provider credentials and does not use subscription OAuth/keychain login. It also uses `--tools ""`, `--strict-mcp-config`, and an allowlist for the profile's retrieval tools. This keeps built-in retrieval, other MCP servers, and auto-loaded repository instructions out of the trial. Managed client policies can still affect a run. See the official [headless](https://code.claude.com/docs/en/headless) and [CLI](https://code.claude.com/docs/en/cli-reference) documentation.

```sh
cd /path/to/retrieval-mcp
cargo build --locked --release --bin retrieval-mcp --example ollama_backend

# Use a new output path outside the indexed repository.
python3 experiments/benchmark.py \
  --root /path/to/retrieval-mcp/experiments/sample_repo \
  --questions /path/to/retrieval-mcp/experiments/questions.example.json \
  --server /path/to/retrieval-mcp/target/release/retrieval-mcp \
  --semantic-command '["/path/to/retrieval-mcp/target/release/examples/ollama_backend"]' \
  --model YOUR_EXPLICIT_MODEL_ID \
  --output /path/to/new-benchmark-run \
  --profiles A B C D --repetitions 3 --seed 42 \
  --semantic-cache warm --timeout 300 --tool-timeout 120 --max-budget-usd 1

python3 experiments/analyze.py /path/to/new-benchmark-run
python3 experiments/analyze.py /path/to/new-benchmark-run/retry-cap-1-D/server.jsonl
```

Add `--dry-run` to write the schedule, prompts, per-run commands, and configuration without invoking a model or MCP server. Remove it and choose a **different new output directory** to execute. Actual runs consume the selected client's model quota/API budget. `--max-budget-usd` is per Claude trial, not a total batch cap; repetitions multiply the number of calls. The harness never writes credentials or alters global client configuration.

The example contains three small, independently answerable questions over a two-file Python repository. Questions have unique safe IDs, a `question` string, and an optional `expected` string. Expected answers are compared with the final answer after trimming outer whitespace; no fuzzy model grader is used. Questions and output must be outside the indexed repository, preventing the model from retrieving the answer key. Broader task ideas appear below.

### Artifacts and controls

The output directory contains `manifest.json` (repository content fingerprint, optional Git revision, server binary hash, model/client settings and question set) and a seeded, shuffled `schedule.json`. Every task/trial/profile has its own directory:

```text
retry-cap-1-D/
  prompt.txt                identical question/instructions across profiles
  mcp.json                  isolated MCP configuration with profile and cache path
  tools.json                actual server tool schemas from preflight
  server-info.json          actual MCP initialization result
  preflight-events.jsonl    preflight/warm-up calls, excluded from model metrics
  warmup.json               semantic warm-up result (C/D, warm mode)
  transcript.jsonl          raw client stdout, including model/tool messages
  client-stderr.log         client diagnostics
  server.jsonl              model-session retrieval events
  answer.txt                extracted final answer, if present
  run.json                  outcome, usage, correctness, wall time, errors
```

The harness refuses to overwrite an output directory, uses fresh client sessions, and fingerprints source before and after each trial. It stops remaining trials if source changes. The fingerprint covers ripgrep-enumerated files including hidden files, excluding Git metadata, `target`, `.retrieval-mcp`, and ignored files. Files added to ignored areas are outside this check. The corpus is not copied or OS-sandboxed; use a frozen checkout for reproducibility.

`--semantic-cache warm` (default) shares a cache and warms C/D via a separate preflight server before timing the model. Warm-up outputs stay outside the model transcript and `server.jsonl`. `--semantic-cache cold` gives each trial an empty cache and performs no embedding warm-up. Both modes still scan source per query. The structural index remains cold per model session; first structural-call latency includes indexing. A custom semantic backend must honor `RETRIEVAL_SEMANTIC_CACHE_DIR` for these controls to apply.

Timeouts kill the client process group, preserve partial transcripts/logs, and mark the trial unsuccessful. Client errors, missing final results, malformed transcripts, source changes, and observed unexpected retrieval tools are recorded. Failed, contaminated, incomplete-telemetry, or source-changed trials are excluded from matched savings calculations. The scripts do not infer tokens from bytes: `usage` retains the client-reported object. Full transcripts are private experiment data and may contain source text.

### Other clients

Use `--client command --agent-command '["/absolute/path/to/wrapper", "{model}", "{mcp_config}", "{prompt_file}", "{run_dir}"]'` for another client or a different authentication mode. Commands are argument arrays executed without a shell; placeholders are literal substitutions. The prompt is also sent on stdin. The wrapper must launch a **fresh** session, connect the generated retrieval MCP server, restrict or accurately expose other retrieval tools in its transcript, and emit JSONL to stdout.

The analyzer recognizes Claude stream JSON events and Codex `--json` events. A generic wrapper may emit `{"type":"result","result":"final answer","usage":{},"is_error":false}` after its full model/tool transcript. For Codex, translate the generated MCP JSON into per-invocation Codex configuration and retain its original events; command execution, file changes, web searches, and other MCP servers are marked as contamination when observed. There is no built-in Codex launcher or guarantee that a custom wrapper exposes every tool: the wrapper owns isolation, budgets, and event fidelity. See [Codex non-interactive output](https://developers.openai.com/codex/noninteractive).

## Read the analysis

`analyze.py` emits JSON. Each session reports `first_tool`, `tool_sequence`, per-tool counts, switches, errors, incomplete calls, retrieval volume, and these conservative proxies:

- `fallback_count`: switches to a different retrieval method after the most recently completed prior call returned an error or zero results. Successful advanced-to-read transitions are not automatically called failures.
- `duplicate_request_count`: repeated successful tool/argument requests after an earlier identical request completed. Default arguments and equivalent relative paths are normalized. Changes to the repository can make a repeat legitimate.
- `redundant_read_count`: successful source reads whose entire returned range was covered by earlier successful source reads. Partial overlap is insufficient.
- `repeated_location_calls`: returned ranges already covered by earlier retrieval locations, including other methods. Excerpt truncation means this is a location proxy, not proof that identical code was shown.
- `advanced_followups`: for each successful nonempty structural/semantic retrieval, count later exact searches and reads, plus source reads that overlap its evidence. All later calls are counted, so windows for successive advanced calls can overlap.

Starts are ordered by their per-session sequence, not completion time. Strict event-file order determines whether a response preceded a later request, including events with identical millisecond timestamps; concurrent source reads are not credited as verification. Missing/duplicate/malformed events produce warnings. Sessions are never joined into a synthetic call chain.

Matched comparisons include A→B, A→C, A→D, B→D, and C→D for the same question, repetition, model/client, source fingerprint, prompt hash, server binary, and cache mode. Positive `search_exact_saved` / `read_source_saved` means fewer total low-level calls in the variant; incorrect answers remain labeled, and `both_correct` supports a correctness-controlled comparison. Post-advanced counts and total matched differences answer different questions. Neither proves causality or that a particular route was optimal.

## Verify without model charges

```sh
cargo build --locked --bin retrieval-mcp --example ollama_backend
cargo test --locked --all-targets
python3 -m unittest discover -s experiments -p 'test_*.py' -v

# Opt-in local embeddings: tests persistence, reuse and deletion through MCP.
cargo test --locked --test mcp_stdio real_ollama_semantic_over_stdio -- --ignored
```

The Python integration test uses a deterministic fake client and semantic fixture against the real Rust MCP server under all four profiles. Its token fields and answers are synthetic test data, not empirical model results. Rust tests cover cache round trips, content/model/repository invalidation, deletion, corruption, and symlink rejection.

## Run controls

1. Select a fixed repository revision, model/version, prompt, task ID, and sampling configuration. Keep the checkout unchanged during each run.
2. Run each task in a fresh agent session for A, B, C, and D. Randomize profile order and repeat trials. Give the same task prompt without prescribing a tool sequence.
3. Configure the same semantic backend/model in C and D. Record its version and index settings. Retain the server version, dependency lockfile, and tool schemas/descriptions with results.
4. Give each run a distinct `--run-id task-ID-profile-trial` and `--log-file /absolute/events.jsonl`. Record model transcripts and client token usage separately.
5. Control the client's other retrieval routes. Claude Code/Codex may have shell, read, search, or other MCP tools already enabled; record or restrict them consistently. A server profile cannot disable a client's built-in tools. Keep MCP tool discovery settings consistent too.
6. Decide whether you measure cold or warm semantic retrieval. B/D lazily index on the first structural call. The persistent Ollama adapter reuses document vectors but still scans source and embeds queries. Record the distinction rather than calling either operation a pure database lookup.

New sessions must have the intended profile's tool list. The server rejects calls to omitted tools, so cached client schemas cannot silently enable another profile.

## Measures

| Question | Evidence / computation |
|---|---|
| Was the answer correct? | Compare the final answer with an independently verified expected answer or runnable check on the pinned revision. |
| How many input tokens? | Client/model usage records, separating cached and uncached tokens. Bytes are not tokens. |
| How many tool calls? | Count `tool_start` by run/session; count success/error/cancelled outcomes using matching `tool_end`. |
| Which tool was first? | Smallest start `sequence`, not earliest completion timestamp. Concurrent calls can finish out of order. |
| How much time? | Per-call `latency_ms`, including errors; separately measure task wall time. Summing overlapping calls overstates wall time. |
| How much retrieval? | Sum `retrieval_bytes`, `response_bytes`, and `result_count` separately. Source reads count returned lines; search tools count hits. MCP response bytes include the text compatibility copy. |
| Did retrieval reduce follow-ups? | Compare later call counts and correctness across matched task/profile trials. Account for task difficulty and failures. |
| Was the first method unsuitable? | Human/evaluator labeling of the question, returned evidence, subsequent calls, and final answer. There is no universal correct first tool. |
| Did the model verify uncertain structural evidence? | Pair successful structural end-event locations/coverage with subsequent successful `read_source` ranges in the same run. Require the read to start after the structural response; overlap is an observable proxy, not proof of understanding. |

Example with `jq` (optional, not a server dependency):

```sh
# Counts by selected method; --log-file contains only invocation events.
jq -s '[.[] | select(.event == "tool_start")] | group_by(.tool) | map({tool: .[0].tool, calls: length})' events.jsonl

# First selected method per session, based on invocation order.
jq -s '[.[] | select(.event == "tool_start")] | group_by(.session_id) | map(min_by(.sequence) | {run_id, session_id, tool, arguments})' events.jsonl

# Completed-call volume and latency; keep errors in the dataset.
jq -s '[.[] | select(.event == "tool_end")] | {completed:length, errors:map(select(.error != null))|length, payload_bytes:map(.retrieval_bytes // 0)|add, response_bytes:map(.response_bytes)|add, summed_latency_ms:map(.latency_ms)|add}' events.jsonl
```

## Task set to prepare

Use a small real repository with independently verified answers. Keep questions and answers outside the indexed root to avoid answer leakage. Include these ten task types; instantiate concrete names, expected answers, and evidence on the chosen revision before scoring:

1. Find where a specific error message originates and explain the condition that triggers it.
2. Follow a configuration value from parsing to its effect on request handling.
3. Locate a declaration whose name also appears in comments and unrelated strings.
4. Distinguish two same-named methods and identify a real caller of one implementation.
5. Trace a cross-file function call through an import or alias and verify the binding.
6. Find behavior from a natural-language description that shares few identifiers with the code.
7. Determine why a retry stops, including a boundary case covered by a test.
8. Determine whether a suspected unused function is actually referenced indirectly.
9. Inspect functionality in a language the structural index does not support.
10. Ask for callers involving dynamic dispatch or macro expansion and evaluate whether the model verifies the index's uncertainty.

These are task templates, not scored benchmark results. Start with correctness plus first-tool choice, calls, bytes, and wall time. Add automated judgment of routing quality only after the transcripts show a useful, reproducible labeling scheme.
