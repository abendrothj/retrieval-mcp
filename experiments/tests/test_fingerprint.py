import subprocess
import tempfile
import unittest
from pathlib import Path

import benchmark


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class FingerprintProvenanceTests(unittest.TestCase):
    """A recorded revision must belong to the corpus, not to whatever repository encloses it."""

    def corpus(self, parent):
        root = Path(parent) / "corpus"
        root.mkdir()
        (root / "sample.py").write_text("x = 1\n", encoding="utf-8")
        return root

    def test_a_snapshot_inside_an_unrelated_repository_records_no_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            outer = Path(directory).resolve()
            git("init", "-q", "-b", "main", cwd=outer)
            (outer / "README.md").write_text("outer\n", encoding="utf-8")
            git("add", "README.md", cwd=outer)
            git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "outer", cwd=outer)
            root = self.corpus(outer)
            self.assertIsNone(benchmark.fingerprint(root)["git_head"])

    def test_a_corpus_that_is_itself_a_checkout_records_its_own_head(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.corpus(Path(directory).resolve())
            git("init", "-q", "-b", "main", cwd=root)
            git("add", "sample.py", cwd=root)
            git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "corpus", cwd=root)
            expected = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                                      capture_output=True, text=True, check=True).stdout.strip()
            self.assertEqual(benchmark.fingerprint(root)["git_head"], expected)

    def test_content_hash_ignores_the_enclosing_repository(self):
        with tempfile.TemporaryDirectory() as bare, tempfile.TemporaryDirectory() as nested:
            plain = self.corpus(Path(bare).resolve())
            outer = Path(nested).resolve()
            git("init", "-q", "-b", "main", cwd=outer)
            enclosed = self.corpus(outer)
            self.assertEqual(benchmark.fingerprint(plain)["sha256"],
                             benchmark.fingerprint(enclosed)["sha256"])


if __name__ == "__main__":
    unittest.main()
