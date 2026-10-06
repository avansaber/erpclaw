"""M481 depth: behavioural evidence for 8 module/foundation actions.

Every action below already had a test that proved the wrong thing (contract
routability: "Unknown action" not in response; or response-shape keys). The
tests here observe the DATABASE through the seam instead: which stored row
exists afterwards with which exact values, which row changed from what to
what, and what did not change.

Signal per action (acceptance 3):
  available-modules    stored-row read   (cross-references erpclaw_module; DB unchanged)
  list-all-actions     stored-row read   (renders erpclaw_module_action cache; DB unchanged)
  rebuild-action-cache stored-row write  (rewrites erpclaw_module_action + action_count)
  remove-module        stored-row write  (deletes erpclaw_module + cache rows + dir)
  rollback-foundation  stored-row none   (files only, never schema; DB unchanged)
  search-modules       stored-row read   (filters registry; DB unchanged)
  update-foundation    stored-row write  (heals erpclaw_module.version row)
  verify-trust-root    stored-row none   (reports embedded keys; DB unchanged)

Ledger note (applies to every test in this file): none of these 8 actions
reaches the ledger, so no balanced-legs assertion can hold. The actions above
marked "stored-row write" assert exact stored rows instead; the read-only and
file-only ones assert byte-identical database state. A later reader must NOT
add gl_* assertions here.

Money note: erpclaw_module / erpclaw_module_action carry no monetary column
(version is TEXT, action_count is a plain INTEGER count, never money), so no
Decimal assertion can hold either. Every value assertion below compares exact
strings (or exact ints for the non-money count); there is no float, no round,
no approximation anywhere in this file.

Each action also gets the refusal case it owns (item 5): the refusal happens
(non-zero exit with a truthful message) and the database is byte-identical
afterwards. Actions with no err() path say so in a comment and instead prove
the closest no-op guard leaves the database identical.

Rule 6 note: if any action below turns out broken, the test documents the real
behaviour and the finding goes to CHANGES.md. No production file is touched.
"""
import argparse
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from types import SimpleNamespace

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(_TESTS_DIR)
_LIB_DIR = os.path.join(_SCRIPTS_DIR, "erpclaw-setup", "lib")
if importlib.util.find_spec("erpclaw_lib") is None:
    sys.path.insert(0, _LIB_DIR)

from erpclaw_lib.db import get_connection
from erpclaw_lib.query import Q, Table, Field, Star, insert_row, P
import erpclaw_lib.seam as seam

_MM_PATH = os.path.join(_SCRIPTS_DIR, "module_manager.py")
_mm_spec = importlib.util.spec_from_file_location("erpclaw_module_manager_m481", _MM_PATH)
mm = importlib.util.module_from_spec(_mm_spec)
_mm_spec.loader.exec_module(mm)

_INIT_SCHEMA_PATH = os.path.join(_SCRIPTS_DIR, "erpclaw-setup", "init_schema.py")
_init_spec = importlib.util.spec_from_file_location("erpclaw_init_schema_m481", _INIT_SCHEMA_PATH)
init_schema_mod = importlib.util.module_from_spec(_init_spec)
_init_spec.loader.exec_module(init_schema_mod)


# ---------------------------------------------------------------------------
# Small harness: call an action function, capture its ok()/err() JSON payload
# ---------------------------------------------------------------------------

def _call(fn, **kwargs):
    buf = io.StringIO()
    code = None
    with redirect_stdout(buf):
        try:
            fn(argparse.Namespace(**kwargs))
        except SystemExit as e:
            code = e.code
    text = buf.getvalue()
    try:
        payload = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        payload = {"_unparseable_raw": text}
    return code, payload


def _read_all(db_path, table, order_cols):
    conn = get_connection(db_path)
    try:
        query = Q.from_(Table(table)).select(Star())
        for col in order_cols:
            query = query.orderby(Field(col))
        rows = conn.execute(query.get_sql()).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _dump(db_path):
    return json.dumps({
        "erpclaw_module": _read_all(db_path, "erpclaw_module", ["name"]),
        "erpclaw_module_action": _read_all(db_path, "erpclaw_module_action",
                                           ["module_name", "action_name"]),
        "tables": seam.table_names(db_path),
    }, sort_keys=True, default=str)


def _insert_module_row(db_path, name, version="1.0.0", display=None,
                       category="expansion", status="installed", active=1,
                       install_path="", requires="[]", action_count=0):
    sql, _cols = insert_row("erpclaw_module", {
        "id": P(), "name": P(), "display_name": P(), "version": P(),
        "category": P(), "github_repo": P(), "install_path": P(),
        "install_status": P(), "is_active": P(), "requires_json": P(),
        "action_count": P(),
    })
    conn = get_connection(db_path)
    try:
        conn.execute(sql, ["id-%s" % name, name, display or name, version,
                           category, "", install_path, status, active,
                           requires, action_count])
        conn.commit()
    finally:
        conn.close()


def _insert_action_row(db_path, module_name, action_name):
    sql, _cols = insert_row("erpclaw_module_action", {
        "module_name": P(), "action_name": P()})
    conn = get_connection(db_path)
    try:
        conn.execute(sql, [module_name, action_name])
        conn.commit()
    finally:
        conn.close()


def _module_row(db_path, name):
    conn = get_connection(db_path)
    try:
        query = (Q.from_(Table("erpclaw_module")).select(Star())
                 .where(Field("name") == name))
        row = conn.execute(query.get_sql()).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _create_module_tables(db_path):
    # Owner-DDL reuse: the erpclaw_module tables belong to the module manager;
    # the tables are created from init_schema's own DDL constant so the test
    # schema cannot drift from the shipped one.
    conn = get_connection(db_path)
    try:
        conn.executescript(init_schema_mod.MODULE_TABLES)
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def iso(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("ERPCLAW_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    monkeypatch.delenv("ERPCLAW_DB_DIALECT_POSTGRES", raising=False)
    db_path = str(tmp_path / "t.sqlite")
    monkeypatch.setenv("ERPCLAW_DB_PATH", db_path)
    _create_module_tables(db_path)
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(mm, "LOCAL_CACHE_PATH", str(state / "registry_cache.json"))
    monkeypatch.setattr(mm, "LOCAL_SIG_CACHE_PATH", str(state / "registry_cache.json.sig"))
    monkeypatch.setattr(mm, "LOCAL_VERSION_TRACKER", str(state / ".last_registry_version"))
    monkeypatch.setattr(mm, "SYNC_LOCK_PATH", str(state / ".sync.lock"))
    monkeypatch.setattr(mm, "SYNC_LOG_PATH", str(state / "logs" / "sync.log"))
    with open(mm.REGISTRY_PATH, "rb") as f:
        raw = f.read()
    with open(mm.REGISTRY_PATH + ".sig", "rb") as f:
        sig = f.read()
    with open(str(state / "registry_cache.json"), "wb") as f:
        f.write(raw)
    with open(str(state / "registry_cache.json.sig"), "wb") as f:
        f.write(sig)
    yield SimpleNamespace(home=home, db=db_path, state=state,
                          registry=json.loads(raw))
    seam.dispose_engines()


# ---------------------------------------------------------------------------
# available-modules: read-only cross-reference of registry x erpclaw_module
# ---------------------------------------------------------------------------

def test_available_modules_marks_installed_with_exact_version(iso):
    before = _dump(iso.db)
    _insert_module_row(iso.db, "agricultureclaw", version="1.2.3",
                       display="AgricultureClaw", category="vertical")
    code, payload = _call(mm.available_modules, refresh=False,
                          category=None, search=None)
    assert code == 0
    assert payload["status"] == "ok"
    by_name = {m["name"]: m for m in payload["modules"]}
    agri = by_name["agricultureclaw"]
    assert agri["installed"] is True
    assert agri["installed_version"] == "1.2.3"
    assert agri["display_name"] == iso.registry["modules"]["agricultureclaw"]["display_name"]
    assert agri["version"] == iso.registry["modules"]["agricultureclaw"]["version"]
    other = by_name["automotiveclaw"]
    assert other["installed"] is False
    assert "installed_version" not in other
    row = _module_row(iso.db, "agricultureclaw")
    assert row["version"] == "1.2.3"
    assert row["install_status"] == "installed"
    after = _dump(iso.db)
    seeded = json.loads(before)
    seeded["erpclaw_module"].append(row)
    assert json.loads(after)["tables"] == json.loads(before)["tables"]
    assert after == json.dumps({
        "erpclaw_module": sorted(seeded["erpclaw_module"], key=lambda r: r["name"]),
        "erpclaw_module_action": [],
        "tables": json.loads(before)["tables"],
    }, sort_keys=True, default=str)


def test_available_modules_unknown_category_is_noop_on_db(iso):
    # No err() refusal path exists in this action (all inputs optional); the
    # no-op guard is an unknown category returning zero rows, writing nothing.
    _insert_module_row(iso.db, "agricultureclaw", version="1.2.3")
    before = _dump(iso.db)
    code, payload = _call(mm.available_modules, refresh=False,
                          category="no-such-category", search=None)
    assert code == 0
    assert payload["modules"] == []
    assert payload["total"] == 0
    assert _dump(iso.db) == before


# ---------------------------------------------------------------------------
# search-modules: read-only registry filter; refuses without --search
# ---------------------------------------------------------------------------

def test_search_modules_returns_exact_registry_values(iso):
    _insert_module_row(iso.db, "agricultureclaw", version="1.2.3")
    before = _dump(iso.db)
    code, payload = _call(mm.search_modules, search="agricultureclaw", refresh=False)
    assert code == 0
    names = [r["name"] for r in payload["results"]]
    assert "agricultureclaw" in names
    assert "automotiveclaw" not in names
    entry = next(r for r in payload["results"] if r["name"] == "agricultureclaw")
    expected = iso.registry["modules"]["agricultureclaw"]
    assert entry["display_name"] == expected["display_name"]
    assert entry["description"] == expected["description"]
    assert entry["category"] == expected["category"]
    assert entry["version"] == expected["version"]
    assert _dump(iso.db) == before


def test_search_modules_refuses_without_query_and_writes_nothing(iso):
    _insert_module_row(iso.db, "agricultureclaw", version="1.2.3")
    before = _dump(iso.db)
    code, payload = _call(mm.search_modules, search=None, refresh=False)
    assert code == 1
    assert payload["status"] == "error"
    assert "--search is required" in payload["message"]
    assert _dump(iso.db) == before


# ---------------------------------------------------------------------------
# list-all-actions: read-only render of core set + erpclaw_module_action cache
# ---------------------------------------------------------------------------

def test_list_all_actions_reflects_cached_rows_exactly(iso):
    _insert_module_row(iso.db, "fakeclaw", version="2.0.0")
    _insert_action_row(iso.db, "fakeclaw", "fake-do-thing")
    _insert_action_row(iso.db, "fakeclaw", "fake-other-thing")
    _insert_module_row(iso.db, "sleepingclaw", version="0.1.0", status="failed")
    _insert_action_row(iso.db, "sleepingclaw", "sleeping-act")
    before = _dump(iso.db)
    code, payload = _call(mm.list_all_actions)
    assert code == 0
    assert payload["module_actions"]["fakeclaw"] == ["fake-do-thing", "fake-other-thing"]
    assert "sleepingclaw" not in payload["module_actions"]
    assert "list-all-actions" in payload["core_actions"]
    assert "available-modules" in payload["core_actions"]
    assert "remove-module" in payload["core_actions"]
    assert payload["total"] == payload["core_count"] + 2
    assert payload["module_count"] == 1
    assert _dump(iso.db) == before


def test_list_all_actions_empty_cache_is_noop_on_db(iso):
    # No err() refusal path exists in this action (no inputs at all); the
    # no-op guard is an empty cache rendering zero module actions.
    before = _dump(iso.db)
    code, payload = _call(mm.list_all_actions)
    assert code == 0
    assert payload["module_actions"] == {}
    assert payload["module_count"] == 0
    assert _dump(iso.db) == before


# ---------------------------------------------------------------------------
# rebuild-action-cache: rewrites erpclaw_module_action + action_count
# ---------------------------------------------------------------------------

def _write_fake_module_db_query(moddir):
    scripts = os.path.join(moddir, "scripts")
    os.makedirs(scripts, exist_ok=True)
    with open(os.path.join(scripts, "db_query.py"), "w") as f:
        f.write('ACTIONS = {\n    "fake-alpha": handle_alpha,\n'
                '    "fake-beta": handle_beta,\n}\n')


def test_rebuild_action_cache_rewrites_rows_and_bumps_count(iso, tmp_path):
    moddir = str(tmp_path / "fakeclaw")
    _write_fake_module_db_query(moddir)
    _insert_module_row(iso.db, "fakeclaw", version="1.0.0", install_path=moddir,
                       action_count=7)
    _insert_action_row(iso.db, "fakeclaw", "fake-stale-action")
    keepdir = str(tmp_path / "keeperclaw")
    os.makedirs(os.path.join(keepdir, "scripts"), exist_ok=True)
    with open(os.path.join(keepdir, "scripts", "db_query.py"), "w") as f:
        f.write('ACTIONS = {\n    "keep-act": handle_keep,\n}\n')
    _insert_module_row(iso.db, "keeperclaw", version="3.0.0",
                       install_path=keepdir, action_count=1)
    _insert_action_row(iso.db, "keeperclaw", "keep-act")
    keeper_before = _module_row(iso.db, "keeperclaw")
    code, payload = _call(mm.rebuild_action_cache)
    assert code == 0
    rebuilt = {r["module"]: r["action_count"] for r in payload["rebuilt"]}
    assert rebuilt["fakeclaw"] == 2
    assert rebuilt["keeperclaw"] == 1
    assert payload["errors"] == []
    conn = get_connection(iso.db)
    try:
        rows = conn.execute(
            Q.from_(Table("erpclaw_module_action")).select(Star())
            .where(Field("module_name") == "fakeclaw")
            .orderby(Field("action_name")).get_sql()).fetchall()
        assert [r["action_name"] for r in rows] == ["fake-alpha", "fake-beta"]
    finally:
        conn.close()
    row = _module_row(iso.db, "fakeclaw")
    assert row["action_count"] == 2
    assert row["version"] == "1.0.0"
    assert row["install_status"] == "installed"
    keeper_after = _module_row(iso.db, "keeperclaw")
    # rebuild-action-cache refreshes updated_at on every rebuilt row by design;
    # every other stored value must be identical.
    keeper_after.pop("updated_at")
    keeper_before.pop("updated_at")
    assert keeper_after == keeper_before
    keeper_cache = [r for r in _read_all(iso.db, "erpclaw_module_action",
                                        ["module_name", "action_name"])
                    if r["module_name"] == "keeperclaw"]
    assert [r["action_name"] for r in keeper_cache] == ["keep-act"]


def test_rebuild_action_cache_missing_dir_reports_truthfully(iso, tmp_path):
    # No err() refusal exists (no required inputs); the per-module error entry
    # is the refusal surface. Real behaviour documented: the global truncate
    # runs first, so a module whose directory is gone loses its cached rows and
    # is reported under errors with the exact cause.
    _insert_module_row(iso.db, "ghostclaw", version="1.0.0",
                       install_path=str(tmp_path / "gone"))
    _insert_action_row(iso.db, "ghostclaw", "ghost-old")
    code, payload = _call(mm.rebuild_action_cache)
    assert code == 0
    assert {"module": "ghostclaw", "error": "Install directory missing"} in payload["errors"]
    conn = get_connection(iso.db)
    try:
        rows = conn.execute(
            Q.from_(Table("erpclaw_module_action")).select(Star())
            .where(Field("module_name") == "ghostclaw").get_sql()).fetchall()
        assert list(rows) == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# remove-module: deletes erpclaw_module + cache rows + install dir
# ---------------------------------------------------------------------------

def test_remove_module_deletes_rows_and_directory(iso, tmp_path):
    moddir = tmp_path / "removableclaw"
    (moddir / "scripts").mkdir(parents=True)
    (moddir / "scripts" / "db_query.py").write_text("# fake\n")
    _insert_module_row(iso.db, "removableclaw", version="1.0.0",
                       install_path=str(moddir))
    _insert_action_row(iso.db, "removableclaw", "gone-act-one")
    _insert_action_row(iso.db, "removableclaw", "gone-act-two")
    _insert_module_row(iso.db, "keeperclaw", version="3.0.0",
                       install_path=str(tmp_path / "keeper"))
    _insert_action_row(iso.db, "keeperclaw", "keep-act")
    keeper_before = _module_row(iso.db, "keeperclaw")
    code, payload = _call(mm.remove_module, module_name="removableclaw")
    assert code == 0
    assert payload["removed"] is True
    assert payload["module"] == "removableclaw"
    assert _module_row(iso.db, "removableclaw") is None
    remaining = _read_all(iso.db, "erpclaw_module_action",
                          ["module_name", "action_name"])
    assert [r for r in remaining if r["module_name"] == "removableclaw"] == []
    assert [r for r in remaining if r["module_name"] == "keeperclaw"] == [
        {"module_name": "keeperclaw", "action_name": "keep-act"}]
    assert _module_row(iso.db, "keeperclaw") == keeper_before
    assert not os.path.exists(str(moddir))


def test_remove_module_refuses_unknown_module_without_writing(iso):
    _insert_module_row(iso.db, "keeperclaw", version="3.0.0")
    before = _dump(iso.db)
    code, payload = _call(mm.remove_module, module_name="no-such-module")
    assert code == 1
    assert payload["status"] == "error"
    assert "no-such-module" in payload["message"]
    assert "is not installed" in payload["message"]
    assert _dump(iso.db) == before


def test_remove_module_refuses_when_dependents_exist(iso):
    _insert_module_row(iso.db, "baseclaw", version="1.0.0")
    _insert_module_row(iso.db, "depclaw", version="1.0.0", requires='["baseclaw"]')
    before = _dump(iso.db)
    code, payload = _call(mm.remove_module, module_name="baseclaw")
    assert code == 1
    assert "depclaw" in payload["message"]
    assert _module_row(iso.db, "baseclaw") is not None
    assert _module_row(iso.db, "depclaw") is not None
    assert _dump(iso.db) == before


def test_remove_module_refuses_without_name_and_writes_nothing(iso):
    _insert_module_row(iso.db, "keeperclaw", version="3.0.0")
    before = _dump(iso.db)
    code, payload = _call(mm.remove_module, module_name=None)
    assert code == 1
    assert "--module-name is required" in payload["message"]
    assert _dump(iso.db) == before


# ---------------------------------------------------------------------------
# update-foundation / rollback-foundation sandbox helpers (self-contained)
# ---------------------------------------------------------------------------

import hashlib
import shutil
import subprocess

from erpclaw_lib.signing import TRUSTED_KEYS, fingerprint


def _sandbox_install(dest, *, with_migrations=False):
    # Minimal foundation install: only the files the reconciler touches.
    # (A full copytree of the source tree is used by the version-tier tests,
    # but this tree's setgid directory bits cannot be preserved by copystat
    # in every environment, so the sandbox here is assembled file by file.)
    if os.path.exists(dest):
        shutil.rmtree(dest)
    os.makedirs(os.path.join(dest, "scripts"))
    shutil.copy(os.path.join(_SCRIPTS_DIR, "module_manager.py"),
                os.path.join(dest, "scripts", "module_manager.py"))
    shutil.copy(os.path.join(os.path.dirname(_SCRIPTS_DIR), "SKILL.md"),
                os.path.join(dest, "SKILL.md"))
    if with_migrations:
        mig_src = os.path.join(_SCRIPTS_DIR, "erpclaw-setup", "migrations")
        mig_dst = os.path.join(dest, "scripts", "erpclaw-setup", "migrations")
        os.makedirs(mig_dst)
        for fn in sorted(os.listdir(mig_src)):
            # Only the migration modules themselves. A __pycache__ directory
            # sits beside them after any earlier run, and copying it as a file
            # raises IsADirectoryError.
            if not fn.endswith(".py"):
                continue
            shutil.copy(os.path.join(mig_src, fn), os.path.join(mig_dst, fn))
        setup_src = os.path.join(_SCRIPTS_DIR, "erpclaw-setup")
        setup_dst = os.path.join(dest, "scripts", "erpclaw-setup")
        shutil.copy(os.path.join(setup_src, "migration_runner.py"),
                    os.path.join(setup_dst, "migration_runner.py"))
    return dest


def _sandbox_home(base):
    home = os.path.join(base, "home")
    libdir = os.path.join(home, ".openclaw", "erpclaw", "lib")
    os.makedirs(libdir, exist_ok=True)
    link = os.path.join(libdir, "erpclaw_lib")
    if not os.path.exists(link):
        os.symlink(os.path.join(_SCRIPTS_DIR, "erpclaw-setup", "lib", "erpclaw_lib"), link)
    return home


def _manifest_of(install):
    manifest = {}
    for root, dirs, files in os.walk(install):
        dirs[:] = [d for d in dirs if d not in mm.SYNC_SKIP_DIRS]
        for fname in files:
            if fname.endswith(mm.SYNC_SKIP_SUFFIXES):
                continue
            if fname in mm.SYNC_SKIP_BASENAMES_FOUNDATION:
                continue
            rel = os.path.relpath(os.path.join(root, fname), install)
            if rel in mm.SYNC_SKIP_RELPATHS_FOUNDATION:
                continue
            with open(os.path.join(root, fname), "rb") as f:
                manifest[rel] = hashlib.sha256(f.read()).hexdigest()
    return manifest


def _write_registry_cache(home, manifest, version):
    payload = {"version": version, "modules": {"erpclaw": {
        "version": version, "github": "avansaber/erpclaw",
        "files_sha256": manifest}}}
    with open(os.path.join(home, ".openclaw", "erpclaw", "registry_cache.json"), "w") as f:
        json.dump(payload, f, indent=2)


def _write_bundled_registry(install, manifest, version):
    payload = {"version": version, "modules": {"erpclaw": {
        "version": version, "github": "avansaber/erpclaw",
        "files_sha256": manifest}}}
    with open(os.path.join(install, "scripts", "module_registry.json"), "w") as f:
        json.dump(payload, f, indent=2)


def _run_mm(install, action, *extra, home, db):
    env = {k: v for k, v in os.environ.items()
           if k not in ("ERPCLAW_HOME", "ERPCLAW_DB_PATH", "ERPCLAW_DB_URL",
                        "ERPCLAW_DB_DIALECT")}
    env["HOME"] = home
    env["ERPCLAW_DB_PATH"] = db
    env["ERPCLAW_DB_DIALECT"] = "sqlite"
    cmd = [sys.executable, os.path.join(install, "scripts", "module_manager.py"),
           "--action", action, *extra]
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=180)


# ---------------------------------------------------------------------------
# update-foundation: heals the erpclaw_module.version row on converged success
# ---------------------------------------------------------------------------

def test_update_foundation_in_sync_heals_version_row(tmp_path):
    install = _sandbox_install(str(tmp_path / "install"), with_migrations=True)
    home = _sandbox_home(str(tmp_path))
    db = os.path.join(home, ".openclaw", "erpclaw", "data.sqlite")
    _create_module_tables(db)
    _insert_module_row(db, "erpclaw", version="1.0.0",
                       display="ERPClaw Foundation", category="core")
    manifest = _manifest_of(install)  # skip list keeps module_registry.json out either way
    _write_bundled_registry(install, manifest, "9.9.9")
    with open(os.path.join(install, "SKILL.md"), "rb") as f:
        skill_before = f.read()
    res = _run_mm(install, "update-foundation", "--user-confirmed",
                  "--unsafe-trust-bundled", home=home, db=db)
    assert res.returncode == 0, res.stdout + res.stderr
    parsed = json.loads(res.stdout)
    assert parsed["in_sync"] is True
    assert "replaced" not in parsed
    assert parsed["version_bump"]["bumped"] is True
    assert parsed["migrations"]["ran"] is False
    assert parsed["migrations"]["reason"] == \
        "foundation DB not initialized (nothing to migrate)"
    row = _module_row(db, "erpclaw")
    assert row["version"] == "9.9.9"
    assert row["install_status"] == "installed"
    with open(os.path.join(install, "SKILL.md"), "rb") as f:
        assert f.read() == skill_before
    baks = []
    for _root, _dirs, _files in os.walk(install):
        baks.extend(f for f in _files if f.endswith(".bak"))
    assert baks == []
    seam.dispose_engines()


def test_update_foundation_unsafe_recovery_ignores_the_cache(tmp_path):
    install = _sandbox_install(str(tmp_path / "install"), with_migrations=True)
    home = _sandbox_home(str(tmp_path))
    db = os.path.join(home, ".openclaw", "erpclaw", "data.sqlite")
    _create_module_tables(db)
    _insert_module_row(db, "erpclaw", version="1.0.0",
                       display="ERPClaw Foundation", category="core")
    _write_registry_cache(home, _manifest_of(install), "9.9.9")
    res = _run_mm(install, "update-foundation", "--user-confirmed",
                  "--unsafe-trust-bundled", home=home, db=db)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "bundled registry is missing" in (res.stdout + res.stderr)
    row = _module_row(db, "erpclaw")
    assert row["version"] == "1.0.0"
    baks = []
    for _root, _dirs, _files in os.walk(install):
        baks.extend(f for f in _files if f.endswith(".bak"))
    assert baks == []
    seam.dispose_engines()


def test_update_foundation_refuses_dev_tree_and_writes_nothing(iso):
    assert mm._is_dev_source_tree(mm.FOUNDATION_INSTALL_ROOT) is True
    _insert_module_row(iso.db, "keeperclaw", version="3.0.0")
    before = _dump(iso.db)
    code, payload = _call(mm.update_foundation_action)
    assert code == 1
    assert payload["status"] == "error"
    assert "git-tracked source tree" in payload["message"]
    assert _dump(iso.db) == before
    assert sorted(os.listdir(str(iso.state))) == [
        "registry_cache.json", "registry_cache.json.sig"]


def test_update_foundation_refuses_a_busy_installed_tree(iso, monkeypatch):
    monkeypatch.setattr(mm, "FOUNDATION_INSTALL_ROOT", str(iso.home))
    _insert_module_row(iso.db, "keeperclaw", version="3.0.0")
    before = _dump(iso.db)
    lock = mm._acquire_sync_lock()
    assert lock is not None
    try:
        code, payload = _call(mm.update_foundation_action)
    finally:
        mm._release_sync_lock(lock)
    assert code == 1
    assert payload["status"] == "error"
    assert "in progress" in payload["message"]
    assert _dump(iso.db) == before


# ---------------------------------------------------------------------------
# rollback-foundation: restores .bak files; files only, never schema
# ---------------------------------------------------------------------------

def test_rollback_foundation_restores_bak_and_clears_it(tmp_path):
    install = _sandbox_install(str(tmp_path / "install"))
    home = _sandbox_home(str(tmp_path))
    db = os.path.join(home, ".openclaw", "erpclaw", "data.sqlite")
    _create_module_tables(db)
    _insert_module_row(db, "keeperclaw", version="3.0.0")
    before = _dump(db)
    target = os.path.join(install, "scripts", "probe_rollback.txt")
    with open(target, "wb") as f:
        f.write(b"NEW-CONTENT\n")
    with open(target + ".bak", "wb") as f:
        f.write(b"OLD-CONTENT\n")
    res = _run_mm(install, "rollback-foundation", "--user-confirmed",
                  home=home, db=db)
    assert res.returncode == 0, res.stdout + res.stderr
    parsed = json.loads(res.stdout)
    assert parsed["status"] == "ok"
    assert "scripts/probe_rollback.txt" in parsed["restored"]
    with open(target, "rb") as f:
        assert f.read() == b"OLD-CONTENT\n"
    assert not os.path.exists(target + ".bak")
    # Files only, never schema: the catalog is byte-identical afterwards.
    assert _dump(db) == before
    seam.dispose_engines()


def test_rollback_foundation_refuses_when_lock_held(iso):
    _insert_module_row(iso.db, "keeperclaw", version="3.0.0")
    before = _dump(iso.db)
    fh = mm._acquire_sync_lock()
    assert fh is not None
    try:
        code, payload = _call(mm.rollback_foundation_action)
    finally:
        mm._release_sync_lock(fh)
    assert code == 1
    assert "in progress" in payload["message"]
    assert _dump(iso.db) == before


def test_rollback_foundation_restores_nested_backups_and_is_idempotent(
        iso, tmp_path, monkeypatch):
    install = tmp_path / "install"
    nested = install / "scripts" / "nested"
    nested.mkdir(parents=True)
    monkeypatch.setattr(mm, "FOUNDATION_INSTALL_ROOT", str(install))
    _insert_module_row(iso.db, "keeperclaw", version="3.0.0")
    before = _dump(iso.db)
    targets = {install / "SKILL.md": b"previous skill\n",
               nested / "rule.txt": b"previous rule\n"}
    for target, previous in targets.items():
        target.write_bytes(b"current content\n")
        target.with_name(target.name + ".bak").write_bytes(previous)
    unrelated = nested / "unchanged.txt"
    unrelated.write_bytes(b"keep this file\n")
    code, payload = _call(mm.rollback_foundation_action)
    assert code is None
    assert payload["status"] == "ok"
    assert payload["restored"] == ["SKILL.md", "scripts/nested/rule.txt"]
    assert payload["skipped"] == []
    for target, previous in targets.items():
        assert target.read_bytes() == previous
        assert not target.with_name(target.name + ".bak").exists()
    assert unrelated.read_bytes() == b"keep this file\n"
    code, repeated = _call(mm.rollback_foundation_action)
    assert code is None
    assert repeated == {"status": "ok", "restored": [], "skipped": []}
    assert _dump(iso.db) == before


def test_rollback_foundation_leaves_excluded_cache_backups_untouched(
        iso, tmp_path, monkeypatch):
    install = tmp_path / "install"
    cache = install / "__pycache__"
    cache.mkdir(parents=True)
    monkeypatch.setattr(mm, "FOUNDATION_INSTALL_ROOT", str(install))
    target = cache / "cached.pyc"
    backup = cache / "cached.pyc.bak"
    target.write_bytes(b"current cache\n")
    backup.write_bytes(b"previous cache\n")
    before = _dump(iso.db)
    code, payload = _call(mm.rollback_foundation_action)
    assert code is None
    assert payload == {"status": "ok", "restored": [], "skipped": []}
    assert target.read_bytes() == b"current cache\n"
    assert backup.read_bytes() == b"previous cache\n"
    assert _dump(iso.db) == before


# ---------------------------------------------------------------------------
# verify-trust-root: reports the embedded keys; pure read, DB unchanged
# ---------------------------------------------------------------------------

def test_verify_trust_root_reports_embedded_fingerprints(iso):
    _insert_module_row(iso.db, "keeperclaw", version="3.0.0")
    before = _dump(iso.db)
    code, payload = _call(mm.verify_trust_root_action)
    # Real behaviour documented: unlike its ok()/err() siblings this action
    # prints and returns normally instead of raising SystemExit (main()
    # tolerates the fall-through; the CLI still exits 0). Assert what it does.
    assert code is None
    assert payload["status"] == "ok"
    expected = [{"label": k.label, "fingerprint": fingerprint(k.public_key_hex),
                 "valid_until": k.valid_until} for k in TRUSTED_KEYS]
    assert payload["trusted_keys"] == expected
    for entry in payload["trusted_keys"]:
        assert ":" in entry["fingerprint"]
    # No stored row and no ledger effect exist for this action by design (it
    # only prints the trust root for out-of-band verification); the signal is
    # exact fingerprint equality plus a byte-identical database.
    assert _dump(iso.db) == before


def test_verify_trust_root_has_no_refusal_path_and_writes_nothing(iso):
    # This action takes no input and owns no err() branch; document that the
    # call is a pure read with zero database effect.
    before = _dump(iso.db)
    code, payload = _call(mm.verify_trust_root_action)
    assert code is None
    assert _dump(iso.db) == before
