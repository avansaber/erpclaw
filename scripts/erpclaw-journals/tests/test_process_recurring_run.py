"""Wave F S1.3 — process-recurring on the crash-safe billing_run registry.

Pins: legacy happy path (action name + output shape), crash + resume with
zero duplicate journal entries, per-target isolation (garbage lines JSON no
longer aborts the run), and the preserved auto-submit-failure semantic
(JE stays draft, target still succeeds).
"""
import json
import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest
from journals_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
)

mod = load_db_query()


def _lines(env, amount="100.00"):
    # cost_center_id on every line: GL validation step 6 requires one for any
    # P&L account, so an auto-submit template without it can only ever produce
    # a draft (test_cwip_je_hook.py idiom).
    return json.dumps([
        {"account_id": env["cash"], "debit": amount, "credit": "0",
         "cost_center_id": env["cc"]},
        {"account_id": env["expense"], "debit": "0", "credit": amount,
         "cost_center_id": env["cc"]},
    ])


def _mk_template(conn, env, start="2026-03-01", end=None, auto_submit=None,
                 amount="100.00", name=None):
    result = call_action(mod.add_recurring_template, conn, ns(
        company_id=env["company_id"],
        template_name=name or f"T-{uuid.uuid4().hex[:6]}",
        frequency="monthly", start_date=start, end_date=end,
        entry_type="journal", auto_submit=auto_submit,
        lines=_lines(env, amount), remark=None,
    ))
    assert is_ok(result), result
    return result["template_id"]


def _run(conn, env, as_of="2026-03-31", **kw):
    return call_action(mod.process_recurring, conn, ns(
        company_id=env["company_id"], as_of_date=as_of, **kw))


def _je_count(conn):
    return conn.execute(
        "SELECT COUNT(*) AS c FROM journal_entry").fetchone()["c"]


class TestHappyPathRegression:
    def test_generate_draft_je(self, conn, env):
        tid = _mk_template(conn, env)
        result = _run(conn, env)
        assert is_ok(result), result
        assert result["generated"] == 1
        entry = result["results"][0]
        assert entry["template_id"] == tid
        assert entry["je_status"] == "draft"
        assert entry["next_run_date"] == "2026-04-01"
        assert entry["template_status"] == "active"
        assert result["run_status"] == "completed"

        je = conn.execute(
            "SELECT * FROM journal_entry WHERE id = ?",
            (entry["journal_entry_id"],)).fetchone()
        assert je["status"] == "draft"
        assert je["total_debit"] == "100.00"

        # Registry rows
        target = conn.execute(
            "SELECT * FROM billing_run_target WHERE billing_run_id = ?",
            (result["billing_run_id"],)).fetchone()
        assert target["status"] == "done"
        assert target["result_voucher_id"] == entry["journal_entry_id"]

    def test_auto_submit_posts_gl(self, conn, env):
        _mk_template(conn, env, auto_submit=True)
        result = _run(conn, env)
        assert is_ok(result), result
        entry = result["results"][0]
        assert entry["je_status"] == "submitted"
        gl = conn.execute(
            "SELECT COUNT(*) AS c FROM gl_entry WHERE voucher_id = ?",
            (entry["journal_entry_id"],)).fetchone()["c"]
        assert gl == 2

    def test_rerun_is_noop(self, conn, env):
        _mk_template(conn, env)
        assert _run(conn, env)["generated"] == 1
        again = _run(conn, env)
        assert is_ok(again), again
        assert again["generated"] == 0
        assert _je_count(conn) == 1

    def test_auto_submit_failure_keeps_draft_and_target_done(self, conn, env):
        # posting date 2025 has no fiscal year in the env → GL validation
        # fails → legacy semantic: JE stays draft, run still succeeds
        _mk_template(conn, env, start="2025-06-01", auto_submit=True)
        result = _run(conn, env, as_of="2025-06-30")
        assert is_ok(result), result
        assert result["generated"] == 1
        assert result["results"][0]["je_status"] == "draft"
        assert result["run_status"] == "completed"
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM gl_entry").fetchone()["c"] == 0


class TestCrashResume:
    def test_crash_mid_run_then_resume_zero_duplicates(self, conn, env):
        tids = [_mk_template(conn, env) for _ in range(3)]
        real_get_next_name = mod.get_next_name
        calls = {"n": 0}

        def _crashing(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise KeyboardInterrupt("simulated process kill")
            return real_get_next_name(*a, **kw)

        with patch.object(mod, "get_next_name", side_effect=_crashing):
            with pytest.raises(KeyboardInterrupt):
                _run(conn, env)
        conn.rollback()

        assert _je_count(conn) == 1
        run_id = conn.execute(
            "SELECT id FROM billing_run WHERE run_type = "
            "'recurring_journals'").fetchone()["id"]

        resumed = _run(conn, env, as_of=None, resume_run_id=run_id)
        assert is_ok(resumed), resumed
        assert resumed["generated"] == 2
        assert resumed["run_status"] == "completed"
        assert _je_count(conn) == 3  # ZERO duplicates
        for tid in tids:
            tmpl = conn.execute(
                "SELECT next_run_date FROM recurring_journal_template "
                "WHERE id = ?", (tid,)).fetchone()
            assert tmpl["next_run_date"] == "2026-04-01"  # advanced ONCE

    def test_resume_completed_run_refused(self, conn, env):
        _mk_template(conn, env)
        result = _run(conn, env)
        resumed = _run(conn, env, as_of=None,
                       resume_run_id=result["billing_run_id"])
        assert is_error(resumed)
        assert "completed" in resumed["message"]


class TestPerTargetIsolation:
    def test_garbage_lines_json_is_contained(self, conn, env):
        tids = [_mk_template(conn, env) for _ in range(3)]
        bad = tids[1]
        conn.execute(
            "UPDATE recurring_journal_template SET lines = 'not-json' "
            "WHERE id = ?", (bad,))
        conn.commit()

        result = _run(conn, env)
        assert is_ok(result), result
        assert result["generated"] == 2
        assert len(result["errors"]) == 1
        assert result["errors"][0]["template_id"] == bad
        assert result["run_status"] == "partially_completed"
        assert _je_count(conn) == 2

        failed = conn.execute(
            "SELECT * FROM billing_run_target WHERE billing_run_id = ? "
            "AND status = 'failed'", (result["billing_run_id"],)).fetchall()
        assert len(failed) == 1
        assert failed[0]["target_id"] == bad


class TestEligibilityReRead:
    def test_stale_run_resume_skips_reprocessed_templates(self, conn, env):
        """QA S1.3 round 1, DEFECT-2 pin (journals leg): the in-transaction
        'no longer due' re-read in _process_one_recurring_template is
        load-bearing — a stale run resumed AFTER a plain re-run already
        processed those templates must skip, never mint a second JE."""
        from erpclaw_lib import billing_run as billing_run_lib
        tid = _mk_template(conn, env)
        stale = billing_run_lib.start(
            conn, "recurring_journals", "2026-03-31",
            [("recurring_journal_template", tid)],
            company_id=env["company_id"])
        # A plain cron re-run processes the template first (advances the date).
        plain = _run(conn, env)
        assert is_ok(plain) and plain["generated"] == 1
        # Resuming the stale run must mint nothing.
        resumed = _run(conn, env, resume_run_id=stale)
        assert is_ok(resumed), resumed
        assert resumed["generated"] == 0
        assert resumed["skipped"] == 1
        assert _je_count(conn) == 1
