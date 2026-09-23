# What the field already knows, and what of it applies here

Every finding in this repository is measured against this repository's own arms. That is the
point — but it means the record has had no outside number in it, and until 2026-09-23 the only
external links anywhere were three vendor CLI pages. A claim like "quality ties" is much weaker
without an anchor for what the task's ceiling looks like when someone else measures it.

**Admission rule.** An entry earns its place only if it changed a decision here or calibrates a
number here. This is not a reading list. Each entry carries what it claims, where it comes from,
how good that evidence is, what it changed, and — the part that matters most — **how this
project's setting differs**, because most published figures do not transfer whole.

**Evidence grades.** `peer-reviewed` · `preprint` (arXiv, not obviously reviewed) · `dataset`
(the artifact itself) · `vendor` (a company's own blog about its own product) · `blog`
(independent, unreviewed) · `ours` (measured in this repository).

**Where this file lives, deliberately.** Not the root `AGENTS.md`. Codex loads the nearest
`AGENTS.md` up to the git root into the first user message of every trial, so writing there is
writing into the next run's prompt. A file under `experiments/` is not loaded.

---

## 1. Function-level localisation is a different task from file-level, and much harder

**Claim.** LocAgent reports 94.16% file-level Acc@5, 87.59% module-level Acc@10 and **77.37%
function-level Acc@10** on SWE-Bench-Lite. An independent trajectory study finds that among
*successful* agent patches, over 90% touch the same file as the gold patch and only about **27%
the same function**.

**Source.** LocAgent, arXiv 2503.09089 / ACL 2025 (`peer-reviewed`); Understanding Code Agent
Behaviour, arXiv 2511.00197 (`preprint`).

**What it changed.** It explains `runs/gson-contamination-20260922`, where 19 of 22 questions
scored zero for every arm: that suite demanded an exact `path::name` at effectively k=1, and for
15 of 25 questions an *exhaustive set* of them. That is well past the operating point the field
reports for its best systems. It is why any future suite should report a file tier beside the
function tier, and grade with Acc@k rather than set equality.

**How we differ.** Those figures are k=5 and k=10 with multiple attempts. This project grades one
answer, exactly, and calls anything else zero. The comparison is not like for like, and the
direction of the difference is that our grading is strictly harsher.

---

## 2. Half of SWE-bench is a retrieval problem, and the other half leaks

**Claim.** 51.3% of SWE-bench Lite instances never name the file that must be fixed in the issue
text (48.3% on Full); including `hints_text` drops that to 38.0%. Separately, manual review found
**32.67%** of model-marked successes had the answer in the issue description or comments, and
31.08% of accepted patches passed only because the tests were too weak to reject them.
Localisation experiments show LLMs identifying correct files at **3–6× higher rates** on
SWE-bench than on fresh benchmarks, from ticket text alone.

**Source.** swebench-localization analysis, github.com/hammas159/swebench-localization (`blog`,
but the method is stated and reproducible); SWE-Bench+, arXiv 2410.06992 (`preprint`);
contamination figures via SWE-bench 500 summaries (`blog`).

**What it changed.** Three things. It justifies `commit_suite.leaks` and `locbench_suite`'s
refusal of any statement naming a gold symbol or its file — 24% of LOC-BENCH by the symbol rule
alone (`ours`). It is the quantitative case for the whole prompt-contamination workstream. And
its conclusion is the same one this project reached independently: *a single score cannot
separate retrieval failure from reasoning failure.*

**How we differ.** The field treats leakage as a covariate to stratify on; we use it as a hard
filter, because `validate_suite.py` refuses a leaked identifier. Our sample is therefore
deliberately the non-leaking half, which is a different population from the published baselines.

---

## 3. Gold derived from a diff is noisy, and the field's answer is human curation

**Claim.** CORE-Bench builds Level-2 gold as patch-aligned chunks matched to pre-PR snapshots;
Agent Retrieval Bench derives evidence from changed files in pull requests, post-review commits
and resolved test failures, freezing snapshots at base commits and verifying "zero fatal leakage
from exact paths, patches, or fix commits". But SWE-Bench Pro uses a **manually curated** gold
set precisely because PR-derived gold "can be substantially larger" than the true answer.

**Source.** CORE-Bench, arXiv 2606.11864 (`preprint`); Agent Retrieval Bench, arXiv 2607.24882
(`preprint`); SWE-Bench Pro via published comparisons (`blog`).

**What it changed.** It predicted, and explains, `commit_suite.py`'s failure. "The definitions a
diff touched" is the change's *locus*, not the answer to its prose: "Add ProGuard / R8
integration tests" keyed to `ConstructorConstructor::newUnsafeAllocator` is unanswerable, and all
four arms answered it differently (`ours`). Nobody has made diff-derived gold work mechanically;
that is why `commit_suite.py` is now a fallback for languages LOC-BENCH does not cover rather
than the primary instrument.

---

## 4. Degraded and oracle control arms are established practice

**Claim.** Agent Retrieval Bench runs a seed-intervention pilot comparing *no seed, random
context, lexical/embedding seeds, and oracle gold*, reporting retrieval seeds needing 5.4
post-seed tool calls against random context's 8.5. The RAG-robustness literature uses random and
shuffled retrieval as counterfactuals specifically to rule out "the gain came from prompt
lengthening, not retrieval".

**Source.** Agent Retrieval Bench, arXiv 2607.24882 (`preprint`); Evaluating the Retrieval
Robustness of LLMs, arXiv 2505.21870 (`preprint`).

**What it changed.** It is the external warrant for `degrade_server.py` and for the
`oracle-seeded` arm added in `comparison_runner.py`. It also supplies the discipline this project
was missing: an oracle upper bound run *in the pilot*, so "the suite is too hard" and "the arms
are bad" can be told apart before the money is spent rather than after.

**How we differ, and a caution.** Our oracle seeds the *file*, never the symbol. On LOC-BENCH
that turned out to be worth about one question in twenty-five at both corpus sizes (`ours`,
`runs/locbench-pilot-20260923` and `runs/locbench-hard-20260923`) — because finding the file was
never the bottleneck. A file-level oracle cannot create head-room on a function-level question.
Anyone reusing this arm should size that first.

---

## 5. Code execution with MCP — the headline number does not transfer, the mechanism might

**Claim.** Anthropic's "code execution with MCP" reports **150k → 2k tokens (98.7%)** by having
the model write code that calls MCP servers in a sandbox instead of calling tools turn by turn.
Six mechanisms are named: progressive disclosure of tool definitions, context-efficient results
(filter in the sandbox, log only what matters), control flow (loops and conditionals without a
model turn each), privacy, state persistence, and reusable skills.

**Source.** anthropic.com/engineering/code-execution-with-mcp (`vendor`); independent write-ups
concur on the mechanism and note the figure is a best case for definition-heavy, data-heavy
workflows (`blog`).

**What it changed.** It is the next candidate for the token work — but only half of it.

**How we differ, precisely.** The 98.7% is dominated by *progressive disclosure of tool
definitions*: not loading hundreds of schemas up front. This project already measured its
four-tool surface at **zero prompt tokens** (stub-server probe, 13,802 either way, `ours`), so
that half is banked and cannot be won twice. What may transfer is the **control-flow half** —
removing a model round trip per dependent step. That matters here because round trips are where
this project's cost actually is: a dependent hop prices at ~15,000 tokens against ~2,400 for a
20 KB payload (`ours`, `runs/token-metric-20260921`).

**The distinction that keeps this from being a repeat.** `runs/composed-callers-20260922`
collapsed a two-hop into one server-side call and failed: "the model's two hops are
error-correcting; removing the check removes the correction." Code mode is not that. The model
still writes the control flow and still sees intermediate results; it just does not pay a request
per step. It may dodge the failure mode that killed composed callers, and that is the hypothesis
worth testing rather than assuming. Codex exposes `code_mode` (under development, off) and
`code_mode_host` (stable, on) as feature flags, so it is probeable on this client without
building anything.

---

## 5b. Bash alone beats typed tools, and programmatic tool calling does not rescue them

**Claim.** An enterprise-agent study compares five tool interfaces — typed tools, typed tools
*plus* bash, bash alone, bash with agent-synthesized persistent tools, and programmatic tool
calling (PTC, i.e. code that may only call a typed catalogue). **Bash alone outperforms typed
tools on both benchmarks, by 21.8–24.5 points on TheAgentCompany and 4.8–7.4 on APEX-Agents,
while using 19–72% fewer total tokens.** Adding typed tools to bash produces no detectable
pooled gain. PTC uses fewer tokens than direct typed calls but "generally underperforms bash
alone in both quality and cost efficiency".

**Source.** Is Bash All You Need?, arXiv 2609.11999 (`preprint`, 2026-09-10).

**What it changes.** Two things, and neither is comfortable.

First, "typed tools plus bash" is exactly this project's `retrieval-mcp` arm on Codex, and the
paper finds it no better than bash alone. That is an independent replication of what two
LOC-BENCH pilots found here: native 15/17 and 19/25 against the MCP arm's 14/17 and 18/25, at
roughly twice the tokens (`ours`).

Second, it tempers the code-mode recommendation in §5 before it was acted on. PTC is the nearest
published analogue to code mode over a fixed tool catalogue, and it lost to bash. Code mode may
still be worth a run — it is cheap to test on this client — but it should be registered as a
long shot rather than the obvious next lever.

**How we differ.** Enterprise tasks, not code localisation, and their typed catalogues are far
larger than four tools — the regime where schema cost dominates and where bash's advantage is
most expected. Our four-tool surface costs zero prompt tokens (`ours`), so the mechanism that
hurts their typed arm is absent here. That is a real difference and it is the reason this is not
a verdict on the four-tool surface.

---

## 5c. Retrieval helps a coding agent only above a precision threshold

**Claim.** CodeGrep observes that coding agents "spend much of their token budget finding the
file to patch, rather than patching it" — a 30B OpenHands agent averages 23 rounds and 631K
tokens per resolved SWE-Bench Verified issue. Feeding a downstream agent retrieved candidates
helps or hurts depending on retriever precision: **BM25 at precision 0.375 degrades the agent,
Jina at 0.445 is neutral, and their trained retriever at 0.677 crosses the threshold** where
retrieval begins to reduce rollout cost. At 0.677 it preserves resolve rate while cutting rounds
15% and tokens 19%.

**Source.** CodeGrep, arXiv 2608.05886 (`preprint`, 2026-08-06).

**What it changes.** This is the most directly explanatory result found so far, because this
project has already measured its own number on the same axis. `runs/precision-20260922`: the
gold definition is at rank 1 for **8 of 21** kernel questions on verbatim queries (0.38) and
**10 of 21** on the agent's own queries (0.48). Both sit at or below the band where the
published threshold says retrieval is neutral-to-harmful. If that threshold transfers, it is a
mechanism for why the MCP arm costs more tokens without resolving more: the agent pays for a
page, does not trust it, and searches again.

It also reframes three years of this project's optimisation history. "Precision at rank 1 is the
thing to attack" was already the standing conclusion of the precision thread, reached from the
inside. This is the first outside evidence that the conclusion has a *threshold* rather than a
gradient, and that below it retrieval is not merely weak but negative.

**How we differ.** Their precision is measured over candidate *files* for patch generation; ours
is rank-1 of a gold *definition* for a localisation answer. The numbers are not directly
comparable and the threshold should be treated as an order of magnitude, not a line. Testing it
here would mean varying retrieval precision deliberately and watching agent cost — which is a
study this project has never run and which `degrade_server.py` is most of the way to supporting.

---

## 6. The "MCP burns 35× the tokens of a CLI" folklore is not measured the way we measure

**Claim.** Widely repeated: MCP carries 35× the token overhead of a CLI; an MCP schema load
consumes 55,000 tokens before the first call, roughly 275× a CLI interaction; CLI agents score
202.1 on a Token Efficiency Score against MCP's 152.3; CLI reliability 100% against MCP's 72% on
complex tasks.

**Source.** Vendor and independent blogs (`blog`/`vendor`). No peer-reviewed measurement of this
comparison was found.

**What it changed.** Nothing — and recording that is the point. This project's own probe measured
the four-tool surface at **13,802 prompt tokens either way**, i.e. zero cost to attach (`ours`).
One of the same sources concedes that with lazy tool loading MCP ends at 48–50k context against a
CLI's 45–48k, which is a different claim from 35×. The published folklore is mostly measuring
schema-load behaviour this server does not exhibit on this client.

**What is real in the same area.** Codex truncates shell output on the way into context
(coefficient ~0.09 measured here) and forwards MCP results whole, so the *client* trims for the
competitor and not for us (`ours`, `runs/per-request-20260921`). That is a genuine asymmetry and
it is the argument for trimming payloads server-side rather than hoping the client does it.

---

## 7. Context management: truncation, compaction, summarisation, sub-agents

**Claim.** The standard ladder is truncation (cap each tool result), compaction (replace older
tool-result turns with placeholders, no model call), summarisation (model-generated replacement
under further pressure), and sub-agents (isolate exploration so the parent sees only the result).
Practitioners report quality degrading well before the context limit — around 256k on a 1M
window — and recommend compacting before that zone rather than on error.

**Source.** Arize, Redis and LangChain engineering write-ups (`blog`); Inside the Scaffold, arXiv
2604.03515, and Code as Agent Harness, arXiv 2605.18747 (`preprint`).

**What it changed.** Nothing yet. Logged because it is the menu for the *secondary* term in this
project's token gap: 1.20× more context per request, against 1.74× more requests (`ours`,
`runs/locbench-hard-20260923`). Sub-agents in particular would isolate exploration, but they
change what "one trial" means and would break comparability with every archived run.

**One measured number worth holding.** A controlled comparison of five trimming strategies finds
conventional ones (recency, relevance, summarisation) saving ~60% of tokens at 66.6–77.3% task
success, while protocol-aware trimming reaches 92.2% and adaptive budget guardrails reach 96.0%
success with 56.0% savings and 1.0% cascading failure. Trimming is not free, and the naive
versions cost a fifth to a third of task success for the tokens they save — arXiv 2609.16461
(`preprint`).

---

## 8. Benchmarks for MCP servers measure tool *selection*, not retrieval quality

**Claim.** MCP-Bench, MCPVerse, MCPAgentBench, MCP-Zero and MCP-Flow evaluate agents choosing and
invoking tools across many servers — retrieval of the right *tool* from fuzzy instructions,
multi-hop planning, cross-domain orchestration.

**Source.** arXiv 2508.20453, 2508.16260, 2512.24565, 2506.01056, 2510.24284 (`preprint`).

**What it changed.** It ruled out an option. None of these measures retrieval quality or context
cost for a single retrieval server, so none is a substitute for the comparison this project runs.
It also means there is no established benchmark for the thing this project actually claims, which
is why the claim rests on its own paired within-run token accounting.

---

## 9. Methodological traps this project hit that the literature would not have warned us about

`ours` in every case. Kept here because they are the same *class* of error as the published ones.

- **A prompt prefix measured by alternating configurations measures the cache, not the prefix.**
  `--disable shell_tool` appeared to cost +9,600 tokens a request when the two conditions were
  interleaved. Run consecutively it converges 15,555 → 4,547 → 451 → 451, against 1,091 for the
  default: the flag is slightly *cheaper*. Same family as this project's existing trap that
  `tools/list` bytes are not model-facing tokens — count what the model is charged, after the
  cache settles.
- **`source_fingerprint` is location-sensitive.** ripgrep changes its ignore behaviour inside a
  git tree, so an identical corpus enumerates 6,823 files outside this repository and 6,822
  inside it. Materialise corpora where they will be used, or the copy check fails for a reason
  that has nothing to do with the copy.
- **`copytree` dereferences symlinks.** django's epub theme symlinks four icons; the copy gained
  four files the source listing never had. The fingerprint guard caught it.
- **On Codex an MCP arm keeps its shell unless it is taken away.** Every Codex comparison in this
  record, the kernel arc included, measures shell-plus-MCP against shell. `--disable shell_tool`
  removes it — verified: a session asked to run a command executes none and reports having no
  such tool.

---

## Standing summary

The field's evidence says four things this project should hold onto.

Function-level localisation is hard for everyone and our grading is harsher than anyone's.
Diff-derived gold needs human curation and mechanical mining does not substitute for it. Round
trips, not payload size, are where agentic token cost lives — which this project measured
independently before reading any of it.

And the fourth, which arrived last and is the least comfortable: **an independent study finds
bash alone beating typed tools on quality *and* tokens, with typed-tools-plus-bash — this
project's own arm shape on Codex — showing no detectable gain**; while a second finds that
retrieval only starts paying for itself above a precision of roughly 0.5, and this project's
measured rank-1 precision is 0.38 to 0.48. Those two together are a coherent mechanism for
every token result in `runs/locbench-pilot-20260923` and `runs/locbench-hard-20260923`, and they
point the next piece of work at precision rather than at payload, batching or code mode.

None of that is a verdict. Both are preprints, both differ from this setting in ways recorded
above, and neither measured this server. But they are the first outside evidence this record has
carried, and the honest reading is that they agree with the uncomfortable half of our own
numbers rather than the comfortable half.

## Verification

Every arXiv identifier in this file was checked against the arXiv API on 2026-09-23: all
fourteen resolve, and the titles match what is cited here. Non-arXiv sources are graded
`vendor` or `blog` in place and should be treated as weaker than the preprints, which are
themselves weaker than the one peer-reviewed entry.
