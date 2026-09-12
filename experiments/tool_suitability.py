"""Would each medium have found the gold evidence? No model, no routing - just the tools."""
import json, os, re, sys, tempfile
from pathlib import Path
sys.path.insert(0, "experiments")
import benchmark

ROOT = Path("../runs/projects-v2-suite/coreutils/corpus").resolve()
# The warm semantic cache lives beside the run artifacts, outside this repository; its location is
# an operator input, not a constant that could be correct on another machine.
CACHE = Path(os.environ.get("RETRIEVAL_SEMANTIC_CACHE_DIR")
             or sys.exit("set RETRIEVAL_SEMANTIC_CACHE_DIR to the warm coreutils cache")).resolve()
questions = json.load(open("experiments/v2_questions_draft.json"))
server = Path("target/release/retrieval-mcp").resolve()
backend = Path("target/release/examples/ollama_backend").resolve()
env = dict(os.environ, RETRIEVAL_SEMANTIC_CACHE_DIR=str(CACHE))

def strip_identifiers(text):
    """The question minus the code-ish tokens, i.e. what a description-only query looks like."""
    return " ".join(w for w in re.split(r"[\s,.]+", text)
                    if not re.search(r"[_:{}\"\[\]]|\.rs|::", w) and w not in {"JSON", "Return"})

with tempfile.TemporaryFile(mode="w+") as err:
    c = benchmark.MCP([str(server), "--root", str(ROOT), "--profile", "D",
                       "--semantic-command", json.dumps([str(backend)]),
                       "--timeout-seconds", "600"], env, ROOT, err, 900)
    try:
        c.request("initialize", {"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"suitability","version":"1"}})
        c.send({"jsonrpc":"2.0","method":"notifications/initialized"})
        def paths(tool, args):
            r = c.request("tools/call", {"name":tool,"arguments":args})
            if r.get("isError"):
                return []
            d = r.get("structuredContent") or {}
            return [h.get("path") for h in d.get("results", [])]
        print(f"{'question':34s} {'gold file':44s} {'sem@5':>6s} {'sem-desc@5':>11s}")
        hits = {"semantic": 0, "description": 0}
        for q in questions:
            gold = {e["path"] for e in q["evidence"]}
            full = paths("search_concept", {"query": q["question"], "limit": 5})
            desc = paths("search_concept", {"query": strip_identifiers(q["question"]), "limit": 5})
            a, b = bool(gold & set(full)), bool(gold & set(desc))
            hits["semantic"] += a; hits["description"] += b
            print(f"{q['id']:34s} {sorted(gold)[0][-44:]:44s} {str(a):>6s} {str(b):>11s}")
        print()
        print("semantic top-5 contains a gold evidence file:", hits["semantic"], "/", len(questions))
        print("  same, identifiers stripped from the query:", hits["description"], "/", len(questions))
    finally:
        c.close()
