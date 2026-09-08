# Separating availability, first-tool policy, and syntax help

Status: offline design, not an execution-ready study. Historical results and prompts remain frozen. No model calls are authorized by generating a plan.

## Three estimands

| Factor | Manipulation | Matched comparison | What it does not establish |
|---|---|---|---|
| Availability | A/B/C/D | Hold free routing and syntax help fixed | Benefit of free routing versus a policy |
| First-tool policy | Free / lexical-first / semantic-first, under D | Hold all tools and syntax help fixed | Benefit versus a fully fixed multi-step policy |
| Syntax help | Baseline / primer | Hold availability and policy fixed | Intrinsic model competence |

The overlapping design has 12 unique conditions per question: 4 availability levels × 2 help levels with free routing, plus 2 constrained-first policies × 2 help levels under D. The D/free conditions are shared controls, not duplicated. It cannot estimate availability-by-policy interactions outside D. All tools stay visible under D; a policy must not be implemented by silently removing competing tools.

The primer gives concrete syntax information for regex alternation, pagination, and file reads. It deliberately supplies no task-specific identifiers or route recommendation. It bundles three corrections, so any benefit belongs to the bundle, not regex knowledge alone. Prompt length changes: count primer tokens in total input cost and do not claim a token-matched placebo control.

## Offline planning

`plan_factors.py` accepts the existing list-of-question-objects format, constructs isolated prompts, generates one-factor contrasts, hashes prompts and question input, and shuffles a balanced schedule. Gold and category labels are not inserted into prompts. Existing output files are refused.

```sh
python3 experiments/plan_factors.py --questions /path/to/reviewed-questions.json \
  --output /path/to/new-factor-plan.json --repetitions 1
python3 -m unittest discover -s experiments -p 'test_plan_factors.py' -v
```

Use old questions only to validate this machinery, not as a newly independent confirmatory sample. One repetition of 12 questions would already be 144 trials; this is a plan count, not a request to spend that quota. Start with offline checks and a separately approved small feasibility run.

## Probe machinery

`competency_runner.py` runs the synthetic probes in `competency_questions.json` against the real MCP server through `policy_gate.py` under free routing, one availability profile and both syntax-help levels. It refuses a probe whose reference evidence needs a tool the profile omits, so structural probes cannot run silently under A or C. `--client claude` requires `--allow-model-usage` and an explicit `--model`; nothing else launches a model. Scoring is `competency-strict-v1`: the typed answer, the answer format, and a behaviour constraint read from the delivered MCP results (regex flag, page limits, read range), so a right value reached by the wrong call does not pass. The gate now optionally records full tool responses (`response_log`) next to `policy.jsonl`; factor runs leave it off.

```sh
python3 experiments/competency_runner.py --output /path/to/new-probe-run --profile B
python3 -m unittest discover -s experiments -p 'test_competency*.py' -v
```

Scripted mode replays each probe's reference actions and derives its answer from the returned results. It verifies plumbing, gating, capture and scoring only — it measures no model behaviour, and it ignores the prompt, so it cannot estimate the primer effect. The five syntax probes have fixture agents; the six structural probes have none and are refused under `--client scripted`.

## Before any model trial

1. Freeze independently reviewed tasks and typed gold. Include exact, conceptual, direct-call and transitive tasks, with duplicate names, higher-order references, absent evidence and abstention cases. Separate whole-answer correctness from formatting. Explicitly define how conflicting prose is scored; do not fit the rule to new answers.
2. Pin model/client/effort, corpus/server hashes, cache conditions, tool/turn/response budgets and timeouts. Use fresh sessions. Block comparisons by repository, question and repetition; never pair across factor levels other than the factor being estimated. Record initialization contamination and failed trials.
3. Implement first-tool constraints in a new runner, without modifying the historical runner. Instruction text alone does not guarantee compliance. Record the first attempted tool and policy violations; any enforcement message and retry count against budget. Retain assigned-condition outcomes as primary; compliant-only subsets are secondary and outcome-selected. Do not silently reroute, inject query arguments, or drop violations.
4. Measure syntax competence separately on synthetic, repository-independent probes: literal versus regex search, alternation versus literal pipes, pagination, bounded file reads. Score whether arguments retrieve the known evidence, not whether they match one preferred spelling. Evaluate in separate sessions so probes do not train benchmark sessions. Baseline/primer probes test the bundle's effect; deterministic fake-agent probes test plumbing only, not a model's competence.
5. Obtain explicit model-usage approval. No automatic resume, model download, provider switch, or paid fallback.

## Focused first contrast: does syntax help substitute for tool availability?

Retrospective evidence (`query_pathology.py` over the 144 ModelShare trials, no model calls):

| Profile | Calls | Empty-result calls | Escaped-pipe regex queries | Truncated pages not followed |
|---|---:|---:|---:|---:|
| A | 172 | 32 (18.6%) | 15 | 14 |
| B | 124 | 19 (15.3%) | 0 | 6 |
| C | 118 | 10 (8.5%) | 0 | 2 |
| D | 100 | 8 (8.0%) | 0 | 0 |

Every escaped-pipe query in the study occurs under A, and 13 of the 15 fall in the three A trials of
`identity-check-caller` — the question supplying 53 of the 72 net calls A spends over D. Deleting
empty calls arithmetically leaves 48 of the 72, and 35 of those still belong to that one question.
So query pathology is A-specific and material, but it does not by itself explain the outlier, and
retrospection cannot separate the two accounts: the pathology may appear under A precisely because
grep is the only lever there. Multi-word literal queries appear under every profile (A 18, B 13,
C 15, D 8) and are not an A-specific behaviour.

The first authorized trial should therefore estimate one thing: **how much of A's penalty a syntax
primer removes, measured against the availability effect on the same questions.**

Cells: `A-free-baseline`, `A-free-primer`, `D-free-baseline`. Primary contrasts, blocked by question
and repetition: `A-primer − A-baseline` (syntax help under scarcity) and `D-baseline − A-baseline`
(availability), with `A-primer − D-baseline` reported as the substitution gap. `D-free-primer` is
omitted from this round; without it no availability-by-help interaction is estimable, and the
substitution gap is descriptive.

Primary outcome is retrieval calls to a correct answer, with correctness a gate rather than the
estimand: v1 payload matches were 34/34/34/36 of 36, so correctness alone cannot discriminate these
conditions at any feasible sample size. Report empty-call share, escaped-pipe and pagination counts,
bytes, and separated token classes alongside. Question selection must be fixed before running and must
include discovery-hard items; selecting questions by their v1 outcomes would fit the sample that
generated the hypothesis. Predictions to record before the run: the primer reduces A's empty-call
share toward B/C/D levels, and it removes less of the call gap than availability does.

## Analysis commitments

Primary availability contrasts: B−A, C−A, D−A within each help level under free routing. Primary first-tool contrasts: lexical-first−free and semantic-first−free within each help level under D. Primary syntax contrasts: primer−baseline within each of the six availability/policy combinations. Other generated one-factor contrasts are exploratory, not additional primary claims.

Report answer correctness, budget exhaustion, policy adherence, total retrieval calls and latency, retrieval bytes, and separate ordinary/cache-creation/cache-read input tokens. Preserve every paired observation. Aggregate by question before broad summaries; repetitions are not independent tasks. Report category means/medians and leave-one-question-out sensitivity. Keep all-assigned results distinct from both-correct or compliant-only subsets.

Evidence-support annotations must identify the claim, supporting source, missing scope/binding evidence, and whether the model recovered or abstained. An inline snippet, earlier read, later read, and lexical result are distinct evidence channels. No later read does not imply no verification; a later read does not imply a supported conclusion. Blind independent annotation is preferable; disclose single-agent review when used.

These experiments separate availability from first-action policy and syntax assistance. A complete fixed-policy router and a direct causal claim about intrinsic competence remain outside this initial design.
