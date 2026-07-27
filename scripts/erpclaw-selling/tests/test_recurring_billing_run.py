"""Wave F S1.3 — generate-recurring-invoices on the crash-safe billing_run registry.

Pins the sprint acceptance (wave file §F-debt.3):
  - crash simulation: kill mid-run, resume → ZERO duplicate invoices
  - per-target error isolation: one bad template, the rest still invoice
  - idempotent re-run of a done target is a no-op
  - legacy happy-path regression (action name + output keys preserved)
  - the PLE write rides the SAME per-target transaction as the invoice
    insert (INV-25 contract: a crash can never commit one without the other)
"""
import json
import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest
from selling_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
)

from erpclaw_lib import billing_run as billing_run_lib

mod = load_db_query()


def _items(env, *specs):
    return json.dumps([
        {"item_id": env[k], "qty": q, "rate": r}
        for k, q, r in specs
    ])


def _mk_template(conn, env, rate="500.00", start="2026-01-01",
                 end="2026-12-31", freq="monthly"):
    """Create a template AND activate it (add-recurring-template lands it
    'draft'; generate-recurring-invoices only picks up 'active' — activation
    goes through the real update action, never a raw UPDATE)."""
    result = call_action(mod.add_recurring_template, conn, ns(
        customer_id=env["customer"], company_id=env["company_id"],
        items=_items(env, ("item1", "1", rate)), frequency=freq,
        start_date=start, end_date=end,
        tax_template_id=None, payment_terms_id=None,
    ))
    assert is_ok(result), result
    template_id = result["template_id"]
    activated = call_action(mod.update_recurring_template, conn, ns(
        template_id=template_id, template_status="active",
        frequency=None, items=None,
    ))
    assert is_ok(activated), activated
    return template_id


def _gen(conn, env, as_of="2026-01-31", **kw):
    return call_action(mod.generate_recurring_invoices, conn, ns(
        as_of_date=as_of, company_id=env["company_id"], **kw))


def _invoice_count(conn):
    return conn.execute(
        "SELECT COUNT(*) AS c FROM sales_invoice").fetchone()["c"]


def _invoices_for_template_month(conn, customer_id):
    return conn.execute(
        "SELECT * FROM sales_invoice WHERE customer_id = ? "
        "ORDER BY posting_date", (customer_id,)).fetchall()


def _only_run_id(conn, run_type="recurring_invoices"):
    rows = conn.execute(
        "SELECT id FROM billing_run WHERE run_type = ?",
        (run_type,)).fetchall()
    assert len(rows) == 1, f"expected exactly one run, got {len(rows)}"
    return rows[0]["id"]


class TestHappyPathRegression:
    def test_generate_legacy_shape_and_documents(self, conn, env):
        tid = _mk_template(conn, env, rate="500.00")
        result = _gen(conn, env)
        assert is_ok(result), result
        # Legacy keys preserved
        assert result["invoices_generated"] == 1
        assert result["templates_processed"] == 1
        assert result["templates_completed"] == 0
        assert result["errors"] == []
        inv = result["invoices"][0]
        assert inv["template_id"] == tid
        assert inv["amount"] == "500.00"
        # Additive registry keys
        assert result["run_status"] == "completed"
        run_id = result["billing_run_id"]

        si = conn.execute(
            "SELECT * FROM sales_invoice WHERE id = ?",
            (inv["invoice_id"],)).fetchone()
        assert si["status"] == "submitted"
        assert si["outstanding_amount"] == "500.00"
        gl = conn.execute(
            "SELECT COUNT(*) AS c FROM gl_entry WHERE voucher_id = ?",
            (inv["invoice_id"],)).fetchone()["c"]
        assert gl == 2  # AR debit + income credit
        ple = conn.execute(
            "SELECT * FROM payment_ledger_entry WHERE voucher_id = ?",
            (inv["invoice_id"],)).fetchall()
        assert len(ple) == 1
        assert ple[0]["amount"] == "500.00"

        # Registry rows
        run = conn.execute(
            "SELECT * FROM billing_run WHERE id = ?", (run_id,)).fetchone()
        assert run["status"] == "completed"
        assert run["total_targets"] == 1
        targets = conn.execute(
            "SELECT * FROM billing_run_target WHERE billing_run_id = ?",
            (run_id,)).fetchall()
        assert len(targets) == 1
        assert targets[0]["status"] == "done"
        assert targets[0]["result_voucher_id"] == inv["invoice_id"]

    def test_advance_makes_rerun_a_noop(self, conn, env):
        _mk_template(conn, env)
        first = _gen(conn, env)
        assert first["invoices_generated"] == 1
        again = _gen(conn, env)
        assert is_ok(again), again
        assert again["invoices_generated"] == 0
        assert again["templates_processed"] == 0
        assert _invoice_count(conn) == 1


class TestCrashResume:
    def test_crash_mid_run_then_resume_zero_duplicates(self, conn, env):
        tids = [_mk_template(conn, env, rate=f"{100 + i}.00")
                for i in range(3)]
        real_get_next_name = mod.get_next_name
        calls = {"n": 0}

        def _crashing(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise KeyboardInterrupt("simulated process kill")
            return real_get_next_name(*a, **kw)

        with patch.object(mod, "get_next_name", side_effect=_crashing):
            with pytest.raises(KeyboardInterrupt):
                _gen(conn, env)
        # Simulate what a real process death implies: uncommitted work gone
        conn.rollback()

        # Exactly one invoice committed (target 1); crashed target rolled back
        assert _invoice_count(conn) == 1
        run_id = _only_run_id(conn)
        run = conn.execute(
            "SELECT * FROM billing_run WHERE id = ?", (run_id,)).fetchone()
        assert run["status"] == "running"
        statuses = sorted(r["status"] for r in conn.execute(
            "SELECT status FROM billing_run_target WHERE billing_run_id = ?",
            (run_id,)).fetchall())
        assert statuses == ["done", "pending", "pending"]

        resumed = _gen(conn, env, as_of=None, resume_run_id=run_id)
        assert is_ok(resumed), resumed
        assert resumed["invoices_generated"] == 2
        assert resumed["run_status"] == "completed"

        # THE acceptance: zero duplicate invoices after crash + resume
        assert _invoice_count(conn) == 3
        for tid in tids:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM billing_run_target "
                "WHERE target_id = ? AND status = 'done'", (tid,)).fetchone()["c"]
            assert n == 1
            tmpl = conn.execute(
                "SELECT next_invoice_date FROM recurring_invoice_template "
                "WHERE id = ?", (tid,)).fetchone()
            assert tmpl["next_invoice_date"] == "2026-02-01"  # advanced ONCE

    def test_resume_after_crash_is_stable_on_second_resume(self, conn, env):
        _mk_template(conn, env)
        result = _gen(conn, env)
        run_id = result["billing_run_id"]
        resumed = _gen(conn, env, as_of=None, resume_run_id=run_id)
        assert is_error(resumed)
        assert "completed" in resumed["message"]
        assert _invoice_count(conn) == 1


class TestPerTargetIsolation:
    def test_one_bad_template_does_not_sink_the_batch(self, conn, env):
        tids = [_mk_template(conn, env) for _ in range(10)]
        bad = tids[4]
        conn.execute(
            "DELETE FROM recurring_invoice_template_item WHERE template_id = ?",
            (bad,))
        conn.commit()

        result = _gen(conn, env)
        assert is_ok(result), result
        assert result["invoices_generated"] == 9
        assert result["templates_processed"] == 10
        assert len(result["errors"]) == 1
        assert result["errors"][0]["template_id"] == bad
        assert "no items" in result["errors"][0]["error"]
        assert result["run_status"] == "partially_completed"
        assert _invoice_count(conn) == 9  # predecessors NOT rolled back

        run_id = result["billing_run_id"]
        failed = conn.execute(
            "SELECT * FROM billing_run_target WHERE billing_run_id = ? "
            "AND status = 'failed'", (run_id,)).fetchall()
        assert len(failed) == 1
        assert failed[0]["target_id"] == bad


class TestDoneTargetIdempotency:
    def test_rerunning_a_done_target_is_a_noop(self, conn, env):
        _mk_template(conn, env)
        result = _gen(conn, env)
        run_id = result["billing_run_id"]
        target = conn.execute(
            "SELECT * FROM billing_run_target WHERE billing_run_id = ?",
            (run_id,)).fetchone()
        assert target["status"] == "done"

        calls = {"n": 0}

        def _never_called(cb_conn, t):
            calls["n"] += 1
            raise AssertionError("callback must not run for a done target")

        outcome = billing_run_lib.process_target(
            conn, run_id, dict(target), _never_called)
        assert outcome == {"status": "already_done"}
        assert calls["n"] == 0
        assert _invoice_count(conn) == 1


class TestPLESameTransaction:
    def test_crash_before_advance_commits_nothing(self, conn, env):
        """Invoice + GL + PLE + cursor advance are one atomic unit: a crash
        after the PLE write but before the advance leaves NO trace."""
        _mk_template(conn, env)

        def _boom(*a, **kw):
            raise KeyboardInterrupt("simulated kill between PLE and advance")

        with patch.object(mod, "_next_invoice_date", side_effect=_boom):
            with pytest.raises(KeyboardInterrupt):
                _gen(conn, env)
        conn.rollback()

        assert _invoice_count(conn) == 0
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM payment_ledger_entry").fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM gl_entry").fetchone()["c"] == 0
        tmpl = conn.execute(
            "SELECT next_invoice_date FROM recurring_invoice_template").fetchone()
        assert tmpl["next_invoice_date"] == "2026-01-01"  # NOT advanced
        target = conn.execute(
            "SELECT status FROM billing_run_target").fetchone()
        assert target["status"] == "pending"  # claim rolled back too


class TestDelegatedResumeE2E:
    def test_resume_dispatch_reaches_the_callers_database(
            self, conn, env, db_path):
        """QA S1.3 round 1, DEFECT-1 pin — end to end, no subprocess mock:
        billing's resume-billing-run must forward --db-path to the delegated
        owner. The delegated owners resolve db_path eagerly (args.db_path or
        DEFAULT_DB_PATH), so without the flag the child targets the DEFAULT
        database and reports 'Billing run not found' on any non-default DB.
        Child = the real in-tree foundation router, real temp-file DB."""
        import importlib.util
        import os

        tids = [_mk_template(conn, env, rate=f"{100 + i}.00")
                for i in range(2)]
        # Exactly what a crash leaves: run 'running', targets pending.
        # billing_run_lib.start COMMITs, so the child process sees it.
        run_id = billing_run_lib.start(
            conn, "recurring_invoices", "2026-01-31",
            [("recurring_invoice_template", t) for t in tids],
            company_id=env["company_id"])

        scripts_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", ".."))
        router = os.path.join(scripts_dir, "db_query.py")
        billing_path = os.path.join(scripts_dir, "erpclaw-billing",
                                    "db_query.py")
        spec = importlib.util.spec_from_file_location(
            "qa_billing_delegated_resume", billing_path)
        billing_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(billing_mod)

        with patch("erpclaw_lib.dependencies.resolve_skill_script",
                   return_value=router):
            result = call_action(billing_mod.resume_billing_run, conn,
                                 ns(run_id=run_id, db_path=db_path))
        assert is_ok(result), result
        assert result["dispatched_to"] == "generate-recurring-invoices"
        child = result["result"]
        assert child.get("invoices_generated") == 2, child
        # The invoices landed in THIS database, not the default one.
        assert _invoice_count(conn) == 2
        run = conn.execute("SELECT status FROM billing_run WHERE id = ?",
                           (run_id,)).fetchone()
        assert run["status"] == "completed"


class TestEligibilityReRead:
    def test_stale_run_resume_skips_rebilled_templates(self, conn, env):
        """QA S1.3 round 1, DEFECT-2 pin (selling leg): the in-transaction
        'no longer due' re-read in _invoice_one_template is load-bearing —
        a stale run resumed AFTER a plain re-run already billed those
        templates must skip, never mint a second document (QA's mutation
        testbed proved the whole suite survives with the re-read removed;
        this pin does not)."""
        tids = [_mk_template(conn, env, rate=f"{100 + i}.00")
                for i in range(2)]
        stale = billing_run_lib.start(
            conn, "recurring_invoices", "2026-01-31",
            [("recurring_invoice_template", t) for t in tids],
            company_id=env["company_id"])
        # A plain cron re-run bills the templates first (advances the dates).
        plain = _gen(conn, env)
        assert is_ok(plain) and plain["invoices_generated"] == 2
        # Resuming the stale run must mint nothing.
        resumed = _gen(conn, env, resume_run_id=stale)
        assert is_ok(resumed), resumed
        assert resumed["invoices_generated"] == 0
        assert resumed["skipped"] == 2
        assert _invoice_count(conn) == 2
