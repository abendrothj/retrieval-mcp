#!/usr/bin/env python3
"""Rebuild the pinned corpus from upstream and print the exact held-out reproduction commands.

This repository ships question sets, gold answers and analysis JSON, but never a corpus copy: trial
directories quote source verbatim, so they stay local. That promise is only worth something if a
reader can rebuild the corpus the results were measured on and get the same bytes, which is what
this does - clone the pinned Django revision, take the five packages the suite scopes, and verify
the result against the fingerprint recorded in `django_suite_manifest.json` before anything else
runs. A mismatch is a hard failure: a corpus that differs by one byte is a different experiment.

Nothing here calls a model or spends money. It ends by printing the commands that would.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import comparison_runner


def clone_scope(url, commit, scope, workdir):
    """A blobless sparse clone of exactly the packages the suite scopes, at the pinned commit."""
    workdir.mkdir(parents=True, exist_ok=True)
    checkout = workdir / "upstream"
    if not (checkout / ".git").is_dir():
        subprocess.run(["git", "clone", "--filter=blob:none", "--no-checkout", url, str(checkout)],
                       check=True)
    subprocess.run(["git", "-C", str(checkout), "sparse-checkout", "set", "--no-cone", *scope],
                   check=True)
    subprocess.run(["git", "-C", str(checkout), "checkout", "--detach", commit], check=True)
    head = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    if head != commit:
        raise RuntimeError(f"checkout is at {head}, not the pinned {commit}")
    return checkout


def materialise(checkout, scope, destination):
    """Copy the scoped packages, preserving repository-relative paths, into an empty destination."""
    if destination.exists():
        raise FileExistsError(destination)
    destination.mkdir(parents=True)
    for relative in scope:
        source = checkout / relative
        if not source.is_dir():
            raise FileNotFoundError(source)
        shutil.copytree(source, destination / relative,
                        ignore=shutil.ignore_patterns(*comparison_runner.IGNORED_STATE))
    return destination


def verify(destination, manifest):
    """Fail loudly unless the rebuilt corpus and the shipped suites are byte-identical to the pins."""
    problems = []
    # A symlinked parent - /tmp on macOS - would otherwise trip the corpus symlink guard with a
    # confusing error about the corpus rather than about the path it was handed.
    rebuilt = comparison_runner.source_fingerprint(Path(destination).resolve())
    pinned = manifest["corpus"]
    if rebuilt["sha256"] != pinned["sha256"]:
        problems.append(f"corpus sha256 is {rebuilt['sha256']}, pinned {pinned['sha256']}")
    if rebuilt["files"] != pinned["files"]:
        problems.append(f"corpus has {rebuilt['files']} files, pinned {pinned['files']}")
    suites = Path(__file__).resolve().parent / "suites"
    for name, filename in (("development", "django_development_questions.json"),
                           ("heldout", "django_heldout_questions.json")):
        digest = hashlib.sha256((suites / filename).read_bytes()).hexdigest()
        if digest != manifest[name]["sha256"]:
            problems.append(f"{filename} sha256 is {digest}, pinned {manifest[name]['sha256']}")
    return rebuilt, problems


def commands(destination, workspace, output, here):
    server = here.parent / "target/release/retrieval-mcp"
    semantic = here.parent / "target/release/examples/ollama_backend"
    return [
        f"python3 {here}/validate_suite.py --questions {here}/suites/django_heldout_questions.json"
        f" --corpus {destination}",
        f"python3 {here}/comparison_runner.py prepare --source-root {destination}"
        f" --workspace {workspace} --systems {here}/systems/comparison_systems_heldout.json"
        f" --semantic-command '[\"{semantic}\"]'",
        f"python3 {here}/comparison_runner.py run --workspace {workspace}"
        f" --systems {here}/systems/comparison_systems_heldout.json"
        f" --questions {here}/suites/django_heldout_questions.json --output {output}"
        f" --semantic-command '[\"{semantic}\"]' --client claude --model YOUR_EXPLICIT_MODEL_ID"
        f" --allow-model-usage --timeout 600 --seed 20260912",
        f"python3 {here}/end_to_end.py --run {output}"
        f" --questions {here}/suites/django_heldout_questions.json --output {output}/end-to-end.json",
        f"# build first if needed: cargo build --release --manifest-path {here.parent}/Cargo.toml"
        f" (binary: {server})",
    ]


def main():
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=here / "suites/django_suite_manifest.json")
    parser.add_argument("--destination", type=Path, required=True,
                        help="new directory for the rebuilt corpus")
    parser.add_argument("--workdir", type=Path, required=True,
                        help="scratch directory for the upstream clone")
    parser.add_argument("--workspace", type=Path, default=Path("/path/to/new-workspace"))
    parser.add_argument("--output", type=Path, default=Path("/path/to/new-run"))
    parser.add_argument("--url")
    args = parser.parse_args()

    manifest = json.loads(args.manifest.resolve(strict=True).read_text(encoding="utf-8"))
    upstream = manifest["upstream"]
    checkout = clone_scope(args.url or upstream["repository"], upstream["commit"],
                           manifest["scope"], args.workdir.resolve())
    destination = materialise(checkout, manifest["scope"], args.destination.resolve())
    rebuilt, problems = verify(destination, manifest)
    print(json.dumps({
        "upstream": upstream,
        "corpus": str(destination),
        "rebuilt": rebuilt,
        "matches_pinned_corpus": not problems,
        "problems": problems,
    }, indent=2))
    if problems:
        return 1
    print("\nReproduce the held-out comparison with:\n")
    for command in commands(destination, args.workspace, args.output, here):
        print(f"  {command}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
