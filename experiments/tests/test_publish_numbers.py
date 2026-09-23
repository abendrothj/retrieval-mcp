"""The published token column, and the conditions a figure is only a measurement under."""
import json
from pathlib import Path
import tempfile
import unittest

from publish_numbers import aggregate, prompt_document_evidence, quality_separation


def row(**tokens):
    return {"resolved_correct": True, "answered_without_evidence": False, "calls": 1,
            "context_token_turns": 10, "calls_to_first_hit": 1,
            "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cost": 0.0,
                       **tokens}}


class InputTokenTests(unittest.TestCase):
    def test_the_cached_prefix_is_counted_once(self):
        """`input` is already the whole context: Codex reports the cache as a subset of it and
        `end_to_end.steps_from` folds Claude's three fields into it. Adding `cache_read` published
        etcd at -44.6% where the same reports say -42.4%."""
        self.assertEqual(aggregate([row(input=100, cache_read=60)])["input_tokens"], 100)

    def test_an_arm_that_reuses_more_context_is_not_charged_for_it_twice(self):
        cold = aggregate([row(input=1000, cache_read=0)])["input_tokens"]
        warm = aggregate([row(input=1000, cache_read=900)])["input_tokens"]
        self.assertEqual(cold, warm)

    def test_output_and_reasoning_are_not_input(self):
        totals = aggregate([row(input=100, output=500, reasoning=400)])
        self.assertEqual(totals["input_tokens"], 100)


def launch(root, name, command, session=None):
    trial = root / name
    trial.mkdir(parents=True)
    (trial / "run.json").write_text(json.dumps({"agent_command": command}), encoding="utf-8")
    if session is not None:
        (trial / "codex-session.jsonl").write_text(session, encoding="utf-8")


class PromptDocumentTests(unittest.TestCase):
    """The leak found on 2026-09-22: a project document in the first user message of every Codex
    trial, invisible to the shell-command detector because it arrives as text."""

    def test_a_rollout_carrying_a_document_outvotes_a_launch_that_looks_clean(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            launch(root, "trial-0000", ["codex", "-c", "project_doc_max_bytes=0"],
                   session='{"text": "# AGENTS.md instructions for /repo"}\n')
            evidence = prompt_document_evidence([root])
        self.assertEqual(evidence["derived"], "present in both arms")
        self.assertEqual(evidence["rollouts_carrying_a_document"], 1)

    def test_one_unsuppressed_launch_is_enough_to_report_the_document(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            launch(root, "report/trial-0000", ["codex", "-c", "project_doc_max_bytes=0"])
            launch(root, "report/trial-0001", ["codex"])
            evidence = prompt_document_evidence([root])
        self.assertEqual(evidence["derived"], "present in both arms")
        self.assertEqual(evidence["launches_suppressing_the_document"], 1)

    def test_rehearsals_and_discarded_chunks_are_not_the_study(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            launch(root, "report/trial-0000", ["claude", "--settings",
                                               '{"claudeMdExcludes": ["/**"]}'])
            launch(root, "dry-run/trial-0000", ["codex"])
            launch(root, "discarded-contaminated-report/trial-0000", ["codex"])
            launch(root, "partial-auth-expired-report/trial-0000", ["codex"])
            evidence = prompt_document_evidence([root])
        self.assertEqual(evidence, {"derived": "excluded", "launches": 1,
                                    "launches_suppressing_the_document": 1, "rollouts_read": 0,
                                    "rollouts_carrying_a_document": 0})


class QualitySeparationTests(unittest.TestCase):
    """A saturated control cannot show a tie. Every study that published one is below the floor
    where a paired sign test could have reached significance."""

    def arms(self, control, treatment, trials):
        return {"native-control": {"correct": control, "trials": trials},
                "retrieval-mcp": {"correct": treatment, "trials": trials}}

    def test_four_discordant_answers_cannot_reach_significance_at_any_n(self):
        separation = quality_separation(self.arms(63, 59, 63))
        self.assertFalse(separation["separable"])
        self.assertIn("absence of power", separation["reading"])

    def test_the_kernel_loss_is_separated(self):
        self.assertTrue(quality_separation(self.arms(62, 56, 63))["separable"])

    def test_the_held_out_tie_is_a_failure_to_separate_not_equivalence(self):
        separation = quality_separation(self.arms(28, 29, 30))
        self.assertEqual(separation["net_resolved_difference"], -1)
        self.assertFalse(separation["separable"])

    def test_control_headroom_is_published_so_a_ceiling_is_visible(self):
        self.assertEqual(quality_separation(self.arms(63, 59, 63))["control_headroom"], 0)


if __name__ == "__main__":
    unittest.main()
