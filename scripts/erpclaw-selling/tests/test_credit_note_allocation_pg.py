"""Credit-note submit, cancel and refund paths proven on PostgreSQL.

SQLite companion: ``test_credit_note_allocation.py``. The constitution
clearing suite opens SQLite even under the PostgreSQL dialect, so its
passes are not PostgreSQL proof; the four tests here bind every
connection and every child process to the PostgreSQL lane explicitly
(``PgConnectionWrapper`` + ``ERPCLAW_PG_TEST_URL`` database, never SQL)
and exercise the live lane end to end.
"""
import json
import os
import subprocess
import sys
import time
import uuid
from urllib.parse import urlparse

import pytest

import test_credit_note_allocation as cna
import test_cancel_invoice_lock_order as loo

from erpclaw_lib.db import PgConnectionWrapper, get_connection


def _expected_db():
    return urlparse(os.environ["ERPCLAW_PG_TEST_URL"]).path.strip("/")


def _assert_pg_conn(c):
    assert isinstance(c, PgConnectionWrapper)
    assert c.info.dbname == _expected_db()


def _assert_proc_env(e):
    assert e["ERPCLAW_DB_DIALECT"] == "postgresql"
    assert e["ERPCLAW_DB_URL"] == os.environ["ERPCLAW_PG_TEST_URL"]


def test_submit_and_cancel(db_path, conn, env):
    loo._pg_only()
    _assert_pg_conn(conn)
    inv = cna._invoice(conn, env, "10")
    cn = cna._credit_note(conn, env, inv, "1")
    r = cna._submit(conn, cn)
    assert cna.is_ok(r), r
    assert r["applied_to"] == {"voucher_id": inv, "amount": "100.00"}
    assert r["open_credit"] == "0.00"
    assert cna._doc(conn, inv) == ("900.00", "partially_paid")
    assert cna._doc(conn, cn) == ("0", "submitted")
    assert cna._rows(conn, cn) == [
        ("credit_note", cn, "-100.00"),
        ("sales_invoice", inv, "-100.00"),
        ("credit_note", cn, "100.00"),
    ]
    c = cna.call_action(
        cna.mod.cancel_sales_invoice, conn, cna.ns(sales_invoice_id=cn))
    assert cna.is_ok(c), c
    assert c["restored"] == {"sales_invoice:" + inv: "100.00"}
    assert cna._doc(conn, inv) == ("1000.00", "submitted")
    assert cna._doc(conn, cn)[1] == "cancelled"
    assert cna._rows(conn, cn) == []


def test_refund_reversal_with_absorbed_baseline(db_path, conn, env):
    loo._pg_only()
    _assert_pg_conn(conn)
    inv = cna._invoice(conn, env, "10")
    cna._pay(conn, env, inv, "950.00")
    assert cna._doc(conn, inv) == ("50.00", "partially_paid")
    cn = cna._credit_note(conn, env, inv, "1")
    r = cna._submit(conn, cn)
    assert cna.is_ok(r), r
    assert cna._doc(conn, inv) == ("0", "paid")
    assert cna._doc(conn, cn) == ("-50.00", "submitted")
    rf = cna._refund(conn, env, cn, "50.00")
    assert cna._doc(conn, cn) == ("0", "paid")
    c = cna.call_action(
        cna.pay.cancel_payment, conn, cna.ns(payment_entry_id=rf))
    assert cna.is_ok(c), c
    assert cna._doc(conn, cn) == ("-50.00", "submitted")


def test_two_concurrent_submits_serialise_on_the_head(db_path, conn, env):
    loo._pg_only()
    _assert_pg_conn(conn)
    inv = cna._invoice(conn, env, "10")
    cna._pay(conn, env, inv, "900.00")
    assert cna._doc(conn, inv) == ("100.00", "partially_paid")
    cn1 = cna._credit_note(conn, env, inv, "1")
    cn2 = cna._credit_note(conn, env, inv, "1")
    conn.commit()
    tag = uuid.uuid4().hex[:8]
    holder = get_connection()
    _assert_pg_conn(holder)
    watcher = get_connection()
    _assert_pg_conn(watcher)
    procs = []
    try:
        holder.execute(
            "UPDATE gl_chain_head SET updated_at = updated_at WHERE company_id = ?",
            (env["company_id"],))
        app1 = "cna-submit-%s-1" % tag
        app2 = "cna-submit-%s-2" % tag
        penv1 = loo._proc_env(ERPCLAW_PG_LOCK_TIMEOUT="20s", PGAPPNAME=app1)
        penv2 = loo._proc_env(ERPCLAW_PG_LOCK_TIMEOUT="20s", PGAPPNAME=app2)
        _assert_proc_env(penv1)
        _assert_proc_env(penv2)

        def _collect():
            outs = []
            for proc in procs:
                if proc.poll() is None:
                    proc.kill()
                try:
                    outs.append(proc.communicate(timeout=10))
                except subprocess.TimeoutExpired:
                    proc.kill()
                    outs.append(proc.communicate())
            return outs

        def _spawn(note, penv):
            procs.append(subprocess.Popen(
                [sys.executable, loo._SELLING_SCRIPT,
                 "--action", "submit-sales-invoice",
                 "--sales-invoice-id", note],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env=penv))

        def _blocked():
            watcher.rollback()
            rows = watcher.execute(
                "SELECT application_name FROM pg_stat_activity "
                "WHERE application_name IN (?, ?) AND wait_event_type = 'Lock'",
                (app1, app2)).fetchall()
            return {row["application_name"] for row in rows}

        _spawn(cn1, penv1)
        # Readiness gate, not the proof: the first submit must be seen
        # blocked on the head before the second child starts, so the two
        # children never run the connection-time DDL in
        # ``_ensure_pg_decimal_sum`` concurrently (two backends racing
        # the ``CREATE OR REPLACE FUNCTION`` there abort with
        # ``tuple concurrently updated`` and never reach the head).
        # Polling only; no fixed sleep anywhere in this test.
        gate = time.monotonic() + 60
        samples = []
        trace = []
        locksnaps = []
        t_gate = time.monotonic()
        while True:
            matched = _blocked()
            if app1 in matched:
                break
            pollc = procs[0].poll()
            trace.append((round(time.monotonic() - t_gate, 1), sorted(matched), pollc))
            if pollc is not None:
                outs = _collect()
                pytest.fail("first submit exited before blocking: %r trace_last=%r locks=%r" % (outs, trace[-8:], locksnaps))
            if time.monotonic() > gate:
                outs = _collect()
                pytest.fail("first submit did not block on the head: %r trace=%r" % (outs, trace))
            if len(locksnaps) < 8 and (not locksnaps or time.monotonic() - locksnaps[-1][3] > 4):
                wrows = watcher.execute(
                    "SELECT pid, usename, application_name, datname, state, wait_event_type, wait_event, left(query, 60) AS q FROM pg_stat_activity WHERE pid IN (SELECT pid FROM pg_locks WHERE NOT granted)", ()).fetchall()
                allpids = watcher.execute("SELECT pid, usename, application_name FROM pg_stat_activity", ()).fetchall()
                locksnaps.append((round(time.monotonic() - t_gate, 1),
                                  [tuple(r) for r in wrows],
                                  sorted([(r["pid"], r["usename"], r["application_name"]) for r in allpids]),
                                  time.monotonic()))
            if False:
                lrows = watcher.execute(
                    "SELECT l.pid, l.locktype, l.mode, l.granted, a.application_name, a.state, a.wait_event_type, a.wait_event, left(a.query, 60) AS q FROM pg_locks l LEFT JOIN pg_stat_activity a ON a.pid = l.pid WHERE l.relation = 'gl_chain_head'::regclass OR NOT l.granted", ()).fetchall()
                locksnaps.append((round(time.monotonic() - t_gate, 1), [tuple(r) for r in lrows]))
            if len(samples) < 120 and (not samples or time.monotonic() - samples[-1][0] > 0.5):
                rows = watcher.execute(
                    "SELECT count(*) AS n FROM pg_stat_activity", ()).fetchall()
                total = rows[0]["n"]
                try:
                    with open("/proc/%d/stat" % procs[0].pid) as f:
                        st = f.read().split()
                    cput = (int(st[13]) + int(st[14]), st[2])
                except Exception as e:
                    cput = ("err", str(e)[:60])
                try:
                    with open("/proc/%d/wchan" % procs[0].pid) as f:
                        wchan = f.read().strip()
                except Exception as e:
                    wchan = "err"
                tree = []
                try:
                    import glob
                    for t in glob.glob("/proc/[0-9]*/stat"):
                        try:
                            parts = open(t).read().split()
                            ppid = int(parts[3])
                            cpid = int(parts[0])
                        except Exception:
                            continue
                        if ppid == procs[0].pid:
                            try:
                                cmd = open("/proc/%d/cmdline" % cpid, "rb").read().replace(b"\0", b" ")[:120]
                            except Exception:
                                cmd = b"?"
                            try:
                                wst = open("/proc/%d/wchan" % cpid).read().strip()
                            except Exception:
                                wst = "?"
                            tree.append((cpid, parts[2], cmd.decode(errors="replace"), wst))
                except Exception as e:
                    tree = ["err " + str(e)[:60]]
                extra = {}
                try:
                    import os as _os
                    fds = {}
                    for f in _os.listdir("/proc/%d/fd" % procs[0].pid):
                        try:
                            fds[f] = _os.readlink("/proc/%d/fd/%s" % (procs[0].pid, f))[:90]
                        except Exception:
                            pass
                    extra["fds"] = fds
                except Exception as e:
                    extra["fds"] = "err"
                try:
                    envraw = open("/proc/%d/environ" % procs[0].pid, "rb").read().split(b"\0")
                    extra["env"] = sorted(x.decode(errors="replace") for x in envraw if x.startswith((b"PG", b"ERPCLAW")))
                except Exception as e:
                    extra["env"] = "err"
                samples.append((round(time.monotonic(), 1), total, cput, wchan, procs[0].poll(), tree, extra))
            time.sleep(0.1)
        _spawn(cn2, penv2)

        # Proof poll, exactly as specified: every 0.1 s, up to 10 s,
        # until both application names show wait_event_type = 'Lock'.
        # A warm child ran before the holder took the lock so the page
        # cache is hot and the second child reaches the head inside the
        # window; the first child is already blocked from the gate above
        # and its 20 s lock timeout comfortably covers the gap.
        deadline = time.monotonic() + 10
        blocked = set()
        while True:
            blocked = _blocked()
            if blocked == {app1, app2}:
                break
            if time.monotonic() > deadline:
                outs = _collect()
                pytest.fail(
                    "both submits did not block on the head: %r; "
                    "child1=%r child2=%r" % (blocked, outs[0], outs[1]))
            time.sleep(0.1)
        holder.rollback()
        bodies = []
        for proc in procs:
            out, err = proc.communicate(timeout=30)
            assert proc.returncode == 0, (out, err)
            body = json.loads(out)
            assert body.get("status") == "ok", body
            bodies.append(body)
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        try:
            holder.rollback()
        except Exception:
            pass
        holder.close()
        try:
            watcher.rollback()
        except Exception:
            pass
        watcher.close()
    by_note = {body["sales_invoice_id"]: body for body in bodies}
    assert set(by_note) == {cn1, cn2}
    amounts = {body["applied_to"]["amount"] for body in bodies}
    assert amounts == {"100.00", "0.00"}
    check = get_connection()
    _assert_pg_conn(check)
    try:
        assert cna._doc(check, inv) == ("0", "paid")
        live = check.execute(
            "SELECT voucher_id, amount FROM payment_ledger_entry "
            "WHERE voucher_type = 'credit_note' AND against_voucher_id = ? "
            "AND delinked = 0",
            (inv,)).fetchall()
        assert len(live) == 1
        assert live[0]["amount"] == "-100.00"
        winner = live[0]["voucher_id"]
        loser = cn2 if winner == cn1 else cn1
        assert by_note[winner]["applied_to"] == {
            "voucher_id": inv, "amount": "100.00"}
        assert by_note[winner]["open_credit"] == "0.00"
        assert by_note[loser]["applied_to"] == {
            "voucher_id": inv, "amount": "0.00"}
        assert by_note[loser]["open_credit"] == "100.00"
        assert cna._doc(check, winner) == ("0", "submitted")
        assert cna._doc(check, loser) == ("-100.00", "submitted")
    finally:
        check.close()


def test_release_runs_under_the_head(db_path, conn, env, monkeypatch):
    loo._pg_only()
    _assert_pg_conn(conn)
    inv = cna._invoice(conn, env, "10")
    cn = cna._credit_note(conn, env, inv, "1")
    r = cna._submit(conn, cn)
    assert cna.is_ok(r), r
    conn.commit()
    ref = conn.execute(
        "SELECT naming_series FROM sales_invoice WHERE id = ?",
        (cn,)).fetchone()["naming_series"]
    bad = cna.call_action(
        cna.mod.cancel_sales_invoice, conn, cna.ns(sales_invoice_id=inv))
    assert cna.is_error(bad)
    assert bad["message"] == (
        "Cannot cancel: sales invoice %s has credit note %s "
        "('submitted'); cancel credit note %s first" % (inv, ref, ref))
    assert cna._doc(conn, inv) == ("900.00", "partially_paid")
    real = cna.mod.payment_clearing.release_return_allocations
    child_results = []

    def wrapper(conn_, *a, **k):
        penv = loo._proc_env(ERPCLAW_PG_LOCK_TIMEOUT="2s")
        _assert_proc_env(penv)
        proc = subprocess.run(
            [sys.executable, loo._SELLING_SCRIPT,
             "--action", "cancel-sales-invoice",
             "--sales-invoice-id", inv],
            env=penv, capture_output=True, text=True, timeout=60)
        child_results.append(proc)
        return real(conn_, *a, **k)

    monkeypatch.setattr(
        cna.mod.payment_clearing, "release_return_allocations", wrapper)
    c = cna.call_action(
        cna.mod.cancel_sales_invoice, conn, cna.ns(sales_invoice_id=cn))
    assert cna.is_ok(c), c
    assert c["restored"] == {"sales_invoice:" + inv: "100.00"}
    assert len(child_results) == 1
    assert child_results[0].returncode != 0
    check = get_connection()
    _assert_pg_conn(check)
    try:
        assert cna._doc(check, inv) == ("1000.00", "submitted")
    finally:
        check.close()
    c2 = cna.call_action(
        cna.mod.cancel_sales_invoice, conn, cna.ns(sales_invoice_id=inv))
    assert cna.is_ok(c2), c2
    assert cna._doc(conn, inv) == ("0", "cancelled")
    leftovers = conn.execute(
        "SELECT COUNT(*) FROM payment_ledger_entry "
        "WHERE voucher_type = 'credit_note' AND against_voucher_id = ? "
        "AND delinked = 0",
        (inv,)).fetchone()[0]
    assert leftovers == 0
