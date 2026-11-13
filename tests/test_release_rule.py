"""The release gate compares real plugin packages from isolated Git commits."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
RULE = ROOT / "tools" / "release_rule.py"
PLUGIN = Path("plugins/erpclaw-email-folder/src/erpclaw_email_folder/__init__.py")
CORE = Path("packages/erpclaw-core/src/erpclaw_core")


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, text=True, capture_output=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    shutil.copytree(ROOT / "plugins", repo / "plugins", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(ROOT / CORE, repo / CORE, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    git(repo, "add", "plugins", "packages")
    git(repo, "commit", "-m", "base")
    return repo


def commit(repo):
    git(repo, "add", "-A")
    git(repo, "commit", "--allow-empty", "-m", "candidate")


def run_rule(repo, *args, env=None):
    return subprocess.run([sys.executable, str(RULE), *args], cwd=repo, text=True, capture_output=True,
                          env={**os.environ, **(env or {})})


def change(repo, old, new):
    path = repo / PLUGIN
    path.write_bytes(path.read_bytes().replace(old, new))


def compare(repo):
    commit(repo)
    return run_rule(repo, "--base", "HEAD^")


def test_unchanged_plugin_passes(repo):
    result = compare(repo)
    assert result.returncode == 0, result.stderr + result.stdout


def test_code_change_without_version_bump_fails(repo):
    with (repo / PLUGIN).open("ab") as f:
        f.write(b"\n# changed code\n")
    result = compare(repo)
    assert result.returncode != 0 and "email_folder" in result.stdout and "code" in result.stdout


def test_manifest_change_without_version_bump_fails(repo):
    change(repo, b'files=["mail"]', b'files=["inbox"]')
    result = compare(repo)
    assert result.returncode != 0 and "email_folder" in result.stdout and "manifest" in result.stdout


def test_core_only_manifest_digest_change_without_plugin_version_bump_fails(repo):
    path = repo / CORE / "manifest.py"
    source = path.read_text()
    old = 'return hashlib.sha256(body.encode()).hexdigest()'
    assert source.count(old) == 1
    path.write_text(source.replace(old, 'return hashlib.sha256((body + "changed").encode()).hexdigest()'))
    result = compare(repo)
    assert result.returncode == 1 and "email_folder" in result.stdout and "manifest" in result.stdout, result.stderr


def test_base_core_digest_change_cannot_be_hidden_by_candidate_core(repo):
    path = repo / CORE / "manifest.py"
    original = path.read_text()
    old = 'return hashlib.sha256(body.encode()).hexdigest()'
    assert original.count(old) == 1
    path.write_text(original.replace(old, 'return hashlib.sha256((body + "changed").encode()).hexdigest()'))
    commit(repo)
    path.write_text(original)
    result = compare(repo)
    assert result.returncode == 1 and "email_folder" in result.stdout and "manifest" in result.stdout, result.stderr


def test_worktree_core_digest_change_without_plugin_version_bump_fails(repo):
    path = repo / CORE / "manifest.py"
    source = path.read_text()
    old = 'return hashlib.sha256(body.encode()).hexdigest()'
    assert source.count(old) == 1
    path.write_text(source.replace(old, 'return hashlib.sha256((body + "changed").encode()).hexdigest()'))
    result = run_rule(repo, "--base", "HEAD", "--worktree")
    assert result.returncode == 1 and "email_folder" in result.stdout and "manifest" in result.stdout, result.stderr


def test_missing_snapshot_core_fails_closed(repo):
    shutil.rmtree(repo / CORE)
    result = compare(repo)
    assert result.returncode != 0 and "missing core runtime in snapshot" in result.stderr


def test_code_change_with_higher_version_passes(repo):
    change(repo, b'version="0.0.1"', b'version="0.0.2"')
    result = compare(repo)
    assert result.returncode == 0, result.stderr + result.stdout


def test_version_downgrade_fails(repo):
    change(repo, b'version="0.0.1"', b'version="0.0.0"')
    result = compare(repo)
    assert result.returncode != 0 and "email_folder" in result.stdout and "version" in result.stdout


def test_line_ending_change_to_hashed_file_fails(repo):
    path = repo / PLUGIN
    raw = path.read_bytes()
    assert b"\r\n" not in raw
    path.write_bytes(raw.replace(b"\n", b"\r\n"))
    result = compare(repo)
    assert result.returncode != 0 and "email_folder" in result.stdout and "code" in result.stdout


def test_shallow_candidate_fetches_main(repo, tmp_path):
    git(repo, "checkout", "-b", "feature")
    with (repo / PLUGIN).open("ab") as f:
        f.write(b"\n# changed code\n")
    commit(repo)
    clone = tmp_path / "shallow"
    subprocess.run(["git", "clone", "--depth=1", "--branch", "feature", repo.as_uri(), str(clone)],
                   check=True, capture_output=True)
    result = run_rule(clone, env={"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/feature"})
    assert result.returncode != 0 and "email_folder" in result.stdout and "code" in result.stdout, result.stderr


def test_shallow_main_push_fetches_first_parent(repo, tmp_path):
    with (repo / PLUGIN).open("ab") as f:
        f.write(b"\n# changed code\n")
    commit(repo)
    clone = tmp_path / "shallow"
    subprocess.run(["git", "clone", "--depth=1", repo.as_uri(), str(clone)], check=True, capture_output=True)
    result = run_rule(clone, env={"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main"})
    assert result.returncode != 0 and "email_folder" in result.stdout and "code" in result.stdout, result.stderr
