"""One operator-issued id travels from the model-facing call to the gate."""
import json
import os
import subprocess
import sys

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from test_journal_envelope_gate import (
    _env,
    _fresh_db,
    _grant,
    _issue,
    _je,
    _open,
    _patch_actor,
    _patch_ready,
    _read_one,
    _std,
    _via_router,
)


class _SpawnCounter:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"status": "ok"}), "")


def _mcp_modules():
    repo_root = os.path.normpath(
        os.path.join(_TESTS_DIR, "..", "..", "..", "..", ".."))
    nl_dir = os.path.join(repo_root, "testing", "nl")
    if nl_dir not in sys.path:
        sys.path.insert(0, nl_dir)
    import mcp_driver as driver
    driver._load_server_module()
    import importlib
    tool_router = importlib.import_module("erpclaw_mcp.tool_router")
    server = importlib.import_module("erpclaw_mcp.server")
    return tool_router, server


@pytest.fixture
def active_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    home = str(tmp_path / "home")
    os.makedirs(home, exist_ok=True)
    monkeypatch.setenv("ERPCLAW_HOME", home)
    monkeypatch.setenv("HOME", home)
    path = _fresh_db(tmp_path, monkeypatch, "mcp-auth")
    handle = _open(path)
    try:
        env = _env(handle)
    finally:
        handle.close()
    yield (path, env)
    from erpclaw_lib import seam
    seam.dispose_engines()


def test_mcp_authorization_id_reaches_the_gate(active_env, tmp_path, monkeypatch):
    """Model carries one operator-issued id to the gate; not qualification."""
    tool_router, server = _mcp_modules()
    path, env = active_env
    entry = _je(path, env)
    import authority_fixtures as fixtures
    fixtures.make_active(path)
    _patch_ready(monkeypatch)
    _patch_actor(monkeypatch)
    _grant(path, env["company_id"], entry, "submit-journal-entry")
    auth_id = _issue(
        path, "submit-journal-entry", _std("submit-journal-entry", path, entry))
    fake = _SpawnCounter()
    monkeypatch.setattr(tool_router.subprocess, "run", fake)
    args = {"journal_entry_id": entry}
    result = tool_router.dispatch(
        "submit-journal-entry", dict(args), True, authorization_id=auth_id)
    assert result.get("status") == "ok", result
    assert len(fake.calls) == 1
    argv = fake.calls[0]
    token = "--authorization-id=%s" % (auth_id,)
    assert argv.count(token) == 1
    assert argv[-1] == "--user-confirmed"
    fake.calls.clear()
    refused = tool_router.dispatch(
        "submit-journal-entry", {"authorization_id": auth_id}, True)
    assert refused.get("error") == "reserved_arg", refused
    refused_dash = tool_router.dispatch(
        "submit-journal-entry", {"authorization-id": auth_id}, True)
    assert refused_dash.get("error") == "reserved_arg", refused_dash
    assert fake.calls == []
    bad_number = tool_router.dispatch(
        "submit-journal-entry", dict(args), True, authorization_id=5)
    assert bad_number.get("error") == "invalid_authorization", bad_number
    bad_text = tool_router.dispatch(
        "submit-journal-entry", dict(args), True, authorization_id="a b")
    assert bad_text.get("error") == "invalid_authorization", bad_text
    assert "a b" not in json.dumps(bad_text)
    assert fake.calls == []
    specs = server._tool_specs()
    action_spec = next(
        spec for spec in specs if spec.get("name") == server.ACTION)
    assert "authorization_id" in action_spec["inputSchema"]["properties"]
    fake.calls.clear()
    via_server = server.call_action({
        "action_name": "submit-journal-entry",
        "args": dict(args),
        "user_confirmed": True,
        "authorization_id": auth_id,
    })
    assert via_server.get("status") == "ok", via_server
    assert len(fake.calls) == 1
    assert fake.calls[0].count(token) == 1
    assert fake.calls[0][-1] == "--user-confirmed"
    rest = argv[2:]
    code, payload = _via_router(rest, tmp_path, monkeypatch)
    assert code == 0, payload
    assert payload.get("document_status") == "submitted", payload
    stored = _read_one(
        path, "operation_authorization", ["id", "consumed_at"], auth_id)
    assert stored["consumed_at"] is not None
    code_replay, payload_replay = _via_router(rest, tmp_path, monkeypatch)
    assert code_replay == 0, payload_replay
    assert payload_replay.get("replayed") is True, payload_replay
    rest_without = [
        item for item in rest if not item.startswith("--authorization-id")]
    code_bare, payload_bare = _via_router(rest_without, tmp_path, monkeypatch)
    assert code_bare == 1, payload_bare
    assert payload_bare.get("message") == "AUTHORIZATION_REQUIRED", payload_bare
