"""Synthetic retrieval contracts, not model-competence or semantic-quality scores.

Gold stays outside the searchable corpus. Reference actions are evidence plans,
not answers to be injected into a model prompt. Their requested syntax/range/page
constraints need separate behavior grading when real model trials are authorized.
"""
import ast
import json
import os
from pathlib import Path
import tempfile
import unittest

import benchmark


HERE = Path(__file__).resolve().parents[1]
CORPUS = HERE / "competency_repo"
QUESTIONS = json.loads((HERE / "suites/competency_questions.json").read_text())


class CompetencyTests(unittest.TestCase):
    def test_gold_has_independent_source_evidence(self):
        self.assertEqual(len(QUESTIONS), 11)
        self.assertEqual(len({q["id"] for q in QUESTIONS}), 11)
        self.assertEqual({p.name for p in CORPUS.iterdir()},
                         {"tokens.py", "flow.py", "alpha.rs", "beta.rs"})
        for question in QUESTIONS:
            self.assertEqual(set(question["expected_json"]), {"answer"})
            self.assertTrue(question["evidence"])
            self.assertTrue(question["reference_actions"])
        # Verify calls with Python's independent stdlib parser, not the server index.
        functions = {node.name: node for node in ast.parse((CORPUS / "flow.py").read_text()).body}
        calls = {name: [node.func.id for node in ast.walk(body)
                        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)]
                 for name, body in functions.items()}
        self.assertEqual(calls, {"leaf": [], "middle": ["leaf"], "entry": ["middle"],
                                 "callbacks": [], "dispatch": ["handler"]})
        callback = functions["callbacks"].body[0]
        self.assertIsInstance(callback, ast.Return)
        self.assertEqual(ast.dump(callback.value), "List(elts=[Name(id='leaf', ctx=Load())], ctx=Load())")
        self.assertEqual(functions["dispatch"].args.args[0].arg, "handler")

    def test_reference_actions_against_real_mcp(self):
        server = HERE.parent / "target/debug/retrieval-mcp"
        self.assertTrue(server.exists(), "run cargo build --bin retrieval-mcp first")
        with tempfile.TemporaryFile(mode="w+") as stderr:
            client = benchmark.MCP([str(server), "--root", str(CORPUS), "--profile", "B"],
                                   os.environ.copy(), CORPUS, stderr, 10)
            try:
                client.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                    "clientInfo": {"name": "offline-competency-contract", "version": "1"}})
                client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                responses = {}
                for question in QUESTIONS:
                    responses[question["id"]] = []
                    for action in question["reference_actions"]:
                        result = client.request("tools/call", action)
                        self.assertFalse(result.get("isError"), (question["id"], result))
                        responses[question["id"]].append(result["structuredContent"])
            finally:
                client.close()
        gold = {q["id"]: q["expected_json"]["answer"] for q in QUESTIONS}
        for name in ("literal-pipe", "regex-alternation", "regex-literal-pipe", "paginate-entries"):
            lines = [hit["line"] for page in responses[name] for hit in page["results"]]
            self.assertEqual(lines[0] if name == "literal-pipe" else lines, gold[name])
        pages = responses["paginate-entries"]
        self.assertEqual([p["next_offset"] for p in pages], [2, 4, None])
        self.assertEqual([p["has_more"] for p in pages], [True, True, False])
        bounded = responses["bounded-read"][0]
        self.assertEqual([line["line"] for line in bounded["lines"]], [9])
        self.assertEqual(ast.literal_eval(bounded["lines"][0]["text"].split("=", 1)[1].strip()),
                         gold["bounded-read"])
        self.assertEqual(bounded["next_line"], 10)
        symbols = responses["duplicate-definition"][0]
        self.assertEqual({"unique": len(symbols["results"]) == 1,
                          "paths": sorted(hit["path"] for hit in symbols["results"])},
                         gold["duplicate-definition"])
        self.assertFalse(symbols["coverage"]["complete"])
        direct = responses["direct-not-transitive"][0]["results"]
        self.assertEqual([hit["caller"] for hit in direct], gold["direct-not-transitive"])
        self.assertTrue(all(hit["kind"] == "call" for hit in direct))
        chain = responses["transitive-chain"]
        self.assertEqual([chain[2]["results"][0]["caller"],
                          chain[1]["results"][0]["caller"], chain[1]["results"][0]["name"]],
                         gold["transitive-chain"])
        refs = responses["reference-not-call"][0]["results"]
        self.assertTrue(any(hit["caller"] == gold["reference-not-call"] and hit["kind"] == "possible_reference"
                            for hit in refs))
        unresolved = responses["unresolved-dispatch"][0]["results"]
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0]["candidate_count"], 0)
        self.assertEqual(unresolved[0]["resolution"], "unresolved")
        self.assertIsNone(gold["unresolved-dispatch"])
        self.assertEqual(len(responses["unresolved-dispatch"][2]["results"]), 1)
        for response in responses["absent-definition"]:
            self.assertEqual(response["results"], [])
            self.assertFalse(response["has_more"])
        self.assertIsNone(gold["absent-definition"])


if __name__ == "__main__":
    unittest.main()
