"""The row filter that decides whether an index looks broken.

`find_callers` omits `kind` when it is the default, which `src/index/mod.rs` states and the
payload diet in `runs/linux-diet-20260921` introduced. This audit went on testing
`kind == "call"` afterwards and so dropped every plain call row, reporting `caller_recall: 0.0`
against a working index for every language - the believable false finding this file exists to
stop coming back.
"""
import unittest

import language_audit


class PlainCall(unittest.TestCase):
    def test_an_omitted_kind_is_a_plain_call(self):
        """The diet omits the field rather than spelling the default."""
        self.assertTrue(language_audit.is_plain_call({"caller": "f", "path": "a.java"}))

    def test_an_explicit_null_kind_is_a_plain_call(self):
        self.assertTrue(language_audit.is_plain_call({"kind": None, "caller": "f"}))

    def test_an_explicit_call_kind_is_still_a_plain_call(self):
        """Older archived payloads spell it, and they must keep working."""
        self.assertTrue(language_audit.is_plain_call({"kind": "call", "caller": "f"}))

    def test_another_relation_is_not_a_call(self):
        for kind in ("import", "reference", "definition", "type_reference"):
            self.assertFalse(language_audit.is_plain_call({"kind": kind, "caller": "f"}), kind)


if __name__ == "__main__":
    unittest.main()
