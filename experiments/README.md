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
| 13 | Does the v0.1.2 ranking gain reach the agent? | [No, and it still beats zvec](#layer-2-four-arms-120-trials-claude-sonnet-4-6): offline v0.1.2 goes from behind zvec to ahead (MRR 0.126 → 0.234 vs 0.165), but end to end on 120 trials it ties v0.1.1 while both beat zvec-grep 28–29/30 against 25 at 36% fewer total tokens |
| 14 | Is the 9 KB of advertised output schema a token tax? | [No — the client never forwards it](#layer-3-the-schema-diet-and-the-saving-that-was-not-there): a pre-registered ≥15% context cut came out at −0.1%, and the same measurement showed this server's model-facing surface is 1,450 tokens *smaller* than zvec's, not larger |
| 15 | Does an enclosing-container line fix wrong-level answers? | [Undecided, and it found a bug](#layer-4-the-container-line-and-the-bug-it-was-hiding): no wrong_level error occurred in 36 trials, but caller rows were naming local consts as callers, and fixing that cut source reads 66% |
| 16 | Was that the fix or the feature? | [The fix](#layers-5-and-6-isolating-the-feature-from-the-fix): isolated, the container fields move nothing and the attribution guard cuts source reads 74%, calls 30%, total tokens 39.5% and the worst trial 81%, at identical correctness |
| 17 | Do the remaining backlog candidates earn their keep? | [Three of four do not](#four-backlog-items-measured): test down-ranking helps inside the suites and hurts outside them, a snapshot cache buys 5% of wall time for the worst defect class, concurrency was already sound; payload de-duplication measured −52.8% offline and is pre-registered |
| 18 | Does de-duplicating the caller payload reach the model? | [Yes, on Codex](#layer-7-the-de-duplication-run-on-the-other-client): context_token_turns -18.6% median and -32% on per-question medians, input tokens -11.1%, quality identical at 16/18, and 3.1x more call sites fit under the response cap |
| 19 | Does the index really work in five more languages? | [Yes, after five defects](#adding-five-languages): pre-registered audit of Go, Java, C, C++ and JavaScript against an independent reader — 99/100 definitions found, caller precision 0.92–1.00, recall 0.93–1.00, with constructor calls and C++ reference accessors fixed on the way |

**The result in one line.** On a sealed 30-question held-out set, this server matched zvec-grep at
29/30 on the same 78 tool calls while spending 24% fewer input tokens and carrying 34% less
persistent context, with no unsupported answers by any arm.

**Why, in one line.** Not a better ranker: model-side vocabulary translation, cheap lexical
retrieval, bounded source verification, one relational primitive, and a stopping rule — everything
else was removed after it failed to pay for itself.

**The last thing learned, in one line.** *Correct evidence reduces recovery turns.* The largest
efficiency win measured after the freeze came from a row that had been telling the truth about a
call site — not from compressing a payload, enriching a result, or adding retrieval machinery.

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

Running alongside all of it: twenty-three defects in the measuring apparatus, several of which had already
produced convincing results. A stopping rule that looked like a free 22% saving, a treatment loss
that looked like a premature stop, an arm whose payload metrics read zero, a 0.40 that looked like a
noisy tool, and — in the held-out run itself — this server's only loss, which had actually named both
correct symbols. The habit that caught them is the one methodological claim worth repeating: **every
surprising result triggers an evaluator audit before an architectural interpretation.**

What survived is what was left after each hypothesis and each tool was made to earn its place.

The post-freeze work added one principle to that. Four optimisations were proposed on plausible
accounting — markdown in the concept index, macro-argument callers, a schema diet, an
enclosing-container line — and all four were measured and rejected. The one change that paid was a
defect: caller rows had been naming local bindings as callers, and the agent had been reading source
to recover from it. Correcting the row cut source reads 74%, calls 30% and total context 39.5% at
identical correctness. **Correct evidence reduces recovery turns**, and that is a cheaper lever than
any of the four features it was found underneath.

## Harness defect ledger

The harness is the second experimental subject. FortyForty defects in it have produced or nearly
produced believable false findings, and they run in both directions: some flattered this server,
some penalised it, one was found inside its own single held-out loss, one would have made every
brace-language caller question unauthorable, one scored three arms to zero on questions they had
answered exactly right, twice, one survived its own repair by moving up one scope level, and the
seven newest before it were found by an audit built to admit five
new languages, by the release that followed it and by the Go pilot that ran on them — five in the
evaluator, two in the server. The seven newest were found by taking the harness to a
Linux-kernel corpus: two in the evaluator's reading of C, one in the server's, one in the offline
ranking instruments, one in the chunk-diet study that followed, and two while authoring the kernel
agent study — including the only one so far that reached beyond its own run, since the same leak
sat in all 738 archived Codex trials, two introduced by that leak's own repair, and one that had been charging a shell arm for evidence it had plainly seen. Each is
pinned by a test. This table is the authoritative list; prose below refers to it rather than to
ordinals.

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
| `credit()` scored a bare list naming exactly the gold identities as zero against a single-key gold object | Three arms failing both coreutils caller questions, reading as a structural-retrieval weakness where the grader was judging the container |
| `enclosing()` implemented only its Python branch, and its first brace-language replacement named frames from call syntax | On Rust and TypeScript corpora `true_callers()` returned nothing, so every correct caller gold failed validation as unverifiable; the first fix then attributed call sites to `Ok`, `Err` and, worst, to a real function defined elsewhere — a caller set that looks plausible and is fiction |
| `find_callers` reported no call site for any constructor in JavaScript, TypeScript or C++ | `new Table(rows)` is `new_expression`, not a call expression, so "who constructs this class" answered with the factories that mention it and none of the code that builds it |
| The C++ symbol index skipped every reference-returning accessor | `const BlockHandle& metaindex_handle() const {` wraps its name in a `reference_declarator`, so LevelDB's accessors were no definitions at all |
| `enclosing()` named a frame for C macro blocks, initialiser lists and one-line definitions wrongly | Redis' `TEST("...") { ... }` credited calls to `TEST`, LevelDB's `cache_(NewLRUCache(entries)) {}` credited them to the namespace, and three one-line Redis wrappers were credited to nobody at all |
| `true_callers()` counted declarations and annotations as calls | A C prototype, a Java interface signature, a `.d.ts` method signature and LevelDB's `EXCLUSIVE_LOCKS_REQUIRED(mutex_)` each demanded a caller no system reports; `WriteBatchInternal::SetSequence(batch, seq)` was then read as a prototype and lost real callers |
| The documentation check ran the quickstart against whatever `retrieval-mcp` was on PATH | A two-day-old `cargo install` copy answered on the developer machine and the check passed, while CI — which has none — failed on seven consecutive pushes with an empty result; the check was validating a binary that was not the build under test |
| `quality_pass.definitions()` indexed C call sites and plain bindings as definitions | `if (!ReadBlock(rep_->file, ...)) {` claimed ReadBlock was defined at its own call site, and `const both = context.options[0] === "both";` became an answerable identity that no structural index defines |
| A caller set named identities its own grader cannot distinguish | Four Go methods called `GetRequestMetadata` on four receivers in one file collapse to one `path::name` gold entry, so all three arms enumerated them correctly and all three scored 0.571 — every arm docked for being right |
| Grader read `Server.method` but not `(*Server).method` | A Go caller list spelled the way Go spells it scored 0.33 - two misses and two extras - and the arm that wrote the source's own notation lost two trials to notation, not retrieval |
| Oracle counted a helper named inside a format string as a call | An exhaustive gold demanding `server.go::register` as a caller of `RegisterService`, when the line is `s.printf("RegisterService(%q)", …)` |
| Oracle refused any definition whose name is a control word in another language | `func (tw *storeTxnWrite) delete(...)` owned nothing, so every call inside a Go method named `delete` disappeared from the gold and the arms that found it were scored as inventing it |
| Two-hop gold expanded through a namesake on the first hop | The leasing `acquire` pulled a rate limiter's callers into the gold; all three arms scored 0.60 on a question they answered as asked |
| Two-hop question left it open whether a direct caller counts as reached in two hops | The gold took the union, one arm read the difference, and two cells moved on a reading rather than on retrieval |
| A caller question excluded "the file that defines it" while that file's test twin called the helper | Every arm skipped `http_util_test.go` when told to skip `http_util.go` - 14 of 19 missing identities in one study, 6 of 6 for one arm - and the loss read as a retrieval difference |
| A caller question scoped its exclusion by "module" while the gold counted a caller in the helper's own directory | `cu-callers-02`'s own repair: all three retrieval trials found `parse_signed_num.rs::parse_count`, excluded it because it sits in `features/parser/`, and scored 0.667 three times against the native arm's 1.000 — a replicated, entirely false reading that the structural surface is worse at exhaustive caller questions |
| `true_callers()` counted a helper named on a C block comment's continuation line | Linux `mm/vmpressure.c` writes ` * shrink_node() just adds reclaimed pages` inside a function body, and the line-local `#`/`//` test cannot see a comment opened on an earlier line, so an exhaustive gold demanded a caller that is prose; kernel `mm` caller recall read 0.870 where it is 0.889 |
| `enclosing()` read a C pointer return type as the frame | `struct folio *folio_walk_start(struct folio_walk *fw,` made `folio` the caller of everything in that body — the shape most of the kernel writes a definition in — and mm caller precision read 0.976 where it is 1.000 |
| The index read a C iteration macro as a definition | `for_each_online_node(nid) { ... }` parses as a function definition whose declarator is `(nid)`, so `nid` was a symbol and every call in the loop named a loop variable as its caller — the same wrong-row shape whose repair for local bindings produced the largest post-freeze efficiency win, in the construct 21,136 of Linux's 34,657 `.c` files write |
| `study_a` and `study_b` graded gold in four languages only | `.rs`, `.py`, `.ts`, `.tsx` — so every question in the Go etcd suite was ungradable, `study_a` died inside `statistics.fmean` with "requires at least one data point", and a whole corpus would have been dropped from a registered comparison while the failure read as a broken corpus rather than a blind instrument |
| `quality_pass` could not index a C function whose parameter list wraps | The kernel writes most long signatures across lines, so golds naming them looked undefined and `validate_suite.py` refused a correct question |
| `comparison_runner` dropped every directory named `target` at any depth | Linux's `scripts/target`, `drivers/target` and `include/target` vanished from both arms' corpus copies — a Rust build-artifact rule silently deleting kernel subsystems, and the fingerprint would have recorded the mutilated tree as the corpus |
| Codex trials read the operator's home directory | `codex exec --ignore-user-config` covers Codex's own config and not `~`, so all 7 trials of the first Linux launch — and all 738 archived Codex trials before them — opened `~/.agents/skills/retrieval-mcp/SKILL.md` as their first command: a routing guide for the four tools under test, written by this project, read by both arms from outside the corpus. The treatment arm got a manual for its own tools; the control spent a call reading about tools it does not have |
| A single-use refresh token copied into every trial's credential home | The isolation fix above gave each trial its own `CODEX_HOME` with a copy of `auth.json`; the first token refresh invalidated all 125 remaining copies, so the second Linux launch died at trial 41 on six consecutive 401s, and the operator's own credential had to be re-issued. Read as an arm failure rather than an auth failure, it would have been six trials of "the client aborted" attributed to whichever arm drew them |
| The contamination detector read a ripgrep pattern as a path | `rg '/gfs2/' fs` and `rg '(^|/)(pids|cgroup)'` tokenise as absolute paths, so two clean native trials were flagged as having read outside the corpus. `analyze_comparison.eligible` drops any flagged trial, so a guard against contamination would have quietly deleted honest work from an arm — the flag now requires the path to exist on the machine |
| The evidence test scored a tool's output and discarded its request | `sed -n '1200,1260p' kernel/time/timer.c` names the gold path in the command, and ripgrep given one file does not repeat the path on each match, so 17 correct native answers on the kernel run were recorded as answered without evidence against the MCP arm's 7 — the one safety column where the structural surface appeared to win, and the repair takes it to 10 and 7 with all ten correct |

The habit that found them is in [The habit that made the numbers trustworthy](#the-habit-that-made-the-numbers-trustworthy).

## Where things live

One flat directory had grown to 120 files, so the data moved out of the instruments' way on
2026-09-16. Nothing was renamed and nothing was deleted:

| Path | Holds |
|---|---|
| `experiments/*.py` | the instruments, one concern each, plus `experiments/fixture_backends.py`, the scripted agent and semantic backend the harness tests drive |
| `experiments/suites/` | question sets and their manifests, including `experiments/suites/stratified.json` |
| `experiments/systems/` | arm definitions: one file per study, differing only in `id` and `server` where a study says so |
| `experiments/tests/` | the test modules; discovery is unchanged, `-m unittest discover -s experiments -p 'test_*.py'` |
| `experiments/competency_repo/`, `experiments/sample_repo/` | the two synthetic fixture repositories |

Archived run artifacts under `runs/` name the pre-move paths - `experiments/<suite>.json` rather
than `experiments/suites/<suite>.json` - because a registration is a record of what was run and is
not rewritten. Every such file is still one directory away under the same name.

### Why the 45 scripts stay flat

They cannot be foldered as they are. Every instrument is run as `python3 experiments/<name>.py`,
which puts `experiments/` on `sys.path` and is why `import benchmark` resolves; 35 of the 45 import
a sibling and 32 are imported by a sibling or a test, so only `make_mechanical_suite.py` and
`notation_diagnostic.py` would survive a move. Copying `end_to_end.py` one directory down and
running it fails on `ModuleNotFoundError: No module named 'quality_pass'`. Subdirectories would
therefore need either a `sys.path` shim in every script or a package conversion that turns every
documented command into `python3 -m experiments.<name>` and invalidates the command recorded beside
every archived run. Neither is worth a tidier listing, so the roster is documented instead.

| Role | Scripts |
|---|---|
| The instruments the current protocol uses | `experiments/validate_suite.py`, `experiments/cut_corpora.py`, `experiments/study_a.py`, `experiments/study_b.py`, `experiments/lexical_oracle.py`, `experiments/language_audit.py`, `experiments/comparison_runner.py`, `experiments/end_to_end.py`, `experiments/regrade.py`, `experiments/check_docs.py` |
| Shared modules, imported rather than run | `experiments/benchmark.py` (25 importers), `experiments/quality_pass.py` (13), `experiments/audit_failures.py`, `experiments/analyze.py` |
| Clients, gates and fixtures a run launches | `experiments/codex_wrapper.py`, `experiments/opencode_wrapper.py`, `experiments/comparison_gate.py`, `experiments/policy_gate.py`, `experiments/warm_semantic.py`, `experiments/fixture_backends.py` |
| Suite and corpus authoring | `experiments/make_reformulations.py`, `experiments/make_mechanical_suite.py`, `experiments/prepare_stratified.py`, `experiments/reproduce_heldout.py`, `experiments/snapshot_projects.py` |
| Superseded generations, kept because archived runs were produced by them | the factor/competency protocol in [FACTOR_PROTOCOL.md](FACTOR_PROTOCOL.md) - `experiments/competency_runner.py`, `experiments/factor_runner.py`, `experiments/plan_factors.py`, `experiments/score_competency.py`, `experiments/analyze_factors.py`, `experiments/query_pathology.py`, `experiments/notation_diagnostic.py`; the three-project study - `experiments/run_projects.py`, `experiments/analyze_projects.py`, `experiments/research_review.py`; the a-series navigation studies - `experiments/study_a1.py`, `experiments/study_a2.py`, `experiments/study_a3.py`; and the one-off readouts `experiments/audit_answers.py`, `experiments/closure_audit.py`, `experiments/failure_modes.py`, `experiments/schema_ablation.py`, `experiments/tool_reachability.py`, `experiments/analyze_comparison.py` |

One script is referenced by nothing at all - no importer, no test, no document, no archived run:
`experiments/tool_suitability.py`. It stays anyway, decided 2026-09-16: deleting one orphan buys a
listing one line shorter, and every other file here is load-bearing for some archived run, so
nothing in this directory is removed on tidiness grounds.

## Corpora

No suite validates against an upstream checkout. `corpora/` holds the three checkouts the scoped corpora were cut from; the pinned corpus each question set was compiled against lives beside its run artifacts, and pointing a tool at the wrong one produces confusing `gold is declared exhaustive but omits N callers` failures rather than a clean error.

| Question set | Pinned corpus | Files |
|---|---|---|
| `django_*_questions.json` (all seven) | `../runs/django-suite/corpus` | 276, scoped to `django/db`, `core`, `utils`, `dispatch`, `apps` |
| `experiments/suites/comparison_questions.json`, `experiments/suites/v2_questions_draft.json` | `../runs/projects-v2-suite/coreutils/corpus` | 673 |
| `../runs/vscode-platform-suite/authored-questions*.json` | `../runs/vscode-platform-suite/corpus` | 472 |
| `experiments/suites/stratified.json` | `../runs/stratified-v1-suite/corpus` | 15 |
| v1 ModelShare / pig / Sigil | `../runs/projects-v1-suite/*/corpus` | — |

Scope, upstream commit, and corpus hash for the Django suite are in `experiments/suites/django_suite_manifest.json`; for coreutils, in [CORPUS_V2.md](CORPUS_V2.md). `experiments/suites/comparison_questions.json` predates `validate_suite.py` and does not pass it — it was frozen under the earlier human-review gate described in [Freeze gate](#freeze-gate), and is kept as recorded rather than retrofitted.

## Offline work: no Claude calls

The next study's [factor-separation protocol](FACTOR_PROTOCOL.md) separates tool availability, first-tool policy, and syntax help. `plan_factors.py` generates balanced prompts and one-factor contrasts offline; it is not a model runner and does not resume historical trials.

```sh
cargo build --offline --locked --bin retrieval-mcp
cargo test --offline --locked --all-targets
python3 -W error::ResourceWarning -m unittest discover -s experiments -p 'test_*.py' -v
python3 experiments/audit_answers.py /path/to/project-results --output /path/to/new-answer-audit.json
```

The fake agent and semantic backend in `experiments/fixture_backends.py` exercise real MCP transport and all four profiles without inference. Rust tests cover bounded reads, path escapes, lexical pagination, structural ambiguity, subprocess failures, and embedding-cache persistence with synthetic vectors. The live Ollama test is ignored by default. Offline builds require dependencies already cached.

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

### The ranker bake-off, and where the semantic win actually lives

**Description questions are a vocabulary problem, not an embedding problem.** A model-free ranker
bake-off on 18 gradable coreutils questions put dense retrieval well ahead of BM25: recall@5 of
12/18 semantic and 13/18 hybrid against 6/18 lexical. On a separate authored VS Code suite the same
three rankers ran twice — once on the raw question, once on a frozen one-shot rewrite of it into
code vocabulary. Rewriting moved BM25 from 3/15 to 9/15 at recall@5 (MRR 0.167 → 0.484) while dense
moved 2/15 to 4/15 (0.144 → 0.273), so reformulated BM25 beat dense on every metric, reformulated
or not. The expensive semantic work pays off in *query formation*, not in document ranking, which
is why `lexical` is the default ranker and embeddings are an optional backend.

**Grep-first habits are model-specific.** Running the same coreutils suite inside a real agent loop,
DeepSeek V4 Flash scored 20/22 while rarely calling the semantic tool at all. Retrieval-engine
differences were marginal in a tool-rich agent; corpus naming conventions dominated. Conclusions
from an offline ranker bake-off do not survive contact with a different model's habits, which is
why every finding in this record names the model it came from.

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

`experiments/systems/comparison_systems_three.json` is that three-arm subset of `experiments/systems/comparison_systems.json`, with the
zvec install pinned to a local package file so preparation needs no registry. `codex_wrapper.py`
drives Codex CLI and `opencode_wrapper.py` drives OpenCode; both translate their client's event
stream into the harness transcript shape and keep the raw stream beside the trial for scoring.

```sh
# 1. Prepare one corpus copy per arm, with warm indexes and recorded versions. No model calls.
python3 experiments/comparison_runner.py prepare \
  --source-root /path/to/corpus --workspace /path/to/workspace \
  --systems experiments/systems/comparison_systems_three.json \
  --semantic-command '["/path/to/target/release/examples/ollama_backend"]'

# 2. Run the matrix. Requires explicit model approval; writes one directory per trial.
python3 experiments/comparison_runner.py run \
  --workspace /path/to/workspace --systems experiments/systems/comparison_systems_three.json \
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
  --questions experiments/suites/django_heldout_questions.json --corpus ../runs/django-suite/corpus
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

`experiments/suites/django_development_questions.json` and `experiments/suites/django_heldout_questions.json` are the rebuilt instrument
the VS Code audit demanded: 30 development and 30 held-out questions over a 276-file, five-package
subset of Django 5.1.4 (`django/db`, `django/core`, `django/utils`, `django/dispatch`,
`django/apps`), pinned with corpus and suite hashes in `experiments/suites/django_suite_manifest.json`. Both sets
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
arms in `experiments/systems/comparison_systems_closure.json` are byte-identical apart from `id` and this text:

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

A stopping rule tested where nothing can be lost is not tested. `experiments/suites/django_hard_questions.json` is
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
The clause is now frozen into `experiments/systems/comparison_systems_closure.json` and every later arm carries it.

### A suite that was supposed to separate retrieval strategies, and a validator for it

`experiments/suites/django_retrieval_strategy_questions.json` is 12 development questions aimed at capability
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
definition that encloses the line. `experiments/suites/django_necessity_questions.json` is ten questions built on that
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
caller that does not exist. All three are pinned by `experiments/tests/test_audit_failures.py`, and all four existing
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

`experiments/suites/django_tool_trial_questions.json` is ten questions in three claimed classes — container plus
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
its hash pinned in `experiments/suites/django_suite_manifest.json` and absent from all 40 prior run manifests. It was
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
fingerprint in `experiments/suites/django_suite_manifest.json` byte for byte. It was run against a fresh clone and
returns `038e9fdd…` over 276 files, the same corpus every number in this file was measured on, and
it then prints the exact validate/prepare/run/score commands. It calls no model.

### The habit that made the numbers trustworthy

The thirty entries in the [defect ledger](#harness-defect-ledger) run in both directions — some
flattered this server, some penalised it, and the last was found inside its own single held-out loss.
None was found by auditing on a schedule. Every one came from the same rule, which is the methodological
claim this project would actually defend:

> **Every surprising result triggers an evaluator audit before an architectural interpretation.**

A stopping rule that looked like a free 22% saving, a treatment loss that looked like a premature
stop, an arm whose payload metrics read zero, two tools that agreed on nine helpers and disagreed on
five, a 0.40 that looked like over-listing: each was a measurement bug, and each would have become a
published architectural finding under the opposite habit.

## Adding five languages

The surface had been measured on Rust, Python and TypeScript only. Adding Go, Java, C, C++ and the
JavaScript half of the ECMAScript family is a capability change, so it was measured rather than
asserted — and the reason to distrust the obvious approach is already in the ledger: on TypeScript,
the index parsed the corpus, answered every question with a confident shape, and silently dropped
every `this.method()` call site. Nothing in its own output said so.

**One family, not eight extensions.** Candidate matching had compared file extensions, so a `.js`
call site could not resolve against a `.ts` definition. Languages are now a family: `.js`, `.jsx`,
`.mjs`, `.cjs`, `.ts`, `.tsx`, `.mts` and `.cts` are one, `.c` and the C++ suffixes are one, and a
relative ESM specifier written `./util.js` resolves to `util.ts`, which is what every TypeScript
build emits.

**The instrument.** `language_audit.py` asks the server and reads the corpus separately. For each
sampled symbol it compares `find_symbol` against an independent ripgrep definition index and
`find_callers` against `audit_failures.true_callers`, which enumerates call sites by ripgrep and
attributes each to its enclosing definition by reading the file. It also cross-checks the oracle
itself against universal-ctags, which parses independently and reports definition line ranges.
ctags emits those ranges for C, C++, Go, Java and Python but **not** for Rust or
JavaScript/TypeScript, which is exactly why it is a cross-check and not the oracle: no available
parser-backed tool answers "which definition encloses this line" in every family this server
indexes.

**Pre-registered**, in `runs/language-support-20260914/preregistration.json`: five arms, 20 symbols
each, seed 2 — deliberately not the seed the instrument was developed against — with pass criteria
of 18 of 20 definitions found and 0.90 caller precision and recall per arm, and the decision rule
that a family missing any criterion is not advertised until the defect behind it is fixed and the
arm re-run. The first confirmatory run missed it: LevelDB recall was 0.696.

| Corpus | Family | Definitions found | Caller precision | Caller recall | Oracle vs ctags |
|---|---|---:|---:|---:|---:|
| Cobra | Go | 20/20 | 1.000 | 0.988 | 150/150 |
| Gson | Java | 20/20 | 0.917 | 0.929 | 142/150 |
| Redis | C | 19/20 | 0.979 | 0.931 | 147/150 |
| LevelDB | C++ | 20/20 | 0.941 | 1.000 | 145/150 |
| ESLint | JavaScript | 20/20 | 0.981 | 1.000 | — |

**What the audit found before it agreed.** Five [ledger](#harness-defect-ledger) entries, three in
the evaluator and two in the server. The server reported no call site for any constructor —
`new Table(rows)` is a `new_expression`, not a call expression — and skipped every
reference-returning C++ accessor, because `const BlockHandle& metaindex_handle() const {` hides its
name inside a `reference_declarator`. The evaluator credited Redis' `TEST("...") { ... }` macro
blocks with the calls inside them, credited a C++ constructor's initialiser list to the member
rather than the constructor, missed one-line definitions entirely, and counted prototypes, Java
interface signatures, `.d.ts` method signatures and LevelDB's `EXCLUSIVE_LOCKS_REQUIRED(mutex_)`
annotations as call sites. Each is pinned by a test in `experiments/tests/test_audit_failures.py`.

**What still disagrees, and why it is not a defect.** Rows the server reports and the oracle misses
are real calls a line-based reader cannot attribute — `(*func)(reader.LastRecordOffset(), record,
dst);` in LevelDB, a default argument `const Slice& msg2 = Slice()`. Rows the oracle reports and
the server does not are chained-call continuation lines in Gson's tests and macro-expanded contexts
in Redis' vendored dependencies, where the oracle names a frame the index does not define. None is
a construct class: no shape of definition or call is systematically absent in any family.

**The honest weak spot is macro-heavy C.** 355 of 841 indexed Redis files contain a region
Tree-sitter cannot parse. Those files still answer — precision 0.979, recall 0.931 on that corpus —
and `coverage.parse_error_files` reports the count on every response. Parsing the same files with
the C++ grammar instead was measured and changed nothing (45 against 46 files with errors), so `.c`
keeps the C grammar.

**And a C++ shape no guard fixed.** A macro between `class` and its name —
`class LEVELDB_EXPORT WriteBatch {`, which 15 of LevelDB's 132 headers write — defeats the C++
grammar, and inside the wreckage the constructor declaration `WriteBatch();` parses as a call of
itself, reported against the namespace. The obvious repair was to drop call rows salvaged from
regions the parser marked as errors. It was built and measured: LevelDB was unchanged at precision
0.941, Redis lost a real caller row (recall 0.931 → 0.921), because the mis-parse is local and does
not always raise an ERROR node where the bogus row sits. **Reverted** — the fifth post-freeze
optimisation to be refused by its own run. The file is still reported as partly parsed through
`coverage.parse_error_files`.

**Not claimed.** This is an offline correctness study, not an agent-level one: no model ran, so
nothing here says a Go or Java question is answered in fewer turns or less context. The end-to-end
evidence in this file remains Rust, Python and TypeScript.

## The Go caller study: the first agent-level run in a new language

The five languages admitted in `v0.1.3` carried a correctness claim and no agent-level one. This
closes half of that gap for Go, and it closes it with a criterion missed.

**Pre-registered** in `runs/go-caller-20260914/preregistration.json` before the first trial: 24
authored exhaustive-caller questions over a 307-file gRPC-Go corpus
(`2a1d6d47...`, revision `e4711283`), three arms — Codex CLI's own shell tools, `zvec-grep 0.2.2`,
and `retrieval-mcp` 0.1.5's default four-tool surface (`6b026c7b...`) — three repetitions, seed 11,
`gpt-5.6-luna` via Codex CLI, 25 calls per trial. Pass criteria: quality within one resolved answer
of the best arm, at least 20% fewer input tokens than the native control, and zero answers given
without evidence. Every question compiled clean under `validate_suite.py` and survived
`lexical_oracle.py` at budget 25, so a bounded lexical crawl dissolves none of them.

**rep-1 was a pilot, and is reported as one.** It found two gold defects rather than a result:
`credentials/oauth/oauth.go` defines four methods named `GetRequestMetadata` on four receivers, so
a `path::name` caller set can name that identity once while the corpus holds four distinct callers —
all three arms enumerated all four, correctly, and all three scored 0.571. And the caller golds
excluded the helper's own defining file without the prose saying so. Two questions were dropped,
the remaining 22 restated, and both defects are now build failures in `validate_suite.py`.

**Two repetitions on the repaired suite, identical binary, seeds 12 and 13.** Twenty questions
completed in all three arms in both (two cells in rep-2 hit a provider capacity error and their
questions are excluded from every arm, keeping the matrix balanced):

| | native control | zvec-grep | retrieval-mcp |
|---|---:|---:|---:|
| Resolved correct / 40 | **35** | 34 | 33 |
| Graded credit | 0.963 | 0.944 | 0.942 |
| Input tokens | 17.13 M | 13.94 M | **8.30 M** |
| Retrieval calls | 229 | 234 | **135** |
| Persistent context (tok·turns) | 2,050 k | 2,189 k | **670 k** |
| Median calls to first evidence | 2 | 3 | **2** |
| Answered without evidence | 1 | 2 | **0** |

Against the criteria registered before the first trial: efficiency **passes** by a wide margin
(−51.5% input tokens, −41% calls, −67% persistent context, replicated at −51.5% and −50.0% in the
two repetitions separately), safety **passes** (zero unsupported answers in 40 trials, against one
and two), and quality **misses** — 33 against 35 is two answers, and the criterion was one. The
pre-registered claim is therefore not supported as written, and that is the published result.

**What the quality column actually measured.** Seventeen of the eighteen losses across both
repetitions, in all three arms, are the same thing: a test function that calls the helper and was
not named. Pooled, every arm named **64 of 72** test callers — 89%, the same for all three — so the
convention costs each arm equally and which questions its misses land on decides a 33-versus-35.
The questions never said whether a test caller counts; the golds said yes silently. That is the
`cu-callers-02` defect class in a third costume, it is now refused by `validate_suite.py`, and the
suite has been restated (`Test functions count as callers; name them too.`) for the next run. The
restatement makes this suite incomparable with these two repetitions by construction.

**The efficiency result does not depend on any of that.** It is a token and call count, not a
grade: the retrieval arm reached first evidence in 2 calls and stopped, and spent half the context
to do it, in both repetitions, on the same questions where it lost credit for omitting a test.

**One incident worth recording.** rep-3 refused to start: `server or semantic backend changed after
preparation`. Uncommitted server work had appeared in the checkout after rep-2 finished and its
release build had been overwritten. The guard was right and rep-2 was clean — its last trial was
written six minutes before the rebuild — so HEAD was built in a separate worktree, reproduced the
registered hash byte for byte, and rep-3 ran against that. Without the check, a repetition would
have silently measured a different server.

### The restatement, re-run: three repetitions, and the criterion still missed by one

Pre-registered in `runs/go-caller-restated-20260915/preregistration.json`, superseding the
2026-09-14 registration because the restated wording makes the suites incomparable: same corpus,
same binary `6b026c7b`, 22 questions whose prose now settles the test-caller convention, three
repetitions at seeds 21, 22 and 23, 198 trials, no failed or aborted cells.

| | native control | zvec-grep | retrieval-mcp |
|---|---:|---:|---:|
| Resolved correct / 66 | 60 | **62** | 60 |
| Graded credit | 0.965 | **0.980** | 0.975 |
| Input tokens | 27.55 M | 22.72 M | **13.41 M** |
| Retrieval calls | 384 | 379 | **222** |
| Persistent context (tok·turns) | 4,789 k | 6,344 k | **964 k** |
| Median calls to first evidence | 2 | 3 | **2** |
| Answered without evidence | 0 | 0 | **0** |

Efficiency **passes** and replicates tightly: −50.4%, −52.3% and −51.3% input tokens against the
native control in the three repetitions, −42% calls, −80% persistent context. Safety **passes** for
every arm. Quality **misses** by exactly one answer — 60 against the best arm's 62, where the
criterion was within one — so for the second study running the claim is published as a miss.

**Stating the convention worked, and the remainder is noise.** Test-caller retention rose from 89%
for every arm under the silent wording to 96% native, 97% zvec-grep and 94% retrieval-mcp. What is
left is not a stable failure anywhere: all thirteen losing cells in the study are questions an arm
answers correctly in some repetitions and not others — five for native, four each for the other two
— and no arm has a question it fails in all three. A 60-60-62 spread over 66 trials is that
instability, not a retrieval difference, and this project has watched a cell swing 1/3 to 3/3 on an
identical binary before.

**One grader defect, found by disbelieving a loss.** Two native trials had named
`(*Server).handleStream` and `(*http2Client).operateHeaders` — how Go, godoc and the source spell a
pointer-receiver method — and were scored as two misses plus two extras, 0.33 each, while
`Server.handleStream` had always graded correct. `quality_pass.context_segments` now reads the
receiver's parentheses and `*` as the notation they are, `regrade.py` applied the repair to all
three repetitions symmetrically, and it moved exactly two cells, both in the arm it had been
penalising. The table above is the regraded one; unrepaired, native scored 58.

### The exclusion was an artifact, and it cost more than it hid

Two studies in a row lost quality to a sentence about which files do not count, so the sentence was
measured rather than reworded again.

`audit_failures.true_callers` skipped the helper's own defining file, so every caller question had
to explain that policy to the model — and no user asking "who calls this" would ever say it. Run
with the exclusion removed across all 22 helpers, the whole corpus holds **three** in-file callers.
One of the three was a false positive: `s.printf("RegisterService(%q)", …)` names the helper inside
a format string, which the oracle read as a call and the server correctly ignored. Of the two real
ones, the server reports both. The clause that cost six trials was protecting the gold from two
identities.

Why it cost them: told to count calls "from outside the file that defines it", every arm also
skipped the defining file's `_test.go` twin, because in Go `http_util.go` and `http_util_test.go`
are one unit to a reader. Fourteen of the nineteen missing identities in the study are twins, and
six of six for `retrieval-mcp`, which had the rows in front of it — re-probed with the arm's own
arguments, every dropped caller was in a complete unpaginated page, and the same answers named
other test callers. The model was not filtering tests; it was applying the exclusion the way a Go
programmer would.

Three repairs, all offline:

- `true_callers` no longer counts a name inside a string literal. Re-running the five pre-registered
  language audits with the repaired oracle moves nothing — Cobra 1.000/0.988, Gson 0.917/0.929,
  Redis 0.979/0.931, LevelDB 0.941/1.000, ESLint 0.981/1.000, identical to the published run.
- `true_callers(..., include_defining_file=True)` lets a suite ask the question a user asks. The
  default stays the repository convention every earlier gold was authored under.
- A definition is not a caller of itself: `ClientHandshakeInfoFromContext` calls the internal
  namesake `icredentials.ClientHandshakeInfoFromContext` on its own second line, which would
  otherwise make its gold non-exhaustive forever.

And one refusal: `validate_suite.py` rejects a caller question that excludes a file while that
file's test twin calls the helper, unless the question names the twin. The 22-question suite now
asks plainly — *"Count every call in the corpus, including callers in test files and in the file
that defines it"* — carries `include_defining_file`, regolds to the full verified set (two
identities moved), compiles clean, and remains resistant to `lexical_oracle.py` at budget 25. It is
a third wording, so it is again incomparable with what came before; the point is that it is the
last one, because there is no longer a convention for a reader to guess.

### A suite built to separate the arms: etcd, 28 questions, attribution-hard by construction

The gRPC-Go study saturated - three arms within two answers of each other, every residual loss an
enclosing-definition disagreement rather than a retrieval failure - so the next suite was mined for
that property instead of stumbling onto it.

Corpus: `runs/etcd-suite/corpus`, 306 Go files cut from etcd at `0da1b60a` (`server/etcdserver`,
`server/lease`, `server/mvcc`, `server/storage`), fingerprinted in `corpus-manifest.json`. No
question set in this repository has been authored against etcd, so it is unspent by construction,
and it is concurrency-heavy in a way gRPC-Go's scoped cut is not.

The miner keeps a helper only when at least one of its callers is *attribution-hard*: the call sits
more than 25 lines below its enclosing header and the owner's name appears nowhere in a ±10-line
window, so a local text crawl cannot read the answer off the screen. 50 helpers qualified; 28 were
authored, in four parallel batches, against caller sets the oracle had already verified.

What that yields: **115 gold identities, 105 of them at attribution-hard sites**, median gold size 4,
worst case a call 142 lines below its header inside a `sendLoop` goroutine. `validate_suite.py`
compiles it clean, and `lexical_oracle.py` dissolves none of the 28 at budget 25. `find_callers`
returns all 115 in one default call per question, so the suite is answerable - the open question is
whether an agent driving text tools attributes them correctly, which is exactly what the previous
suite could no longer ask.

**Two oracle defects fell out of building it**, both found by disbelieving a disagreement rather
than by reading code. A name inside a format string counted as a call. And `declaration()` refused
any name in its control-word list for every language, so `func (tw *storeTxnWrite) delete(...)` -
a Go method whose name is a JavaScript operator - owned nothing, and every call site in its body
vanished from the oracle's caller sets. Go and Rust reach a name only through `func` and `fn`, so
the refusal is now limited to the families that write `if (` where a declaration writes its name.
Re-running all five pre-registered language audits after both repairs reproduces the published
numbers exactly: Cobra 1.000/0.988, Gson 0.917/0.929, Redis 0.979/0.931, LevelDB 0.941/1.000,
ESLint 0.981/1.000.

### A second mixed-shape suite, so the claim rests on two corpora

The claim this project defends is quality ties, tokens materially cheaper. It rested on one
mixed-shape suite - 30 held-out Django questions across six shapes, where the arms scored 29/29/28
and `retrieval-mcp` spent 33.5% fewer input tokens than the native control and 23.7% fewer than
zvec-grep. One corpus, one language, one snapshot.

`experiments/suites/etcd_mixed_questions.json` is the second: 30 questions over 311 Go files of etcd
(`client/v3`, `client/pkg`, `server/proxy`, `server/auth`, `server/embed`, `pkg/flags`,
`etcdctl/ctlv3`) sharing no file with any suite authored here, in the held-out suite's exact shape
proportions - 8 conceptual lookup, 6 symbol resolution, 6 direct caller, 5 transitive blast radius,
4 generic name, 1 mixed discovery. Authored in five parallel batches against material the oracle had
already verified, `validate_suite.py` clean.

The interesting number is the gate. `lexical_oracle.py` at budget 25 dissolves **11 of 30** - and
dissolves **11 of 30** of the Django held-out suite too, the same 19 resistant. Those eleven are
kept on purpose. A necessity suite excludes the work a bounded grep can already do, which is right
when the question is whether structural retrieval is *needed*; a representativeness suite must
include it, or the token comparison is measured only where grep is helpless and the result flatters
the server by construction. Dissolution is concentrated exactly where it should be: 4 of 8
conceptual, 4 of 6 symbol resolution, 3 of 4 generic name, and **0 of 12** caller and transitive
questions.

Not run. It exists so that the next model spend buys a replication of the claim that is actually
defended, on an independent corpus, rather than another rewording of a caller suite.

### The replication, and the two criteria it missed

`runs/etcd-mixed-20260915`, pre-registered before the first trial: 30 mixed-shape questions over the
independent etcd client corpus, three arms, three repetitions, 270 trials, none failed or aborted.

**As registered, pooled over three repetitions:**

| | native control | zvec-grep | retrieval-mcp |
|---|---:|---:|---:|
| Resolved correct / 90 | 85 | **86** | 82 |
| Graded credit | 0.976 | **0.981** | 0.950 |
| Input tokens | 30.42 M | 25.24 M | **17.77 M** |
| Retrieval calls | 396 | 447 | **318** |
| Persistent context | 3,066 k | 2,720 k | **2,054 k** |
| Answered without evidence | 3 | 2 | **1** |

Efficiency **passes**: −41.6% input tokens against native, −29.6% against zvec-grep, replicated at
−46.9%, −38.9% and −38.3% in the three repetitions. Quality **misses** by four answers. Safety
**misses**: one unsupported answer where the criterion was zero, and the registered decision rule
says a safety miss voids the efficiency claim. So the registered verdict is: **this run does not
replicate the Django result**, and that is what it is published as.

**Every loss in the study, in all three arms, is in one shape.** The other five shapes - 25
questions, 75 trials per arm - are 75/75 for every arm in every repetition:

| | native | zvec | retrieval-mcp |
|---|---:|---:|---:|
| Five shapes, 75 trials | 75/75 | 75/75 | **75/75** |
| Two-hop questions, 15 trials | 10/15 | 11/15 | 7/15 |

On those 25 the token gap is unchanged - −41.7% against native, −31.9% against zvec - so the
efficiency result is not carried by the questions that broke.

**What broke.** Two-hop golds are expanded by name, and that inherits everything name matching
inherits. One question's first hop went through `client/v3/leasing/kv.go::acquire`, whose name is
also a rate limiter's method, so the gold demanded callers of a namesake: all three arms scored 0.60
on it, twice. Two more turned on whether a direct caller that itself calls another direct caller is
"reached in two hops" - the gold says yes, this server's arm read the sentence as excluding them and
lost two cells for it, while both text arms read it the gold's way. The last loss is real and is
this project's known failure mode: the arm committed to `CheckAfterTest` when the question described
`RegisterLeakDetection`, then answered from the wrong expansion - a premature commit, and the single
safety flag.

**Repaired without being made easier.** The namesake was not a hard problem, it was an unread language rule: Go scopes an unexported identifier to its own package, so a lower-case `acquire` under `client/v3/leasing` cannot be the `acquire` called in `client/pkg/transport`, whatever ripgrep sees. `true_callers` now drops those references, which is exact rather than heuristic - no type inference is involved - and `validate_suite.py` measures ambiguity only where an expansion could actually reach it. Applied to every suite in the repository it changes **no gold at all**: 39 etcd caller questions, 22 gRPC-Go questions and the 25 unaffected mixed questions are byte-identical afterwards. The only movement anywhere is the two impossible callers leaving the one broken gold, 5 identities to 3.

So the suite is 30 questions again rather than 29 - the broken question returns with a correct gold instead of being deleted - and nothing about it got softer: the crawl gate still dissolves 11 of 30, `find_callers` alone still answers 6 of the 12 helper questions outright and no more, total gold identities go 69 to 67, and the only wording added anywhere is one sentence on the two questions whose answer set was genuinely ambiguous: *a direct caller that also calls another direct caller belongs in the answer*. That sentence states a set boundary; it names nothing and finds nothing. The suite is not comparable with this run, because a repaired instrument never is.

**What this study is worth.** The tie-plus-cheaper claim replicates on five of six shapes on an
independent corpus, and the sixth is a shape whose gold this project cannot yet build reliably -
which is a finding about the instrument, not about retrieval. A second run on the repaired suite
would settle the two-hop column; nothing about the −41.6% depends on it.

### The rerun on HEAD: the repaired suite, and a miss by one answer

`runs/etcd-mixed-rerun-20260915`, pre-registered against the HEAD binary `7c12a264` rather than the
0.1.5 tag, on the repaired 30-question suite. 270 trials, none failed or aborted.

| pooled, three repetitions | native control | zvec-grep | retrieval-mcp |
|---|---:|---:|---:|
| Resolved correct / 90 | **89** | **89** | 87 |
| Graded credit | 0.997 | 0.993 | 0.983 |
| Input tokens | 32.08 M | 25.12 M | **17.79 M** |
| Retrieval calls | 409 | 455 | **320** |
| Persistent context | 2,510 k | 2,682 k | **2,033 k** |
| Answered without evidence | 1 | 0 | **0** |

Efficiency **passes**: −44.6% input tokens against native and −29.2% against zvec-grep, at −38.2%,
−45.9% and −48.7% in the three repetitions. Safety **passes**: zero unsupported answers in 90
trials. Quality **misses by one answer** — 87 against 89, where the criterion was within one — so
the registered replication criterion, which needs both, is **not met**. That is the second study in
a row to miss quality by a single answer, and it is published as a miss both times.

**What the repair bought.** Against the previous run on the same corpus, the two-hop stratum went
7/15 to 13/15 for this server and 10/15 to 14/15 for the native control: the losses there were the
instrument, and repairing it lifted every arm. Outside that shape, 25 questions and 75 trials per
arm, the standing is 75/75 native, 75/75 zvec-grep, 74/75 here, with the token gap unchanged at
−46.0% and −32.7%.

**The three remaining losses, all in one repetition.** One named `TestNewOnlyJWT` where the corpus
defines `TestNewOnlyJWTExclusivity`. One answered a caller question with the helper itself included,
which the question's own "including callers in the file that defines it" invites and the gold
refuses, since a definition does not call itself. One answered a two-hop question with its one-hop
set - the same question where zvec-grep scored 0.33 in that repetition, so the hard part is the
question rather than the arm. None is a retrieval failure in the sense of missing evidence: the rows
were returned in every case.

**Where that leaves the claim.** On two independent corpora, three arms and 468 scored trials, the
arms are within two answers of each other and this server spends 34-51% fewer input tokens in every
comparison. Quality has never separated in either direction; the token gap has never failed to
replicate. Saying more than that would need a suite where quality can separate, and this project has
now twice found that such a suite is easier to bias than to build.

**A leak found later, disclosed here.** On 2026-09-19 the kernel study's first launch was stopped
because both arms opened `~/.agents/skills/retrieval-mcp/SKILL.md` — a routing guide for this
server's four tools, written by this project — as their first shell command. `codex exec
--ignore-user-config` isolates Codex's own config and not the operator's home directory, so every
Codex study before that date carries the same read: 270 of 270 trials here, 270 of 270 in
`runs/etcd-mixed-20260915`, 197 of 198 in `runs/go-caller-restated-20260915`. Its *cost* is
symmetric and small — mean 1.0 reads and 4,231 bytes for this server's arm, 1.03 and 4,234 for
native, 1.03 and 4,234 for zvec-grep — roughly one call and one percent of input tokens added to
every arm alike, so the differences above absorb an offset rather than a tilt. Its *content* is not
symmetric: it tells the treatment arm how to route questions to the tools it has, and tells the
other two arms about tools they do not have. Nothing in the archived bytes can separate a strategy
advantage from none, so the honest statement is that the −44.6% is measured with that document in
both contexts. The fix (own `HOME` per trial, a `read_outside_corpus` flag, a `contaminated` column
in `end_to_end.py`, tests in `experiments/tests/test_codex_wrapper.py`) landed before the kernel
study, which is therefore the first clean Codex run in this record.

### The Linux kernel: where the claim stops

`runs/linux-agent-20260919`. Every corpus this project had measured fitted in a few hundred files.
The kernel is 86,602 — two orders of magnitude further out — and the registered question was
whether the tie-plus-cheaper result survives the jump. It does not. All three criteria were missed,
and the run is published for that reason.

**Registration.** Linux 6.12 with symlinks removed, fingerprint
`b94f0462f57e54799833773c76e72c8b44141bba6722f0d41e76058290158bf1`, copied per arm. 21 questions
across five shapes, each verified by `validate_suite.py` and each resistant to `lexical_oracle.py`
at budget 25 over the whole tree — two candidates that the oracle dissolved were dropped before
freezing. Two arms: Codex CLI's own shell tools, and this server's four-tool default surface pinned
at `4043cd5bcfc870457ebb8335cd7f9ccda181d463cd8df627ba04e4a148231c8b`. 126 trials, seed 29, 25
calls per trial, `gpt-5.6-luna`. Criteria: quality no worse than one answer behind native, at least
20% fewer input tokens, and zero answers whose gold identity never appeared in a payload.

| pooled, three repetitions | native control | retrieval-mcp |
|---|---:|---:|
| Resolved correct / 63 | **62** | 56 |
| Graded credit | **0.997** | 0.889 |
| Input tokens, mean per trial | **138,757** | 161,451 |
| Retrieval calls | 243 | **203** |
| Bytes returned, median | 1,057,730 | **23,851** |
| Persistent context, median | 788,913 | **11,668** |
| Answered without evidence | 10 | **7** |

**Quality misses by six**, not by one. **Efficiency misses by sign**: +16.4% mean input tokens
where the criterion asked for −20%. **Safety misses**: seven answers, all of them the seven wrong
ones, named a symbol no payload contained. Nothing here is a tie.

| repetition | native | retrieval-mcp | native tokens | mcp tokens | delta |
|---|---:|---:|---:|---:|---:|
| 1 | 21/21 | 19/21 | 127,969 | 145,332 | +13.6% |
| 2 | 21/21 | 19/21 | 160,610 | 145,033 | −9.7% |
| 3 | 20/21 | 18/21 | 127,692 | 193,988 | +51.9% |

The token column swings from −9.7% to +51.9% across identical configurations, which is this
project's own three-repetition warning arriving on schedule; the quality column does not swing, and
that is the part to believe.

| bucket | native | retrieval-mcp | native tokens | mcp tokens | delta |
|---|---:|---:|---:|---:|---:|
| conceptual_lookup | 21/21 | 19/21 | 123,942 | 146,816 | +18.5% |
| direct_caller_lookup | 20/21 | 20/21 | 166,154 | 168,959 | +1.7% |
| generic_name | 6/6 | 4/6 | 96,057 | 152,278 | +58.5% |
| mixed_discovery_structure | 3/3 | 3/3 | 266,750 | 264,592 | −0.8% |
| symbol_resolution | 12/12 | 10/12 | 106,090 | 152,722 | +44.0% |

Caller questions are the one shape that holds: 20/21 both ways at +1.7% tokens, on the corpus with
the most callers to get wrong. Everything the structural surface is supposed to be worst at — a
vague description, a generic name — is where it loses.

**The audit came before the interpretation, and moved two columns.** Four harness defects surfaced
in this run alone and all four are in the [ledger](#harness-defect-ledger): trials reading a skill
document from the operator's home directory, a single-use credential copied 126 times, a
contamination flag that read `rg '/gfs2/'` as a path, and an evidence test that scored a shell
call's output while discarding the command. That last one mattered here: `sed -n '1200,1260p'
kernel/time/timer.c` names the gold path in the request, and ripgrep given a single file does not
repeat the path on each match, so the native arm was charged 17 unevidenced answers where the
repaired test charges 10 — and all ten answered correctly, four of them with a recording that hit
the 1 MiB Codex caps. Read that column as a floor for a shell arm and as exact for an MCP arm.
The seven wrong answers were audited individually against their gold and their rejected alternates:
`inode_update_time` for `file_update_time`, `dynevent_cmd_init` for `synth_event_cmd_init`,
`keyctl_update_key` for `key_update`, a `tools/perf` function for `kernel/kprobes.c`. Every one
names a neighbour the question's own `rejected_alternates` list anticipated. They are wrong
answers, not grading artifacts.

**Why the tokens invert, measured rather than argued.** Three facts, each from its own measurement:

1. *The tool surface is free.* A stub MCP server advertising the same four 16 KB schemas was
   attached to nine sessions against a no-MCP control: 13,802 input tokens either way, to the
   token. Whatever costs more here, it is not the shape of `tools/list`.
2. *The shell arm's bytes are not the shell arm's context.* Fitting input tokens on calls and
   payload per arm gives a payload coefficient of **0.09** for native and **3.97** for this server:
   about nine percent of what ripgrep printed locally ever reaches the model, while an MCP payload
   is delivered whole and re-sent on roughly four later requests. The byte columns above — 1.06 MB
   against 23.9 KB — describe what each tool produced, not what each model read.
3. *A shell call is not one retrieval.* Native commands chain a mean of **1.95** sub-commands and
   21% of them cap their own output with `head` or `-m`. So 3.9 native calls buy about 7.5
   retrieval operations per trial against this server's 3.2, in fewer round trips, and a round trip
   is what re-sends the conversation.

At 311 files a structural index wins because grep's output is a poor summary of a small tree. At
86,602 files the same agent writes `rg -n pattern subsystem | head -100`, pays for a truncated
slice, and composes two or three of those per call. The four-tool surface answers one question per
round trip and cannot be piped into anything.

**What this does to the claim.** The published result stands where it was measured — Django at
−33.5%, etcd at −44.6%, 468 trials with quality never separating — and it now has a stated
boundary: on a corpus of this size, with a shell-capable client, it reverses on every axis. That is
one corpus, one client and 126 trials, so the boundary is as provisional as the claim was after its
first corpus. What it is not is unknown.

### The ladder: what size actually did, and what the scorer did

`runs/scale-response-20260920`. The kernel study could report a reversal and could not attribute
it: size, language, question author and suite vintage all moved together. This is the identified
version, offline and free. Four nested corpora are cut from Linux 6.12 — **928, 5,637, 18,652 and
86,605 files** — with every gold and evidence file present in the smallest, so the only thing that
grows is the distractor set. Two behaviours are measured at each rung: the archived run's own 243
shell commands and 203 tool calls, replayed verbatim, and `search_concept` asked the raw question.

| rung | files | gold found, before | MCP payload | shell output | shell calls carrying gold |
|---|---:|---:|---:|---:|---:|
| r1 | 928 | 14/21 | 75 KB | 1.5 MB | 16.6 |
| r2 | 5,637 | 9/21 | 75 KB | 2.9 MB | 16.1 |
| r3 | 18,652 | 8/21 | 73 KB | 4.4 MB | 16.0 |
| r4 | 86,605 | **4/21** | 74 KB | 5.9 MB | 16.0 |

**The payload discipline is scale-invariant and the recall was not.** 73-75 KB across a 93× range
of corpus size, against a shell arm whose output grows four-fold; and a hit rate that falls by
more than half while the shell arm's is flat. The efficiency half of this project's claim survived
the kernel. The retrieval half did not.

**Then the audit, before any interpretation.** At 86,605 files, `limit: 10` and `limit: 100`
returned the same four hits, so nothing was being cut off by the page size. Of the 21 questions,
**17 failed because the gold file never entered the 400-file candidate set, and none failed inside
it.** One function, `files_about`, owned the whole result, and it held three defects that only a
large tree exposes:

- It scored a file by **how many distinct query tokens it wrote**. On Linux, 56,000 of 60,000
  source files write at least one of a question's words, hundreds tie at the top on `event`,
  `buffer`, `return`, and the tie-break is path order — the "answer everything out of `arch/`"
  failure that this scoring was introduced to prevent, re-created by ties at a scale it was never
  measured at.
- It read **32 matched lines per file**. A long source file spends that budget on lines carrying
  the query's common words and never reaches the one line with the rare identifier, and a long
  source file is where a kernel answer lives.
- It seeded with `scan_pattern`, whose `\b` anchor is right for a symbol scan and wrong for a
  description: `_` is a word character, so `\bbucket\b` **cannot match `quiesce_bucket`** — the
  spelling a C corpus uses for exactly the thing the question describes.

**The repair, one variable at a time.** Score by rarity, `ln(1 + eligible/df)` with document
frequency accumulated in the walk that was already running; raise the per-file budget to bound a
pathological file rather than sample a normal one; walk across cores so the budget costs wall time
instead of forbidding it; and give the concept scan its own code-aware word boundary.

| arm at 86,605 files | gold found | median latency |
|---|---:|---:|
| count + 32 lines (shipped in 0.1.6) | 4/21 | 4.3 s |
| rarity + 32 lines | 9/21 | 4.5 s |
| count + 2,048 lines | 12/21 | 10.0 s |
| rarity + 2,048 lines, serial | 12/21 | 13.0 s |
| **rarity + 2,048 lines, parallel** | **12/21** | **6.5 s** |

Across the ladder the decay is flattened rather than removed: **15, 13, 13, 12** where it was 14,
9, 8, 4. Latency roughly doubles on the largest corpus and is unchanged on small ones. The two
corpora this project publishes on do not move — Django 16/30 before and after with the median rank
improving 2.0 to 1.0, etcd 10/30 both, latency 2.6 s to 2.7 s and 0.69 s to 0.80 s — which is the
point: this is a defect that only existed above the sizes ever measured.

**A defect the repair introduced, caught by a three-file test.** The first parallel version merged
each worker's tallies only after 512 files, so a worker's last partial batch was dropped. At
kernel scale every worker crosses 512 and the answers looked perfect; on a three-file repository
every file is lost, and `a_description_ranks_over_the_files_its_own_words_choose` failed
immediately. Workers now merge when they are dropped.

**What is and is not claimed.** This is offline: a rank, not an answer. Six of the nine questions
still unrecalled at kernel scale are caller questions, which an agent answers with `find_callers`
rather than `search_concept`, so the metric is hardest on the shape it least describes. Whether
tripling candidate recall changes what a model answers was measured next, in
`runs/linux-scanfix-20260920` against `runs/linux-agent-20260919` as the registered baseline.

### The repair, measured under a model: every predicted answer, and a new failure one level down

`runs/linux-scanfix-20260920`. One variable against `runs/linux-agent-20260919`: the binary. Same
21 questions and hashes, same corpus fingerprint, same seed 29, same three repetitions, same
25-call cap, same client and model. 126 trials, none failed, zero contamination flags.

The registration was written against the mechanism rather than the arithmetic, which is the part
worth copying. An earlier draft said "recover 4 of the 6 lost answers" — a bar set by counting
what was lost, which cannot be wrong for an interesting reason. The offline ladder said something
sharper: `search_concept` now returns the gold at rank 1-2 for four *named* questions and still
does not for a fifth, `lx-callers-tls-closure-alert`, because that is a caller question this
change does not touch. So the registered claim named the four, named the fifth as not moving, and
asked for at least 5 of their 6 lost trials back.

| | baseline `0.1.6` scan | rarity scan |
|---|---:|---:|
| Resolved correct / 63 | 56 | **58** |
| Graded credit | 0.889 | **0.931** |
| Input tokens, mean | 161,451 | **145,653** |
| Against the native control | +16.4% | **+8.3%** |
| Answered without evidence | 7 | **4** |
| Median wall time | 29.3 s | **27.8 s** |
| Per repetition | 19, 19, 18 | 20, 19, 19 |

**Criterion 1, the per-question prediction: passed exactly.** `synth-event-cmd-start` 1/3 → 3/3,
`inode-timestamps` 1/3 → 3/3, `probe-entry-check` 2/3 → 3/3, `key-payload-update` 2/3 → 3/3. Six
of six, against a bar of five, and no question that stood at 3/3 fell below 2/3.

**Criterion 2, paired tokens: missed on its own test.** Against the archived arm over 21 question
medians, 11 were lower and the paired median is −3,568 tokens. The median is negative and the sign
test is a coin flip; the registration asked for the sign test, so this is a miss rather than a
small win. **Criterion 3, safety: missed** at 4 unevidenced answers against a bar of 2, improving
from 7.

**The interesting part is what the prediction did not cover.** The guard protected questions the
baseline answered 3/3. `lx-callers-tls-closure-alert` stood at 2/3, fell to **0/3**, and no
criterion saw it. That is a hole in the registration, and the data behind it is a real mechanism:

> The question describes a function that "looks the request up by its sock, gives up if there is
> none or if the session flag was already clear, and otherwise sends a warning-level close-notify".
> That is `net/handshake/tlshd.c::tls_handshake_close`, whose last statement calls
> `tls_alert_send`. The baseline asked `find_callers` about `tls_handshake_close` in two trials of
> three. The repaired arm asked about **`tls_alert_send` in all three**.

Rarity weighting ranks the site where the rare tokens literally appear, and for a wrapper that
site is the callee it names. `lx-callers-blk-tag-wake` fails the same way into `lib/sbitmap.c`;
`lx-callers-oom-shares-mm` fails differently, an over-inclusive caller set scored 0.667. All four
lost trials are caller questions. The question was checked before any of this was interpreted:
`tls_handshake_close` performs every step the prose names and `tls_alert_send` performs none, so
the question is sound and the answers are wrong.

**Where that leaves it.** Candidate selection was the kernel study's dominant failure and the
repair removes it — every question predicted to recover did, first attempt, nothing solid broke,
and the token gap halves. It also exposes a narrower failure one level down: choosing between a
wrapper and the callee it names. That is now the largest remaining loss on this corpus, it is a
ranking question rather than a selection one, and it is open.

### Completeness, and the drift that swallowed it

`runs/linux-complete-20260921`. One variable against `runs/linux-scanfix-20260920`: a C struct,
union, enum or class specifier with no body is a type reference, not a definition. Offline that
took one-call completeness - the share of questions whose whole answer already sits in a single
payload - from **2 of 21 to 11 of 21**, and rank-1 rows from 3 of 21 real functions to 20 of 21.
The cost model said why it should matter: a dependent round trip costs about 15,000 input tokens
on this client, a 20 KB payload about 2,400, so a removed hop is worth ~10% of a trial and a
smaller payload is worth almost nothing.

| kernel, 126 trials each | baseline `0.1.6` | rarity scan | + completeness |
|---|---:|---:|---:|
| Resolved / 63 | 56 | 58 | **58** |
| Retrieval calls per trial | 3.20 | 3.20 | **2.50** |
| Input tokens, mean | 161,451 | 145,653 | **135,448** |
| Against same-day native | +16.4% | **+8.3%** | +10.1% |
| Answered without evidence | 7 | 4 | **4** |

**Calls: the mean passed, the paired test did not.** 3.20 to 2.50 is inside the registered 2.70
bar, but the paired per-question median change is +0.0 with only 7 of 21 questions lower. The mean
moved because a few questions shed several calls, not because most shed one, and the registration
asked for both.

**Tokens: missed, and the reason is the instrument.** This arm's own tokens fell 7.0% on the same
questions, 14 of 21 question medians lower, paired median −14,202. But **native, on an identical
binary, corpus, seed and question set, spent 134,545 tokens one day and 123,001 the next — 8.6%
apart.** Provider drift between runs is the same size as the effect being chased. Only the
same-day within-run comparison can be read, and it says +10.1%, worse than the +8.3% it replaced.
Quality held at 58 of 63 with no question falling from 3/3, and safety held at 4.

**What the mechanism check says.** Tokens per call rose 45,517 to 54,179 while calls fell 22%. The
calls that vanished were the cheap ones; the expensive dependent hops remain. Completeness removed
exactly what it was built to remove and did not touch the rest.

**Three kernel studies now agree.** At 86,602 files the structural surface is level on quality -
56, 58, 58 against native's 62, 61, 61 - and behind on tokens by +16.4%, +8.3%, +10.1%. The
deficit is not made of retrieval bytes: payload is a 2,400-token term against a 15,000-token hop,
and this server already returns 40x fewer bytes than the shell arm. Whatever closes it has to
remove dependent round trips wholesale, not shave payloads.

### Where the tokens actually go: per-request accounting

`runs/per-request-20260921`. Three kernel studies had this server behind a shell agent on input
tokens while returning 40× fewer bytes and making fewer calls, and the harness could not say why,
because Codex reports usage once per turn. It does not have to: **a non-ephemeral session writes a
log with a `token_count` event per model request**. Six real kernel questions through both arms,
with that log on:

| | native | retrieval-mcp |
|---|---:|---:|
| First request | 15,393 | 15,435 |
| Requests | 34 | **32** |
| Calls | 28 | **26** |
| **Context added per call** | **4,378** | 6,042 |
| Total over six questions | 987,245 | 1,022,405 |

Three things fall out, and two of them correct earlier claims in this record.

**The advertised tool surface is free.** 15,393 against 15,435 on the first request — 42 tokens.
An earlier stub probe had priced four tools at ~1k per request; it was measuring four tools
*stacked on top of* an existing surface, not four instead of Codex's built-ins.

**Requests and calls already favour this server**, 32 against 34 and 26 against 28. The
completeness work did what it was built to do.

**The entire deficit is weight per call: 6,042 tokens against 4,378.** Every later request
re-reads the conversation, so that difference compounds. And the asymmetry belongs to the client,
not the corpus: **Codex truncates shell output before it enters context — the fitted coefficient
is 0.09 — and forwards MCP results whole.** A shell agent's megabytes arrive as ~17 KB of context
per call; this server's disciplined payloads arrive as ~24 KB. We were losing the axis we thought
we owned because the client trims for the competitor and not for us.

That also retires "payload is a second-order term", which came from a stub whose payload was
fixed. Payload bytes are the cost — but only the bytes nobody trims on your behalf.

### The response diet: say each fact once

`runs/payload-diet-20260921`. Nothing dropped, only repetition. A ranked row carried `symbol`
(`path::name`) beside a separate `name` and `path` and repeated its own line span; caller rows
repeated `resolution` and `candidate_count`, which concern the single name the page is about, and
`kind: "call"` on a page made of calls, and `expression` where it merely spells the name again;
`snippet_truncated` and `truncated` were serialised when false.

| call | before | after | |
|---|---:|---:|---:|
| `search_concept` ×3 | 3,750 / 4,051 / 4,132 | 2,854 / 3,057 / 3,100 | ~24% |
| `find_callers`, 20 rows | 12,323 | 9,544 | 22.6% |
| `find_callers`, other | 6,895 / 5,223 / 1,852 | 6,518 / 4,998 / 1,897 | 5.5% / 4.3% / **−2.4%** |
| `search_exact`, single and batched | 2,574 / 3,407 | 2,014 / 2,847 | 21.8% / 16.4% |
| `read_source` | 1,978 | 1,978 | nothing to remove |
| **total** | **46,185** | **38,807** | **16.0%** |

The one row that got worse is a single-hit caller page, where a page-level field costs more than
one row saves. **Every recoverable fact was compared call by call** — path, line, span, caller,
snippet, excerpt, qualified symbol, name, expression, kind, truncation flags, resolution — and all
ten calls are identical before and after. This is not the chunk diet that failed in
`runs/concept-chunk-lean-20260919`: that one cut evidence and collapsed recall 59 to 13; this one
cuts repeated field names and cannot touch recall.

**Then the larger half: stop paying for JSON.** A client bills the `content` text and not
`structuredContent` - measured by sending the same payload in both and seeing no change in usage -
and `content` was the JSON, so a twenty-row caller page spelled seven field names twenty times.
`content` is now a table: columns named once per array, one tab-separated line per row, tabs and
newlines inside values escaped so a snippet cannot forge structure, scores rounded to three
decimals. `structuredContent` is untouched, so every instrument in this harness reads exactly what
it read before.

| billed content bytes | before | after | |
|---|---:|---:|---:|
| `search_concept` | 3,488 | 2,043 | 41.4% |
| `find_callers`, 20 rows | 11,499 | 6,075 | 47.2% |
| `search_exact` | 2,408 | 1,302 | 45.9% |
| `read_source` | 1,830 | 1,203 | 34.3% |

Per-call context growth was 6,042 tokens against a shell agent's 4,378. Roughly 40% lighter puts
it near **3,800 - below the shell arm** - while this server already makes fewer calls. That is the
first configuration whose arithmetic predicts a win rather than a narrowing, and it is still only
arithmetic: no agent has run against it.

**And the asymmetry is not Codex's alone.** In the Claude held-out run, bytes per tool result were
704 median and 1,531 mean for the native arm against 3,458 and 3,292 here - also about twice as
heavy per call. That study was won on call count, 77 against 146. So the rule is not "Codex
truncates": it is **fewer calls AND payloads that are not heavier**. Django had a 2:1 call ratio
and could afford heavy payloads; the kernel's ratio is near 1:1, so the payload decides.

### The payload benchmark: parity, and the term that replaced bytes

`runs/linux-diet-20260921`. One variable: a binary whose payloads say each fact once and reach the
model as tables. 126 trials, none failed, zero contamination. The wrapper also stopped passing
`--ephemeral` so each trial keeps its Codex rollout log, which carries a token count **per model
request** - applied to both arms, changing no prompt, tool or policy, and declared in the
registration.

| kernel study | native | retrieval-mcp | gap | calls | unevidenced |
|---|---:|---:|---:|---:|---:|
| baseline `0.1.6` | 62/63 · 138,757 | 56/63 · 161,451 | +16.4% | 3.20 | 7 |
| rarity scan | 61/63 · 134,545 | 58/63 · 145,653 | +8.3% | 3.20 | 4 |
| completeness | 61/63 · 123,001 | 58/63 · 135,448 | +10.1% | 2.50 | 4 |
| **payload diet** | 63/63 · 136,716 | **59/63** · 138,069 | **+1.0%** | **2.10** | **2** |

Quality is the best this suite has recorded and so is the control's; safety is the best; calls are
the fewest. **The registered sign change did not happen**: +1.0% is parity, comfortably inside the
8.6% drift measured between two runs of an identical configuration, so the claim as written is a
miss.

**The mechanism criterion was written with the wrong denominator, and both readings are
published.** It asked for context added *per call* at or below native. That reads 9,662 against
5,796 and fails. But a client bills *requests*, and the two arms no longer make them the same way:

| per trial | native | retrieval-mcp |
|---|---:|---:|
| First request | 15,904 | 16,292 |
| Requests | 4.67 | 5.05 |
| Tool calls | 3.67 | **2.10** |
| Context added per call | 5,796 | 9,662 |
| Context added **per request** | 5,796 | **4,462** |
| Requests that make no tool call | **0** | **2.0** |

Per request this server now adds **4,462 tokens against a shell agent's 5,796 - 23% lighter**,
which is what the offline byte work predicted. Dividing by calls charges an arm for thinking
between them.

**And that is the finding worth keeping.** A shell arm's requests are its calls plus one: it
thinks by running another command. This arm spends about **two model requests per trial that call
nothing** - deliberation between retrievals. Two requests at ~4,500 tokens is ~9,000, which is
most of the distance between parity and the win the payload arithmetic predicted. The next lever
is not bytes and not ranking: it is whether a response is sufficient enough, and trusted enough,
that the model answers instead of thinking again.

### Two handshake sentences, measured and rejected

The one prompt-side change left untested was the handshake itself: does a single added sentence
change how many caller questions an agent resolves? `runs/instructions-ab-rerun-20260916` answers
no, and the way it answers is the point.

Three arms, one variable each — control is `0.1.6`'s instructions, `disambiguate` adds a rule about
choosing between plausible candidates before answering, `verbatim` adds one about copying symbol
names exactly as the rows spell them. No arm carries a `prompt_policy`, so the sentence under test
is the only place the advice appears. 39 questions from the etcd caller suite, 27 attribution-hard
and 12 local controls, three repetitions, seeds 61/62/63, `gpt-5.6-luna`: **351 trials, none failed
or aborted.**

| arm | resolved / 117 | per repetition | median input | paired vs control | median token delta |
|---|---:|---|---:|---|---:|
| control | 115 | 38 / 39 / 38 | 321,624 | — | — |
| disambiguate | 115 | 38 / 38 / 39 | 321,313 | 2 wins, 2 losses, 113 ties | −11,114 (−3.5%) |
| verbatim | 115 | 39 / 38 / 38 | 318,745 | 1 win, 1 loss, 115 ties | −5,711 (−1.8%) |

The registered bar was +3 resolved of 117. Both arms came in at **+0**, so neither sentence ships;
the cost bar of +10% was never in danger, since both treatments were marginally *cheaper*. Strata
are flat — every arm resolved 79 of 81 attribution-hard and 36 of 36 local — median calls per trial
is 8 on all three arms, and no arm answered without evidence in any of the 351 trials.

**Why this is the most useful negative in the file.** Two repetitions of the superseded study read
as a win on its way to shipping: control 75/78, `disambiguate` 77/78, `verbatim` **78/78**, with
`verbatim` sitting exactly on the +3 bar at a +3.2% token cost against a +10% allowance. The third
repetition, on binaries serving byte-identical instructions, turned that into 115/115/115 — and the
cost delta did not shrink, it **changed sign**, from +3.2% and +2.3% to −1.8% and −3.5%. With 29 of
117 paired trials above +50k tokens and 39 below −50k for `disambiguate`, the per-question
distribution is heavy-tailed in both directions, and a median over 78 trials was not measuring the
sentence at all. The stability rule — three repetitions decide, a difference carried by fewer is
unstable — is the only reason a noise artifact did not become a shipped instruction change.

**What it cost to get the answer honestly.** The original study's treatment binaries were built in
`/tmp` and a reboot took them; they were not reproducible from the record, because the source edit
had been reverted after building and the physical layout of one string literal is unrecoverable.
Control rebuilt to its pinned hash exactly, which localised the loss, and sixteen rebuilds across
five insertion placements and four line-count variants failed to reproduce either treatment. Rather
than argue the pins away, the study's own remedy was applied: a superseding registration, fresh
arms pinned *and preserved outside `/tmp`*, and all three repetitions re-run. The treatment's
identity is evidenced rather than assumed — `rep-1` of the superseded study recorded each arm's
served instructions, and the new arms handshake to byte-identical strings, 2,361 / 2,613 / 2,603
characters. Three hours of model time, $0 on a subscription client.

### Losses that are not the server's, recorded so they are not read as retrieval failures

Three studies produced a handful of cells this server lost while holding the right rows. They are
listed here with the evidence that the retrieval layer answered correctly, because a reader counting
losses cannot otherwise tell them apart from a tool that failed to find something — and this project
has already spent three question-set revisions on losses that turned out to be the instrument.

| incident | what the server returned | what the arm did |
|---|---|---|
| `etcdc-transitive-pretest-goroutine-guard`, rerun rep-3 | `find_callers` rows name both `client_test.go::TestNewWithOnlyJWT` and `client_test.go::TestNewOnlyJWTExclusivity` | answered `TestNewOnlyJWT`, a name the corpus does not define |
| `etcdc-callers-sort-enum-range-check`, rerun rep-3 | three rows, none of them the helper: `kv.go::Do`, `namespace/kv.go::Get`, `op_test.go::TestIsSortOptionValid` | added the helper itself to the caller list |
| `etcdc-transitive-leasing-session-lease-accessor`, rerun rep-3 | every hop was served — the arm issued seven `find_callers` calls covering both hops | answered with the one-hop set; `zvec-grep` scored 0.33 on the same cell |
| `etcdc-transitive-pretest-goroutine-guard`, first mixed run | `search_concept` ranked the intended helper **first**, 22.05 against 18.41, naming it in the `symbol` field | called `find_callers` on a symbol that appears in none of the rows, then answered from that expansion |
| `gg-creds-handshake-attrs-from-ctx-callers`, Go pilot | the ranker answered the query it was given, correctly | the query described a different thing than the question asked; the arm then spent seven reads confirming the wrong file |

Four of the five are one behaviour: **committing to a symbol and not revisiting**. It is the failure
mode the frozen closure policy's disambiguation clause was written for, and that clause measurably
fixed it once — 30/30 restored on the Django hard suite. It is not fixed everywhere, and the rate is
what decides whether to touch a frozen instrument: **0 of 90 trials in the last run**, one in 90 in
the run before, one in 72 in the Go pilot. Two anecdotes and a zero are not a rate, so nothing has
been changed on their account, and nothing should be until a suite produces a denominator.

The one server-side defect this line of audit did find — a call qualified by an imported package
credited to a same-named definition next door — is fixed and in the changelog. The audit that found
it is the same one that found seven evaluator defects: ask whether the bug you just fixed in the
grader also exists in the code under test.

## Answering a repository you cannot index

The kernel probe left one fact that no ranking change could address: a whole-repository snapshot
of Linux 6.12 stops at 8,500 of 60,283 eligible files, so `find_callers("vfs_read")` returned an
empty page — correctly labelled partial, and worthless — after 57 seconds and 1.4 GB. The
[chunk diet](#the-chunk-diet-that-ranked-fine-and-bought-nothing) tried to buy coverage by making
the index smaller and could not: the concept index is about a gigabyte of several, and trimming
it 15–28% changes no repository's affordability.

**So the question changed shape.** A caller question is addressed by a *name*. The repository can
be searched for that name and only the files that write it parsed — which is exactly what
`audit_failures.true_callers` has always done to verify every caller gold in this project. The
server now does the same when a snapshot cannot cover the corpus: `--structural auto|snapshot|scan`,
with `auto` reading the file listing and declining to build an index the budget cannot hold.

| Linux 6.12, whole tree | snapshot | scan |
|---|---|---|
| `find_callers("vfs_read")` | 0 rows | `fs/exec.c::read_code`, `fs/read_write.c::ksys_read`, `fs/read_write.c::ksys_pread64` |
| `find_callers("shrink_folio_list")` | 0 rows | 4 rows, all `mm/vmscan.c` |
| `find_symbol("ksys_read")` | 0 definitions | 1, `fs/read_write.c` |
| first answer | 56.1 s | 1.5 s |
| peak resident | 1,296 MB | 65 MB |
| coverage | 8,500 of 60,283, `budget_truncated` | 60,233 eligible, 1–4 files parsed, not truncated |

**The claim is a differential, not a new capability.** The parsing, the enclosing-definition
attribution and the row construction are the snapshot's own code; a scan builds a `StructuralIndex`
over the candidate files and asks it the question. So the test is that the two modes cannot be told
apart where both can see the whole repository: nine corpora across six languages, 30 singly-defined
symbols each, every one asked of `find_callers`, `find_symbol` and `trace_dependencies`, payloads
compared with the coverage block removed — **810 of 810 identical**, recorded in
`runs/scan-backend-20260919/`.

Two earlier iterations were not clean, and both failed on the same field. `nearest_indexed_names`
suggests neighbours for a name the index does not know, and a scan has no corpus-wide name list to
draw them from; the first attempt left the field empty and the second filled it from whatever files
the scan happened to read. Both are defensible and both make the differential un-runnable, so the
scan now takes that list from the snapshot. A field that cannot be made identical is a field that
turns a test into a comparison with an excuse.

**What it does not do.** `search_concept` ranks definitions across the corpus and `locate` is
addressed by a line, so both still need a snapshot, and on the kernel that snapshot is still
truncated and still says so. A scan session that never asks a concept question never builds one —
the kernel's first caller answer costs 1.8 s under `auto` — but a session that does pays the 56 s.
Nothing here ran a model: this is an offline correctness and cost result.

### And then the snapshot could stop holding three quarters of its rows

The scan path made a second change safe. Identifier references — every `name` that is not a call
site — are about 90% of an index's records once call sites are counted with them, and only 18–25%
of references are calls: Linux `mm` holds 206,526 references over 186 files, 41,673 of them calls;
Redis 471,326 with 84,753; Django 715,698 with 180,699. Every reader filters the rest out again —
the trace, the neighbourhood, `locate`, the caller page — except `find_callers(include_references:
true)`, one optional flag. With a scan available to answer that flag, the snapshot stops building
them.

| | before | after |
|---|---:|---:|
| Django 5.1.4, 2,898 files | 409 MB, 4.0 s | **222 MB**, 3.9 s |
| VS Code 1.96, 5,463 files | 739 MB, 7.3 s | **479 MB**, 7.2 s |
| Linux 6.12, snapshot mode | 8,500 of 60,283 files | **13,581** of 60,283 |

Build time does not move, because parsing was never what the change saved. On the kernel the
memory does not move either — it is pinned at a ceiling either way — but the ceiling now covers
60% more of the tree, and the one that binds is no longer the record ceiling but the 128 MiB byte
ceiling, which a cumulative read of the listing puts at exactly 13,453 files. The tree is still far
too large to hold, which is why a kernel session answers by scanning.

The acceptance test is the same shape and one column wider: nine corpora, 30 symbols each, now
through `find_callers`, `find_callers` with `include_references`, `find_symbol`, `inspect_symbol`
and `trace_dependencies` — **1,350 of 1,350 payloads identical** to the build that held every
reference. `runs/calls-only-20260919/` holds the arms and their hashes. A snapshot asked directly
for references it no longer keeps returns an error rather than the smaller set, so a future
miswiring cannot answer the question quietly.

### Ranking a repository you cannot index, and the heuristic that looked obvious

`search_concept` was the last reader that needed a whole-repository snapshot, so a kernel session
still paid 60 s and 1.45 GB for one that covers 13,581 of 60,283 files and ranks out of whatever
the walk reached first: asked to "read bytes from a file descriptor into a user buffer" it
answered `block/partitions/aix.c` and `arch/powerpc/.../spufs/file.c`. The candidate-scan idea
applies here too — a description is made of words, and a file writing none of them holds nothing
worth ranking — so the query seeds a file set and BM25 ranks the definitions of those files.

**The first registration missed, and the miss is the interesting part.**
`runs/seeded-concept-20260919/` registered seeding on the query's *four longest words*, on the
theory that length is the only rarity estimate available without the corpus statistics a snapshot
would have provided. Over the same seven suites the chunk-diet study used: pooled recall@5 51
against the snapshot's 59, MRR 0.2683 against 0.3288, `dj-heldout` and `etcd-mixed` down four
answers each — a rejection on the per-suite criterion alone.

The audit named the mechanism in one pass. Six of the nine lost golds sat in files that carry
*none* of the four seeded words, because a question's long words are English — "immediately",
"qualified", "definition", "reconstruction" — while the word that finds the file is short and
technical: `wsgi`, `flush`, `fd`, `tls`, `wasm`. Length is a rarity estimate for prose and an
anti-estimate for code.

**Seeding on every word ties exactly.** An exploratory arm using every token of three characters
or more scored 59 and 0.3288 — the snapshot's figures, suite for suite. It was chosen after
looking, so it was registered again and re-run into fresh artifacts rather than adopted on the
look: `runs/seeded-concept-confirm-20260919/`, same criteria, both arms identical in all seven
suites over five corpora.

| | snapshot | seeded |
|---|---|---|
| pooled recall@5 / MRR, 148 questions | 59 / 0.3288 | 59 / 0.3288 |
| warm query, corpus that fits | 1–3 ms | 200–550 ms |
| Linux 6.12, first answer | 60 s, 1,447 MB, 13,581 of 60,283 files | 3.3–7.7 s, 566 MB, 400 files |

The latency row is why this answers only where a snapshot cannot: a warm snapshot is two orders of
magnitude faster per query, and on a corpus that fits it is also complete. What the kernel numbers
are *not* is a result — no suite measures a corpus that large, so "`include/linux/workqueue.h` and
`kernel/workqueue.c` instead of `arch/x86` and `block/partitions`" is an observation. A kernel-scale
concept suite would settle it, and does not exist.

### The ceilings stayed, and that is a measurement

With identifier references gone, the obvious next move was to raise the byte ceiling and let a
large repository hold more of itself. `runs/ceilings-20260919/` measured it: 128 MiB holds 13,581
kernel files at 1,419 MB, 256 MiB holds 15,943 at 1,638 MB, and 512 MiB holds 19,648 at 1,814 MB —
where the 20,000-file ceiling takes over. Quadrupling the ceiling buys 45% more files for 28% more
memory, and a third of a repository answers an exhaustive caller question no better than a fifth
does; that question is answered by a scan covering all of it in 1.5 s and 65 MB. A corpus that fits
is unaffected at every setting. No change.

### And then there was no index

Three changes had moved every question onto a search, and each had been measured to answer
identically. What was left was a snapshot kept for one reason: on a repository small enough to hold,
a warm index answers in 1–3 ms where a search takes 20–170 ms. The question was whether that speed
is worth anything to the only client this server has.

**It is not, and it was costing a wrong answer.** A snapshot is built once per process. An agent
session is a session in which code changes. One session, two identical questions, a caller written
between them:

```
snapshot   before ['service.py::handle']   after ['service.py::handle']
search     before ['service.py::handle']   after ['report.py::render', 'service.py::handle']
```

The snapshot does not see a file saved a second ago and says nothing about it beyond a freshness
string. That is the only difference between the two designs that produces a wrong answer rather
than a slower one, and it is the failure an editing agent meets first.

**The differential.** Nine corpora, six languages, 25 singly-defined symbols each through
`find_callers`, `find_callers` with `include_references`, `find_symbol`, `inspect_symbol` and
`trace_dependencies`: 1,125 comparisons, **10 differences, all of them `nearest_indexed_names` on
an unknown symbol, and all of them improvements**. For redis's `GNUC_VERSION` the search suggests
`RM_GetServerVersion`, `RM_GetTypeMethodVersion`, `RedisModuleCommandInfoVersion` and
`XXH_versionNumber`; the snapshot suggested `C`, `E`, `G`, `O`, `S`. No row, definition, edge or
count moved. Ranking is not payload-comparable — a seeded BM25 computes its IDF over the files it
read — and its equality is the registered result in `runs/seeded-concept-confirm-20260919`.

**The cost, and it is a real one.** Session totals, fresh process:

| | 4 calls | | 13 calls | |
|---|---|---|---|---|
| | snapshot | no index | snapshot | no index |
| cobra | 0.1 s / 32 MB | 0.3 s / 30 MB | 0.1 s | 0.5 s |
| dj-heldout | 0.3 s / 75 MB | 0.4 s / 68 MB | 0.3 s | 0.5 s |
| redis | 1.3 s / 221 MB | 1.7 s / 223 MB | 1.4 s | 3.6 s |
| django | 3.7 s / 222 MB | 2.8 s / 189 MB | 3.7 s | 4.0 s |
| vscode | 7.0 s / 475 MB | 2.1 s / 220 MB | 7.1 s / 479 MB | 4.2 s / 233 MB |

Below Django scale a short session pays a tenth of a second and a thirteen-call one on redis pays
two seconds; from Django up the search is faster, and at VS Code scale it is three to five seconds
faster on half the memory. The archived runs make 2.6 structural calls per trial, which is the
left-hand column. `runs/no-snapshot-20260919/` holds the arms, the differential and the timings.

**What this deletes.** `Budget` stops describing a repository and starts describing one answer;
`--structural` and its three modes are gone; so are the whole-corpus BM25 build, the file-listing
walk that fed it, the mode routing, and the truncation semantics that came with a standing index.
What replaces all of it is one sentence in every result: how many files this answer read, out of
how many the repository holds.

## The chunk diet that ranked fine and bought nothing

Measuring the Linux kernel showed where a whole-repository snapshot's memory goes, and the concept
index looked like the cheap half to reclaim: on a full 60,189-file snapshot the BM25 index is
roughly 1.3 GB, and `mm` alone tokenises 1,096,565 concept tokens out of 186 files, because the
chunk text is the *entire definition body*. If a description question is a vocabulary problem and
the vocabulary lives in the doc comment and the name — which is exactly what
[Three index changes, one survivor](#three-index-changes-one-survivor) concluded — then the body
could go. `runs/concept-chunk-lean-20260919/preregistration.json` registered that prediction, three
arms differing only in the `end` bound of one slice, seven suites over five corpora, and a decision
rule with a memory bar as well as a quality bar.

| Suite | n | head recall@5 / MRR | lean | lean16 |
|---|---:|---:|---:|---:|
| cu-text | 10 | 4 / 0.337 | 3 / 0.300 | 4 / 0.325 |
| dj-forms | 10 | 3 / 0.250 | 0 / 0.000 | 3 / 0.220 |
| vs-editor | 10 | 1 / 0.114 | 1 / 0.100 | 2 / 0.083 |
| dj-heldout | 29 | 17 / 0.510 | 4 / 0.089 | 16 / 0.463 |
| dj-development | 29 | 13 / 0.343 | 0 / 0.011 | 12 / 0.293 |
| dj-hard | 30 | 11 / 0.317 | 1 / 0.016 | **16 / 0.392** |
| etcd-mixed | 30 | 10 / 0.246 | 4 / 0.092 | 9 / 0.246 |
| **pooled** | **148** | **59 / 0.3288** | **13 / 0.0684** | **62 / 0.3197** |

**The prediction was wrong in the direction that matters.** `lean` — doc comment, path, container
and signature line — collapses pooled recall@5 from 59 to 13. The 2026-09-12 finding said the
vocabulary is in the comment; it is, *for languages that write comments above the definition*. A
Python docstring sits inside the body, so `lean` deletes the very text that study credited, and
Django falls from 17 to 4, from 13 to 0, from 11 to 1.

**`lean16` — sixteen body lines — passes quality and fails the point.** It is within one answer on
every suite it loses, gains five on `dj-hard`, and pooled it is +3 recall@5 at −0.009 MRR, inside
the registered tolerances. But the registered benefit criterion was resident memory, and the
deterministic index counts say the diet is worth 15–28% of the concept index at that setting —
241,400 postings to 174,651 on the held-out Django corpus, 516,300 to 441,082 on Linux `mm`. A few
hundred megabytes off an index that is one gigabyte of a multi-gigabyte snapshot does not change
which repositories can be covered. By the decision rule the shipped chunk stays, and this is
published as the miss it is.

**Two instrument problems, both found before the result was read.** `study_a.py` and `study_b.py`
graded gold only in `.rs`, `.py`, `.ts` and `.tsx`, so every question in the Go etcd suite was
silently ungradable and `study_a` died inside `statistics.fmean` instead of saying so; both now
name every language the server indexes, `study_a` refuses a suite it can grade nothing in, and the
six cells produced before the repair are kept under `pre-instrument-repair/` and were re-run. And
peak resident memory sampled from `ps` turned out not to be an instrument at all at this
resolution: the same unbounded binary on the same corpus measured 7,884 MB once and 5,924 MB
later. Anything this project says about snapshot memory is therefore "several gigabytes", not a
figure, and the numbers that decided this run are the deterministic posting counts.

**The lead this run did not take.** `lean16` improving `dj-hard` by five answers looks like BM25
length normalisation — a long body dilutes term frequency, so a short chunk that still contains
the signature and the first statements ranks better. That is a ranking hypothesis, not a memory
one, and bundling it into a memory study would make neither attributable. It needs its own
registered run.

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

## The v0.1.2 performance study

The doc-comment chunk was measured on the corpora and questions that were already lying around,
which is enough to accept or reject a change and not enough to claim an advantage. This study asks
the harder question — does a ranking gain become an agent-level advantage against zvec-grep — on
material neither the change nor the tuning ever saw. The Django held-out set is spent and is not
reopened.

Everything is declared before anything runs. `runs/perf-v020-20260912/preregistration.json` holds
the arms, the corpus fingerprints, the binary hashes, the buckets, the success target and the
decision rules, written before the first arm was run.

The development build under test is unreleased: the last tag is `v0.1.1`, and the doc-comment build
is numbered 0.1.2 in `Cargo.toml`. Its artifacts were written before that number was settled, so the
run directory, the arm id and the pinned binary are spelled `perf-v020-20260912`, `retrieval-v020`
and `bin/retrieval-mcp-v0.2.0`. Those names are left exactly as the manifests recorded them, hashes
included; renaming a measured artifact to match a later decision is how a record stops being one.

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
`answered_without_evidence` or `wrong_without_evidence`. v0.1.2 must additionally not be worse than
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
| **retrieval-mcp v0.1.2** | **0.234** | **6** | **7** | **8** | 10 |
| zvec-grep 0.2.2 | 0.165 | 3 | 6 | 7 | 10 |

The coreutils result generalises: v0.1.2 beats v0.1.1 on `cu-text` (MRR 0.337 against 0.100) and on
`vs-editor` (0.114 against 0.029), and is identical on `dj-forms`, exactly as the mechanism predicts
— Python docstrings already sat inside the definition. The comparison that matters is the third row:
**v0.1.1 was behind zvec-grep on this suite and v0.1.2 is ahead of it**, by 42% on MRR and twice as
often at rank 1, while tying at recall@10. On economics the two are not comparable in kind:
retrieval-mcp answers warm in 1.3–2.6 ms with no index build, zvec-grep needs a 4.0–7.6 s index and
395–443 ms per warm query, and returns leaner rows (1.7–2.2 KB against 3.2–3.9 KB).

One bucket result is worth more than the table. `terminology_mismatch` scores **zero for every arm**,
including zvec: on raw question text, no ranker here bridges a deliberate vocabulary gap. That is the
project's first finding restated — the expensive work is query formation, and only an agent loop does
it — and it is why this layer is a screen, not the claim. Pooled bucket n is 3–6, so only the three
buckets with n = 6 support any statement at all.

### Layer 2: four arms, 120 trials, `claude-sonnet-4-6`

`experiments/systems/comparison_systems_perf_v020.json` declares `native-control`, `zvec-grep`, `retrieval-v011` and
`retrieval-v020`. The two retrieval arms are byte-identical except for the pinned binary — same four
visible tools, same closure stopping rule copied verbatim from the held-out systems file, no semantic
backend on either, so the only difference in the treatment is the doc-comment chunk. `comparison_runner.py`
takes a per-system `server` binary and records its sha256 in the prepared manifest, which is what
makes a two-version comparison possible at all; `end_to_end.py` reports every primary and safety
measure per bucket as well as per arm. Every arm's corpus copy fingerprints identically to its
source, and the zvec install comes from the cached 0.2.2 tree because this run had no registry access.

30 questions × 4 arms × 1 repetition, 30-call ceiling, $6.38 of model spend:

| | native | zvec-grep | v0.1.1 | **v0.1.2** |
|---|---:|---:|---:|---:|
| Correct / 30 | 29 | 25 | **29** | 28 |
| Graded credit | 0.98 | 0.87 | **0.99** | 0.98 |
| Input tokens, total | 1.286 M | 1.215 M | 783 k | **779 k** |
| Input tokens, median trial | 34.5 k | **21.2 k** | 24.1 k | 23.0 k |
| Tool calls, median | 4 | 2 | 2 | 2 |
| Calls to first sufficient evidence | 1.63 | 2.48 | **1.20** | 1.37 |
| Persistent context, tok·turns median | 3,996 | 3,154 | 3,133 | **2,726** |
| Answered without evidence | 1 | 2 | **0** | **0** |

Those are the repaired grades. The first scoring put every arm at zero on both coreutils caller
questions, which is exactly the shape of result the project's habit says to audit before
interpreting — and the audit found the grader, not the arms. Against a single-key gold object
(`{"callers": [...]}`) a reply naming exactly those identities as a bare list scored **0.0**, while
the same identities in prose scored 1.0: the container was being graded, the mirror image of the
sixteenth ledger entry. `quality_pass.credit` now treats a single-key gold and the bare value of
that key as the same assertion, multi-key golds stay strict, extras are still penalised, and
`validate_suite.py` compiles no suite whose gold identities fail in a different container.
`regrade.py` applied it to all 120 trials at once: 7 moved — native 27 → 29, v0.1.1 28 → 29,
v0.1.2 27 → 28, zvec unchanged at 25 because its answer to that question named the wrong functions.
Every token, call and context column is untouched by the repair.

**Against zvec-grep the pre-registered target is met on every criterion.** Quality 28 against 25
with paired discordance 4:1 (v0.1.1 is 29 and 4:0), input tokens 36% lower in total, first
sufficient evidence in 1.37 calls against 2.48, and zero unsupported answers against two. One honest
qualification: zvec's *median* trial is the cheapest of the four at 21.2 k tokens — its total is
carried by a heavy tail, so the context advantage is about failure modes, not about the typical
question.

**Against v0.1.1 the pre-registered target is not met, and that is the result.** v0.1.2 loses one
question (28 against 29) and reaches first evidence in 1.37 calls against 1.20; it carries 13% less
persistent context and spends 5% fewer tokens at the median, both inside the run-to-run floor. The
offline ranking gain — MRR 0.126 → 0.234, recall@1 2 → 6 — did **not** convert into an agent-level
advantage. The most economical explanation is the one this project already measured: the model
rewrites the question into code vocabulary before it searches, and a ranker improvement measured on
raw question text is partly redundant with work the model was already doing. `terminology_mismatch`
is the sharpest version of that: every arm scored zero on it offline, and every arm answered all six
of those questions in the agent loop.

The single v0.1.2 loss was audited before it was interpreted, per the habit. Every gold identity was
retrieved — `unretrieved_identities` is empty — and the model named the third caller as
`EnterOperation::_goodIndentForLine` where the corpus defines `TabOperation::_goodIndentForLine`.
That is `wrong_level`, the container-versus-member error the `inspect_symbol` A/B could not fix
either, scored at 2/3 credit. It is not a retrieval failure and not a grader defect.

Two of the 120 trials failed on infrastructure — an unreachable API after ten client retries with
zero tokens spent, and one 600 s client timeout — and were re-run with identical settings; the
originals are quarantined under `run-vs-editor/failed-trials/` and the substitution is recorded in
`run-vs-editor/repair.json`.

### Layer 3: the schema diet, and the saving that was not there

The advertised four-tool surface serialises to 14,831 bytes, of which **9,029 are generated output
schemas** — 6,614 for `find_callers` alone, a tool called in 6 of 30 trials. Re-sent every turn,
that arithmetic said roughly 2,240 tokens per turn, around 29% of median input tokens, for something
that is not retrieved evidence and so has no expansion-turn penalty to trade against. It was the
first proposed optimisation in this project whose accounting looked overwhelming before
implementation, and it was pre-registered narrowly: *removing nonessential output-schema detail
reduces end-to-end input context without reducing answer quality, tool-use reliability, or evidence
grounding*, with a ≥15% context reduction required to ship and every quality and safety column
required not to move.

Three representations were measured offline first, since jumping from 9 KB to nothing without
checking what the framework allows would have confounded the test:

| Representation | tools bytes | output-schema bytes | ≈ tokens |
|---|---:|---:|---:|
| A: full generated schemas | 14,831 | 9,029 | 3,708 |
| B: `{"type": "object"}` | 5,874 | 72 | 1,468 |
| C: omitted entirely | 5,730 | 8 | 1,432 |

B costs 144 bytes more than C across four tools and keeps the truthful statement that the tool
returns an object, so B is the treatment. All three still return structured content, verified with a
live call against each binary. The two arms differ by one expression in `tools::definition` and
nothing else — same descriptions, input schemas, instructions, results, closure policy, corpus bytes
and ceilings — and the systems file asserts it: the only fields that differ between the arms are
`id` and `server`.

30 questions × 2 arms × 1 repetition:

| | baseline | diet | Δ |
|---|---:|---:|---:|
| Correct / 30 | 28 | 29 | +1 |
| Graded credit | 0.978 | 0.989 | +0.011 |
| Input tokens, median | 23,300 | 23,282 | **−0.1%** |
| Input tokens, total | 808,781 | 837,493 | **+3.6%** |
| Calls, median / mean | 2 / 2.43 | 2 / 2.63 | +8% mean |
| Calls to first evidence | 1.37 | 1.43 | +4% |
| Answered / wrong without evidence | 0 / 0 | 0 / 0 | — |

**The target is missed completely, and the reason is the interesting part.** Per-trial
`cache_creation` — the tokens of the cached prompt prefix, which is where tool definitions live — is
4,374 tokens median for the baseline and 4,253 for the treatment, means 5,130 and 5,145. Unchanged,
where the byte arithmetic predicted about 2,240 fewer. **Claude Code does not forward `outputSchema`
to the model.** Those 9,029 bytes are client-side metadata; the surface the model actually sees is
the descriptions and input schemas, 5,802 bytes, byte-identical in both arms.

That falsifies the bottleneck reading that motivated the experiment, and the correction runs the
other way: measured model-facing prefixes in the Layer 2 run are **6,053 tokens median for
zvec-grep, 4,600 for this server and 4,374 for the native control**. This server's advertised
surface is about 1,450 tokens *smaller* than the competitor's, not larger. Counting `tools/list`
bytes measured what the client keeps to itself.

So the diet does not ship: it costs nothing measurable and buys nothing measurable, and a change
with no measured benefit does not enter the default binary. A client that does forward output
schemas would see a different number; none was measured here.

Where the context actually goes, per trial: a 4.6 k cached prefix plus **18.8 k of cache reads
across turns**. Turns, not advertised surface — the project's oldest quantitative finding, arrived
at from the opposite direction for the third time. The remaining lever against zvec is the one this
server already leads on: first sufficient evidence in 1.37 calls against 2.48.

What survives: **on three corpora and a suite none of the systems had seen, both versions of this
server beat zvec-grep 0.2.2 on quality, total context and speed to first evidence, and the
doc-comment change is a ranking improvement that an agent loop does not need.** Artifacts in
`runs/perf-v020-20260912/`.


### Layer 4: the container line, and the bug it was hiding

All three remaining quality misses were `wrong_level` - the right member named under the wrong
container - so the next intervention was the smallest thing that could fix it: the enclosing
container identity on caller rows and on `search_concept`'s symbol block, and nothing else. No
outlines, no member listings. Pre-registered on the caller bucket alone, six questions, two arms,
**three repetitions**, because one-question signals have misled this project before.

Building the treatment found a defect first. `owner()` accepted any definition-shaped parent,
including a TypeScript `variable_declarator` that the symbol index itself refuses to index, so a
call written into a local binding was attributed to the binding. On the vs-editor corpus the three
call sites of `getEnterAction` were reported as `enterAction`, `r` and `expectedEnterAction`, and
both call sites of `guessIndentation` as `guessedIndentation`. **Every caller row on both graded
TypeScript questions named a local const rather than a function.** With the same guard the symbol
index applies, those five rows read `ShiftCommand::getEditOperations`, `EnterOperation::_enter`,
`TabOperation::_goodIndentForLine`, `TextModel::resolveOptions` and `TextModel::detectIndentation`
- the two gold sets exactly. The row was not missing a level; it was naming the wrong definition.

36 trials, `claude-sonnet-4-6`:

| | baseline | + container | 
|---|---:|---:|
| Correct / 18 | **15** | 14 |
| Graded credit | **0.944** | 0.907 |
| `wrong_level` errors | 0 | 0 |
| Calls, mean | 3.28 | **2.50** |
| `read_source` per trial | 0.83 | **0.28** |
| Input tokens, total | 677,601 | **487,924** |
| Input tokens, median | **25,138** | 25,644 |
| Input tokens, worst trial | 107,670 | **42,130** |
| Answered without evidence | **0** | 1 |

**The primary endpoint could not be demonstrated: zero `wrong_level` errors occurred in either
arm.** The Layer 2 failure did not reproduce in three repetitions, so there was nothing to reduce -
which is what three repetitions are for. Correctness differs by one repetition of one question, and
the audit says it is a routing error rather than a row defect: the model called `find_callers` on
`getIndentForEnter`, a symbol the question never describes, took its single row and committed.
`unretrieved_identities` names the gold it never retrieved, so the safety flag is correct.

What the intervention did do is remove recovery work: `read_source` calls fell 66%, mean calls
3.28 → 2.50, and the worst trial dropped from 107,670 input tokens to 42,130. The median is
unchanged; the whole saving is in the tail, because the model no longer re-reads source to work out
which function a call site sits in. Payload grew 0.9-6.0% per response, as predicted.

By the pre-registered rule the bundle does not ship: the endpoint was not demonstrated, correctness
was not held, and a guardrail moved. But the two halves are not the same kind of change. A row
naming a local `const` as a caller is a false statement about the corpus, so **the attribution guard
ships on its own**, with a regression test pinning that a call bound to a local const is attributed
to its enclosing function while an arrow function bound to a const is still a caller. The container
fields do not ship. This run cannot attribute the 66% drop in source reads between the two halves,
so the next A/B is bugfix-only against bugfix-plus-container-fields on the same six questions.


### Layers 5 and 6: isolating the feature from the fix

Layer 4 bundled a correctness fix with a feature, so its efficiency could not be credited to either.
Two more runs separate them, six questions and three repetitions each, everything else frozen.

**Layer 5, against a baseline whose caller rows are already correct, the container fields move
nothing.** `read_source` per trial is 0.28 in both arms, mean calls 2.39 in both, total input tokens
rise 4%, median 5.8%, retrieval bytes 21.8%. Correctness 12 of 18 against 13. The fields are not
shipped: there is no measured benefit to weigh the payload against.

**Layer 6 isolates the guard with the seed held equal, and it is free and large:**

| | pre-guard | post-guard |
|---|---:|---:|
| Correct / 18 | 15 | 15 |
| Graded credit | 0.944 | 0.944 |
| `read_source` per trial | 0.83 | **0.22** (−74%) |
| Calls, mean | 3.67 | **2.56** (−30%) |
| Input tokens, total | 773,300 | **467,775** (−39.5%) |
| Input tokens, worst trial | 158,376 | **30,098** (−81%) |
| Answered without evidence | 0 | 0 |

So Layer 4's entire efficiency signal belongs to the attribution guard — the correctness fix — and
none of it to the container identity that was the hypothesis. Correct rows stop the model
re-reading source to work out which function a call site sits in; a container label on rows that
were already right buys nothing.

One more thing happened on the way, and it is the most useful part. Layer 5 appeared to show the
shipped guard *costing* answers: `vs-callers-enter-rule-resolution` scored 1 of 3 where the
pre-guard arm had scored 3 of 3, and the call traces even offered a mechanism — before the fix the
model had to read source, and while doing so it stumbled onto the helper the question describes.
Layer 6 ran that exact configuration again — same binary, same seed, same questions — and scored
**3 of 3**. The cell is model variance, not a treatment effect. Pooled across every run, that one
question scored full credit in 10 of 12 trials without the container fields and 2 of 6 with them,
which is the only evidence that has ever pointed at the fields at all, and it points the wrong way
for them.

The rule this project keeps re-learning, in its sharpest form yet: a single three-repetition cell is
not a finding, even when a plausible mechanism is sitting right next to it.


### The caller bucket had a ceiling defect

Every arm of every run scored exactly 0.667 on `cu-callers-02` - six runs, thirty trials, the same
value - which is the signature of a suite defect rather than a model failure. It was. The question
asked for "every function in this corpus" that configures the size parser through its whitelist
method; the corpus has five such call sites, three in production and two inside the parser's own
`#[cfg(test)]` module, and the gold lists three, following the harness convention that a caller set
excludes the helper's own defining module. The convention is fine; the question never stated it, so
every arm answered with four entries and lost a third of the credit for being right about the
corpus. The wording now states the scope the gold uses, and the repair is recorded in the question's
own `author_notes`. Archived trials answered the old wording and are **not** re-graded against the
new one - they answered a different question - so they are excluded from any caller-bucket number
that uses it.

Recomputed without that question, the bucket says there is no caller-quality problem left to solve
on this server:

| run | arm | as scored | excluding the defective question |
|---|---|---:|---:|
| Layer 6 | pre-guard | 15/18 | **15/15** |
| Layer 6 | post-guard | 15/18 | **15/15** |
| Layer 4 | baseline | 15/18 | **15/15** |
| Layer 2 | retrieval-mcp v0.1.1 | 5/6 | **5/5** |
| Layer 2 | native control | 5/6 | 4/5 |
| Layer 2 | zvec-grep | 3/6 | 3/5 |

The bucket still discriminates - it is the one place the competitor and the native control lose -
but it no longer discriminates *against this server*, which is why the next caller-quality feature
should not be invented until a suite exists that can see one fail.


### The repair of that ceiling was itself defective, and it took a paid run to see it

`runs/cu-callers-02-confirm-20260916`, pre-registered before the first trial: the repaired
`cu-callers-02` alone, one question, two arms, three repetitions, 6 trials, `claude-sonnet-4-6`,
seed 42, on the `cu-text` corpus whose fingerprint `1e2b3838` reproduces the perf-v020 pin.
Criteria: full credit in at least 4 of 6 trials **and** no trial showing the old ceiling. Result
**3 of 6, three trials at exactly 0.667**, so the criteria are missed and the negative result is
what stands.

| arm | rep 1 | rep 2 | rep 3 |
|---|---:|---:|---:|
| native-control | 1.000 | 1.000 | 1.000 |
| retrieval-mcp | 0.667 | 0.667 | 0.667 |

Read at face value that table says the structural surface is worse at exhaustive caller questions,
consistently, across repetitions. The answers say something else. All three retrieval trials
**found** `parse_signed_num.rs::parse_count` and excluded it on purpose, quoting the question back:
*"The callers in parse_signed_num.rs and parse_size.rs are within the same features/parser module
and are excluded."* The gold counts it. The first repair had named the `#[cfg(test)]` boundary and
then scoped the rest as *"outside the parser's own module"* - and `parse_count` lives in the
defining file's own directory, so "module" named the defining module, which the gold meant, and the
enclosing `parser` module, which the file paths show, with equal right. The arms did not disagree
about the corpus. They disagreed about the question, and the arm whose evidence made the directory
most visible - `find_callers` rows carry full paths - is the arm the wording punished.

So the original defect survived its own repair by moving one scope level up. Three consequences,
all of them recorded rather than argued:

- The wording is restated a second time, in the question's `author_notes`: the exclusion is now a
  **file** boundary, and neighbouring files of the same directory are said to count in so many words.
- `validate_suite.py` refuses the old wording. A caller question that scopes an exclusion by module
  while a verified caller sits in the helper's own directory is now a build failure, with a
  regression test that fails on the wording this run used and passes on the restatement.
- The question stayed out of every caller-bucket number until a second 6-trial confirmation passed.
  It has: `runs/cu-callers-02-confirm-2-20260916`, below.

The run cost $0.8846 against a $0.30 estimate, which is worth recording as its own small lesson: the
estimate used a pooled median trial cost, and this study's arms are not interchangeable. The native
arm spent 9-18 calls and $0.13-$0.34 per trial to grep and read its way to the same answer the
retrieval arm reached in 3 calls for $0.05-$0.10.

#### The second restatement passes, 6 of 6

`runs/cu-callers-02-confirm-2-20260916`, registration copied unchanged before the first trial, all
four pins re-verified at run time - suite `097fb8be`, binary `65aa8389`, backend `11b8af2a`,
systems file `95a88f0a` - same corpus, same arms, same seed, only the wording different. The
threshold was raised to 5 of 6 because the first run's failure was deterministic per arm rather
than noisy, so 4 of 6 could not have distinguished a repaired question from one arm still reading
it the other way.

| arm | rep 1 | rep 2 | rep 3 |
|---|---:|---:|---:|
| native-control | 1.000 | 1.000 | 1.000 |
| retrieval-mcp | 1.000 | 1.000 | 1.000 |

**6 of 6, no ceiling value, both criteria met**, $0.9627 against a $0.90 estimate built from the
first run's per-arm rates. All six answers name `parse_signed_num.rs::parse_count`, the caller the
previous wording taught three trials to exclude, and two of them quote the new clause back:
*"neighboring file, same directory - explicitly counts per the question"*. So the earlier loss was
the wording and nothing else, and `cu-callers-02` is gradeable again - by an arm with structural
tools and by one with grep, which is what the two-arm design was for.

It licenses nothing else. One question, one model, one corpus, six trials. The call and cost
columns do separate - retrieval 3 calls and $0.05-$0.10 per trial against native's 14-18 calls and
$0.20-$0.27 - and that is an observation about one question, not a measurement; the efficiency
claim rests on the mixed-shape suites.


### Four backlog items, measured

Driving the server against Django rather than a scoped corpus produced a backlog of four candidates
and one untested property. All five were settled without a single model call.

**Test-file ranking: rejected.** A multiplicative down-weight on chunks whose path looks like test
code lifts the authored coreutils suite from MRR 0.474 to 0.519 (recall@5 10 → 11) and moves
Django's recall@10 by one, with VS Code unchanged and no regression anywhere. It is not a fitted
constant either: weights of 0.10, 0.25, 0.40, 0.55 and 0.70 all produce exactly the same scores, so
any down-weight moves the same one or two questions. It is still not shipped, and the reason is the
cost no suite in this project can see. A query about test code loses its evidence: *"test that
sorting handles numeric suffixes"* returns two test files in the base top five and **none** under
the treatment, and **zero of the 62 graded questions have a gold in a test file**. A change whose
benefit is inside the evidence base and whose cost is outside it does not get made on that evidence.

**Payload de-duplication: measured, pre-registered, not yet run.** 27–61% of a `find_callers`
response is the identical `candidate_definitions` array and `confidence` sentence repeated once per
row. Stating them once per page cuts 18 real calls across three corpora from 494,683 to 233,609
bytes — **−52.8%**, from −7.2% on a two-row page to −61.2% on a twenty-row page — with identical
rows in identical order. That is a shape change to model-facing output, so it is pre-registered with
a ≥10% `context_token_turns` reduction to ship and every quality and grounding column held. The
distinction from the schema diet is the one that matters: output schemas are client-side metadata
Claude Code never forwards, while tool results are sent verbatim and re-read every turn.

**A cross-process snapshot cache: do not build.** Mining 1,227 archived trials for the ceiling
settles it. A trial starts exactly one server process (937 of 937) which builds at most one
snapshot, so a *perfect* cache saves one build: 1.05 s median, **5.0% of an 18.4 s median trial**,
12.4% at p90, and 9.8% of structural-capable trials never make a structural call at all, where a
cache is pure cost. Validation is not the blocker — walk-and-stat costs 1.0–2.5% of a rebuild and
sound content hashing 2.8–4.5% — and the snapshot would weigh 78–382 MB. Net of validating and
loading, the cache buys 0.7–0.9 s per process: 5% of wall time, 0% of tokens, calls or correctness,
in exchange for the defect class this project treats most seriously, since `Symbol.excerpt` and
`Reference.excerpt` are captured at build time and a stale entry would quote source that no longer
exists. Full arithmetic in `runs/snapshot-cache-feasibility-20260913/report.json`.

**Concurrency: no defect found.** Every measurement in this project until now drove one request at a
time. Four stdio tests now pipeline 24 calls without awaiting any reply, race eight structural calls
against the single lazy index build, cancel a call while its build is running, and close stdin with
nine requests in flight. All four passed on their first run: no dropped or misrouted reply, no
duplicate, no deadlock, no second build. The assertions are shown to discriminate rather than to
pass vacuously — removing eight ids from the owed set makes the pipelining test fail on the stray
reply, two processes over the same corpus produce different `snapshot_id`s, and a lexical-only
session logs zero `index_built` lines against exactly one for each build.


### Layer 7: the de-duplication, run on the other client

`find_callers` repeated the requested name's `candidate_definitions` and the same `confidence`
sentence once per row, which is 27-61% of a caller response. Stating them once per page cut 18 real
calls across three corpora from 494,683 to 233,609 bytes offline, and fits **800 caller rows where
the shipped build fits 258** under the same 64 KiB cap on Django - 3.1x more call sites per call at
identical row content. That is a shape change to model-facing output, so it was pre-registered at a
>=10% `context_token_turns` reduction with every quality and grounding column held, and deliberately
run on **Codex CLI with `gpt-5.6-luna`** rather than the model every other post-freeze result came
from.

36 trials, six caller questions, three repetitions:

| | base | de-duplicated |
|---|---:|---:|
| Correct / 18 | 16 | 16 |
| Graded credit | 0.907 | 0.907 |
| `context_token_turns`, median | 14,061 | **11,448** (-18.6%) |
| `context_token_turns`, per-question medians | 119,114 | **81,023** (-32.0%) |
| Input tokens, total | 2.518 M | **2.238 M** (-11.1%) |
| Input tokens, per-question medians | 866,406 | **720,619** (-16.8%) |
| `find_callers` payload per trial | 7,375 B | **6,325 B** (-14.2%) |
| Answered / wrong without evidence | 2 / 0 | 2 / 0 |

Shipped. Two things are worth stating alongside the pass. The offline arithmetic predicted about 4%
of input tokens on Claude and the measured saving on Codex is 11-17%, because this client re-sends
more per turn - the same "turns dominate" mechanism, arriving from the other side. And the arms are
discordant one question each way: the base lost one question inside a 16-call flailing trial, the
treatment lost one repetition of `cu-callers-01` by answering about the wrong symbol entirely, with
`unretrieved_identities` showing it never retrieved the gold. That is the wrong-seed routing error
this suite has produced before, but the pre-registration said plainly that 36 trials are weakly
powered for a comprehension regression, and one trial is not evidence either way.


### Two drivers, and the defect every suite was blind to

Two agents were given the server, three corpora and no instruction to be kind. One drove Django
(49 calls), one drove VS Code's editor core (163 calls, not one `isError` reply). Between them they
found the most serious defect in this project since the caller attribution guard, and it was
invisible to every question set here for the same structural reason as the last two.

**`find_callers` dropped every TypeScript member call.** The callee resolver knew the field names
Rust and Python use and accepted `identifier`, `field_identifier` and `type_identifier`. TypeScript
keeps a callee under a `property` field as a `property_identifier`, so `this.method()` and
`obj.method()` resolved to nothing at all. The driver sampled 25 class methods with real call sites
and got zero rows for 24 of them. Checked against ripgrep after the fix: `getEdits` 14 real call
sites, 0 rows before and 14 after; `_isAutoIndentType`, `resolveOptions`, `detectIndentation`,
`onElectricCharacter`, `atomicPosition` all 0 before and exactly their true count after; `dispose`
10 rows before against 173 real sites. Free functions were unaffected in both directions
— `guessIndentation` 2, `getEnterAction` 3, `renderViewLine` 3 — **and that is exactly why no suite
caught it: every TypeScript, Python and Rust caller gold in this repository names a free function.**
Python and Rust extraction is unchanged, verified on a synthetic file and row-for-row on two corpora.

The Django driver found three more wrong successes. `import_string(r)()` produced two rows for one
call because the outer invocation descended into the inner call. `candidate_definitions` silently
showed 5 of 31 `database_forwards` definitions with no field saying 26 were absent. And a
`path:"django/db/migrations"` caller page returned 10 scoped rows and `has_more:false` beside
repository-wide orientation counts of 171. The TypeScript driver found a fourth: an empty
five-line constructor carried 440 `callees`, because `search_concept` attached a name-wide
`constructor` aggregate to each distinct definition.

All four are correctness defects, not feature proposals. A pre-registered nine-file audit under
`runs/driver-correctness-20260913/` compared the `46293db` binary with the fixes, with no model
usage. `import_string(name)()` fell from two same-position rows to one; the seven-definition cell
now reports five rows plus `candidate_definition_count:7` and
`candidate_definitions_truncated:true`; scoped orientation fell from the wrong 7/7 to 1/1; and the
empty `dispose` method now reports `direct_callees:0` rather than inheriting the other `dispose`
method's one call. The concept fields are renamed to expose their scopes:
`name_candidate_callers` is a same-language spelling count, while `direct_callees` belongs to the
exact definition. The schema cost is exact and small but not hidden: the final four-tool
`tools/list` payload grew from 27,073 to 27,838 bytes, **+765 bytes / +2.8%**.

The praise is worth recording too, because it is specific: `search_concept` ranked the owning
definition first on the first attempt for the hardest conceptual question either driver asked, and
212 calls produced no errors. So did the criticism: caller rows still do not name the class of the
function they name, conceptual results still surface test files, and one caller response spent
4,661 of 6,795 bytes on unresolved imports. The first two have been measured and refused; the third
is a payload question of exactly the kind the de-duplication just answered, and is not yet measured.


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

`experiments/suites/comparison_questions.json` is the frozen, pre-registered workload: the twelve reviewed
`experiments/suites/v2_questions_draft.json` tasks plus ten authored in `experiments/suites/comparison_questions_new.json` (ids prefixed
`comp-`). Every gold answer is verified against the pinned corpus by reading source and by
independent ripgrep; `experiments/tests/test_comparison_questions.py` re-checks each anchor, stratum, and null answer
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

Before any model run, review the answer key, not the model's behavior. `experiments/tests/test_comparison_questions.py`
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

`experiments/suites/project_questions.json` freezes 36 questions over committed ModelShare, pig, and Sigil source: two per category per repository. Each question has a source-evidence checklist and a typed gold answer. `snapshot_projects.py` exports committed `.rs`/`.py` blobs only; uncommitted edits, untracked files, and non-source files such as credential files, databases, datasets, and documentation are not copied. This extension allowlist is not a secret scanner; review committed source for embedded credentials before sharing it with a model provider. The snapshot manifests retain revisions and per-file hashes. Corpus contents are never executed.

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

`experiments/suites/stratified.json` contains 24 source-checked questions, four in each category:

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
  --questions /path/to/retrieval-mcp/experiments/suites/questions.example.json \
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
