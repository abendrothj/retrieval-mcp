# Routing experiments

The experiment asks whether one model, given separate retrieval tools, chooses useful evidence. `benchmark.py` runs independent model sessions under A/B/C/D. `analyze.py` reports selection patterns and matched retrieval counts. Both use Python 3.11+ standard libraries and POSIX subprocess groups; the server and semantic backend remain Rust.

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

Repairing the two wrong golds and accepting prose that names every gold symbol — a sensitivity
analysis, not a change to the frozen score — gives 25/30 for `retrieval-mcp`, 22/30 for
`native-control`, and 22/30 for `zvec-grep`, with paired discordance of 4 to 1 in this server's
favour against each competitor. That is a larger separation than the frozen numbers show, and it is
still too small to claim on 15 questions. The correction also destroys two more questions by
saturation: under corrected gold, 8 of 15 are solved by everyone and 2 by nobody, leaving 5
discriminating. The suite must be rebuilt before any competitive claim, and the rebuild needs a
development set for tuning difficulty and a held-out set frozen before the final comparison.

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
