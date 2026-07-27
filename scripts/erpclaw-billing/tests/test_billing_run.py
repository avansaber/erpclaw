"""Wave F S1.3 — billing_run registry: library semantics, run-billing on the
registry, PM rate_plan_id stamp, and the three new actions.

Pins:
  - run-billing records one target per meter (done/skipped/failed) and keeps
    every stage-1 output key + containment semantic
  - natural-key idempotency preserved (already-billed → skipped target)
  - crash mid-run + in-process resume → zero duplicate billing periods
  - PM contract item: an open period re-rated under the meter's CURRENT
    plan stamps billing_period.rate_plan_id in the same UPDATE (exact
    Decimal charge under the new plan)
  - library: done target re-run is a no-op; finalize status matrix;
    resume refuses completed runs
  - list/get/resume-billing-run action shapes + dispatch-by-run_type
"""
import json
import sys
import uuid
from decimal import Decimal
from unittest.mock import patch, MagicMock

import pytest
from billing_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
)

from erpclaw_lib import billing_run as billing_run_lib

mod = load_db_query()


# ── factories (idioms shared with test_rating_engine) ───────────────────────

def _plan_args(**overrides):
    base = dict(
        name="Plan", billing_model="flat", service_type="electricity",
        base_charge=None, base_charge_period=None, effective_from=None,
        effective_to=None, minimum_charge=None, minimum_commitment=None,
        overage_rate=None, tiers=None, tier_strategy=None,
    )
    base.update(overrides)
    return ns(**base)


def _create_plan(conn, rate="0.10", model="flat"):
    result = call_action(mod.add_rate_plan, conn, _plan_args(
        name=f"{model} plan", billing_model=model,
        tiers=json.dumps([{"rate": rate}])))
    assert is_ok(result), result
    return result["rate_plan"]


def _create_meter(conn, env, plan_id):
    result = call_action(mod.add_meter, conn, ns(
        customer_id=env["customer"], meter_type="electricity",
        name="M", address=None, rate_plan_id=plan_id,
        install_date=None, unit="kWh",
    ))
    assert is_ok(result), result
    return result["meter"]


def _add_event(conn, meter_id, timestamp, quantity):
    result = call_action(mod.add_usage_event, conn, ns(
        meter_id=meter_id, event_date=timestamp, quantity=quantity,
        event_type="usage", properties=None, idempotency_key=None,
    ))
    assert is_ok(result), result


def _run_billing(conn, env, from_date="2026-06-01", to_date="2026-06-30"):
    return call_action(mod.run_billing, conn, ns(
        company_id=env["company_id"], billing_date=to_date,
        from_date=from_date, to_date=to_date,
    ))


def _period(conn, period_id):
    row = conn.execute(
        "SELECT * FROM billing_period WHERE id = ?", (period_id,)).fetchone()
    assert row is not None
    return dict(row)


def _targets(conn, run_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM billing_run_target WHERE billing_run_id = ? "
        "ORDER BY created_at, id", (run_id,)).fetchall()]


# ── run-billing on the registry ─────────────────────────────────────────────

class TestRunBillingRegistry:
    def test_targets_recorded_and_done(self, conn, env):
        plan = _create_plan(conn)
        m1 = _create_meter(conn, env, plan["id"])
        m2 = _create_meter(conn, env, plan["id"])
        _add_event(conn, m1["id"], "2026-06-10 12:00:00", "500")
        _add_event(conn, m2["id"], "2026-06-11 12:00:00", "300")

        result = _run_billing(conn, env)
        assert is_ok(result), result
        assert result["periods_created"] == 2
        assert Decimal(result["total_billed"]) == Decimal("80.00")
        assert result["error_count"] == 0
        assert result["run_status"] == "completed"

        run_id = result["billing_run_id"]
        run = conn.execute(
            "SELECT * FROM billing_run WHERE id = ?", (run_id,)).fetchone()
        assert run["run_type"] == "usage_billing"
        assert run["status"] == "completed"
        assert run["company_id"] == env["company_id"]
        params = json.loads(run["params_json"])
        assert params["from_date"] == "2026-06-01"
        assert params["to_date"] == "2026-06-30"

        targets = _targets(conn, run_id)
        assert len(targets) == 2
        assert all(t["status"] == "done" for t in targets)
        assert sorted(t["result_voucher_id"] for t in targets) == \
            sorted(result["period_ids"])

    def test_already_billed_becomes_skipped_target(self, conn, env):
        plan = _create_plan(conn)
        meter = _create_meter(conn, env, plan["id"])
        _add_event(conn, meter["id"], "2026-06-10 12:00:00", "500")
        first = _run_billing(conn, env)
        assert first["periods_created"] == 1

        second = _run_billing(conn, env)
        assert is_ok(second), second
        assert second["periods_created"] == 0
        assert second["already_billed"] == 1
        assert second["run_status"] == "completed"
        targets = _targets(conn, second["billing_run_id"])
        assert len(targets) == 1
        assert targets[0]["status"] == "skipped"
        assert "Already billed" in targets[0]["error_message"]
        # Natural-key idempotency: still exactly one period for the window
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM billing_period WHERE meter_id = ?",
            (meter["id"],)).fetchone()["c"]
        assert n == 1

    def test_per_meter_failure_is_failed_target(self, conn, env):
        plan_ok = _create_plan(conn)
        plan_bad = _create_plan(conn, rate="0.20")
        m1 = _create_meter(conn, env, plan_ok["id"])
        m2 = _create_meter(conn, env, plan_bad["id"])
        m3 = _create_meter(conn, env, plan_ok["id"])
        for m in (m1, m2, m3):
            _add_event(conn, m["id"], "2026-06-10 12:00:00", "100")
        conn.execute("UPDATE rate_tier SET rate = 'abc' WHERE rate_plan_id = ?",
                     (plan_bad["id"],))
        conn.commit()

        result = _run_billing(conn, env)
        assert is_ok(result), result
        assert result["periods_created"] == 2
        assert result["error_count"] == 1
        entry = result["errors"][0]
        assert entry["meter_id"] == m2["id"]
        assert "not a valid number" in entry["error"]
        assert result["run_status"] == "partially_completed"

        failed = [t for t in _targets(conn, result["billing_run_id"])
                  if t["status"] == "failed"]
        assert len(failed) == 1
        assert failed[0]["target_id"] == m2["id"]
        assert "not a valid number" in failed[0]["error_message"]
        # The failed meter has no period (rolled back / never written)
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM billing_period WHERE meter_id = ?",
            (m2["id"],)).fetchone()["c"] == 0

    def test_crash_mid_run_then_resume_zero_duplicates(self, conn, env):
        plan = _create_plan(conn)
        meters = [_create_meter(conn, env, plan["id"]) for _ in range(3)]
        for m in meters:
            _add_event(conn, m["id"], "2026-06-10 12:00:00", "100")

        real_calc = mod._calculate_charge
        calls = {"n": 0}

        def _crashing(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise KeyboardInterrupt("simulated process kill")
            return real_calc(*a, **kw)

        with patch.object(mod, "_calculate_charge", side_effect=_crashing):
            with pytest.raises(KeyboardInterrupt):
                _run_billing(conn, env)
        conn.rollback()

        assert conn.execute(
            "SELECT COUNT(*) AS c FROM billing_period").fetchone()["c"] == 1
        run_id = conn.execute(
            "SELECT id FROM billing_run").fetchone()["id"]

        resumed = call_action(mod.resume_billing_run, conn, ns(run_id=run_id))
        assert is_ok(resumed), resumed
        assert resumed["resumed"] is True
        assert resumed["periods_created"] == 2
        assert resumed["run_status"] == "completed"

        # ZERO duplicates: one period per meter for the window
        for m in meters:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM billing_period WHERE meter_id = ?",
                (m["id"],)).fetchone()["c"]
            assert n == 1


class TestReRateStampsCurrentPlan:
    def test_open_period_rerate_records_pricing_plan(self, conn, env):
        """PM contract item: mid-cycle plan upgrade → the rated row records
        the plan that actually priced it (rate_plan_id + exact charge)."""
        plan_a = _create_plan(conn, rate="1.00")
        plan_b = _create_plan(conn, rate="2.00")
        meter = _create_meter(conn, env, plan_a["id"])

        opened = call_action(mod.create_billing_period, conn, ns(
            customer_id=env["customer"], meter_id=meter["id"],
            from_date="2026-06-01", to_date="2026-06-30",
            rate_plan_id=None,
        ))
        assert is_ok(opened), opened
        period_id = opened["billing_period"]["id"]
        assert opened["billing_period"]["rate_plan_id"] == plan_a["id"]

        _add_event(conn, meter["id"], "2026-06-10 12:00:00", "100")

        # Mid-cycle upgrade to plan B
        upgraded = call_action(mod.update_meter, conn, ns(
            meter_id=meter["id"], rate_plan_id=plan_b["id"],
            name=None, status=None, address=None, unit=None,
            install_date=None, meter_type=None,
        ))
        assert is_ok(upgraded), upgraded

        result = _run_billing(conn, env)
        assert is_ok(result), result
        assert result["periods_created"] == 1
        assert result["period_ids"] == [period_id]  # SAME row re-rated

        bp = _period(conn, period_id)
        assert bp["rate_plan_id"] == plan_b["id"]     # records pricing plan
        assert bp["usage_charge"] == "200.00"          # priced under B, exact
        assert bp["grand_total"] == "200.00"
        assert bp["status"] == "rated"


# ── library semantics ───────────────────────────────────────────────────────

class TestLibrarySemantics:
    def test_done_target_rerun_is_noop(self, conn, env):
        run_id = billing_run_lib.start(
            conn, "usage_billing", "2026-06-30",
            [("meter", "m-1")], company_id=env["company_id"])
        target = _targets(conn, run_id)[0]
        outcome = billing_run_lib.process_target(
            conn, run_id, target, lambda c, t: {"voucher_id": "v-1"})
        assert outcome["status"] == "done"

        calls = {"n": 0}

        def _never(c, t):
            calls["n"] += 1
            raise AssertionError("must not be called")

        again = billing_run_lib.process_target(
            conn, run_id, _targets(conn, run_id)[0], _never)
        assert again == {"status": "already_done"}
        assert calls["n"] == 0

    def test_finalize_status_matrix(self, conn, env):
        def _mk(outcomes):
            run_id = billing_run_lib.start(
                conn, "usage_billing", "2026-06-30",
                [("meter", f"m-{i}") for i in range(len(outcomes))])
            for t, kind in zip(_targets(conn, run_id), outcomes):
                if kind == "done":
                    billing_run_lib.process_target(
                        conn, run_id, t, lambda c, tt: {"voucher_id": "v"})
                elif kind == "failed":
                    def _boom(c, tt):
                        raise ValueError("boom")
                    billing_run_lib.process_target(conn, run_id, t, _boom)
                elif kind == "skipped":
                    def _skip(c, tt):
                        raise billing_run_lib.SkipTarget("skip")
                    billing_run_lib.process_target(conn, run_id, t, _skip)
            return billing_run_lib.finalize(conn, run_id)

        assert _mk(["done", "done"])["status"] == "completed"
        assert _mk(["done", "failed"])["status"] == "partially_completed"
        assert _mk(["failed", "failed"])["status"] == "failed"
        assert _mk(["skipped", "skipped"])["status"] == "completed"
        assert _mk(["skipped", "failed"])["status"] == "partially_completed"

    def test_failed_target_rolls_back_callback_writes(self, conn, env):
        run_id = billing_run_lib.start(
            conn, "usage_billing", "2026-06-30", [("meter", "m-1")])

        def _write_then_fail(c, t):
            c.execute(
                "INSERT INTO billing_run (id, run_type, as_of_date, status) "
                "VALUES (?, 'usage_billing', '2026-01-01', 'pending')",
                (str(uuid.uuid4()),))
            raise ValueError("late failure")

        before = conn.execute(
            "SELECT COUNT(*) AS c FROM billing_run").fetchone()["c"]
        outcome = billing_run_lib.process_target(
            conn, run_id, _targets(conn, run_id)[0], _write_then_fail)
        assert outcome["status"] == "failed"
        after = conn.execute(
            "SELECT COUNT(*) AS c FROM billing_run").fetchone()["c"]
        assert after == before  # the callback's INSERT rolled back
        assert _targets(conn, run_id)[0]["status"] == "failed"

    def test_resume_refuses_completed(self, conn, env):
        run_id = billing_run_lib.start(
            conn, "usage_billing", "2026-06-30", [("meter", "m-1")])
        billing_run_lib.process_target(
            conn, run_id, _targets(conn, run_id)[0],
            lambda c, t: {"voucher_id": "v"})
        billing_run_lib.finalize(conn, run_id)
        with pytest.raises(billing_run_lib.BillingRunError):
            billing_run_lib.resume(conn, run_id, lambda c, t: {})


# ── new actions ─────────────────────────────────────────────────────────────

class TestBillingRunActions:
    def _seed_run(self, conn, env):
        plan = _create_plan(conn)
        meter = _create_meter(conn, env, plan["id"])
        _add_event(conn, meter["id"], "2026-06-10 12:00:00", "500")
        return _run_billing(conn, env)["billing_run_id"]

    def test_list_billing_runs(self, conn, env):
        run_id = self._seed_run(conn, env)
        result = call_action(mod.list_billing_runs, conn, ns(
            status=None, run_type=None, from_date=None, to_date=None,
            limit=None, offset=None))
        assert is_ok(result), result
        assert result["total_count"] == 1
        assert result["billing_runs"][0]["id"] == run_id

        filtered = call_action(mod.list_billing_runs, conn, ns(
            status="completed", run_type="usage_billing",
            from_date=None, to_date=None, limit=None, offset=None))
        assert filtered["total_count"] == 1
        empty = call_action(mod.list_billing_runs, conn, ns(
            status=None, run_type="recurring_invoices",
            from_date=None, to_date=None, limit=None, offset=None))
        assert empty["total_count"] == 0

        bad = call_action(mod.list_billing_runs, conn, ns(
            status="bogus", run_type=None, from_date=None, to_date=None,
            limit=None, offset=None))
        assert is_error(bad)

    def test_get_billing_run(self, conn, env):
        run_id = self._seed_run(conn, env)
        result = call_action(mod.get_billing_run, conn, ns(run_id=run_id))
        assert is_ok(result), result
        assert result["billing_run"]["id"] == run_id
        assert result["target_count"] == 1
        assert result["targets"][0]["status"] == "done"

        missing = call_action(mod.get_billing_run, conn,
                              ns(run_id="nope"))
        assert is_error(missing)

    def test_resume_completed_refused(self, conn, env):
        run_id = self._seed_run(conn, env)
        result = call_action(mod.resume_billing_run, conn, ns(run_id=run_id))
        assert is_error(result)
        assert "completed" in result["message"]

    def test_resume_dispatches_selling_runs_to_owner(self, conn, env,
                                                     db_path):
        # A recurring_invoices run is owned by erpclaw-selling: resume must
        # delegate via the router with --resume-run-id (per-item logic never
        # leaves its owning module) AND must forward the DB context — the
        # delegated owners resolve db_path eagerly, so a missing --db-path
        # silently targets the DEFAULT database (QA S1.3 round 1, DEFECT-1;
        # this assertion makes the mocked test unable to pass with that bug).
        run_id = billing_run_lib.start(
            conn, "recurring_invoices", "2026-06-30",
            [("recurring_invoice_template", "t-1")],
            company_id=env["company_id"])

        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = json.dumps({"status": "ok", "invoices_generated": 1})
        fake.stderr = ""
        with patch("erpclaw_lib.dependencies.resolve_skill_script",
                   return_value="/fake/db_query.py"), \
             patch.object(mod.subprocess, "run",
                          return_value=fake) as sub:
            result = call_action(mod.resume_billing_run, conn,
                                 ns(run_id=run_id, db_path=db_path))
        assert is_ok(result), result
        assert result["dispatched_to"] == "generate-recurring-invoices"
        cmd = sub.call_args[0][0]
        assert cmd[0] == sys.executable
        assert "--resume-run-id" in cmd and run_id in cmd
        assert "--company-id" in cmd and env["company_id"] in cmd
        assert "generate-recurring-invoices" in cmd
        assert "--db-path" in cmd
        assert cmd[cmd.index("--db-path") + 1] == db_path

    def test_stale_run_resume_skips_already_billed_meter(self, conn, env):
        """QA S1.3 round 1, DEFECT-2 pin (billing leg): _bill_one_meter's
        in-transaction already-billed re-read is load-bearing — a stale
        usage_billing run resumed AFTER a plain re-run already billed the
        window must skip, never mint a second billing period."""
        plan = _create_plan(conn)
        meter_id = _create_meter(conn, env, plan["id"])["id"]
        _add_event(conn, meter_id, "2026-06-10 10:00:00", "100")
        # Stale run: exactly what a crash leaves — 'running', target pending.
        stale = billing_run_lib.start(
            conn, "usage_billing", "2026-06-30", [("meter", meter_id)],
            company_id=env["company_id"],
            params={"from_date": "2026-06-01", "to_date": "2026-06-30",
                    "billing_date": "2026-06-30"})
        # A plain cron re-run bills the window first.
        plain = _run_billing(conn, env)
        assert is_ok(plain) and plain["periods_created"] == 1
        # Resuming the stale run must mint nothing.
        resumed = call_action(mod.resume_billing_run, conn,
                              ns(run_id=stale, db_path=None))
        assert is_ok(resumed), resumed
        assert resumed["periods_created"] == 0
        n = conn.execute("SELECT COUNT(*) AS c FROM billing_period "
                         "WHERE meter_id = ?", (meter_id,)).fetchone()["c"]
        assert n == 1
