"""Audit rows name their actor: reader, recording, inheritance and refusal.

The operating-system account is a fact the process cannot change; the channel,
principal claim and hop list arrive through the environment and are claims, not
evidence. A malformed value is refused by the foundation router before anything
runs; the same value on the direct domain path is recorded as invalid.
"""
import importlib.util
import json
import os
import pwd
import subprocess
import sys

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SETUP_DIR = os.path.dirname(_TESTS_DIR)
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(_SETUP_DIR))))
_ROUTER = os.path.join(_REPO_ROOT, "source", "erpclaw", "scripts", "db_query.py")
_SETUP_ROUTER = os.path.join(_SETUP_DIR, "db_query.py")
_MCP_DIR = os.path.join(_REPO_ROOT, "source", "erpclaw", "mcp")

if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import setup_helpers  # noqa: E402  (binds erpclaw_lib to this tree)
from setup_helpers import init_all_tables, open_reader, read_all  # noqa: E402
from erpclaw_lib import seam  # noqa: E402  (after the lib binding)


def _account():
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return "uid:%d" % os.geteuid()


ACCOUNT = _account()

VAR = "ERPCLAW_ACTOR_CONTEXT"
REFUSAL = {"status": "error", "error": "ACTOR_CONTEXT_INVALID",
           "message": ("The actor context passed to this command is "
                       "malformed, so nothing was run.")}
ACTOR_COLS = ["actor_os_account", "actor_channel", "actor_principal_claim",
              "actor_status", "actor_hop"]


@pytest.fixture(autouse=True)
def _fresh_actor():
    from erpclaw_lib import actor
    actor._reset_cache()
    yield
    actor._reset_cache()
    seam.dispose_engines()


def _padded(total):
    blank = json.dumps({"v": 1, "channel": "cli", "principal": "owner-1",
                        "hop": [], "pad": ""},
                       separators=(",", ":"), sort_keys=True)
    need = total - len(blank)
    assert need >= 0
    return blank.replace('"pad":""', '"pad":"' + ("x" * need) + '"')


def _load_tool_router():
    pkg_name = "actor_test_mcp_pkg"
    if pkg_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            pkg_name, os.path.join(_MCP_DIR, "__init__.py"),
            submodule_search_locations=[_MCP_DIR])
        pkg = importlib.util.module_from_spec(spec)
        sys.modules[pkg_name] = pkg
        spec.loader.exec_module(pkg)
    import importlib as _il
    return _il.import_module(pkg_name + ".tool_router")


def _make_home(tmp_path):
    home = tmp_path / "actor_home"
    home.mkdir()
    os.symlink(os.path.join(_SETUP_DIR, "lib"), str(home / "lib"))
    db = str(home / "data.sqlite")
    init_all_tables(db)
    return str(home), db


def _child_env(home, db, extra=None):
    env = dict(os.environ)
    env["ERPCLAW_HOME"] = home
    env["HOME"] = home
    env["ERPCLAW_DB_PATH"] = db
    env["ERPCLAW_DB_DIALECT"] = "sqlite"
    env.pop(VAR, None)
    if extra:
        env.update(extra)
    return env


def _run_router(env, name):
    return subprocess.run(
        [sys.executable, _ROUTER, "--action", "setup-company",
         "--name", name],
        capture_output=True, text=True, env=env, timeout=180)


def _run_direct(env, name):
    return subprocess.run(
        [sys.executable, _SETUP_ROUTER, "--action", "setup-company",
         "--name", name],
        capture_output=True, text=True, env=env, timeout=180)


def _audit_row(db, company_id):
    conn = open_reader(db)
    try:
        rows = read_all(conn, "audit_log",
                        ["entity_type", "entity_id"] + ACTOR_COLS)
    finally:
        conn.close()
    for row in rows:
        if row["entity_type"] == "company" and row["entity_id"] == company_id:
            return (row["actor_os_account"], row["actor_channel"],
                    row["actor_principal_claim"], row["actor_status"],
                    row["actor_hop"])
    raise AssertionError("no audit row for company %r" % (company_id,))


def _table_count(db, table):
    conn = open_reader(db)
    try:
        return len(read_all(conn, table, ["id"]))
    finally:
        conn.close()


# ── the reader ──────────────────────────────────────────────────────────────

def test_absent_when_no_variable():
    from erpclaw_lib import actor
    assert actor.resolve({}) == actor.ActorContext(ACCOUNT, None, None, (),
                                                  "absent")


def test_valid_null_principal_is_absent_with_channel():
    from erpclaw_lib import actor
    tool_router = _load_tool_router()
    ctx = actor.resolve({VAR: tool_router._MCP_ACTOR_CONTEXT})
    assert (ctx.channel, ctx.principal_claim, ctx.hop, ctx.status) == (
        "mcp", None, (), "absent")
    assert tool_router._MCP_ACTOR_CONTEXT == actor.encode("mcp", None, ())


def test_claimed_and_a_status_key_is_ignored():
    from erpclaw_lib import actor
    ctx = actor.resolve({VAR: '{"v":1,"channel":"mcp","principal":"owner-1",'
                              '"hop":[],"status":"attested"}'})
    assert (ctx.channel, ctx.principal_claim, ctx.hop, ctx.status) == (
        "mcp", "owner-1", (), "claimed")


@pytest.mark.parametrize("raw", [
    "not json",
    "[]",
    '{"v":2,"channel":"cli","principal":"owner-1","hop":[]}',
    '{"v":true,"channel":"cli","principal":"owner-1","hop":[]}',
    '{"v":1,"channel":"web","principal":"owner-1","hop":[]}',
    '{"v":1,"channel":"cli","principal":"owner 1","hop":[]}',
    '{"v":1,"channel":"cli","principal":"owner-1",'
    '"hop":["a","b","c","d","e"]}',
    '{"v":1,"channel":"cli","principal":"owner-1"}',
    _padded(1025),
])
def test_invalid_values(raw):
    from erpclaw_lib import actor
    assert actor.resolve({VAR: raw}) == actor.ActorContext(
        ACCOUNT, None, None, (), "invalid")


def test_1024_bytes_is_accepted():
    from erpclaw_lib import actor
    raw = _padded(1024)
    assert len(raw.encode("utf-8")) == 1024
    ctx = actor.resolve({VAR: raw})
    assert (ctx.principal_claim, ctx.status) == ("owner-1", "claimed")


def test_os_account_fallbacks(monkeypatch):
    from erpclaw_lib import actor

    def _missing(uid):
        raise KeyError(uid)

    monkeypatch.setattr(actor.pwd, "getpwuid", _missing)
    assert actor.os_account() == "uid:%d" % os.geteuid()
    assert actor.resolve({}).status == "absent"
    monkeypatch.delattr(os, "geteuid")
    assert actor.os_account() is None
    assert actor.resolve({}).status == "absent"


def test_current_is_memoised(monkeypatch):
    from erpclaw_lib import actor
    monkeypatch.setenv(VAR, '{"v":1,"channel":"cli","principal":"owner-1",'
                            '"hop":[]}')
    first = actor.current()
    assert first.status == "claimed"
    monkeypatch.setenv(VAR, '{"v":1,"channel":"mcp","principal":null,'
                            '"hop":[]}')
    assert actor.current() is first
    actor._reset_cache()
    assert actor.current().status == "absent"


def test_principal_syntax_matches_the_consumption_primitive():
    from erpclaw_lib import actor
    from erpclaw_lib import authorization_consumption
    assert actor._PRINCIPAL_RE.pattern == \
        authorization_consumption._ID_RE.pattern


# ── A: a missing identity is recorded, not refused ──────────────────────────

def test_a_router_without_context_records_absent(tmp_path):
    home, db = _make_home(tmp_path)
    proc = _run_router(_child_env(home, db), "Absent Co")
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["status"] == "ok"
    assert _audit_row(db, payload["company_id"]) == (
        ACCOUNT, None, None, "absent", None)


def test_a_passwd_less_uid_records_uid_and_succeeds(db_path, monkeypatch):
    from erpclaw_lib import actor
    from erpclaw_lib.db import get_connection

    def _missing(uid):
        raise KeyError(uid)

    monkeypatch.setattr(actor.pwd, "getpwuid", _missing)
    db_query = setup_helpers.load_db_query()
    result = setup_helpers.call_action(
        db_query.setup_company, get_connection(db_path),
        setup_helpers.ns(name="Uid Co", abbr=None, currency=None,
                         country=None, fiscal_year_start_month=None))
    assert result["status"] == "ok"
    conn = open_reader(db_path)
    try:
        rows = read_all(conn, "audit_log",
                        ["entity_type", "entity_id"] + ACTOR_COLS)
    finally:
        conn.close()
    row = [r for r in rows
           if r["entity_type"] == "company"
           and r["entity_id"] == result["company_id"]][0]
    assert (row["actor_os_account"], row["actor_channel"],
            row["actor_principal_claim"], row["actor_status"],
            row["actor_hop"]) == (
        "uid:%d" % os.geteuid(), None, None, "absent", None)


def test_a_raw_connection_records_the_actor(conn):
    # Raw handles carry no wrapper note, so every write probes again; the
    # probe runs on the handle itself and succeeds, recording the marker.
    from erpclaw_lib.audit import audit
    audit(conn, "erpclaw-setup", "create", "company", "raw-c1",
          new_values={"name": "Raw Co"})
    conn.commit()
    rows = read_all(conn, "audit_log", ["entity_id"] + ACTOR_COLS)
    row = [r for r in rows if r["entity_id"] == "raw-c1"][0]
    assert (row["actor_os_account"], row["actor_channel"],
            row["actor_principal_claim"], row["actor_status"],
            row["actor_hop"]) == (ACCOUNT, None, None, "absent", None)


# ── B: the MCP channel is the server's own ──────────────────────────────────

def test_b_mcp_overwrites_a_forged_context(tmp_path, monkeypatch):
    from erpclaw_lib import actor  # noqa: F401  (pins the reader next to use)
    tool_router = _load_tool_router()
    home, db = _make_home(tmp_path)
    monkeypatch.setenv("ERPCLAW_HOME", home)
    monkeypatch.setenv("HOME", home)
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    monkeypatch.setenv(VAR, '{"v":1,"channel":"cli",'
                            '"principal":"forged-owner","hop":[]}')
    assert tool_router._resolve_env()[VAR] == tool_router._MCP_ACTOR_CONTEXT
    result = tool_router.dispatch("setup-company", {"name": "MCP Actor Co"})
    assert result["status"] == "ok"
    assert _audit_row(db, result["company_id"]) == (
        ACCOUNT, "mcp", None, "absent", "[]")


def test_b_readonly_session_child_env_carries_readonly_and_mcp_actor(
        monkeypatch):
    tool_router = _load_tool_router()
    monkeypatch.setenv(VAR, '{"v":1,"channel":"cli",'
                            '"principal":"forged-owner","hop":[]}')
    monkeypatch.setenv("ERPCLAW_MCP_READONLY", "1")
    env = tool_router._resolve_env()
    assert env["ERPCLAW_DB_READONLY"] == "1"
    assert env[VAR] == tool_router._MCP_ACTOR_CONTEXT
    monkeypatch.setenv("ERPCLAW_MCP_READONLY", "sometimes")
    assert tool_router._resolve_env()[VAR] == tool_router._MCP_ACTOR_CONTEXT


def test_b_identity_arguments_are_still_refused(tmp_path, monkeypatch):
    tool_router = _load_tool_router()
    home, db = _make_home(tmp_path)
    monkeypatch.setenv("ERPCLAW_HOME", home)
    monkeypatch.setenv("HOME", home)
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    before = _table_count(db, "company")
    assert tool_router.dispatch(
        "setup-company", {"name": "X", "actor": "owner"}) == {
        "status": "error", "error": "reserved_arg",
        "action": "setup-company",
        "detail": "reserved routing control refused: 'actor'."}
    assert tool_router.dispatch(
        "setup-company", {"name": "X", "session_token": "owner"}) == {
        "status": "error", "error": "reserved_arg",
        "action": "setup-company",
        "detail": "reserved routing control refused: 'session-token'."}
    assert _table_count(db, "company") == before


# ── C: cross-skill children carry the claim ─────────────────────────────────

def test_c_child_env_appends_the_hop(monkeypatch):
    from erpclaw_lib import cross_skill
    monkeypatch.setenv(VAR, '{"v":1,"channel":"cli","principal":"owner-1",'
                            '"hop":[]}')
    monkeypatch.setattr(cross_skill, "resolve_skill_script",
                        lambda skill: "/tmp/fake/db_query.py")
    captured = {}

    class _Done:
        returncode = 0
        stdout = '{"status":"ok"}'

    def _fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return _Done()

    monkeypatch.setattr(cross_skill.subprocess, "run", _fake_run)
    cross_skill.call_skill_action("erpclaw", "list-customers",
                                  {"--company-id": "c1"})
    child = captured["env"]
    assert child is not os.environ
    assert child[VAR] == ('{"channel":"cross-skill",'
                          '"hop":["erpclaw:list-customers"],'
                          '"principal":"owner-1","v":1}')
    for key, value in os.environ.items():
        if key != VAR:
            assert child[key] == value
    assert set(child) == set(os.environ)


def test_c_child_records_the_claim(tmp_path):
    home, db = _make_home(tmp_path)
    value = ('{"channel":"cross-skill","hop":["erpclaw:list-customers"],'
             '"principal":"owner-1","v":1}')
    assert len(value.encode("utf-8")) <= 1024
    proc = _run_router(_child_env(home, db, {VAR: value}),
                       "Child Claim Co")
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["status"] == "ok"
    assert _audit_row(db, payload["company_id"]) == (
        ACCOUNT, "cross-skill", "owner-1", "claimed",
        '["erpclaw:list-customers"]')


def test_c_router_with_cli_claim_records_claimed(tmp_path):
    home, db = _make_home(tmp_path)
    proc = _run_router(_child_env(
        home, db, {VAR: '{"v":1,"channel":"cli","principal":"owner-1",'
                        '"hop":[]}'}), "Cli Claim Co")
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["status"] == "ok"
    assert _audit_row(db, payload["company_id"]) == (
        ACCOUNT, "cli", "owner-1", "claimed", "[]")


def test_c_attested_in_the_value_records_claimed(tmp_path):
    home, db = _make_home(tmp_path)
    proc = _run_router(_child_env(
        home, db, {VAR: '{"v":1,"channel":"cli","principal":"owner-1",'
                        '"hop":[],"status":"attested"}'}), "Attested Co")
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert _audit_row(db, payload["company_id"])[3] == "claimed"


def test_c_five_deep_chain_keeps_four_hops(tmp_path):
    from erpclaw_lib import actor
    parent = {VAR: '{"v":1,"channel":"cross-skill","principal":"owner-1",'
                   '"hop":["a:1","b:2","c:3","d:4"]}'}
    child = actor.child_env(parent, "e:5")[VAR]
    assert child == ('{"channel":"cross-skill",'
                     '"hop":["b:2","c:3","d:4","e:5"],'
                     '"principal":"owner-1","v":1}')
    assert actor.resolve({VAR: child}).status == "claimed"
    home, db = _make_home(tmp_path)
    proc = _run_router(_child_env(home, db, {VAR: child}), "Deep Chain Co")
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert _audit_row(db, payload["company_id"])[4] == \
        '["b:2","c:3","d:4","e:5"]'


def test_c_long_and_odd_hop_text_stays_valid():
    from erpclaw_lib import actor
    parent_value = actor.encode("cross-skill", "p" * 128, ["h" * 150] * 4)
    assert len(parent_value.encode("utf-8")) <= 1024
    assert actor.resolve({VAR: parent_value}).status == "claimed"
    odd_hop = "\u00e9\"\\" * 100
    assert len(odd_hop) == 300
    child_value = actor.child_env({VAR: parent_value}, odd_hop)[VAR]
    assert len(child_value.encode("utf-8")) <= 1024
    hops = actor.resolve({VAR: child_value}).hop
    assert actor.resolve({VAR: child_value}).status == "claimed"
    assert all(len(h) == 64 for h in hops)
    assert hops[-1] == "?" * 64


def test_c_absent_parent_passes_nothing():
    from erpclaw_lib import actor
    tool_router = _load_tool_router()
    for base in ({}, {VAR: tool_router._MCP_ACTOR_CONTEXT}):
        child = actor.child_env(base, "erpclaw:list-customers")
        assert VAR not in child
        resolved = actor.resolve(child)
        assert (resolved.channel, resolved.status) == (None, "absent")


def test_c_mcp_claim_keeps_its_channel_and_invalid_passes_unchanged():
    from erpclaw_lib import actor
    mcp_parent = {VAR: '{"v":1,"channel":"mcp","principal":"owner-1","hop":[]}'}
    assert actor.resolve(mcp_parent).status == "claimed"
    child = actor.child_env(mcp_parent, "erpclaw:list-customers")[VAR]
    assert actor.resolve({VAR: child}).channel == "mcp"
    assert '"mcp"' in child
    assert actor.child_env({VAR: "not json"}, "e:5")[VAR] == "not json"


# ── D: malformed input is refused at the router ─────────────────────────────

@pytest.mark.parametrize("raw", ["not json", _padded(1025)])
def test_d_router_refuses_malformed_context(tmp_path, raw):
    home, db = _make_home(tmp_path)
    before_company = _table_count(db, "company")
    before_audit = _table_count(db, "audit_log")
    proc = _run_router(_child_env(home, db, {VAR: raw}), "Refused Co")
    assert proc.returncode == 1
    assert json.loads(proc.stdout) == REFUSAL
    assert _table_count(db, "company") == before_company
    assert _table_count(db, "audit_log") == before_audit


def test_d_direct_script_records_invalid(tmp_path):
    home, db = _make_home(tmp_path)
    proc = _run_direct(_child_env(home, db, {VAR: "not json"}),
                       "Direct Invalid Co")
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["status"] == "ok"
    assert _audit_row(db, payload["company_id"]) == (
        ACCOUNT, None, None, "invalid", None)
