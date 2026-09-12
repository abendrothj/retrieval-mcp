import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import reproduce_heldout

HERE = Path(__file__).resolve().parent


def pinned_manifest():
    manifest = json.loads((HERE / "django_suite_manifest.json").read_text(encoding="utf-8"))
    return manifest


class VerifyTests(unittest.TestCase):
    """A corpus that differs by one byte is a different experiment and must fail loudly."""

    def corpus(self, directory, body="x = 1\n"):
        # Resolved, because a temporary directory on macOS hangs off the /tmp symlink.
        root = Path(directory).resolve() / "corpus"
        (root / "django/apps").mkdir(parents=True)
        (root / "django/apps/registry.py").write_text(body, encoding="utf-8")
        return root

    def manifest_for(self, root):
        fingerprint = reproduce_heldout.comparison_runner.source_fingerprint(root)
        manifest = pinned_manifest()
        manifest["corpus"] = fingerprint
        return manifest

    def test_an_exact_rebuild_reports_no_problems(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.corpus(directory)
            _, problems = reproduce_heldout.verify(root, self.manifest_for(root))
        self.assertEqual(problems, [])

    def test_one_changed_byte_fails_the_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.corpus(directory)
            manifest = self.manifest_for(root)
            (root / "django/apps/registry.py").write_text("x = 2\n", encoding="utf-8")
            _, problems = reproduce_heldout.verify(root, manifest)
        self.assertTrue(any("corpus sha256" in problem for problem in problems), problems)

    def test_a_repinned_question_set_fails_the_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.corpus(directory)
            manifest = self.manifest_for(root)
            manifest["heldout"] = {**manifest["heldout"], "sha256": "0" * 64}
            _, problems = reproduce_heldout.verify(root, manifest)
        self.assertTrue(any("django_heldout_questions.json" in p for p in problems), problems)


class ShippedPinsTests(unittest.TestCase):
    def test_the_manifest_still_pins_the_question_sets_this_repository_ships(self):
        manifest = pinned_manifest()
        for name, filename in (("development", "django_development_questions.json"),
                               ("heldout", "django_heldout_questions.json")):
            digest = hashlib.sha256((HERE / filename).read_bytes()).hexdigest()
            self.assertEqual(digest, manifest[name]["sha256"], filename)


if __name__ == "__main__":
    unittest.main()
