"""Internal-reference checks for added lines and the current snapshot."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_push_scan import GH_TOKEN, commit, git, scan, setup  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
_PATH = Path(os.environ.get("PUSH_SCAN_UNDER_TEST") or ROOT / "tools/push_scan.py")
_spec = importlib.util.spec_from_file_location("internal_ref_scanner", _PATH)
scanner = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = scanner
_spec.loader.exec_module(scanner)

TASK = f"M{9}-T{7}"
TASKS_TEXT = (
    "# Tasks\n\n"
    + "\n".join(f"- [ ] M{9}-T{number}: Work item" for number in (7, 8, 9))
    + "\n\nPlan: " + TASK + "\n"
)
CASES = [
    (TASK, "task id"),
    (TASK + "a", "task id"),
    *[(f"{directory}/design.md", "artifact path") for directory in ("specs", "notes", "reviews", "lanes")],
    ("PLAN" + "_release", "artifact path"),
    (f"{'specs'}/設計.md", "artifact path"),
    ("PLAN" + "_設計", "artifact path"),
    (f"prefix{'notes'}/design.md", "artifact path"),
    (f"M{3}_RESOLVERS", "spec filename"),
    (f"M{6}_API_SURFACE", "spec filename"),
    (f"DR-{3:03d}", "decision record"),
    (f"prefixDR-{3:03d}", "decision record"),
    *[(f"{kind} round {1}", "review round") for kind in ("build", "spec", "review")],
    *[(f"round {1} {kind}", "review round") for kind in ("finding", "review")],
    (f"finding {12}", "finding number"),
    (f"(spec {2}.{1})", "section citation"),
    (f"spec ({2}.{1})", "section citation"),
    (f"invoice path spec, {2}.{1}", "section citation"),
    (f"section {2}.{1}", "section citation"),
    *[(f"{context} {label}{number}", "decision/invariant label")
      for context in ("decision", "invariant", "spec")
      for label, number in (("D", 3), ("G", 12), ("I", 1))],
    (f"D{3} is the DECISION", "decision/invariant label"),
]


def check(path: str, lines: list[tuple[int, str]]) -> int:
    return scanner._scan_internal_refs("a" * 40, path, lines)


@pytest.mark.parametrize("value,label", CASES)
def test_each_pattern_blocks_with_location_and_no_surrounding_text(capsys, monkeypatch, value, label):
    monkeypatch.setattr(scanner, "_display_path", lambda path: repr(path))
    assert check("packages/example.py", [(17, f"# private-prefix {value} private-suffix")]) > 0
    out = capsys.readouterr().out
    assert "HARD BLOCK" in out and "packages/example.py" in out and "line=17" in out
    assert label in out
    assert "private-prefix" not in out and "private-suffix" not in out


@pytest.mark.parametrize("line", [
    "# Money uses exact decimal arithmetic.", "D" + "3 is a coordinate", "G" + "12 is a cell",
    "I" + "1 is an account", "indecision D" + "3", "invariants D" + "3",
    "speculation D" + "3", "decision D" + "123", "decision AD" + "3",
    TASK + "abc", "specs/", "PLAN" + "_", "finding things",
])
def test_clean_lines_pass(capsys, line):
    assert check("tools/example.py", [(8, line)]) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("path", ["tools/push_scan.py", "tools/tests/test_push_scan.py"])
def test_only_explicit_scanner_files_are_exempt(capsys, path):
    assert check(path, [(1, TASK)]) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("path", [
    "packages/a.py", "plugins/a.py", "tests/a.py", "tools/a.py", "README.md", "README", "LICENSE",
    "docs/guide", "doc/guide.rst", "evals/honesty/README.md",
    "tools/tests/test_internal_refs.py", "tools/nested/push_scan.py",
    "tools/tests/nested/test_push_scan.py",
])
def test_scope_and_exemptions_are_exact(capsys, monkeypatch, path):
    monkeypatch.setattr(scanner, "_display_path", lambda path: repr(path))
    assert check(path, [(6, TASK)]) == 1
    assert path in capsys.readouterr().out


def test_commit_message_is_outside_file_scope(capsys):
    assert check("<commit-message>", [(3, "Plan: " + TASK)]) == 0
    assert capsys.readouterr().out == ""


def test_inline_marker_cannot_exempt_a_line(capsys, monkeypatch):
    monkeypatch.setattr(scanner, "_display_path", lambda path: repr(path))
    assert check("tests/example.py", [(4, TASK + " # internal-ref-ok")]) == 1
    assert "HARD BLOCK" in capsys.readouterr().out


def test_diff_parser_yields_added_lines_with_destination_numbers():
    patch = (
        "diff --git a/tests/a.py b/tests/a.py\n--- a/tests/a.py\n+++ b/tests/a.py\n"
        "@@ -1,2 +1,3 @@\n keep\n-" + TASK + "\n+replacement\n+" + TASK + "\n"
        "@@ -20,0 +22 @@\n++++looks-like-header\n\\ No newline at end of file\n"
    ).encode()
    assert list(scanner._added_diff_lines(patch)) == [(2, "replacement"), (3, TASK), (22, "+++looks-like-header")]


@pytest.mark.parametrize("mode,blocks", [("clean", True), ("private", False), ("public", True)])
def test_new_reference_hard_blocks_only_in_public_bound_modes(setup, mode, blocks):
    make, policy = setup
    name = {"clean": "engine-repo", "private": "notes-repo", "public": "other-repo"}[mode]
    repo = make(name)
    commit(repo, {"packages/example.py": "# clean\n# " + TASK + "\n",
                  "TASKS.md": TASKS_TEXT}, "Example\n\nPlan: " + TASK)
    code, out = scan(repo, policy)
    assert f"Mode: {mode}" in out, out
    if blocks:
        assert code == 1, out
        assert "internal reference" in out and "packages/example.py" in out and "line=2" in out
        assert 'path="TASKS.md"' in out, out
    else:
        assert code == 0, out
        assert "internal reference" not in out, out


def test_private_mode_still_blocks_secret_with_internal_references(setup):
    make, policy = setup
    repo = make("notes-repo")
    commit(repo, {"TASKS.md": TASKS_TEXT + f"\ntoken {GH_TOKEN}\n"}, "Example\n\nPlan: " + TASK)
    code, out = scan(repo, policy)
    assert "Mode: private" in out, out
    assert code == 1 and "secret (github-pat)" in out, out
    assert "internal reference" not in out, out
    assert GH_TOKEN not in out


def test_only_added_lines_in_pushed_range_are_checked(setup, monkeypatch):
    make, policy = setup
    repo = make("engine-repo")
    commit(repo, {"packages/example.py": "# " + TASK + "\nvalue = 1\n# " + TASK + "\n"}, "Seed\n\nPlan: " + TASK)
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    bare = git(repo, "remote", "get-url", "origin").strip()
    git(repo, "--git-dir=" + bare, "fetch", "-q", str(repo), "main:main")
    commit(repo, {"packages/example.py": "# " + TASK + "\nvalue = 2\n"}, "Change\n\nPlan: " + TASK)
    # The existing reference is context, and the other reference was deleted.
    code, out = scan(repo, policy)
    assert code == 0, out
    monkeypatch.chdir(repo)
    _, versions, _ = scanner._introduced_versions([git(repo, "rev-parse", "HEAD").strip()])
    assert scanner._internal_ref_hard_blocks(versions) == 0


def test_transient_reference_is_caught_even_when_deleted_at_tip(setup):
    make, policy = setup
    repo = make("engine-repo")
    commit(repo, {"tests/transient.py": "# " + TASK + "\n"}, "Add\n\nPlan: " + TASK)
    commit(repo, {"tests/transient.py": None}, "Remove\n\nPlan: " + TASK)
    code, out = scan(repo, policy)
    assert code == 1 and "internal reference" in out and "tests/transient.py" in out, out


def test_trailers_and_exact_exempt_paths_pass_end_to_end(setup, monkeypatch):
    make, policy = setup
    repo = make("engine-repo")
    commit(repo, {"tools/push_scan.py": "# " + TASK + "\n",
                  "tools/tests/test_push_scan.py": "# " + TASK + "\n",
                  "README.md": "A clean document.\n"}, "Example\n\nPlan: " + TASK)
    code, out = scan(repo, policy)
    assert code == 0, out
    monkeypatch.chdir(repo)
    _, versions, _ = scanner._introduced_versions([git(repo, "rev-parse", "HEAD").strip()])
    assert scanner._internal_ref_hard_blocks(versions) == 0


def test_merge_reference_and_unusual_path_are_checked(setup):
    make, policy = setup
    repo = make("engine-repo")
    commit(repo, {"README.md": "Seed\n"}, "Seed\n\nPlan: " + TASK)
    git(repo, "checkout", "-q", "-b", "side")
    commit(repo, {"packages/side.py": "# clean\n"}, "Side\n\nPlan: " + TASK)
    git(repo, "checkout", "-q", "main")
    commit(repo, {"packages/main.py": "# clean\n"}, "Main\n\nPlan: " + TASK)
    git(repo, "merge", "--no-commit", "--no-ff", "side")
    path = 'tests/odd\t"line\nname.py'
    commit(repo, {path: "# " + TASK + "\n"}, "Merge\n\nPlan: " + TASK)
    code, out = scan(repo, policy)
    assert code == 1 and "internal reference" in out and "line=1" in out, out
    assert 'odd\\t\\"line\\nname.py' in out


def test_current_tree_against_empty_tree_has_no_internal_references(tmp_path, capsys, monkeypatch):
    """Use an isolated index to include tracked edits and the new regression file."""
    env = {**os.environ, "GIT_INDEX_FILE": str(tmp_path / "snapshot-index")}

    def snapshot_git(*args: str) -> bytes:
        proc = subprocess.run(["git", *args], cwd=ROOT, env=env, capture_output=True)
        assert proc.returncode == 0, proc.stderr.decode()
        return proc.stdout

    snapshot_git("add", "-A", "--", ".")
    tree = snapshot_git("write-tree").decode().strip()
    empty = snapshot_git("hash-object", "-w", "-t", "tree", "--", os.devnull).decode().strip()
    paths = snapshot_git("ls-tree", "-r", "--name-only", "-z", tree).split(b"\0")
    monkeypatch.setattr(scanner, "_display_path", lambda path: repr(path))
    hits = 0
    for raw_path in paths:
        if not raw_path:
            continue
        path = os.fsdecode(raw_path)
        patch = snapshot_git("diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", "--text",
                             "--unified=0", empty, tree, "--", ":(literal)" + path)
        hits += check(path, list(scanner._added_diff_lines(patch)))
    assert hits == 0, capsys.readouterr().out


def test_git_display_configuration_cannot_hide_added_lines(setup):
    make, policy = setup
    repo = make("engine-repo")
    git(repo, "config", "color.ui", "always")
    commit(repo, {"packages/example.py": "# " + TASK + "\n"}, "Add\n\nPlan: " + TASK)
    code, out = scan(repo, policy)
    assert code == 1 and "internal reference" in out and "line=1" in out, out
