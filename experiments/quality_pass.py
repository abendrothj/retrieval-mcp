#!/usr/bin/env python3
"""Graded quality measures over completed trials. No model calls, no reruns.

Four axes the strict path-string score cannot express: whether a written symbol resolves to the gold
definition however it was spelled, how close a partly-right set or chain came, whether the evidence
was ever retrieved at all, and whether a wrong answer was declined or invented. Symbol resolution
uses ripgrep over the pinned corpus, never the structural index under test.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import re
import subprocess

DEFINITION = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:default\s+|const\s+|async\s+|unsafe\s+|extern\s+\"[^\"]*\"\s+)*"
                        r"fn\s+([A-Za-z0-9_]+)|^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z0-9_]+)")
# TypeScript declares the same things in more ways than Rust or Python do; each alternative
# names exactly one definition, and methods are matched only where a body follows.
TS_DEFINITION = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?"
    r"(?:class|interface|enum|type)\s+([A-Za-z_$][\w$]*)"
    r"|^\s*(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)"
    # A `const` is a definition only when it binds a callable or a class. `const both =
    # context.options[0] === "both";` is a local value, and indexing it made a plain binding an
    # answerable identity that no structural index defines.
    r"|^\s*(?:export\s+)?(?:declare\s+)?const\s+([A-Za-z_$][\w$]*)\s*(?::[^=]*)?="
    r"\s*(?:async\s+)?(?:function\b|class\b|\([^()]*\)\s*(?::[^=]*)?=>|[A-Za-z_$][\w$]*\s*=>)"
    # A method may carry several modifiers: `private async request(`. Requiring exactly one
    # dropped every such method from the index, and with it the golds that named them.
    r"|^\s*(?:(?:public|private|protected|static|readonly|abstract|override|async)\s+)+\*?\s*"
    r"([A-Za-z_$][\w$]*)\s*[(<]")
# Go declares a callable with `func`, optionally behind a receiver, and a type with `type`.
GO_DEFINITION = re.compile(
    r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)"
    r"|^\s*type\s+([A-Za-z_]\w*)")
# Java's modifiers precede a return type, which precedes the name; a constructor has no return
# type, and a record, enum or annotation type declares exactly as a class does.
JAVA_DEFINITION = re.compile(
    r"^\s*(?:@\w+(?:\([^)]*\))?\s+)*"
    r"(?:(?:public|protected|private|static|final|abstract|sealed|non-sealed|strictfp)\s+)*"
    r"(?:class|interface|enum|record|@interface)\s+([A-Za-z_$][\w$]*)"
    r"|^\s*(?:@\w+(?:\([^)]*\))?\s+)*"
    r"(?:(?:public|protected|private|static|final|abstract|synchronized|native|default|"
    r"strictfp)\s+)+(?:<[^>]*>\s*)?(?:[\w$.<>\[\],?]+(?:\.\.\.)?\s+)?([A-Za-z_$][\w$]*)\s*\(")
# C and C++ name a function in a declarator rather than after a keyword, so the name is the
# identifier that opens the parameter list; `Engine::render` is named by its member half. A
# function-like macro is a definition too, because nothing else defines it.
C_DEFINITION = re.compile(
    r"^\s*(?:typedef\s+)?(?:struct|union|enum|class|namespace)\s+([A-Za-z_]\w*)"
    r"|^\s*#\s*define\s+([A-Za-z_]\w*)\("
    r"|^\s*typedef\s+[^;]*?\(\s*\*\s*([A-Za-z_]\w*)\s*\)"
    r"|^\s*typedef\s+[^;()]*?\b([A-Za-z_]\w*)\s*;"
    # A definition opens a body, so the line ends in `{`, in the `}` of a one-line body, or in
    # the `)` of a signature whose brace is on the next line; a prototype ends in `;`. It also
    # names a return type, or a `Class::` qualifier, before the name, and opens no parenthesis
    # before it. Without both guards `if (!ReadBlock(rep_->file, opt, ...)) {` indexed ReadBlock
    # as defined at its own call site, and `direction_(kForward) {` indexed a constructor's
    # initialiser member as a function. Control keywords are refused by name.
    r"|^[^=;/(!]*[\w>&*\]]\s+\**(?:[A-Za-z_]\w*::)?"
    r"(?!(?:if|for|while|switch|catch|return|sizeof|new|delete|else|do|case|defined)\b)"
    r"([A-Za-z_]\w*)\s*\(.*[{})]\s*$"
    r"|^\s*(?:[A-Za-z_]\w*::)+(~?[A-Za-z_]\w*)\s*\(.*[{:)]\s*$")
SOURCE_SUFFIXES = (".rs", ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts",
                   ".go", ".java", ".c", ".h", ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx")
PATH_TOKEN = re.compile(
    r"[\w./$-]+\.(?:rs|py|ts|tsx|js|jsx|mjs|cjs|mts|cts|go|java|c|h|cc|cpp|cxx|hh|hpp|hxx)\b")
IDENTIFIER = re.compile(r"[A-Za-z_$][\w$]*")
DECLINE = re.compile(r"\b(cannot|can't|could not|couldn't|unable|do not have|don't have|no reliable|not able)\b", re.I)
RAW_STRING = re.compile(r'r(#*)"')
CHAR_LITERAL = re.compile(r"'(?:\\.|[^\\'])'")


def without_literals(source, suffix=".ts"):
    """Each line with its comments and string, char and raw-string literals blanked out.

    Two readings need it. Brace counting is only meaningful over code: a `{` inside a JSDoc
    block, a string or a Rust raw string opens a scope that never closes, which would
    mis-attribute every call after it in the file. And a line of prose is not a definition or a
    call site: a kernel block comment writes ` * folio_put_testzero() has excluded any other
    users` in exactly the shape a C declarator takes. Only Rust gets the lifetime exception:
    there `'a` is not a char literal and reading it as one swallows the real braces that follow
    on the same line, while in a script `'...'` is an ordinary string and must be blanked whole.
    """
    cleaned, in_block = [], False
    for text in source:
        out, index, length = [], 0, len(text)
        while index < length:
            if in_block:
                if text.startswith("*/", index):
                    in_block, index = False, index + 2
                else:
                    index += 1
                continue
            if text.startswith("/*", index):
                in_block, index = True, index + 2
                continue
            if text.startswith("//", index):
                break
            raw = RAW_STRING.match(text, index) if suffix == ".rs" else None
            if raw:
                closing = '"' + raw.group(1)
                position = text.find(closing, raw.end())
                index = length if position < 0 else position + len(closing)
                out.append(" ")
                continue
            character = text[index]
            if character == "'" and suffix == ".rs" and not CHAR_LITERAL.match(text, index):
                out.append(" ")
                index += 1
                continue
            if character in "\"'`":
                index += 1
                while index < length:
                    if text[index] == "\\":
                        index += 2
                        continue
                    if text[index] == character:
                        index += 1
                        break
                    index += 1
                out.append(" ")
                continue
            out.append(character)
            index += 1
        cleaned.append("".join(out))
    return cleaned


def code_line(corpus, path, number, cache):
    """One line of a brace-language file with its comments and literals blanked, cached per file.

    The cache is per caller, because a corpus is read line by line and re-blanking a 30,000-line
    kernel header for every hit would dominate the pass.
    """
    lines = cache.get(path)
    if lines is None:
        source = (Path(corpus) / path).read_text(encoding="utf-8", errors="ignore")
        lines = cache[path] = without_literals(source.splitlines(), Path(path).suffix)
    return lines[number - 1] if 0 < number <= len(lines) else ""


def definition_lines(corpus):
    """(identifier, path, line number) for every definition the source text shows.

    Independent of every system under test: ripgrep over the corpus, no index consulted. The
    multiplicity matters and is therefore not collapsed here: one Go file can define four methods
    named `GetRequestMetadata` on four receivers, and a caller set that names the identity once
    cannot say which of them a call site reached.
    """
    lines, blanked = [], {}
    passes = (
        ([r"^\s*(pub\s+)?(async\s+)?fn\s+[A-Za-z0-9_]+",
          r"^\s*(async\s+)?(def|class)\s+[A-Za-z0-9_]+"],
         ["-g", "*.rs", "-g", "*.py"], DEFINITION),
        ([r"^\s*(export\s+)?(default\s+)?(declare\s+)?(abstract\s+)?(class|interface|enum|type)\s+[A-Za-z_$]",
          r"^\s*(export\s+)?(default\s+)?(declare\s+)?(async\s+)?function\s*\*?\s*[A-Za-z_$]",
          r"^\s*(export\s+)?(declare\s+)?const\s+[A-Za-z_$][\w$]*\s*[:=]",
          r"^\s*((public|private|protected|static|readonly|abstract|override|async)\s+)+\*?\s*[A-Za-z_$][\w$]*\s*[(<]"],
         ["-g", "*.ts", "-g", "*.tsx", "-g", "*.js", "-g", "*.jsx", "-g", "*.mjs", "-g", "*.cjs",
          "-g", "*.mts", "-g", "*.cts"], TS_DEFINITION),
        ([r"^\s*func\s+", r"^\s*type\s+[A-Za-z_]"], ["-g", "*.go"], GO_DEFINITION),
        ([r"^\s*((public|protected|private|static|final|abstract|sealed|non-sealed|strictfp)\s+)*(class|interface|enum|record|@interface)\s+[A-Za-z_$]",
          r"^\s*((public|protected|private|static|final|abstract|synchronized|native|default|strictfp)\s+)+"],
         ["-g", "*.java"], JAVA_DEFINITION),
        ([r"^\s*(typedef\s+)?(struct|union|enum|class|namespace)\s+[A-Za-z_]",
          r"^\s*#\s*define\s+[A-Za-z_]\w*\(",
          r"^\s*typedef\s+",
          r"^[^=;/]*\b[A-Za-z_]\w*\s*\(.*[{})]\s*$"],
         ["-g", "*.c", "-g", "*.h", "-g", "*.cc", "-g", "*.cpp", "-g", "*.cxx", "-g", "*.hh",
          "-g", "*.hpp", "-g", "*.hxx"], C_DEFINITION),
    )
    for patterns, globs, expression in passes:
        command = ["rg", "--no-config", "-n", "--no-heading"]
        for pattern in patterns:
            command += ["-e", pattern]
        found = subprocess.run(command + globs + ["."], cwd=corpus,
                               capture_output=True, text=True, timeout=300)
        for line in found.stdout.splitlines():
            parts = line.split(":", 2)
            if len(parts) != 3:
                continue
            match = expression.match(parts[2])
            if not match:
                continue
            name = next(group for group in match.groups() if group)
            path = parts[0].lstrip("./")
            number = int(parts[1]) if parts[1].isdigit() else 0
            # Prose is not a definition. Linux writes ` * folio_put_testzero() has excluded any
            # other users of the folio.)` inside a block comment, which reads as a C declarator
            # ending in `)`, and indexing it invented a symbol the corpus does not define -
            # `language_audit` then sampled that symbol and scored the server for not finding it.
            # A comment opened on an earlier line leaves no marker on this one, so the file's own
            # comment state decides. Python is the one suffix here that is not a brace language,
            # and its `def`/`class` rules cannot match behind a `#`.
            if path.endswith(".py") or name in code_line(corpus, path, number, blanked):
                lines.append((name, path, number))
    return lines


def definitions(corpus):
    """identifier -> set of paths that define it, from source text alone."""
    index = {}
    for name, path, _ in definition_lines(corpus):
        index.setdefault(name, set()).add(path)
    return index


def definition_counts(corpus):
    """identifier -> {path: how many definitions of that name the file holds}."""
    counts = {}
    for name, path, _ in definition_lines(corpus):
        counts.setdefault(name, {})
        counts[name][path] = counts[name].get(path, 0) + 1
    return counts


def context_segments(written):
    """The identifier-like pieces of a written symbol: `::`, `:`, `/` and `.` all separate them.

    A source-file chunk is location, not a name, so `base.py` contributes nothing; a dotted owner
    such as `Model.from_db` or a dotted module such as `django.apps.config` contributes each
    piece. A single colon is how editors and grep write the same thing - `query.py:Query.combine`
    names one definition exactly as `query.py::Query.combine` does, and a trailing line number
    such as `query.py:1234` is not an identifier, so it still fails to parse rather than
    resolving to something wrong.
    """
    segments = []
    for chunk in re.split(r"::|:|/", written.strip()):
        if not chunk or chunk.endswith(SOURCE_SUFFIXES):
            continue
        # A Go method is spelled `(*Server).handleStream` by the language, by godoc and by every
        # reader of the source; `Server.handleStream` is the same definition written by someone
        # who dropped the pointer. Parentheses and the receiver's `*` are notation, so they are
        # removed rather than allowed to make the owner unparseable.
        chunk = chunk.replace("(", "").replace(")", "")
        segments.extend(piece.lstrip("*&") for piece in chunk.split(".") if piece.strip("*&"))
    return segments


def parse_symbol(written, index=None):
    """(type, name) from any spelling: a path, a module path, a bare name, or one prose clause.

    With an index, an answer that reads `getResolvedShellEnv in src/.../shellEnv.ts` is parsed by
    keeping the identifiers that are actually defined somewhere in the corpus. A clause naming two
    unrelated definitions stays ambiguous and is not parsed, so prose cannot win by listing names.

    A dot separates an owner from its member exactly as `::` does: `base.py::Model.from_db` and
    `django.apps.config::AppConfig.create` are the spellings a Python reader writes, and scoring
    them zero measures notation rather than retrieval.
    """
    if not isinstance(written, str):
        return None
    parts = context_segments(written)
    if parts and all(IDENTIFIER.fullmatch(part) for part in parts):
        name = parts[-1]
        owner = parts[-2] if len(parts) > 1 and parts[-2][:1].isupper() else None
        return owner, name
    if index is None:
        return None
    # Path segments are location, not the name being claimed; read identifiers from the prose only.
    prose = PATH_TOKEN.sub(" ", written)
    named = [token for token in dict.fromkeys(IDENTIFIER.findall(prose)) if token in index]
    if len(named) > 1:
        # The answer's own file reference is the context it supplied; use it to pick the claim.
        mentioned = {match.group(0).lstrip("./") for match in PATH_TOKEN.finditer(written)}
        named = [token for token in named if index[token] & mentioned] if mentioned else named
    if len(named) != 1:
        return None
    return None, named[0]


def resolve(written, index):
    """The unique defining path for a written symbol, or None when absent or still ambiguous.

    A name defined in several files is disambiguated by the rest of what was written - a crate,
    module, directory or file segment - which is the context the answer actually supplied.
    Segments are matched against the path mechanically; nothing about the gold answer is consulted.
    """
    parsed = parse_symbol(written, index)
    if not parsed:
        return None
    paths = index.get(parsed[1], set())
    if len(paths) > 1:
        mentioned = {match.group(0).lstrip("./") for match in PATH_TOKEN.finditer(written)}
        narrowed = {path for path in paths if path in mentioned} if mentioned else set()
        if len(narrowed) != 1:
            segments = [s.lower().removeprefix("uu_") for s in context_segments(written)
                        if s not in (parsed[1], parsed[0])]
            narrowed = {p for p in paths
                        if all(s in p.lower() for s in segments)} if segments else paths
        paths = narrowed if len(narrowed) == 1 else paths
    return (sorted(paths)[0], parsed) if len(paths) == 1 else None


def same(written, gold, index):
    if not isinstance(gold, str):
        return written == gold
    left, right = resolve(written, index), resolve(gold, index)
    if left and right:
        return left[0] == right[0] and left[1][1] == right[1][1] and (
            left[1][0] == right[1][0] or None in (left[1][0], right[1][0]))
    return str(written).strip() == gold


def mentions(written, gold, index):
    """Whether a prose answer names this one gold identity unambiguously.

    A set-valued answer written as a sentence is still an answer. It counts an identity only when
    the name it uses resolves to the gold's definition: a name defined once resolves on its own, a
    name with namesakes needs the answer to supply its file. Prose therefore cannot prove
    exhaustiveness - ask for a JSON list when the question demands every caller.
    """
    if not isinstance(gold, str) or "::" not in gold:
        return False
    path, leaf = gold.split("::", 1)
    leaf = leaf.split("::")[-1]
    text = str(written)
    if leaf not in IDENTIFIER.findall(PATH_TOKEN.sub(" ", text)):
        return False
    defined = index.get(leaf, set())
    if len(defined) > 1:
        return path in {match.group(0).lstrip("./") for match in PATH_TOKEN.finditer(text)}
    return not defined or path in defined


def flatten(gold):
    """Every identity a set- or object-valued gold asserts, in declaration order."""
    if isinstance(gold, str):
        return [gold]
    if isinstance(gold, list):
        return [item for value in gold for item in flatten(value)]
    if isinstance(gold, dict):
        return [item for value in gold.values() for item in flatten(value)]
    return []

def credit(got, gold, index):
    """Graded closeness in [0, 1]; sets by overlap, chains by correct prefix, scalars exact."""
    if isinstance(gold, (list, dict)) and isinstance(got, str):
        # Prose against a set: recall over the asserted identities. Extras are not penalised
        # because a sentence cannot be read as a closed set; require a JSON list to test that.
        expected = flatten(gold)
        return sum(mentions(got, item, index) for item in expected) / len(expected) if expected else 0.0
    if isinstance(gold, list):
        # A keyed object whose values name the asserted identities is a spelling of the same set.
        # Scoring it zero while a free sentence naming the same two symbols scores 1.0 would grade
        # notation rather than retrieval, which is the oldest defect in this project.
        if isinstance(got, dict):
            got = flatten(got)
        if not isinstance(got, list):
            return 0.0
        matched = sum(any(same(g, expected, index) for g in got) for expected in gold)
        extra = max(0, len(got) - len(gold))
        return max(0.0, (matched - extra) / len(gold))
    if isinstance(gold, dict):
        # The mirror of the case above, and the same defect: a gold that wraps one assertion in a
        # key ({"callers": [...]}) asserts exactly what the bare value asserts, so a reply naming
        # the same identities under no key, or under a differently spelled one, is the same answer.
        # Scoring it zero grades the container, not the retrieval. Multi-key golds are excluded:
        # there the key names which fact is being asserted, so dropping it does lose information.
        if not isinstance(got, dict):
            return credit(got, next(iter(gold.values())), index) if len(gold) == 1 else 0.0
        if set(got) != set(gold):
            if len(gold) == 1 and len(got) == 1:
                return credit(next(iter(got.values())), next(iter(gold.values())), index)
            return 0.0
        return sum(credit(got[k], gold[k], index) for k in gold) / len(gold)
    return float(same(got, gold, index)) if gold is not None else float(got is None)


def answer_json(text):
    """The answer payload regardless of the reply envelope.

    The frozen strict grader in benchmark.py judges the envelope: a bare {"answer": ...} object.
    This quality axis must not repeat that judgment, or a correct symbol wrapped in prose scores
    zero and the measure reports formatting discipline instead of retrieval quality. Fenced answer
    blocks win; otherwise the last {"answer": ...} object anywhere in the reply is used.
    """
    text = text or ""
    for block in re.findall(r"```(?:json)?[ \t]*\n(.*?)\n?```", text, re.DOTALL):
        try:
            parsed = json.loads(block.strip())
        except ValueError:
            continue
        if isinstance(parsed, dict) and set(parsed) == {"answer"}:
            return parsed["answer"]
    decoder = json.JSONDecoder()
    found = None
    for match in re.finditer(r"\{", text):
        try:
            parsed, _ = decoder.raw_decode(text[match.start():])
        except ValueError:
            continue
        if isinstance(parsed, dict) and set(parsed) == {"answer"}:
            found = parsed["answer"]
    return found


def evidence_paths(attempt):
    """Every path the retrieval layer returned or read in this attempt."""
    seen = set()
    log = attempt/"server.jsonl"
    if not log.exists():
        return seen
    for line in log.read_text().splitlines():
        event = json.loads(line)
        if event.get("event") != "tool_end":
            continue
        for location in event.get("locations") or []:
            if location.get("path"):
                seen.add(location["path"])
        path = (event.get("arguments") or {}).get("path")
        if path and event.get("tool") == "read_source":
            seen.add(path)
    return seen


def report(directories, questions_path, corpus):
    questions = {q["id"]: q for q in json.loads(Path(questions_path).read_text())}
    index = definitions(Path(corpus))
    cells = {}
    for directory in directories:
        for trial in sorted(Path(directory).glob("trial-*")):
            attempts = sorted(trial.glob("attempt-*/run.json"))
            record = json.loads(attempts[-1].read_text())
            if record["status"] != "completed":
                continue
            task = questions[record["task_id"]]
            gold = task["expected_json"]["answer"]
            got = answer_json(record.get("answer"))
            score = credit(got, gold, index)
            wanted = {e["path"] for e in task["evidence"]}
            retrieved = evidence_paths(attempts[-1].parent)
            row = cells.setdefault(record["condition"], {"trials":0, "strict":0, "resolved":0,
                "credit":0.0, "evidence_hits":0, "declined":0, "fabricated":0, "notation_only":[]})
            row["trials"] += 1
            row["strict"] += record["payload_matches"] is True
            row["resolved"] += score == 1.0
            row["credit"] += score
            row["evidence_hits"] += bool(wanted & retrieved) if retrieved else 0
            if score < 1.0:
                # An empty answer, an explicit null, or a stated inability are all declines;
                # a named symbol that is simply wrong is not.
                empty = got in (None, [], "")
                row["declined" if empty or (got is None and DECLINE.search(record.get("answer") or ""))
                    else "fabricated"] += 1
            if score == 1.0 and record["payload_matches"] is not True:
                row["notation_only"].append(record["task_id"])
    for row in cells.values():
        row["credit"] = round(row["credit"]/row["trials"], 3) if row["trials"] else None
    return {"version":"quality-pass-v1", "cells":cells, "definitions_indexed":len(index),
            "measures":{"strict":"frozen json-answer-v3 payload equality on the path string",
                        "resolved":"identifier resolved to a unique definition in the pinned corpus by ripgrep",
                        "credit":"graded closeness: sets by overlap less extras, dicts per key, scalars exact",
                        "evidence_hits":"a gold evidence file was returned or read at least once",
                        "declined":"wrong and the reply states it cannot answer",
                        "fabricated":"wrong and stated as an answer"},
            "limitations":"Resolution requires a unique definition, so an ambiguous name scores wrong. "
                          "Evidence coverage is file-level, not line-level, and says nothing about whether "
                          "the evidence was read before the claim. Twelve questions, one repetition."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directories", nargs="+")
    parser.add_argument("--questions", default="experiments/suites/v2_questions_draft.json")
    parser.add_argument("--corpus", default="../runs/projects-v2-suite/coreutils/corpus")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = report(args.directories, args.questions, args.corpus)
    if args.output:
        with args.output.open("x") as stream:
            json.dump(result, stream, indent=2)
            stream.write("\n")
    print(f"{'cell':28s} {'strict':>7s} {'resolved':>9s} {'credit':>7s} {'evidence':>9s} {'declined':>9s} {'fabricated':>11s}")
    for cell, row in sorted(result["cells"].items()):
        n = row["trials"]
        print(f"{cell:28s} {row['strict']:>4d}/{n:<2d} {row['resolved']:>6d}/{n:<2d} {row['credit']:>7.2f}"
              f" {row['evidence_hits']:>6d}/{n:<2d} {row['declined']:>9d} {row['fabricated']:>11d}")
