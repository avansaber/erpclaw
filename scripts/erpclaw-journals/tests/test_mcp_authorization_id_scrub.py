"""Replies from the model-facing call never carry the bearer id back."""
import importlib.util
import json
import os
import subprocess
import sys

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from test_mcp_authorization_id import _SpawnCounter
from test_mcp_authorization_id import _mcp_modules
from test_mcp_authorization_id import active_env as _shared_active_env

active_env = _shared_active_env

FIXED_ID = "authz_0123456789abcdef0123"
PLACEHOLDER = "[authorization-id]"
SETUP_SCRIPT = "source/erpclaw/scripts/erpclaw-setup/db_query.py"


def _seed_home_lib():
    import erpclaw_lib
    tree_lib = os.path.dirname(
        os.path.dirname(os.path.abspath(erpclaw_lib.__file__)))
    home = os.environ.get("ERPCLAW_HOME", "")
    link = os.path.join(home, "lib")
    if (os.path.isdir(os.path.join(tree_lib, "erpclaw_lib"))
            and home
            and not os.path.lexists(link)):
        os.symlink(tree_lib, link)


def _unwired_domain_scripts():
    repo_root = os.path.normpath(
        os.path.join(_TESTS_DIR, "..", "..", "..", "..", ".."))
    path = os.path.join(
        repo_root, "testing", "unit", "L0",
        "test_action_impact_declarations.py")
    spec = importlib.util.spec_from_file_location(
        "_impact_declarations_for_scrub", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.UNWIRED_DOMAIN_SCRIPTS


def test_unknown_flag_refusal_does_not_echo_the_id(
        active_env, tmp_path, monkeypatch):
    tool_router, _server = _mcp_modules()
    assert os.path.exists(active_env[0])
    _seed_home_lib()
    # list-companies must stay on an unwired domain: only a script that
    # rejects --authorization-id as an unknown flag exercises this path.
    assert SETUP_SCRIPT in _unwired_domain_scripts()
    reply = tool_router.dispatch(
        "list-companies", {}, False, authorization_id=FIXED_ID)
    text = json.dumps(reply)
    assert reply.get("status") == "error", reply
    assert "Unknown flags" in text
    assert PLACEHOLDER in text
    assert FIXED_ID not in text


def test_wired_domain_refusal_does_not_echo_the_id(
        active_env, tmp_path, monkeypatch):
    tool_router, _server = _mcp_modules()
    assert os.path.exists(active_env[0])
    _seed_home_lib()
    reply = tool_router.dispatch(
        "list-customers", {}, False, authorization_id=FIXED_ID)
    assert reply.get("status") == "error", reply
    assert reply.get("message") == "AUTHORIZATION_REFUSED", reply
    assert FIXED_ID not in json.dumps(reply)


def test_fallback_stdout_and_stderr_are_scrubbed(active_env, monkeypatch):
    tool_router, _server = _mcp_modules()
    assert os.path.exists(active_env[0])

    def _fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 1,
            "boom " + FIXED_ID + " end",
            "usage: error: unrecognized arguments: "
            "--authorization-id=" + FIXED_ID)

    monkeypatch.setattr(tool_router.subprocess, "run", _fake_run)
    reply = tool_router.dispatch(
        "list-customers", {}, False, authorization_id=FIXED_ID)
    assert reply.get("stdout") == "boom " + PLACEHOLDER + " end", reply
    assert reply.get("stderr", "").endswith(
        "--authorization-id=" + PLACEHOLDER), reply
    assert FIXED_ID not in json.dumps(reply)


def test_nested_json_is_scrubbed(active_env, monkeypatch):
    tool_router, _server = _mcp_modules()
    assert os.path.exists(active_env[0])
    payload = {
        "status": "error " + FIXED_ID,
        "message": "m " + FIXED_ID,
        "detail": {"k": [FIXED_ID, 5, None], FIXED_ID: "v"},
    }

    def _fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 1, json.dumps(payload), "")

    monkeypatch.setattr(tool_router.subprocess, "run", _fake_run)
    reply = tool_router.dispatch(
        "list-customers", {}, False, authorization_id=FIXED_ID)
    assert FIXED_ID not in json.dumps(reply)
    assert reply.get("router_status") == "error " + PLACEHOLDER, reply
    assert reply.get("returncode") == 1, reply
    detail = reply.get("detail", {})
    assert detail.get("k", [None, None, None])[0] == PLACEHOLDER
    assert detail.get("k", [None, None, None])[1] == 5
    assert isinstance(detail.get("k", [None, None, None])[1], int)
    assert detail.get("k", [None, None, None])[2] is None
    assert detail.get(PLACEHOLDER) == "v", reply


def test_reply_without_an_id_is_unchanged(active_env, monkeypatch):
    tool_router, _server = _mcp_modules()
    assert os.path.exists(active_env[0])
    marker = "--authorization-id=zzz"
    payload = {
        "status": "error " + marker,
        "message": "m " + marker,
        "detail": {"k": [marker, 5, None], marker: "v"},
    }

    def _fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 1, json.dumps(payload), "")

    monkeypatch.setattr(tool_router.subprocess, "run", _fake_run)
    reply = tool_router.dispatch("list-customers", {}, False)
    expected = {
        "status": "error",
        "message": "m " + marker,
        "detail": {"k": [marker, 5, None], marker: "v"},
        "router_status": "error " + marker,
        "returncode": 1,
    }
    assert reply == expected


def test_short_id_is_refused_before_spawning(active_env, monkeypatch):
    tool_router, _server = _mcp_modules()
    assert os.path.exists(active_env[0])
    fake = _SpawnCounter()
    monkeypatch.setattr(tool_router.subprocess, "run", fake)
    refused = tool_router.dispatch(
        "list-customers", {}, False, authorization_id="a")
    assert refused.get("error") == "invalid_authorization", refused
    assert refused.get("detail") == "authorization_id must be an id string.", \
        refused
    assert fake.calls == []
    answered = tool_router.dispatch(
        "list-customers", {}, False, authorization_id="b" * 16)
    assert answered.get("status") == "ok", answered
    assert len(fake.calls) == 1


def test_replay_reply_is_scrubbed(active_env, monkeypatch):
    tool_router, _server = _mcp_modules()
    assert os.path.exists(active_env[0])
    payload = {
        "status": "ok",
        "replayed": True,
        "authorization_id": FIXED_ID,
        "result_kind": "journal-entry",
        "result_id": "je1",
        "result_status": "submitted",
    }

    def _fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 0, json.dumps(payload), "")

    monkeypatch.setattr(tool_router.subprocess, "run", _fake_run)
    reply = tool_router.dispatch(
        "submit-journal-entry", {"journal_entry_id": "je1"}, True,
        authorization_id=FIXED_ID)
    expected = dict(payload)
    expected["authorization_id"] = PLACEHOLDER
    assert reply == expected
    assert FIXED_ID not in json.dumps(reply)
