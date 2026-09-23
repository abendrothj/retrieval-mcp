#!/usr/bin/env python3
"""Assert that the documentation still describes the server that exists.

Every claim in this project's READMEs is meant to be downstream of a measurement, but nothing was
checking that the prose kept up with the code. It did not: freezing the default surface at four
tools left the README quickstart calling a tool the default no longer exposes, so the first command
a reader runs returned `unknown or disabled tool`. The same drift left three different test counts,
a profile table marking the wrong row as the default, and a tool documented under a name it had
been renamed away from.

These checks need no model and no network:

  Tool surface  - the tools each document names as the default, as the full catalogue, and in the
                  profile table match `TOOLS`, `DEFAULT_SURFACE` and `PROFILES` in src/config.
  Test counts   - the counts written beside the documented commands match what those commands
                  actually report.
  Paths         - every repository-relative path a document points at exists.
  Links         - every cross-document link resolves, and every `#anchor` names a real heading.
  Quickstart    - the quickstart block runs against this build and returns a result, not an error.
  README shape  - the contract below, which is about what a reader meets and in what order.

The shape contract exists because an audit found the README answering "how do I install this"
for 131 lines before answering "what is this", and explaining client registration in eight
places - one of them a byte-identical duplicate of another. Duplication is how the `--root`
change came to need edits in five places, and a reader who has to scroll past procedure to reach
evidence is a reader who leaves. So:

  Sections      - README_SECTIONS appear exactly once each, in that order, with nothing else at
                  `##` level. Proof (`What it is`, `The result`) precedes procedure (`Install`).
  Install size  - the install section stays within INSTALL_BUDGET lines; the detail belongs in
                  `Platforms, updating, and removal`.
  One of each   - no fenced block appears twice, and the client registration commands appear in
                  exactly one block, so there is one place to edit when they change.
  Contents      - the `## Contents` list names every `##` section after it, in document order.

Exit status is nonzero when any check fails, so this runs as a build step.

The top-level README of the surrounding study lives outside this git repository, so it is checked
only when it is present on disk; CI covers the three tracked documents.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess

TRACKED_DOCS = ("README.md", "experiments/README.md", "experiments/FACTOR_PROTOCOL.md",
                "experiments/CORPUS_V2.md")
OUTSIDE_DOC = "../README.md"
# A path-shaped token in prose is only checked when it looks like this repository's own files.
PATH_PATTERN = re.compile(r'`((?:experiments|src|tests|examples)/[A-Za-z0-9_./*-]+)`')
PLACEHOLDER = re.compile(r'\*|\{|/path/to/|/absolute/')
LINK_PATTERN = re.compile(r'\[[^\]]*\]\(([^)\s#]*)(#[A-Za-z0-9-]+)?\)')
# The result section's figures are bound to `published_results.json`, which `publish_numbers.py`
# derives from the archived reports. Before this existed, nothing compared a published number with
# its own evidence - `runs/` is gitignored, so CI never saw it - and the table drifted to 147 tool
# calls where the report said 146, while the indexing counts outlived four new grammars.
PUBLISHED = "experiments/published_results.json"
# Row label -> the extract field it publishes, and the precision the README prints it at.
RESULT_ROWS = {
    "Correct": ("correct", 0),
    "Input tokens": ("input_tokens", 2),
    "Tool calls": ("calls", 0),
    "Persistent context": ("context_token_turns", 2),
    "Calls to first evidence": ("calls_to_first_evidence", 1),
    "Answered without evidence": ("answered_without_evidence", 0),
}
# Column order of the result table, which the header row is checked against.
RESULT_ARMS = ("native-control", "zvec-grep", "retrieval-mcp")
# What a study has to say about itself before its figures can be quoted. A percentage is a
# measurement only under a client, a corpus, a question class, a suite with a known author, and a
# known answer to what else reached the prompt; quality is an outcome only where the suite could
# have shown a difference. `publish_numbers.py` declares these and checks them against the run
# directory. Declared and unflattering publishes here - undeclared does not.
CONDITION_FIELDS = ("client", "corpus", "question_class", "suite_authored", "prompt_documents",
                    "prompt_document_evidence", "quality_separation")
FIGURE = re.compile(r'^\*{0,2}([0-9]+(?:\.[0-9]+)?)\s*([Mk])?\*{0,2}$')
PERCENT = re.compile(r'[-−]([0-9]+(?:\.[0-9]+)?)%')
# What a reader of README.md meets, in order. Proof before procedure; one troubleshooting surface;
# install detail after the sections that say whether the thing is worth installing.
README_SECTIONS = (
    "Contents",
    "What it is",
    "The result",
    "Install",
    "Quickstart",
    "Connect a client",
    "Tools",
    "Limits that change your answer",
    "Troubleshooting",
    "Platforms, updating, and removal",
    "Build, test, and code layout",
    "Research options — not needed to use the server",
)
# Lines from `## Install` to the next `##`. The whole point of the section is that installing is
# two commands; anything longer has drifted back into reference material.
INSTALL_BUDGET = 40
REGISTRATION = re.compile(r'^(claude|codex) mcp add ', re.M)
FENCE = re.compile(r'^```[a-z]*\n(.*?)^```', re.M | re.S)


def headings(text):
    """GitHub's heading slugs, for checking `#anchor` link targets."""
    return {re.sub(r'[^a-z0-9 -]', '', line.lower()).replace(' ', '-')
            for line in re.findall(r'^#{1,6} (.+)$', text, re.M)}


def rust_constants(repo):
    """Parse TOOLS, DEFAULT_SURFACE and PROFILES out of src/config/mod.rs."""
    source = (repo / "src/config/mod.rs").read_text(encoding="utf-8")

    def names(body):
        return [name for name in re.findall(r'"([a-z_]+)"', body)]

    tools = names(re.search(r'pub const TOOLS[^=]*=\s*\[(.*?)\];', source, re.S).group(1))
    default = names(re.search(r'pub const DEFAULT_SURFACE[^=]*=\s*\[(.*?)\];', source, re.S).group(1))
    block = re.search(r'pub const PROFILES[^=]*=\s*\[(.*?)\n\];', source, re.S).group(1)
    profiles = {}
    for letter, body in re.findall(r'\("([A-D])",\s*&(\[.*?\]|TOOLS)', block, re.S):
        profiles[letter] = list(tools) if body == "TOOLS" else names(body)
    return tools, default, profiles


def test_counts(repo):
    """Run the documented test commands and report what they actually count."""
    targets = {"library": ["--lib"], "stdio": ["--test", "mcp_stdio"], "example": ["--examples"]}
    counts = {}
    for binary, flags in targets.items():
        rust = subprocess.run(["cargo", "test", "--locked", *flags], cwd=repo,
                              capture_output=True, text=True)
        if rust.returncode != 0:
            raise SystemExit(f"cargo test {' '.join(flags)} failed; fix the tests before the docs\n"
                             + rust.stdout[-2000:])
        passed = ignored = 0
        for line in rust.stdout.splitlines():
            result = re.search(r'test result: ok\. (\d+) passed; \d+ failed; (\d+) ignored', line)
            if result:
                passed += int(result.group(1)) + int(result.group(2))
                ignored += int(result.group(2))
        counts[binary] = (passed, ignored)
    python = subprocess.run(
        ["python3", "-W", "error::ResourceWarning", "-m", "unittest", "discover",
         "-s", "experiments", "-p", "test_*.py"], cwd=repo, capture_output=True, text=True)
    if python.returncode != 0:
        raise SystemExit("python tests failed; fix the tests before the docs\n"
                         + python.stderr[-2000:])
    ran = re.search(r'Ran (\d+) tests', python.stderr)
    return counts, int(ran.group(1))


def check_surface(name, text, tools, default, profiles, problems):
    def report(detail):
        problems.append({"document": name, "check": "tool surface", "detail": detail})

    for quoted, documented in re.findall(
            r'(defaults to the four[^:]*:|exposes\s*\n?)((?:\s*`[a-z_]+`[,\s]*(?:and\s*)?)+)', text):
        named = re.findall(r'`([a-z_]+)`', documented)
        if sorted(named) != sorted(default):
            report(f"documented default surface {named} != DEFAULT_SURFACE {default}")

    profile_table = re.search(
        r'^\|\s*Profile\s*\|[^\n]*--tools[^\n]*\|\n\|[-\s|]+\|\n((?:\|.*\n)+)', text, re.M)
    for letter, documented in re.findall(r'^\|\s*([A-D])\s*\|\s*(.+?)\s*\|',
                                         profile_table.group(1) if profile_table else "", re.M):
        if letter not in profiles:
            continue
        named = re.findall(r'[a-z_]+', documented.replace("`", ""))
        expected = profiles[letter]
        if named == ["all", "seven"]:
            named = expected if len(expected) == 7 else ["all", "seven"]
        if named != expected:
            report(f"profile {letter} documented as {named} != PROFILES[{letter}] {expected}")

    for stale in re.findall(r'`(search_semantic|find_definition)`', text):
        if "renamed" not in text[max(0, text.find(stale) - 200):text.find(stale) + 200]:
            report(f"`{stale}` is not a tool this server exposes; the catalogue is {tools}")

    documented_count = re.search(r'(?:can expose|The server exposes) (\w+) tools', text)
    words = {"four": 4, "five": 5, "six": 6, "seven": 7}
    if documented_count and words.get(documented_count.group(1)) != len(tools):
        report(f"documented catalogue size '{documented_count.group(1)}' != {len(tools)}")


def check_counts(name, text, rust, python, problems):
    def report(detail):
        problems.append({"document": name, "check": "test counts", "detail": detail})

    for library, stdio, ignored, example in re.findall(
            r'# (\d+) library, (\d+) stdio \((\d+) ignored\), (\d+) example', text):
        documented = {"library": (int(library), 0), "stdio": (int(stdio), int(ignored)),
                      "example": (int(example), 0)}
        if documented != rust:
            report(f"documented rust counts {documented} != actual {rust}")

    for count in re.findall(r"test_\*\.py'\s*#\s*(\d+) tests", text):
        if int(count) != python:
            report(f"documented python test count {count} != actual {python}")

    for py, rs in re.findall(r'\((\d+) Python tests, (\d+) Rust tests\)', text):
        total = sum(passed for passed, _ in rust.values())
        if int(py) != python or int(rs) != total:
            report(f"documented ({py} Python, {rs} Rust) != actual ({python} Python, {total} Rust)")


def check_paths(name, text, repo, problems):
    for path in sorted(set(PATH_PATTERN.findall(text))):
        if PLACEHOLDER.search(path) or (repo / path).exists():
            continue
        if not (repo / path).parent.is_dir():
            continue
        problems.append({"document": name, "check": "paths",
                         "detail": f"`{path}` does not exist"})


def check_links(name, text, repo, problems):
    document = (repo / name)
    for target, anchor in LINK_PATTERN.findall(text):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        destination = document.parent / target if target else document
        if not destination.exists():
            problems.append({"document": name, "check": "links",
                             "detail": f"link target {target!r} does not exist"})
            continue
        if anchor and destination.suffix == ".md":
            slugs = headings(destination.read_text(encoding="utf-8"))
            if anchor[1:] not in slugs:
                problems.append({
                    "document": name, "check": "links",
                    "detail": f"{target or name}{anchor} names no heading in that document"})


def check_shape(text, problems):
    """Hold README.md to the order, size and single-source rules its audit produced."""
    def report(detail):
        problems.append({"document": "README.md", "check": "shape", "detail": detail})

    sections = re.findall(r'^## (.+)$', text, re.M)
    if sections != list(README_SECTIONS):
        extra = [name for name in sections if name not in README_SECTIONS]
        missing = [name for name in README_SECTIONS if name not in sections]
        if extra:
            report(f"sections not in the contract: {extra}")
        if missing:
            report(f"contract sections missing: {missing}")
        if not extra and not missing:
            report(f"sections out of contract order: {sections}")

    install = re.search(r'^## Install$(.*?)(?=^## )', text, re.M | re.S)
    if install and len(install.group(1).splitlines()) > INSTALL_BUDGET:
        report(f"the install section is {len(install.group(1).splitlines())} lines, over the "
               f"{INSTALL_BUDGET}-line budget; reference detail belongs in a later section")

    blocks = [body.strip() for body in FENCE.findall(text)]
    for body in sorted({body for body in blocks if blocks.count(body) > 1}):
        report(f"this fenced block appears {blocks.count(body)} times: {body.splitlines()[0]!r}")

    registering = [body for body in blocks if REGISTRATION.search(body)]
    if len(registering) != 1:
        report(f"{len(registering)} blocks carry a client registration command; exactly one may, "
               "so a change to how the server is registered has one place to land")

    listed = re.search(r'^## Contents$(.*?)(?=^## )', text, re.M | re.S)
    if not listed:
        report("no `## Contents` section")
    else:
        named = re.findall(r'\]\(#([a-z0-9-]+)\)', listed.group(1))
        expected = [slug for slug in
                    (re.sub(r'[^a-z0-9 -]', '', name.lower()).replace(' ', '-')
                     for name in README_SECTIONS) if slug != "contents"]
        if named != expected:
            report(f"the contents list does not name every section in order: {named} != {expected}")


def figure(cell):
    """Parse a published cell into (value, tolerance) at the precision it is printed to.

    `764 k` promises 764,000 to the nearest thousand, not 763,744 exactly, so the tolerance is half
    the last printed digit in the printed unit. Without that this check would demand the README
    print raw trial sums, which no reader wants.
    """
    match = FIGURE.match(cell.strip())
    if not match:
        return None
    digits, unit = match.group(1), match.group(2)
    scale = {"M": 1e6, "k": 1e3, None: 1}[unit]
    decimals = len(digits.split(".")[1]) if "." in digits else 0
    return float(digits) * scale, 0.5 * 10 ** -decimals * scale


def published_comparisons(arms):
    """Every ratio a reader could legitimately quote from one study, as a percentage."""
    ratios = set()
    fields = ("input_tokens", "calls", "context_token_turns")
    for field in fields:
        for left in arms.values():
            for right in arms.values():
                if right.get(field):
                    ratios.add((left[field] / right[field] - 1) * 100)
    return ratios


def check_conditions(extract, problems):
    """No figure without its conditions. This is the gate the contamination finding needed: the
    project document reached both arms of every Codex study and nothing in the extract said so,
    so a clean-looking percentage could be quoted out of a contaminated run."""
    for name, study in sorted(extract["studies"].items()):
        declared = study.get("conditions") or {}
        for field in CONDITION_FIELDS:
            if declared.get(field) in (None, "", {}):
                problems.append({"document": PUBLISHED, "check": "study conditions",
                                 "detail": f"{name} publishes figures without declaring "
                                           f"{field!r}; regenerate with publish_numbers.py"})


def check_published(text, extract, problems):
    """Bind the result section's figures to the extract derived from the archived reports."""
    def report(detail):
        problems.append({"document": "README.md", "check": "published numbers", "detail": detail})

    section = re.search(r'\n## The result\n(.*?)\n## ', text, re.S)
    if not section:
        return report("no result section found")
    body = section.group(1)
    heldout = extract["studies"]["heldout"]["arms"]
    header = re.search(r'\n\|([^\n]*native[^\n]*)\|\n', body)
    if not header:
        return report("the result table has no header row naming the native control")
    order = [cell.strip() for cell in header.group(1).split("|")]
    if not (len(order) == 4 and "zvec" in order[2] and "retrieval-mcp" in order[3]):
        return report(f"unexpected result table columns {order}; expected native, zvec, "
                      f"retrieval-mcp")
    seen = set()
    for line in body.split("\n"):
        cells = [cell.strip() for cell in line.split("|")[1:-1]] if line.startswith("|") else []
        if len(cells) != 4:
            continue
        label = next((key for key in RESULT_ROWS if cells[0].startswith(key)), None)
        if label is None:
            continue
        field, _ = RESULT_ROWS[label]
        seen.add(label)
        for arm, cell in zip(RESULT_ARMS, cells[1:]):
            parsed, actual = figure(cell), heldout[arm][field]
            if parsed is None:
                report(f"{label!r} for {arm} is not a readable figure: {cell!r}")
                continue
            shown, tolerance = parsed
            if abs(shown - actual) > tolerance + 1e-9:
                report(f"{label!r} for {arm} says {cell.strip('*')} but the run report says "
                       f"{actual} (tolerance +/-{tolerance:g})")
    for missing in sorted(set(RESULT_ROWS) - seen):
        report(f"the result table no longer publishes {missing!r}")
    # Any percentage in the section must be a comparison the studies actually support, at the
    # precision it is printed to. A figure computed under one token definition and printed beside
    # another was how -33.5% and -44.6% came to share a sentence. A percentage that is not a run
    # comparison at all - instruction bytes, say - has to be declared in the extract with its
    # provenance, so an undeclared one fails rather than passing unnoticed.
    allowed = set()
    for study in extract["studies"].values():
        allowed |= published_comparisons(study["arms"])
    declared = {float(value) for value in extract.get("declared_percentages", {})}
    for quoted in PERCENT.findall(body):
        value = -float(quoted)
        tolerance = 0.5 * 10 ** -(len(quoted.split(".")[1]) if "." in quoted else 0)
        if not any(abs(value - candidate) <= tolerance for candidate in allowed | declared):
            report(f"-{quoted}% is not a comparison the archived reports produce, and is not "
                   f"declared in {PUBLISHED}")


def check_quickstart(repo, problems):
    """Run the quickstart exactly as written, so it cannot rot again unnoticed."""
    text = (repo / "README.md").read_text(encoding="utf-8")
    block = re.search(r'## Quickstart\n+```sh\n(.*?)```', text, re.S)
    if not block:
        problems.append({"document": "README.md", "check": "quickstart",
                         "detail": "no quickstart block found"})
        return
    binary = repo / "target/release/retrieval-mcp"
    if not binary.exists():
        problems.append({"document": "README.md", "check": "quickstart",
                         "detail": f"{binary} is not built; run the quickstart's cargo build first"})
        return
    script = block.group(1)
    # The quickstart calls `retrieval-mcp` by name, as a reader with `cargo install` would. Run it
    # with this build first on PATH: otherwise the check asks whatever binary happens to be
    # installed, which passed on a developer machine holding a two-day-old `~/.cargo/bin` copy
    # while CI, having none, failed for seven consecutive pushes.
    environment = {**os.environ, "PATH": f"{binary.parent}{os.pathsep}{os.environ.get('PATH', '')}"}
    completed = subprocess.run(["sh", "-c", script], cwd=repo, capture_output=True, text=True,
                               env=environment)
    line = completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else ""
    try:
        result = json.loads(line)["result"]
    except (ValueError, KeyError, IndexError):
        problems.append({"document": "README.md", "check": "quickstart",
                         "detail": f"quickstart produced no JSON-RPC result: {line[:200]!r}"})
        return
    if result.get("isError"):
        problems.append({
            "document": "README.md", "check": "quickstart",
            "detail": "quickstart returned isError: "
                      f"{result.get('structuredContent', {}).get('error', '')!r}"})
    documented = re.search(r'## Quickstart.*?```json\n(.*?)```', text, re.S)
    if not documented:
        problems.append({"document": "README.md", "check": "quickstart",
                         "detail": "the quickstart shows no sample response"})
        return
    # The sample is printed as evidence, so it is compared against the live reply rather than
    # trusted: every field the README shows must be the field the server just returned. Volatile
    # positions - line, column, excerpt - are deliberately not printed, because a doc that rots on
    # every edit above a call site teaches readers to ignore it.
    live = result.get("structuredContent", {})
    sample = json.loads(documented.group(1))
    differences = [f"{key}: documented {value!r} != returned {live.get(key)!r}"
                   for key, value in sample.items()
                   if key != "results" and not isinstance(value, dict) and live.get(key) != value]
    for key, value in ((k, v) for k, v in sample.items() if isinstance(v, dict)):
        differences += [f"{key}.{field}: documented {shown!r} != returned "
                        f"{live.get(key, {}).get(field)!r}"
                        for field, shown in value.items() if live.get(key, {}).get(field) != shown]
    returned_rows = live.get("results", [])
    for index, row in enumerate(sample.get("results", [])):
        actual = returned_rows[index] if index < len(returned_rows) else {}
        differences += [f"results[{index}].{field}: documented {shown!r} != returned "
                        f"{actual.get(field)!r}"
                        for field, shown in row.items() if actual.get(field) != shown]
    for difference in differences:
        problems.append({"document": "README.md", "check": "quickstart",
                         "detail": f"the sample response is stale - {difference}"})


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--skip-tests", action="store_true",
                        help="skip the checks that must run the test suites to learn the truth")
    args = parser.parse_args()
    repo = args.repo.resolve(strict=True)

    tools, default, profiles = rust_constants(repo)
    rust, python = (None, None) if args.skip_tests else test_counts(repo)

    documents = [name for name in TRACKED_DOCS if (repo / name).exists()]
    outside = (repo / OUTSIDE_DOC).resolve()
    if outside.exists():
        documents.append(OUTSIDE_DOC)

    problems = []
    for name in documents:
        text = (repo / name).read_text(encoding="utf-8")
        check_surface(name, text, tools, default, profiles, problems)
        check_paths(name, text, repo, problems)
        check_links(name, text, repo, problems)
        if not args.skip_tests:
            check_counts(name, text, rust, python, problems)
    # The shape contract is this repository's README, not every document the study ships.
    readme = (repo / "README.md").read_text(encoding="utf-8")
    check_shape(readme, problems)
    # Published figures are checked against the extract, never against prose memory.
    extract_path = repo / PUBLISHED
    if extract_path.exists():
        extract = json.loads(extract_path.read_text(encoding="utf-8"))
        check_published(readme, extract, problems)
        check_conditions(extract, problems)
    else:
        problems.append({"document": PUBLISHED, "check": "published numbers",
                         "detail": "missing; regenerate it with publish_numbers.py"})
    if not args.skip_tests:
        check_quickstart(repo, problems)

    print(json.dumps({"documents": documents, "checked_outside_repo": OUTSIDE_DOC in documents,
                      "readme_sections": len(README_SECTIONS),
                      "tools": len(tools), "default_surface": default,
                      "rust_tests": rust, "python_tests": python,
                      "problems": len(problems)}, indent=2))
    for problem in problems:
        print(f"\n{problem['document']}\n  - [{problem['check']}] {problem['detail']}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
