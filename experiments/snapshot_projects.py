#!/usr/bin/env python3
"""Export committed Rust/Python source only; do not copy secrets, data, or working-tree edits."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

from benchmark import fingerprint, write_json

PROJECTS = {"modelshare": Path("/Users/ja/dev/modelshare"),
            "pig": Path("/Users/ja/dev/mine/pig"), "sigil": Path("/Users/ja/dev/mine/sigil")}


def snapshot(repository, destination):
    repository, destination = repository.resolve(), destination.resolve()
    if destination.is_relative_to(repository):
        raise ValueError("snapshot must be outside the repository")
    revision = subprocess.check_output(["git", "-C", str(repository), "rev-parse", "HEAD"], text=True).strip()
    tree = subprocess.check_output(["git", "-C", str(repository), "ls-tree", "-rz", revision])
    entries = []
    for record in tree.split(b"\0"):
        if not record:
            continue
        metadata, relative = record.split(b"\t", 1)
        mode, kind, oid = metadata.decode().split()
        path = Path(relative.decode())
        if path.suffix not in {".rs", ".py"}:
            continue
        if mode not in {"100644", "100755"} or kind != "blob" or path.is_absolute() or ".." in path.parts:
            raise ValueError("unsafe source tree entry")
        data = subprocess.check_output(["git", "-C", str(repository), "cat-file", "blob", oid])
        if len(data) > 2 * 1024 * 1024 or b"\0" in data:
            raise ValueError(f"invalid source file: {path}")
        data.decode("utf-8")
        entries.append((path, data))
    if not entries:
        raise ValueError("repository has no committed supported source")
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    root = destination/"corpus"
    root.mkdir()
    for relative, data in entries:
        target = root/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    manifest = {"repository": str(repository), "revision": revision, "corpus": fingerprint(root),
                "files": {str(p): hashlib.sha256(data).hexdigest() for p, data in entries},
                "source_files": len(entries), "source_lines": sum(len(data.splitlines()) for _, data in entries)}
    write_json(destination/"snapshot.json", manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if any(output.is_relative_to(root) for root in PROJECTS.values()):
        parser.error("output must be outside the source repositories")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    for name, root in PROJECTS.items():
        manifest = snapshot(root, output/name)
        print(json.dumps({"project": name, "revision":manifest["revision"],
                          "files":manifest["source_files"], "lines":manifest["source_lines"]}))


if __name__ == "__main__":
    main()
