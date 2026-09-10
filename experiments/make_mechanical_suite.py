#!/usr/bin/env python3
"""Derive a description-to-symbol retrieval suite from doc comments. No model, no agent.

Mechanical gold buys breadth across corpora and languages that authored questions cannot: the
query is the prose a maintainer already wrote next to a definition. It also structurally favours
retrieval, because that prose sits beside its target, so it answers the narrow question
"description to documented symbol" and never the broader "does this help real understanding".
Report it beside an authored set, never pooled with one.

Symbol names are stripped from the query. Otherwise "Creates a TextModelService" is a lexical
lookup wearing a description's clothes, and the comparison measures nothing.
"""
import argparse
import hashlib
import json
from pathlib import Path
import random
import re

DOC_BLOCK = re.compile(r"/\*\*(.*?)\*/\s*\n(?P<declaration>[^\n]*)", re.DOTALL)
DECLARATION = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?"
    r"(?:(?:export\s+)?(?:const|let|var)\s+(?P<binding>[A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*"
    r"(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*(?::[^=]+)?=>"
    r"|(?:function\*?|class|interface|enum|type)\s+(?P<named>[A-Za-z_$][\w$]*)"
    r"|(?:public\s+|private\s+|protected\s+|static\s+|readonly\s+)*"
    r"(?P<member>[A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*\()"
)
TAG = re.compile(r"^\s*@\w+.*$", re.MULTILINE)
CODE_SPAN = re.compile(r"`[^`]*`")
LINK = re.compile(r"\{@\w+\s+[^}]*\}")
NOISE = re.compile(r"[*/]+")
CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def name_variants(name):
    """Every spelling of a symbol a doc comment might use, so none of them leaks into the query."""
    words = [part for part in CAMEL.sub(" ", name.replace("_", " ")).split() if part]
    return {name.lower(), " ".join(words).lower(), "".join(words).lower()}


def clean(comment, name):
    text = LINK.sub(" ", comment)
    text = CODE_SPAN.sub(" ", text)
    text = TAG.sub(" ", text)
    text = NOISE.sub(" ", text)
    text = " ".join(text.split())
    # Remove the target's own spelling in any case convention, including inside longer words.
    for variant in sorted(name_variants(name), key=len, reverse=True):
        text = re.sub(re.escape(variant), " ", text, flags=re.IGNORECASE)
    return " ".join(text.split())


def candidates(root, extensions, min_words, max_words):
    found = []
    for path in sorted(root.rglob("*")):
        if path.suffix.lstrip(".") not in extensions or not path.is_file():
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        relative = path.relative_to(root).as_posix()
        for match in DOC_BLOCK.finditer(source):
            declared = DECLARATION.match(match.group("declaration"))
            if not declared:
                continue
            name = declared.group("binding") or declared.group("named") or declared.group("member")
            if not name or name in {"if", "for", "while", "switch", "catch", "return"}:
                continue
            query = clean(match.group(1), name)
            words = query.split()
            if not (min_words <= len(words) <= max_words):
                continue
            found.append({
                "path": relative,
                "name": name,
                "line": source[: match.start()].count("\n") + 1,
                "query": " ".join(words[:max_words]),
            })
    return found


def suite(args):
    root = args.root.resolve(strict=True)
    found = candidates(root, set(args.extensions), args.min_words, args.max_words)
    # One question per symbol name: duplicates would make gold ambiguous across files.
    by_name = {}
    for item in found:
        by_name.setdefault(item["name"], []).append(item)
    unique = [items[0] for items in by_name.values() if len(items) == 1]
    unique.sort(key=lambda item: (item["path"], item["line"]))
    random.Random(args.seed).shuffle(unique)
    selected = unique[: args.count]
    selected.sort(key=lambda item: (item["path"], item["line"]))
    return [{
        "id": f"mech-{hashlib.sha256(f'{item['path']}::{item['name']}'.encode()).hexdigest()[:10]}",
        "category": "mechanical_doc_to_symbol",
        "question": item["query"],
        "expected_json": {"answer": f"{item['path']}::{item['name']}"},
        "evidence": [{"path": item["path"], "contains": [item["name"]]}],
        "notes": "Query is the symbol's own doc comment with every spelling of its name removed.",
    } for item in selected]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--extensions", nargs="+", default=["ts", "tsx", "rs", "py"])
    parser.add_argument("--count", type=int, default=200)
    parser.add_argument("--min-words", type=int, default=6)
    parser.add_argument("--max-words", type=int, default=60)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    tasks = suite(args)
    if args.output.exists():
        raise FileExistsError(args.output)
    args.output.write_text(json.dumps(tasks, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"questions": len(tasks), "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
