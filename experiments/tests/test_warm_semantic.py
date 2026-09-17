import json
from pathlib import Path
import sys
import tempfile
import unittest

import benchmark
from warm_semantic import warm

HERE = Path(__file__).resolve().parents[1]
SERVER = HERE.parent/"target/debug/retrieval-mcp"
FAKE = [sys.executable, str(HERE/"fixture_backends.py"), "--fake-semantic"]


class WarmSemanticTests(unittest.TestCase):
    def test_warms_a_cache_outside_the_corpus(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary)/"cache"
            summary = warm(SERVER, HERE/"sample_repo", cache, FAKE, 30)
            self.assertEqual(summary["root"], str((HERE/"sample_repo").resolve()))
            self.assertGreaterEqual(summary["seconds"], 0)
            self.assertTrue(cache.is_dir())

    def test_cache_inside_the_corpus_is_refused(self):
        with self.assertRaises(ValueError):
            warm(SERVER, HERE/"sample_repo", HERE/"sample_repo/cache", FAKE, 30)

    def test_backend_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(RuntimeError):
                warm(SERVER, HERE/"sample_repo", Path(temporary)/"cache",
                     [sys.executable, "-c", "raise SystemExit(1)"], 30)


if __name__ == "__main__":
    unittest.main()
