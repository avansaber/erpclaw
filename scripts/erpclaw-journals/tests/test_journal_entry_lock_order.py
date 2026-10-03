"""Journal-entry submit/cancel/amend take the company chain head first (m828).

Product rule: every ledger-posting action that changes a document's state takes
the company's ledger chain head before its first write, then decides on state
re-read under the head. The three journal-entry posting actions did not, so two
concurrent reversals could both pass the status check and post twice.

Book for every round: one company with accounts and a two-line journal entry,
DR expense 100.00 / CR cash 100.00. "Snapshot" below means the sorted rows of
gl_entry, journal_entry, journal_entry_line, cwip_cost_accumulation and asset.

1. stale status under the head refused before any write (SQLite + PG, per action)
2. head before the first write (SQLite, cancel + amend)
3. happy paths unchanged (SQLite)
4. head first: cancel waits on a held head (PG only)
5. cancel vs amend reversed exactly once, five rounds (PG only)
6. final compare-and-set on the status flip (PG only)
7. refusal after the reversal rolls back (SQLite)
8. submit posts the lines read under the head (SQLite)
9. amend vs process-recurring do not deadlock, five rounds (PG only)
"""
import importlib.util
import json
import os
import subprocess
import sys
import uuid
from decimal import Decimal

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from journals_helpers import (  # noqa: E402
    call_action,
    get_conn,
    is_error,
    is_ok,
    load_db_query,
    ns,
    seed_account,
    seed_cwip_asset,
)

mod = load_db_query()

from erpclaw_lib.db import get_connection, get_dialect  # noqa: E402
from erpclaw_lib.gl_posting import take_chain_heads as _real_take_heads  # noqa: E402
from erpclaw_lib.query import P, Q, Table  # noqa: E402

_JOURNALS_DIR = os.path.dirname(_TESTS_DIR)
_SCRIPTS_DIR = os.path.dirname(_JOURNALS_DIR)
_SETUP_LIB = os.path.join(_SCRIPTS_DIR, "erpclaw-setup", "lib")
_JOURNALS_SCRIPT = os.path.join(_JOURNALS_DIR, "db_query.py")
_PAYMENTS_TESTS = os.path.join(_SCRIPTS_DIR, "erpclaw-payments", "tests")

DATE = "2026-06-20"

def _ensure_payments_path():
    if _PAYMENTS_TESTS not in sys.path:
        sys.path.insert(0, _PAYMENTS_TESTS)


_proofs_mod = None


def _proofs():
    """Load the payments chain-lock proofs module; reference helpers only."""
    global _proofs_mod
    if _proofs_mod is None:
        _ensure_payments_path()
        spec = importlib.util.spec_from_file_location(
            "payments_chain_lock_proofs",
            os.path.join(_PAYMENTS_TESTS, "test_chain_lock_proofs.py"))
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _proofs_mod = loaded
    return _proofs_mod


_rec_mod = None


def _recording_proxy_cls():
    """Reuse the payments recording proxy; it is not copied here."""
    global _rec_mod
    if _rec_mod is None:
        _ensure_payments_path()
        spec = importlib.util.spec_from_file_location(
            "payments_recording_proxy",
            os.path.join(
                _PAYMENTS_TESTS,
                "test_payment_edit_and_allocation_compare_and_set.py"))
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _rec_mod = loaded
    return _rec_mod._RecordingProxy


# Collection-time skip for the SQLite legs: the journals conftest is SQLite-only,
# so in the PostgreSQL lane the sqlite fixtures cannot even be set up.
_SQLITE_ONLY = pytest.mark.skipif(
    os.environ.get("ERPCLAW_DB_DIALECT", "sqlite") != "sqlite",
    reason="SQLite-only leg",
)


def _sqlite_only():
    if get_dialect() != "sqlite":
        pytest.skip("SQLite-only leg")


@pytest.fixture
def pg_book():
    """Fresh PostgreSQL book mirroring the journals helpers (module-local).

    Same environment steps as erpclaw-payments/tests/conftest.py db_path.
    journals_helpers.build_journals_env is not PostgreSQL-portable
    (INSERT OR IGNORE into naming_series raises SyntaxError on PG), so the
    company/fiscal-year/naming/cost-center rows are mirrored here with
    portable SQL while the accounts come from the portable journals helper
    seed_account. The helper itself is left unedited; see CHANGES.md.
    """
    proofs = _proofs()
    proofs._pg_only()
    spec = importlib.util.spec_from_file_location(
        "payments_helpers_pg",
        os.path.join(_PAYMENTS_TESTS, "payments_helpers.py"))
    pg_helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pg_helpers)
    base_url = os.environ.get("ERPCLAW_PG_TEST_URL")
    if not base_url:
        pytest.skip("needs ERPCLAW_PG_TEST_URL (private PostgreSQL cluster)")
    old_url = os.environ.get("ERPCLAW_DB_URL")
    old_path = os.environ.get("ERPCLAW_DB_PATH")
    os.environ["ERPCLAW_DB_URL"] = base_url
    os.environ.pop("ERPCLAW_DB_PATH", None)
    conn = None
    try:
        pg_helpers.init_all_tables(None)
        conn = get_connection()
        env = _build_pg_book(conn)
        yield conn, env
    finally:
        try:
            if conn is not None:
                conn.close()
        finally:
            if old_url is None:
                os.environ.pop("ERPCLAW_DB_URL", None)
            else:
                os.environ["ERPCLAW_DB_URL"] = old_url
            if old_path is None:
                os.environ.pop("ERPCLAW_DB_PATH", None)
            else:
                os.environ["ERPCLAW_DB_PATH"] = old_path


def _build_pg_book(conn):
    """Portable mirror of build_journals_env for the PostgreSQL legs."""
    cid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO company (id, name, abbr, default_currency, country, "
        "fiscal_year_start_month) VALUES (?, ?, ?, 'USD', 'United States', 1)",
        (cid, "Test Co " + cid[:6], "TC" + cid[:4]))
    fyid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO fiscal_year (id, name, start_date, end_date, company_id) "
        "VALUES (?, ?, '2026-01-01', '2026-12-31', ?)",
        (fyid, "FY-" + fyid[:6], cid))
    conn.execute(
        "INSERT INTO naming_series (id, entity_type, prefix, current_value, "
        "company_id) VALUES (?, 'journal_entry', 'JE-', 0, ?)",
        (str(uuid.uuid4()), cid))
    ccid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO cost_center (id, name, company_id, is_group) "
        "VALUES (?, 'Main CC', ?, 0)",
        (ccid, cid))
    conn.commit()
    cash = seed_account(conn, cid, "Cash", "asset", "cash", "1000")
    cwip = seed_account(conn, cid, "CWIP", "asset",
                        "capital_work_in_progress", "1800")
    expense = seed_account(conn, cid, "Purchases", "expense", "expense", "5000")
    return {"company_id": cid, "fiscal_year_id": fyid, "cc": ccid,
            "cash": cash, "cwip": cwip, "expense": expense}


def _two_lines(env, debit="100.00", credit="100.00"):
    return json.dumps([
        {"account_id": env["expense"], "debit": debit, "credit": "0",
         "cost_center_id": env["cc"]},
        {"account_id": env["cash"], "debit": "0", "credit": credit,
         "cost_center_id": env["cc"]},
    ])


def _add(conn, env, debit="100.00", credit="100.00", cwip_asset_id=None):
    return call_action(mod.add_journal_entry, conn, ns(
        company_id=env["company_id"], posting_date=DATE, entry_type="journal",
        remark="lock-order", lines=_two_lines(env, debit, credit),
        cwip_asset_id=cwip_asset_id, dimensions=None, dimension_key=None,
        dimension_value=None))


def _submit(conn, je_id):
    return call_action(mod.submit_journal_entry, conn,
                       ns(journal_entry_id=je_id))


def _cancel(conn, je_id):
    return call_action(mod.cancel_journal_entry, conn,
                       ns(journal_entry_id=je_id))


def _amend(conn, je_id, lines=None, posting_date=None, remark=None):
    return call_action(mod.amend_journal_entry, conn, ns(
        journal_entry_id=je_id, lines=lines, posting_date=posting_date,
        remark=remark, dimensions=None, dimension_key=None,
        dimension_value=None))


def _msg(result):
    return result.get("message", "") or result.get("error", "")


_SNAP_TABLES = ("gl_entry", "journal_entry", "journal_entry_line",
                "cwip_cost_accumulation", "asset")


def _table_rows(conn, table):
    queries = {
        "gl_entry": "SELECT * FROM gl_entry ORDER BY id",
        "journal_entry": "SELECT * FROM journal_entry ORDER BY id",
        "journal_entry_line": "SELECT * FROM journal_entry_line ORDER BY id",
        "cwip_cost_accumulation":
            "SELECT * FROM cwip_cost_accumulation ORDER BY id",
        "asset": "SELECT * FROM asset ORDER BY id",
    }
    try:
        rows = conn.execute(queries[table]).fetchall()
    except Exception:  # noqa: BLE001 - table may not exist on a fresh book
        return []
    return sorted(
        [json.dumps(dict(r), sort_keys=True, default=str) for r in rows])


def _snapshot(conn):
    return {t: _table_rows(conn, t) for t in _SNAP_TABLES}


def _set_status_pypika(conn, je_id, status):
    t = Table("journal_entry")
    q = Q.update(t).set(t.status, P()).where(t.id == P())
    conn.execute(q.get_sql(), (status, je_id))


def _stale_status_case(conn, env, fresh_open, action, monkeypatch):
    """Shared body for test 1: a concurrent status change lands under head."""
    add = _add(conn, env)
    assert is_ok(add), add
    je_id = add["journal_entry_id"]
    if action in ("cancel", "amend"):
        sub = _submit(conn, je_id)
        assert is_ok(sub), sub
    if action == "submit":
        target_status = "submitted"
        expected = ("Cannot submit: journal entry is 'submitted' "
                    "(must be 'draft')")
        fn = mod.submit_journal_entry
        args = ns(journal_entry_id=je_id)
    elif action == "cancel":
        target_status = "cancelled"
        expected = ("Cannot cancel: journal entry is 'cancelled' "
                    "(must be 'submitted')")
        fn = mod.cancel_journal_entry
        args = ns(journal_entry_id=je_id)
    else:
        target_status = "cancelled"
        expected = ("Cannot amend: journal entry is 'cancelled' "
                    "(must be 'submitted')")
        fn = mod.amend_journal_entry
        args = ns(journal_entry_id=je_id, lines=None, posting_date=None,
                  remark=None, dimensions=None, dimension_key=None,
                  dimension_value=None)
    before = _snapshot(conn)
    je_count_before = len(before["journal_entry"])

    head_calls = {"n": 0}

    def _head_wrapper(c, company_ids):
        head_calls["n"] += 1
        _real_take_heads(c, company_ids)
        _set_status_pypika(c, je_id, target_status)
        c.commit()
        _real_take_heads(c, company_ids)

    monkeypatch.setattr(mod, "take_chain_heads", _head_wrapper, raising=False)

    counts = {"reverse": 0, "insert": 0}
    real_reverse = mod.reverse_gl_entries
    real_insert = mod.insert_gl_entries

    def _count_reverse(*a, **k):
        counts["reverse"] += 1
        return real_reverse(*a, **k)

    def _count_insert(*a, **k):
        counts["insert"] += 1
        return real_insert(*a, **k)

    monkeypatch.setattr(mod, "reverse_gl_entries", _count_reverse,
                        raising=False)
    monkeypatch.setattr(mod, "insert_gl_entries", _count_insert,
                        raising=False)

    result = call_action(fn, conn, args)
    assert is_error(result), result
    assert result.get("message") == expected, result
    assert head_calls["n"] == 1, head_calls

    fresh = fresh_open()
    try:
        after = _snapshot(fresh)
    finally:
        fresh.close()
    assert after["gl_entry"] == before["gl_entry"], (after, before)
    assert len(after["journal_entry"]) == je_count_before, after
    assert counts == {"reverse": 0, "insert": 0}, counts


# ── 1. stale status under the head is refused before any write ──

@_SQLITE_ONLY
def test_1_stale_submit_refused(conn, env, db_path, monkeypatch):
    _sqlite_only()
    _stale_status_case(conn, env, lambda: get_conn(db_path), "submit", monkeypatch)


@_SQLITE_ONLY
def test_1_stale_cancel_refused(conn, env, db_path, monkeypatch):
    _sqlite_only()
    _stale_status_case(conn, env, lambda: get_conn(db_path), "cancel", monkeypatch)


@_SQLITE_ONLY
def test_1_stale_amend_refused(conn, env, db_path, monkeypatch):
    _sqlite_only()
    _stale_status_case(conn, env, lambda: get_conn(db_path), "amend", monkeypatch)


def test_1_stale_submit_refused_pg(pg_book, monkeypatch):
    conn, env = pg_book
    _stale_status_case(conn, env, get_connection, "submit", monkeypatch)


def test_1_stale_cancel_refused_pg(pg_book, monkeypatch):
    conn, env = pg_book
    _stale_status_case(conn, env, get_connection, "cancel", monkeypatch)


def test_1_stale_amend_refused_pg(pg_book, monkeypatch):
    conn, env = pg_book
    _stale_status_case(conn, env, get_connection, "amend", monkeypatch)


# ── 2. head before the first write (SQLite) ──

def _writes(statements):
    return [s for s in statements
            if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]


def _assert_head_guards_writes(writes, allow_naming_first):
    assert writes, "expected the action to write"
    if allow_naming_first:
        assert "naming_series" in writes[0], writes[:3]
        assert writes[1].lstrip().upper().startswith("INSERT"), writes[:3]
        assert "gl_chain_head" in writes[1], writes[:3]
        head_idx = 1
    else:
        assert writes[0].lstrip().upper().startswith("INSERT"), writes[:3]
        assert "gl_chain_head" in writes[0], writes[:3]
        head_idx = 0
    for i, stmt in enumerate(writes):
        body = stmt
        if "gl_chain_head" in body or "naming_series" in body:
            continue
        kind = stmt.lstrip().split(None, 1)[0].upper()
        checked = False
        if kind == "INSERT":
            checked = "gl_entry" in body
        elif kind == "UPDATE":
            checked = ("gl_entry" in body or "journal_entry" in body
                       or "asset" in body)
        if checked:
            assert i > head_idx, (i, stmt)


@_SQLITE_ONLY
def test_2_head_before_first_write_cancel(conn, env):
    _sqlite_only()
    proxy_cls = _recording_proxy_cls()
    add = _add(conn, env)
    assert is_ok(add), add
    je_id = add["journal_entry_id"]
    assert is_ok(_submit(conn, je_id)), je_id
    proxy = proxy_cls(conn)
    result = call_action(mod.cancel_journal_entry, proxy,
                         ns(journal_entry_id=je_id))
    assert is_ok(result), result
    _assert_head_guards_writes(_writes(proxy.statements), False)


@_SQLITE_ONLY
def test_2_head_before_first_write_amend(conn, env):
    _sqlite_only()
    proxy_cls = _recording_proxy_cls()
    add = _add(conn, env)
    assert is_ok(add), add
    je_id = add["journal_entry_id"]
    assert is_ok(_submit(conn, je_id)), je_id
    proxy = proxy_cls(conn)
    result = call_action(mod.amend_journal_entry, proxy,
                         ns(journal_entry_id=je_id, lines=None,
                            posting_date=None, remark=None, dimensions=None,
                            dimension_key=None, dimension_value=None))
    assert is_ok(result), result
    _assert_head_guards_writes(_writes(proxy.statements), True)


# ── 3. happy paths unchanged (SQLite) ──

@_SQLITE_ONLY
def test_3_submit_posts_pair(conn, env):
    _sqlite_only()
    add = _add(conn, env)
    assert is_ok(add), add
    je_id = add["journal_entry_id"]
    result = _submit(conn, je_id)
    assert is_ok(result), result
    rows = conn.execute(
        "SELECT account_id, debit, credit FROM gl_entry "
        "WHERE voucher_type = ? AND voucher_id = ? ORDER BY debit DESC",
        ("journal_entry", je_id)).fetchall()
    assert len(rows) == 2, [dict(r) for r in rows]
    assert rows[0]["account_id"] == env["expense"]
    assert Decimal(rows[0]["debit"]) == Decimal("100.00")
    assert Decimal(rows[0]["credit"]) == Decimal("0")
    assert rows[1]["account_id"] == env["cash"]
    assert Decimal(rows[1]["credit"]) == Decimal("100.00")
    assert Decimal(rows[1]["debit"]) == Decimal("0")
    status = conn.execute(
        "SELECT status FROM journal_entry WHERE id = ?",
        (je_id,)).fetchone()[0]
    assert status == "submitted"


@_SQLITE_ONLY
def test_3_cancel_writes_mirrors(conn, env):
    _sqlite_only()
    add = _add(conn, env)
    assert is_ok(add), add
    je_id = add["journal_entry_id"]
    assert is_ok(_submit(conn, je_id)), je_id
    result = _cancel(conn, je_id)
    assert is_ok(result), result
    rows = conn.execute(
        "SELECT remarks, is_cancelled FROM gl_entry "
        "WHERE voucher_type = ? AND voucher_id = ?",
        ("journal_entry", je_id)).fetchall()
    assert len(rows) == 4, [dict(r) for r in rows]
    mirrors = [r for r in rows
               if (r["remarks"] or "").startswith("Reversal of ")]
    assert len(mirrors) == 2, [dict(r) for r in rows]
    status = conn.execute(
        "SELECT status FROM journal_entry WHERE id = ?",
        (je_id,)).fetchone()[0]
    assert status == "cancelled"


def _line_pairs(conn, je_id):
    rows = conn.execute(
        "SELECT account_id, debit, credit FROM journal_entry_line "
        "WHERE journal_entry_id = ? ORDER BY debit DESC",
        (je_id,)).fetchall()
    return [(r["account_id"], str(r["debit"]), str(r["credit"]))
            for r in rows]


@_SQLITE_ONLY
def test_3_amend_links_new_draft(conn, env):
    _sqlite_only()
    add = _add(conn, env)
    assert is_ok(add), add
    je_id = add["journal_entry_id"]
    assert is_ok(_submit(conn, je_id)), je_id
    before_lines = _line_pairs(conn, je_id)
    result = _amend(conn, je_id)
    assert is_ok(result), result
    new_id = result["new_journal_entry_id"]
    assert conn.execute(
        "SELECT status FROM journal_entry WHERE id = ?",
        (je_id,)).fetchone()[0] == "amended"
    new = conn.execute(
        "SELECT status, amended_from FROM journal_entry WHERE id = ?",
        (new_id,)).fetchone()
    assert new["status"] == "draft"
    assert new["amended_from"] == je_id
    assert _line_pairs(conn, new_id) == before_lines


@_SQLITE_ONLY
def test_3_cwip_cancel_restores_asset(conn, env):
    _sqlite_only()
    asset_id = seed_cwip_asset(conn, env["company_id"])
    cwip_lines = json.dumps([
        {"account_id": env["cwip"], "debit": "100.00", "credit": "0",
         "cost_center_id": env["cc"]},
        {"account_id": env["cash"], "debit": "0", "credit": "100.00",
         "cost_center_id": env["cc"]},
    ])
    add = call_action(mod.add_journal_entry, conn, ns(
        company_id=env["company_id"], posting_date=DATE, entry_type="journal",
        remark="cwip", lines=cwip_lines, cwip_asset_id=asset_id,
        dimensions=None, dimension_key=None, dimension_value=None))
    assert is_ok(add), add
    je_id = add["journal_entry_id"]
    asset_before = conn.execute(
        "SELECT gross_value, current_book_value FROM asset WHERE id = ?",
        (asset_id,)).fetchone()
    assert is_ok(_submit(conn, je_id)), je_id
    assert is_ok(_cancel(conn, je_id)), je_id
    asset_after = conn.execute(
        "SELECT gross_value, current_book_value FROM asset WHERE id = ?",
        (asset_id,)).fetchone()
    assert (Decimal(asset_after["gross_value"]),
            Decimal(asset_after["current_book_value"])) == (
               Decimal(asset_before["gross_value"]),
               Decimal(asset_before["current_book_value"]))


# ── 4. head first (PostgreSQL only) ──

def test_4_cancel_waits_on_held_head(pg_book):
    proofs = _proofs()
    proofs._pg_only()
    conn, env = pg_book
    add = _add(conn, env)
    assert is_ok(add), add
    je_id = add["journal_entry_id"]
    assert is_ok(_submit(conn, je_id)), je_id
    gl_id = conn.execute(
        "SELECT id FROM gl_entry WHERE voucher_type = ? AND voucher_id = ? "
        "ORDER BY id",
        ("journal_entry", je_id)).fetchone()[0]
    holder = get_connection()
    try:
        from erpclaw_lib.gl_posting import take_chain_heads
        take_chain_heads(holder, [env["company_id"]])
        penv = proofs._proc_env(ERPCLAW_PG_LOCK_TIMEOUT="10s")
        proc = subprocess.Popen(
            [sys.executable, _JOURNALS_SCRIPT,
             "--action", "cancel-journal-entry",
             "--journal-entry-id", je_id],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=penv)
        try:
            proc.wait(timeout=1.0)
            out, err = proc.communicate(timeout=5)
            pytest.fail("cancel-journal-entry should block on the held head "
                        "(rc=%s out=%s err=%s)"
                        % (proc.returncode, out, err))
        except subprocess.TimeoutExpired:
            assert proc.poll() is None, "cancel must still be alive"
        probe = get_connection()
        try:
            probe.execute("SET lock_timeout = '1s'")
            probe.execute(
                "UPDATE journal_entry SET status = status WHERE id = ?",
                (je_id,))
            probe.execute(
                "UPDATE gl_entry SET remarks = remarks WHERE id = ?",
                (gl_id,))
            probe.rollback()
        finally:
            probe.close()
        holder.rollback()
        out, err = proc.communicate(timeout=12)
        assert proc.returncode == 0, (out, err)
    finally:
        try:
            holder.rollback()
        except Exception:  # noqa: BLE001 - best effort release
            pass
        holder.close()


# ── 5. reversed once: cancel vs amend, five rounds (PostgreSQL only) ──

def _pg_env_no_timeout(proofs):
    penv = proofs._proc_env()
    penv.pop("ERPCLAW_PG_LOCK_TIMEOUT", None)
    penv.pop("ERPCLAW_PG_STATEMENT_TIMEOUT", None)
    return penv


def _voucher_gl(conn, je_id):
    return conn.execute(
        "SELECT id, remarks, is_cancelled FROM gl_entry "
        "WHERE voucher_type = ? AND voucher_id = ? ORDER BY id",
        ("journal_entry", je_id)).fetchall()


def test_5_cancel_vs_amend_reversed_once(pg_book):
    proofs = _proofs()
    proofs._pg_only()
    conn, _ = pg_book
    penv = _pg_env_no_timeout(proofs)
    for _ in range(5):
        env = _build_pg_book(conn)
        add = _add(conn, env)
        assert is_ok(add), add
        je_id = add["journal_entry_id"]
        assert is_ok(_submit(conn, je_id)), je_id
        cancel = subprocess.Popen(
            [sys.executable, _JOURNALS_SCRIPT,
             "--action", "cancel-journal-entry",
             "--journal-entry-id", je_id],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=penv)
        amend = subprocess.Popen(
            [sys.executable, _JOURNALS_SCRIPT,
             "--action", "amend-journal-entry",
             "--journal-entry-id", je_id],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=penv)
        out_c, err_c = cancel.communicate(timeout=15)
        out_a, err_a = amend.communicate(timeout=15)
        assert "deadlock" not in (out_c + err_c).lower(), (out_c, err_c)
        assert "deadlock" not in (out_a + err_a).lower(), (out_a, err_a)
        assert sorted([cancel.returncode, amend.returncode]) == [0, 1], (
            out_c, err_c, out_a, err_a)
        if cancel.returncode == 0:
            assert amend.returncode == 1, (out_a, err_a)
            assert json.loads(out_a)["message"] == (
                "Cannot amend: journal entry is 'cancelled' "
                "(must be 'submitted')"), (out_a, err_a)
            assert conn.execute(
                "SELECT COUNT(*) FROM journal_entry WHERE amended_from = ?",
                (je_id,)).fetchone()[0] == 0
        else:
            assert cancel.returncode == 1, (out_c, err_c)
            assert json.loads(out_c)["message"] == (
                "Cannot cancel: journal entry is 'amended' "
                "(must be 'submitted')"), (out_c, err_c)
            drafts = conn.execute(
                "SELECT id, status FROM journal_entry WHERE amended_from = ?",
                (je_id,)).fetchall()
            assert len(drafts) == 1, [dict(r) for r in drafts]
            assert drafts[0]["status"] == "draft"
        rows = _voucher_gl(conn, je_id)
        assert len(rows) == 4, [dict(r) for r in rows]
        mirrors = [r for r in rows
                   if (r["remarks"] or "").startswith("Reversal of ")]
        assert len(mirrors) == 2, [dict(r) for r in rows]
        assert all(r["is_cancelled"] in (1, True) for r in rows), [
            dict(r) for r in rows]
        proofs._assert_chain_intact(conn, env["company_id"])
        proofs._assert_contiguous(conn, env["company_id"])


# ── 6. final compare-and-set (PostgreSQL only) ──

def test_6_cancel_compare_and_set(pg_book, monkeypatch):
    proofs = _proofs()
    proofs._pg_only()
    conn, env = pg_book
    add = _add(conn, env)
    assert is_ok(add), add
    je_id = add["journal_entry_id"]
    assert is_ok(_submit(conn, je_id)), je_id
    before = sorted([json.dumps(dict(r), sort_keys=True, default=str)
                     for r in _voucher_gl(conn, je_id)])
    assert len(before) == 2, before
    real_reverse = mod.reverse_gl_entries

    def _flip_underneath(*a, **k):
        ids = real_reverse(*a, **k)
        other = get_connection()
        try:
            _set_status_pypika(other, je_id, "cancelled")
            other.commit()
        finally:
            other.close()
        return ids

    monkeypatch.setattr(mod, "reverse_gl_entries", _flip_underneath)
    result = _cancel(conn, je_id)
    assert is_error(result), result
    assert result.get("message") == (
        "Cannot cancel: journal entry is 'cancelled' (must be 'submitted')"), (
        result)
    fresh = get_connection()
    try:
        after = sorted([json.dumps(dict(r), sort_keys=True, default=str)
                        for r in _voucher_gl(fresh, je_id)])
    finally:
        fresh.close()
    assert after == before, (after, before)
    assert len(after) == 2, after


# ── 7. refusal after the reversal rolls back (SQLite) ──

@_SQLITE_ONLY
def test_7_unbalanced_amend_rolls_back(conn, env):
    _sqlite_only()
    add = _add(conn, env)
    assert is_ok(add), add
    je_id = add["journal_entry_id"]
    assert is_ok(_submit(conn, je_id)), je_id
    bad_lines = json.dumps([
        {"account_id": env["expense"], "debit": "100.00", "credit": "0",
         "cost_center_id": env["cc"]},
        {"account_id": env["cash"], "debit": "0", "credit": "90.00",
         "cost_center_id": env["cc"]},
    ])
    result = _amend(conn, je_id, lines=bad_lines)
    assert is_error(result), result
    assert result.get("message") == (
        "Total debit (100.00) must equal total credit (90.00)"), result
    assert conn.execute(
        "SELECT status FROM journal_entry WHERE id = ?",
        (je_id,)).fetchone()[0] == "submitted"
    rows = conn.execute(
        "SELECT is_cancelled FROM gl_entry "
        "WHERE voucher_type = ? AND voucher_id = ?",
        ("journal_entry", je_id)).fetchall()
    assert len(rows) == 2, [dict(r) for r in rows]
    assert all(r["is_cancelled"] in (0, False) for r in rows), [
        dict(r) for r in rows]
    assert conn.execute(
        "SELECT COUNT(*) FROM journal_entry").fetchone()[0] == 1


# ── 8. submit posts the lines read under the head (SQLite) ──

@_SQLITE_ONLY
def test_8_submit_posts_lines_read_under_head(conn, env, monkeypatch):
    _sqlite_only()
    add = _add(conn, env)
    assert is_ok(add), add
    je_id = add["journal_entry_id"]

    def _rewrite_then_take(c, company_ids):
        c.execute(
            "UPDATE journal_entry_line SET debit = ? "
            "WHERE journal_entry_id = ? AND account_id = ?",
            ("120.00", je_id, env["expense"]))
        c.execute(
            "UPDATE journal_entry_line SET credit = ? "
            "WHERE journal_entry_id = ? AND account_id = ?",
            ("120.00", je_id, env["cash"]))
        c.execute(
            "UPDATE journal_entry SET total_debit = ?, total_credit = ? "
            "WHERE id = ?",
            ("120.00", "120.00", je_id))
        c.commit()
        _real_take_heads(c, company_ids)

    monkeypatch.setattr(mod, "take_chain_heads", _rewrite_then_take,
                        raising=False)
    result = _submit(conn, je_id)
    assert is_ok(result), result
    rows = conn.execute(
        "SELECT account_id, debit, credit FROM gl_entry "
        "WHERE voucher_type = ? AND voucher_id = ? ORDER BY debit DESC",
        ("journal_entry", je_id)).fetchall()
    assert len(rows) == 2, [dict(r) for r in rows]
    assert Decimal(rows[0]["debit"]) == Decimal("120.00")
    assert rows[0]["account_id"] == env["expense"]
    assert Decimal(rows[1]["credit"]) == Decimal("120.00")
    assert rows[1]["account_id"] == env["cash"]


# ── 9. amend and recurring do not deadlock, five rounds (PG only) ──

def _mk_recurring_template(conn, env):
    result = call_action(mod.add_recurring_template, conn, ns(
        company_id=env["company_id"],
        template_name="T-" + uuid.uuid4().hex[:6], frequency="monthly",
        start_date="2026-03-01", end_date=None, entry_type="journal",
        auto_submit=True, lines=_two_lines(env), remark=None))
    assert is_ok(result), result
    return result["template_id"]


def test_9_amend_vs_recurring_no_deadlock(pg_book):
    proofs = _proofs()
    proofs._pg_only()
    conn, _ = pg_book
    penv = _pg_env_no_timeout(proofs)
    for _ in range(5):
        env = _build_pg_book(conn)
        add = _add(conn, env)
        assert is_ok(add), add
        je_id = add["journal_entry_id"]
        assert is_ok(_submit(conn, je_id)), je_id
        _mk_recurring_template(conn, env)
        amend = subprocess.Popen(
            [sys.executable, _JOURNALS_SCRIPT,
             "--action", "amend-journal-entry",
             "--journal-entry-id", je_id],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=penv)
        recurring = subprocess.Popen(
            [sys.executable, _JOURNALS_SCRIPT,
             "--action", "process-recurring",
             "--company-id", env["company_id"],
             "--as-of-date", "2026-03-31"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=penv)
        out_a, err_a = amend.communicate(timeout=15)
        out_r, err_r = recurring.communicate(timeout=15)
        assert "deadlock" not in (out_a + err_a).lower(), (out_a, err_a)
        assert "deadlock" not in (out_r + err_r).lower(), (out_r, err_r)
        assert amend.returncode == 0, (out_a, err_a)
        assert recurring.returncode == 0, (out_r, err_r)
        proofs._assert_chain_intact(conn, env["company_id"])
        proofs._assert_contiguous(conn, env["company_id"])
