# Archived question sets

LOC-BENCH supersedes the suites in this repository **as the instrument for new studies**. It was
curated by people who are not us, its authors filtered it to function-level edits, it was built
to mitigate contamination, and it carries a published baseline — LocAgent reports 77.4%
function-level Acc@10 on it, which is the first external calibration point this project has had.
Every suite in `../` was authored by an agent session inside this repository, with `AGENTS.md`
— the claim, the arms, the expected direction — already in context. `experiments/locbench_suite.py`
compiles LOC-BENCH into this project's schema; `experiments/commit_suite.py` remains the fallback
for Java, Go and C, which LOC-BENCH does not cover.

## What is in here

Question sets that nothing in this repository cites any more — no runner, no test, no document,
no published figure. They are kept rather than deleted because a suite is evidence about what was
asked at the time, and because two of them record decisions rather than data:

- `etcd_caller_questions.json` — 39 questions, built, gated and deliberately never run. It
  answers "does attribution change answers, or only cost", and that question has not been worth
  $16.
- `perf_v020_cu_callers_02_questions.json` — contains `cu-callers-01`, which does not compile:
  `src/uu/head/src/head.rs` defines `print_n_bytes` twice under opposing `#[cfg]` gates, so
  `path::name` attribution cannot say which variant calls `send_n_bytes`. `validate_suite.py` has
  refused it since the namesake guard landed. Repairing it means re-authoring the question or
  dropping it, which changes the suite's bucket counts, so it is a decision rather than a fix.

## What is deliberately **not** in here

Superseded as an instrument is not the same as retracted as evidence. These stay in `../`
because a published figure would otherwise have no suite behind it, or because code depends on
them:

| Suite | Why it stays |
|---|---|
| `django_heldout_questions.json` | The held-out study: 29/29/28 of 30, −33.5% input tokens against native |
| `etcd_mixed_questions.json` | The replication: 270 trials, 87/89/89, −42.4% against native |
| `linux_kernel_questions.json` | The kernel arc, +16.4% → +1.0% over four studies |
| `stratified.json` | Pins this repository's own source; `test_stratified` exists to force a review of any `src/` edit |
| `comparison_questions*.json`, `v2_questions_draft.json`, `competency_questions.json`, `factor_smoke.json`, `project_questions.json`, `django_development_questions.json`, `django_suite_manifest.json` | Imported by runners or tests |
| `questions.example.json` | The documented schema example |
| `gson_commit_questions.json` | The `commit_suite.py` fallback's worked example |

Four more — `django_hard_questions.json`, `django_necessity_questions.json`,
`django_retrieval_strategy_questions.json`, `django_tool_trial_questions.json` — are named only
in `experiments/README.md` prose. They are archive candidates once that prose is rewritten, and
were left alone rather than silently orphaning the text that describes them.
