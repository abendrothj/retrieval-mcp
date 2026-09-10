#!/usr/bin/env python3
"""Ask a model once per question for retrieval terms, with no repository access. Freezes an artifact.

The agent studies cannot say whether a model's retrieval advantage comes from rewriting a question
into codebase vocabulary or from iterating against evidence. Separating them requires a rewrite that
has never seen the corpus, the index, or any retrieval result: this script produces exactly that,
then stops. Study A.1 consumes the artifact deterministically, so the retrieval comparison is
repeatable even though the generation is not.

Charges the configured model. Requires --allow-model-usage, like every other runner here.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

INSTRUCTION = (
    "You turn a question about an unfamiliar codebase into search terms. You cannot see the "
    "codebase. Answer with one JSON object and nothing else, using exactly these keys:\n"
    '{"concepts": [...], "candidate_identifiers": [...], "terms": [...]}\n'
    "concepts: 2-5 short phrases naming the behaviour being asked about.\n"
    "candidate_identifiers: 3-8 plausible function, method, class, or constant names a programmer "
    "would use for it, in the conventions of the language implied by the question.\n"
    "terms: 3-10 literal words, symbols, or numbers likely to appear in the source or its comments.\n"
    "Do not answer the question. Do not explain."
)


def ask(model, variant, question, timeout):
    with tempfile.TemporaryDirectory() as directory:
        home = Path(directory) / "config"
        home.mkdir()
        # Deny every tool: this rewrite must not become a retrieval loop.
        (home / "opencode.json").write_text(json.dumps({"permission": {"*": "deny"}}))
        command = ["opencode", "run", "--format", "json", "--pure", "--model", model]
        if variant:
            command += ["--variant", variant]
        command += ["--dir", directory, f"{INSTRUCTION}\n\nQuestion: {question}"]
        environment = dict(
            os.environ,
            OPENCODE_CONFIG_DIR=str(home),
            XDG_CONFIG_HOME=str(Path(directory) / "xdg-config"),
            XDG_DATA_HOME=str(Path(directory) / "xdg-data"),
            XDG_CACHE_HOME=str(Path(directory) / "xdg-cache"),
            XDG_STATE_HOME=str(Path(directory) / "xdg-state"),
        )
        result = subprocess.run(command, env=environment, capture_output=True, text=True,
                                timeout=timeout)
    text = ""
    usage = {}
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "text":
            text += event["part"].get("text", "")
        elif event.get("type") == "step_finish":
            tokens = event["part"].get("tokens", {}) or {}
            usage = {"input": tokens.get("input", 0), "output": tokens.get("output", 0),
                     "cost_usd": event["part"].get("cost", 0)}
    return text, usage


def parse(text):
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except ValueError:
            continue
        if isinstance(value, dict) and {"concepts", "candidate_identifiers", "terms"} <= set(value):
            return {key: [str(item) for item in value[key] if str(item).strip()]
                    for key in ("concepts", "candidate_identifiers", "terms")}
    raise ValueError("model did not return the requested JSON object")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="deepseek/deepseek-v4-flash")
    parser.add_argument("--variant", default="high")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--allow-model-usage", action="store_true")
    args = parser.parse_args()
    if not args.allow_model_usage:
        raise ValueError("--allow-model-usage is required; nothing was sent")
    if args.output.exists():
        raise FileExistsError(args.output)
    questions = json.loads(args.questions.read_text(encoding="utf-8"))
    entries, spend = {}, 0.0
    for task in questions:
        text, usage = ask(args.model, args.variant, task["question"], args.timeout)
        entries[task["id"]] = {
            "question_sha256": hashlib.sha256(task["question"].encode()).hexdigest(),
            **parse(text),
            "usage": usage,
        }
        spend += usage.get("cost_usd", 0) or 0
    artifact = {
        "version": "reformulations-v1",
        "model": args.model,
        "variant": args.variant,
        "instruction_sha256": hashlib.sha256(INSTRUCTION.encode()).hexdigest(),
        "questions_sha256": hashlib.sha256(args.questions.read_bytes()).hexdigest(),
        "cost_usd": round(spend, 6),
        "reformulations": entries,
    }
    args.output.write_text(json.dumps(artifact, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")
    print(json.dumps({"questions": len(entries), "cost_usd": artifact["cost_usd"],
                      "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
