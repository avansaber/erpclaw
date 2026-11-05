"""End-to-end tests for the push scanner against throwaway local repositories.

Forbidden values are assembled at run time so this file never contains them
literally (the scanner reads its own repository too).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCANNER = Path(os.environ.get("PUSH_SCAN_UNDER_TEST") or Path(__file__).resolve().parents[1] / "push_scan.py")
CODENAME = "zebra" + "corn"
HOME_PATH = "/Us" + "ers/someone/projects/x"
GH_TOKEN = "gh" + "p_" + "Kq7Zp2Lm9Xr4Tv8Wn3Bc6Jd1Hs5Fy0Ge2QaR"  # 36 characters, as GitHub issues them
EMAIL_SAFE = "dev@example.com"


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": EMAIL_SAFE,
           "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": EMAIL_SAFE,
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.fixture
def setup(tmp_path: Path):
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({
        "private_repos": ["owner/notes-repo"],
        "clean_history_repos": ["owner/engine-repo"],
        "safe_emails": [],
        "engine_forbidden": [{"label": "internal codename", "regex": rf"\b{CODENAME}\b"}],
    }))

    def make(name: str) -> Path:
        bare = tmp_path / "remotes" / "owner" / f"{name}.git"
        bare.mkdir(parents=True)
        git(bare, "init", "-q", "--bare", "-b", "main")
        work = tmp_path / name
        work.mkdir()
        git(work, "init", "-q", "-b", "main")
        git(work, "remote", "add", "origin", str(bare))
        return work

    return make, policy


def commit(repo: Path, files: dict[str, str | None], message: str) -> None:
    for name, text in files.items():
        path = repo / name
        if text is None:
            git(repo, "rm", "-q", name)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        git(repo, "add", name)
    git(repo, "commit", "-q", "-m", message)


def scan(repo: Path, policy: Path | None) -> tuple[int, str]:
    args = [sys.executable, str(SCANNER)]
    if policy:
        args += ["--policy", str(policy)]
    proc = subprocess.run([*args, "origin", "main"], cwd=repo, capture_output=True, text=True,
                          env={k: v for k, v in os.environ.items() if k != "PUSH_SCAN_POLICY"})
    return proc.returncode, proc.stdout + proc.stderr


def test_private_mode_allows_internal_words_but_blocks_secrets(setup):
    make, policy = setup
    repo = make("notes-repo")
    commit(repo, {"plan.md": f"{CODENAME} lives at {HOME_PATH}\n"}, "Notes")
    code, out = scan(repo, policy)
    assert "Mode: private" in out
    assert code == 0, out
    commit(repo, {"leak.md": f"token {GH_TOKEN}\n"}, "Oops")
    code, out = scan(repo, policy)
    assert code == 1 and "secret (github-pat)" in out
    assert GH_TOKEN not in out  # the value is redacted


def test_clean_mode_blocks_policy_and_public_shapes_everywhere(setup):
    make, policy = setup
    repo = make("engine-repo")
    commit(repo, {"README.md": "An ERP.\n"}, "Seed\n\nPlan: M0-T1")
    code, out = scan(repo, policy)
    assert "Mode: clean" in out and code == 0, out

    commit(repo, {"doc.md": f"from {CODENAME}\n"}, "Doc\n\nPlan: M0-T1")
    commit(repo, {"doc.md": None}, "Remove doc\n\nPlan: M0-T1")
    code, out = scan(repo, policy)
    assert code == 1 and "internal codename" in out  # caught although deleted at the tip


@pytest.mark.parametrize("files,message,label", [
    ({"a.py": f"ROOT = '{HOME_PATH}'\n"}, "A\n\nPlan: M0-T1", "absolute home path"),
    ({"b.py": "x = 1\n"}, f"Mention {CODENAME}\n\nPlan: M0-T1", "internal codename"),
    ({"c.py": "x = 2\n"}, "No trailer", "no Plan trailer"),
    ({"d.py": "x = 3\n"}, "See " + "PLAN_" + "2026-01-01.md\n\nPlan: M0-T1", "dated plan artifact name"),
    ({f"{CODENAME}/e.py": "x = 4\n"}, "E\n\nPlan: M0-T1", "internal codename in committed path"),
])
def test_clean_mode_hard_blocks(setup, files, message, label):
    make, policy = setup
    repo = make("engine-repo")
    commit(repo, files, message)
    code, out = scan(repo, policy)
    assert code == 1, out
    assert label in out


@pytest.mark.parametrize("name,blocks", [
    (".env", True), (".env.local", True), ("prod.env", True), ("config/.envrc", True), ("deploy/.env-prod", True),
    ("migrations/env.py", False), ("tools/environment.py", False), ("envelope.md", False),
])
def test_dotenv_names_block_and_python_modules_named_env_do_not(setup, name, blocks):
    """A dotenv file name blocks; Alembic's migrations/env.py is code, not a dotenv file."""
    make, policy = setup
    repo = make("engine-repo")
    commit(repo, {name: "x = 1\n"}, "A\n\nPlan: M0-T1")
    code, out = scan(repo, policy)
    assert ("disallowed .env filename" in out) is blocks, out
    assert (code == 1) is blocks, out


def test_trailers_are_ordinary_message_text(setup):
    make, policy = setup
    repo = make("engine-repo")
    commit(repo, {"a.py": "x = 1\n"}, "A\n\nPlan: M0-T2\nCo-Authored-By: Bot <noreply@example.com>")
    code, out = scan(repo, policy)
    assert code == 0, out
    commit(repo, {"b.py": "x = 2\n"}, f"B\n\nPlan: M0-T2\nSigned-off-by: {CODENAME} <dev@example.com>")
    code, out = scan(repo, policy)
    assert code == 1 and "internal codename" in out


def test_unlisted_repo_is_public_and_never_authorizes(setup):
    make, policy = setup
    repo = make("other-repo")
    commit(repo, {"a.py": "x = 1\n"}, "A\n\nPlan: M0-T1")
    code, out = scan(repo, policy)
    assert "Mode: public" in out and code == 0
    assert "never authorizes a push on its own" in out


def test_no_policy_means_public(setup):
    make, _policy = setup
    repo = make("notes-repo")
    commit(repo, {"a.md": f"{HOME_PATH}\n"}, "A\n\nPlan: M0-T1")
    code, out = scan(repo, None)
    assert "Mode: public" in out and code == 1


def test_only_unpushed_commits_are_scanned(setup):
    make, policy = setup
    repo = make("engine-repo")
    commit(repo, {"a.py": "x = 1\n"}, "A\n\nPlan: M0-T1")
    git(repo, "push", "-q", "origin", "main")
    commit(repo, {"b.py": "y = 2\n"}, "B\n\nPlan: M0-T1")
    code, out = scan(repo, policy)
    assert code == 0 and "Commits introduced: 1" in out


def test_dirty_worktree_is_invalid(setup):
    make, policy = setup
    repo = make("engine-repo")
    commit(repo, {"a.py": "x = 1\n"}, "A\n\nPlan: M0-T1")
    (repo / "a.py").write_text("x = 2\n")
    code, out = scan(repo, policy)
    assert code == 3 and "SCAN INVALID" in out


def test_bad_policy_is_invalid(setup, tmp_path):
    make, _ = setup
    repo = make("engine-repo")
    commit(repo, {"a.py": "x = 1\n"}, "A\n\nPlan: M0-T1")
    bad = tmp_path / "bad.json"
    bad.write_text('{"engine_forbidden": [{"label": "x", "regex": "("}]}')
    code, out = scan(repo, bad)
    assert code == 3


def test_two_runs_agree_byte_for_byte(setup):
    make, policy = setup
    repo = make("engine-repo")
    commit(repo, {"a.py": "x = 1\n", "b.md": "contact ops@corp-mail.test\n"}, "A\n\nPlan: M0-T1")
    first = scan(repo, policy)
    second = scan(repo, policy)
    assert first == second


# Regression tests for the first review (one per finding).


@pytest.mark.parametrize("key,value", [
    ("remote.origin.vcs", "evil"),
    ("remote.origin.push", "refs/heads/main:refs/heads/other"),
    ("push.followTags", "true"),
])
def test_push_changing_config_is_invalid(setup, key, value):
    make, policy = setup
    repo = make("notes-repo")
    commit(repo, {"a.md": "a\n"}, "A")
    git(repo, "config", key, value)
    code, out = scan(repo, policy)
    assert code == 3 and "forbidden override" in out


PW = "PASS" + "WORD"
APIKEY = "api" + "_key"


def test_plan_line_in_body_is_not_a_trailer(setup):
    make, policy = setup
    repo = make("engine-repo")
    commit(repo, {"a.py": "x = 1\n"}, "A\n\nPlan: M0-T1\n\nMore body after the line.")
    code, out = scan(repo, policy)
    assert code == 1 and "no Plan trailer" in out


def test_compressed_payload_blocks_on_clean_and_warns_on_private(setup):
    import gzip
    make, policy = setup
    for name, expected in (("engine-repo", 1), ("notes-repo", 2)):
        repo = make(name)
        (repo / "blob.gz").write_bytes(gzip.compress(b"hidden"))
        git(repo, "add", "blob.gz")
        git(repo, "commit", "-q", "-m", "Gz\n\nPlan: M0-T1")
        code, out = scan(repo, policy)
        assert code == expected, out
        assert "opaque gzip payload" in out


# Regression tests for the second review.

AK = "API" + "_KEY"


@pytest.mark.parametrize("line", [
    '{"' + APIKEY + '": "<' + 'your-api-key>"}',
    '{"' + APIKEY + '": "${' + 'API_KEY}"}',
    '{"' + PW.lower() + '": "changeme"}',
])
def test_whole_placeholders_pass(setup, line):
    make, policy = setup
    repo = make("notes-repo")
    commit(repo, {"cfg.txt": line + "\n"}, "Cfg")
    code, out = scan(repo, policy)
    assert code == 0, out


def test_credential_in_filename_blocks_and_is_redacted(setup):
    make, policy = setup
    repo = make("notes-repo")
    value = "ab12cd34ef56gh78ij90kl"
    commit(repo, {f"{AK}={value}.txt": "x\n"}, "F")
    code, out = scan(repo, policy)
    assert code == 1 and value not in out


def test_prefixed_zip_is_opaque(setup):
    import io
    import zipfile
    make, policy = setup
    repo = make("engine-repo")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("t.txt", f"token {GH_TOKEN}\n")
    (repo / "sfx.bin").write_bytes(b"#!/bin/sh\nexit 0\n" + buf.getvalue())
    git(repo, "add", "sfx.bin")
    git(repo, "commit", "-q", "-m", "Sfx\n\nPlan: M0-T1")
    code, out = scan(repo, policy)
    assert code == 1 and "opaque zip payload" in out


# Regression tests for the fourth review (the tokenizer route).


# Regression tests for the fifth review.


# Regression tests for the sixth review.


# Regression tests for the seventh review.


# ---------------------------------------------------------------- secrets come from gitleaks

def blocked(out: str, rule: str) -> bool:
    return f"secret ({rule})" in out


@pytest.mark.parametrize("text,rule", [
    ("ANTHROPIC=" + "sk-" + "ant-" + "api03-" + ("Zx9Qw2Er4Ty6Ui8Op0As3Df5Gh7Jk1Lz" * 3)[:93] + "AA", "anthropic-api-key"),
    ("aws_id = " + "AKIA" + "Q7ZP2LM5XR4TV6WN", "aws-access-token"),
    ("stripe " + "sk" + "_live_" + "4eC39HqLyjWDarjtT1zdp7dc", "stripe-access-token"),
    ("cursor " + "key" + "_" + "0f1e2d3c4b5a6978" * 4, "cursor-api-key"),
    ("cf " + "cfat" + "_" + "Q7ZP2LM5XR4TV6WNq8RmT2vLx9Wz4pKs3Bc6Jd1H", "cloudflare-api-token-cfat"),
    ("muse " + "LL" + "M_" + "1234567890" + "_" + "Zx9Qw2Er4Ty6Ui8Op0As3Df5Gh7", "meta-llama-api-key"),
    ("API" + "_KEY=" + "ab12cd34ef56gh78ij90kl", "credential-assignment"),
    ("url postgresql+psycopg://erpclaw" + ":" + "Kq7Zp2Lm9Xr4Tv8Wn3Bc" + "@db.example.com/x_test", "url-password"),
])
def test_secret_shapes_block_with_their_gitleaks_rule(setup, text, rule):
    make, policy = setup
    repo = make("notes-repo")
    commit(repo, {"k.txt": text + "\n"}, "K")
    code, out = scan(repo, policy)
    assert code == 1 and blocked(out, rule), out
    assert text.split()[-1].split("=")[-1] not in out  # the value is redacted


def test_utf16_secret_is_caught(setup):
    make, policy = setup
    repo = make("notes-repo")
    (repo / "u16.txt").write_bytes(f"token {GH_TOKEN}\n".encode("utf-16"))
    git(repo, "add", "u16.txt")
    git(repo, "commit", "-q", "-m", "U16")
    code, out = scan(repo, policy)
    assert code == 1 and blocked(out, "github-pat"), out


@pytest.mark.parametrize("steps,label", [
    ([({"a.txt": f"t {GH_TOKEN}\n"}, "Add"), ({"a.txt": None}, "Remove")], "secret (github-pat)"),
    ([({"b.txt": "ok\n"}, f"Message with {GH_TOKEN}")], "secret (github-pat)"),
    ([({f"dir/{GH_TOKEN}.txt": "ok\n"}, "Name")], "secret (github-pat) in committed path"),
])
def test_secret_in_a_deleted_blob_a_message_or_a_file_name_blocks(setup, steps, label):
    make, policy = setup
    repo = make("notes-repo")
    for files, message in steps:
        commit(repo, files, message)
    code, out = scan(repo, policy)
    assert code == 1 and label in out, out
    assert GH_TOKEN not in out  # values and flagged paths are redacted


def test_inline_allow_marker_does_not_hide_a_secret(setup):
    make, policy = setup
    repo = make("notes-repo")
    commit(repo, {"a.txt": f"t {GH_TOKEN}  # gitleaks" + ":allow\n"}, "Allow")
    code, out = scan(repo, policy)
    assert code == 1 and "secret (github-pat)" in out, out


def fake_gitleaks(tmp_path: Path, version: str) -> Path:
    exe = tmp_path / "fake-gitleaks"
    exe.write_text(f"#!/bin/sh\necho {version}\n")
    exe.chmod(0o755)
    return exe


@pytest.mark.parametrize("binary", ["missing", "8.0.0"])
def test_without_the_pinned_gitleaks_the_scan_is_invalid(setup, tmp_path, binary):
    make, policy = setup
    repo = make("notes-repo")
    commit(repo, {"a.txt": "ok\n"}, "A")
    path = str(tmp_path / "no-such-gitleaks") if binary == "missing" else str(fake_gitleaks(tmp_path, binary))
    env = {k: v for k, v in os.environ.items() if k != "PUSH_SCAN_POLICY"}
    env["ERPCLAW_GITLEAKS"] = path
    env["PATH"] = "/usr/bin:/bin"
    proc = subprocess.run([sys.executable, str(SCANNER), "--policy", str(policy), "origin", "main"], cwd=repo,
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "gitleaks" in proc.stderr and "SCAN INVALID" in proc.stdout + proc.stderr


def test_secret_names_are_redacted_in_early_errors(setup):
    make, policy = setup
    repo = make("notes-repo")
    commit(repo, {"a.txt": "ok\n"}, "A")
    (repo / f"{GH_TOKEN}.txt").write_text("untracked\n")
    code, out = scan(repo, policy)
    assert code == 3 and "Dirty" in out and GH_TOKEN not in out, out
    (repo / f"{GH_TOKEN}.txt").unlink()
    proc = subprocess.run([sys.executable, str(SCANNER), "--policy", str(policy), "origin", f"no-{GH_TOKEN}"],
                          cwd=repo, capture_output=True, text=True,
                          env={k: v for k, v in os.environ.items() if k != "PUSH_SCAN_POLICY"})
    assert proc.returncode == 3 and GH_TOKEN not in proc.stdout + proc.stderr, proc.stdout + proc.stderr


def test_a_file_name_starting_with_a_bracket_is_still_redacted(setup):
    make, policy = setup
    repo = make("notes-repo")
    commit(repo, {f"<{GH_TOKEN}.txt": "ok\n"}, "Bracket")
    code, out = scan(repo, policy)
    assert code == 1 and "in committed path" in out and GH_TOKEN not in out, out


def test_redacted_url_password_passes(setup):
    make, policy = setup
    repo = make("notes-repo")
    commit(repo, {"a.txt": "url postgresql+psycopg://erpclaw:<redacted>@db.example.com/x_test\n"}, "A")
    code, out = scan(repo, policy)
    assert code in (0, 2) and "url-password" not in out, out


@pytest.mark.parametrize("password,blocked_expected", [
    ("$" + "Zq81mXr4Lw9p", True), ("{" + "Zq81mXr4Lw9p", True), ("%" + "Zq81mXr4Lw9p", True),
    # An upper-case $NAME passes by gitleaks's own default allowlist (an environment
    # variable reference); a mixed-case one is read as a literal and blocks.
    ("${DB_PASSWORD}", False), ("$DB_PASSWORD", False), ("$(password)", False), ("{secret_value}", False),
    ("%(password)s", False),
])
def test_url_password_allowlist_matches_whole_placeholders_only(setup, password, blocked_expected):
    make, policy = setup
    repo = make("notes-repo")
    commit(repo, {"a.txt": f"url postgresql://erpclaw:{password}@db.example.com/x_test\n"}, "A")
    code, out = scan(repo, policy)
    assert (code == 1 and "url-password" in out) == blocked_expected, out


# The report lists each warning once and marks reviewed known-safe ones (one line per finding).

def _policy_with(policy: Path, known_safe: list[dict]) -> Path:
    raw = json.loads(policy.read_text())
    raw["known_safe"] = known_safe
    out = policy.with_name("policy_known.json")
    out.write_text(json.dumps(raw))
    return out


REVIEWED = [{"id": "example-tld", "label": "unfamiliar email", "path": ".*",
             "value": r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+\.)*example", "reason": "the reserved .example TLD"}]


def test_a_warning_repeated_across_commits_is_listed_once(setup):
    make, policy = setup
    repo = make("notes-repo")
    for i in range(3):  # the same address in a file every commit rewrites
        commit(repo, {"log.md": f"entry {i}\nwrite to ap@acme.example\n"}, f"Log {i}")
    code, out = scan(repo, policy)
    assert code == 2, out
    lines = [ln for ln in out.splitlines() if ln.startswith("  WARN")]
    assert len(lines) == 1 and "in 3 commit versions" in lines[0] and "WARN NEW" in lines[0], out
    assert "Warning hits:     3" in out and "Unique warnings:  1 (1 new, 0 known-safe)" in out
    assert "ap@acme.example" not in out  # values stay redacted


def test_a_known_safe_finding_is_marked_and_anything_else_stays_new(setup):
    make, policy = setup
    repo = make("notes-repo")
    commit(repo, {"a.md": "ap@acme.example\nbob@corp.invalid\nap@acme.example.test\n"}, "Addresses")
    code, out = scan(repo, _policy_with(policy, REVIEWED))
    assert code == 2, out
    lines = [ln for ln in out.splitlines() if ln.startswith("  WARN")]
    assert sum("known-safe [example-tld]" in ln for ln in lines) == 1
    assert sum("WARN NEW" in ln for ln in lines) == 2  # another reserved domain, and a lookalike
    assert lines[0].startswith("  WARN NEW")  # new findings come first
    assert "Unique warnings:  3 (2 new, 1 known-safe)" in out


@pytest.mark.parametrize("entry", [
    {"id": "x", "label": "unfamiliar email", "path": ".*", "value": ".*"},  # no reason
    {"id": "x", "label": "unfamiliar email", "path": ".*", "value": ".*", "reason": " "},
    {"id": "x", "label": "unfamiliar email", "path": "(", "value": ".*", "reason": "bad regex"},
    {"id": "x", "label": "unfamiliar email", "path": ".*", "value": ".*", "reason": None},  # review round 1
])
def test_a_known_safe_entry_without_a_reason_or_valid_patterns_invalidates_the_scan(setup, entry):
    make, policy = setup
    repo = make("notes-repo")
    commit(repo, {"a.md": "ap@acme.example\n"}, "Address")
    code, out = scan(repo, _policy_with(policy, [entry]))
    assert code == 3 and "known_safe entry invalid" in out, out



def test_every_kind_of_warning_is_listed_once_and_counted(setup):
    """Review round 1: opaque payloads and large files went around the grouping, so the summary could say no new
    warning while one was there."""
    import gzip
    make, policy = setup
    repo = make("notes-repo")
    for i in range(2):  # the same opaque file and the same large file in two commits
        (repo / "blob.gz").write_bytes(gzip.compress(b"hidden"))
        (repo / "big.txt").write_text("x" * 500_001 + str(i))
        git(repo, "add", "blob.gz", "big.txt")
        git(repo, "commit", "-q", "-m", f"Blobs {i}")
    code, out = scan(repo, policy)
    assert code == 2, out
    lines = [ln for ln in out.splitlines() if ln.startswith("  WARN")]
    assert sum("opaque gzip payload" in ln for ln in lines) == 1
    assert [ln for ln in lines if "file over 500KB" in ln][0].count("in 2 commit versions") == 1  # one finding
    assert all(ln.startswith("  WARN NEW") for ln in lines) and len(lines) == 2
    assert "Unique warnings:  2 (2 new, 0 known-safe)" in out
