"""Reject first-party plugin changes that do not raise their recorded version."""
from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib

from packaging.version import Version

CORE_SOURCE = Path("packages/erpclaw-core/src")


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, stdout=subprocess.PIPE).stdout


def snapshot_commit(commit: str, destination: Path) -> None:
    # Read Git blobs directly: checkout filters, export attributes and line-ending
    # conversion must not change the bytes that the runtime will hash.
    entries = git("ls-tree", "-rz", "--full-tree", commit, "--", "plugins", str(CORE_SOURCE)).split(b"\0")
    for entry in filter(None, entries):
        metadata, path_bytes = entry.split(b"\t", 1)
        mode, kind, oid = metadata.split(b" ")
        path = Path(os.fsdecode(path_bytes))
        if kind != b"blob" or mode not in (b"100644", b"100755"):
            raise ValueError(f"unsupported Git entry in snapshot: {path}")
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(git("cat-file", "blob", oid.decode("ascii")))


def read_plugins(root: Path) -> dict[str, dict[str, str]]:
    """Called in a fresh interpreter for each snapshot to isolate plugin imports."""
    core = root / CORE_SOURCE
    if not (core / "erpclaw_core" / "__init__.py").is_file() or not (core / "erpclaw_core" / "manifest.py").is_file():
        raise ValueError(f"missing core runtime in snapshot: {core}")
    sys.path.insert(0, str(core))
    from erpclaw_core.manifest import FIRST_PARTY_DISTRIBUTIONS, Manifest, artifact_digest

    projects = {}
    for project in sorted((root / "plugins").glob("*/pyproject.toml")):
        config = tomllib.loads(project.read_text(encoding="utf-8"))["project"]
        distribution = config["name"]
        if distribution not in FIRST_PARTY_DISTRIBUTIONS:
            continue
        if distribution in projects:
            raise ValueError(f"duplicate distribution {distribution}")
        entries = config.get("entry-points", {}).get("erpclaw.plugins", {})
        if len(entries) != 1:
            raise ValueError(f"{distribution} must have one plugin entry point")
        entry_name, target = next(iter(entries.items()))
        module, separator, attribute = target.partition(":")
        if not separator or not module or not attribute:
            raise ValueError(f"invalid plugin entry point for {distribution}: {target}")
        package = project.parent / "src" / module.split(".")[0]
        if not (package / "__init__.py").is_file():
            raise ValueError(f"missing package for {distribution}: {package}")
        projects[distribution] = (entry_name, module, attribute, package)
        sys.path.insert(0, str(project.parent / "src"))
    missing = FIRST_PARTY_DISTRIBUTIONS - projects.keys()
    if missing:
        raise ValueError(f"missing first-party plugins: {', '.join(sorted(missing))}")
    result = {}
    for distribution, (entry_name, module, attribute, package) in sorted(projects.items()):
        plugin = importlib.import_module(module)
        for part in attribute.split("."):
            plugin = getattr(plugin, part)
        manifest = plugin.manifest
        if not isinstance(manifest, Manifest) or manifest.distribution != distribution or manifest.name != entry_name:
            raise ValueError(f"{distribution}: entry point and manifest do not agree")
        result[distribution] = {"name": manifest.name, "version": manifest.version,
                                "manifest": manifest.digest(), "code": artifact_digest(package)}
    return result


def read_isolated(root: Path) -> dict[str, dict[str, str]]:
    output = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--snapshot", str(root)],
                            check=True, stdout=subprocess.PIPE, text=True).stdout
    return json.loads(output)


def comparison_commit() -> str:
    git("fetch", "--no-tags", "--depth=1", "origin", "main")
    if os.environ.get("GITHUB_EVENT_NAME") == "push" and os.environ.get("GITHUB_REF") == "refs/heads/main":
        # A shallow commit still contains its parent ID even though Git cannot
        # traverse it until that object has been fetched.
        commit = git("cat-file", "-p", "HEAD").decode("utf-8").splitlines()
        parents = [line.split(" ", 1)[1] for line in commit if line.startswith("parent ")]
        if not parents:
            raise ValueError("main push has no first parent to compare")
        git("fetch", "--no-tags", "--depth=1", "origin", parents[0])
        return parents[0]
    return "FETCH_HEAD"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="comparison commit (otherwise fetch main)")
    parser.add_argument("--candidate", default="HEAD", help="candidate commit (default: HEAD)")
    parser.add_argument("--worktree", action="store_true", help="compare the current worktree instead of a commit")
    parser.add_argument("--snapshot", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.snapshot:
        print(json.dumps(read_plugins(args.snapshot)))
        return 0
    base = args.base or comparison_commit()
    with tempfile.TemporaryDirectory(prefix="erpclaw-release-rule-") as temp:
        root = Path(temp)
        before, after = root / "before", root / "after"
        snapshot_commit(base, before)
        if args.worktree:
            shutil.copytree(Path("plugins"), after / "plugins", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            shutil.copytree(CORE_SOURCE, after / CORE_SOURCE, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            snapshot_commit(args.candidate, after)
        old, new = read_isolated(before), read_isolated(after)
    if old.keys() != new.keys():
        raise ValueError("first-party plugin distributions differ between snapshots")
    failed = False
    for distribution in sorted(old):
        previous, current = old[distribution], new[distribution]
        old_version, new_version = Version(previous["version"]), Version(current["version"])
        changed = [kind for kind in ("manifest", "code") if previous[kind] != current[kind]]
        if new_version < old_version or (changed and new_version <= old_version):
            reason = "version downgrade" if new_version < old_version else "version was not raised"
            print(f"{current['name']} ({distribution}): {', '.join(changed) or 'no digest'} changed; "
                  f"{reason} ({previous['version']} -> {current['version']})")
            failed = True
    if not failed:
        print("Plugin release versions are valid.")
    return int(failed)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Release rule failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
