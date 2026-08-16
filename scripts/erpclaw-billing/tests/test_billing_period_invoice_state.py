"""F22 (N8): billing-period 'invoiced' ⟺ the linked sales_invoice is GL-posted.

Part A pins 1-8:
  (1) generate ⇒ 'rated' + invoice_id, NO invoiced_at (a draft posts no GL)
  (2) submit the invoice, sync ⇒ 'invoiced' + invoiced_at
  (3) cancel the invoice, sync ⇒ 'rated', invoice_id NULL, audit row written
  (4) second generate on a generated-not-submitted period ⇒ already_generated,
      no second invoice
  (5) after cancel+revert the period is re-invoiceable but NOT re-ratable
  (6) dangling invoice_id ⇒ warning only, no write
  (7) billing-status counters reflect the new distribution
  (8) read actions never mutate (updated_at unchanged)

Correction C7: both writers touch ONLY 'rated'/'invoiced' periods; 'paid',
'disputed' and 'void' are reported and never touched, and the
'invoiced'-with-NULL-link class is reported.

Billing MATERIALIZES billing_period.status by READING sales_invoice.status
(a cross-module read — always allowed). How the invoice reached that status is
selling's concern (submit-/cancel-sales-invoice, tested in erpclaw-selling's
own suite); the sync tests here set the stored invoice status directly to pin
billing's own contract, while the state-machine walk drives the REAL selling +
inventory dispatcher end to end for the generate leg.
"""
import importlib.util
import json
import os
import sqlite3 as _sqlite3
import uuid
from decimal import Decimal

import pytest
from billing_helpers import call_action, ns, is_ok, load_db_query, SETUP_DIR

mod = load_db_query()


def _load_migration_033():
    path = os.path.join(
        SETUP_DIR, "migrations",
        "033_rederive_billing_period_invoice_state.py")
    spec = importlib.util.spec_from_file_location("_mig033", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

import billing_helpers as _bh
# <root>/erpclaw/scripts/db_query.py must resolve for the un-mocked generate leg.
_SKILLS_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_bh.MODULE_DIR)))


# ── factories (billing's own actions) ───────────────────────────────────────

def _rated_period(conn, env):
    plan = call_action(mod.add_rate_plan, conn, ns(
        name="flat plan", billing_model="flat", service_type="electricity",
        base_charge=None, base_charge_period=None, effective_from=None,
        effective_to=None, minimum_charge=None, minimum_commitment=None,
        overage_rate=None, tiers=json.dumps([{"rate": "0.10"}]), tier_strategy=None))
    assert is_ok(plan), plan
    meter = call_action(mod.add_meter, conn, ns(
        customer_id=env["customer"], meter_type="electricity", name="M",
        address=None, rate_plan_id=plan["rate_plan"]["id"], install_date=None,
        unit="kWh"))
    assert is_ok(meter), meter
    ev = call_action(mod.add_usage_event, conn, ns(
        meter_id=meter["meter"]["id"], event_date="2026-06-10 10:00:00",
        quantity="500", event_type="usage", properties=None, idempotency_key=None))
    assert is_ok(ev), ev
    run = call_action(mod.run_billing, conn, ns(
        company_id=env["company_id"], billing_date="2026-06-30",
        from_date="2026-06-01", to_date="2026-06-30"))
    assert run["periods_created"] == 1, run
    return run["period_ids"][0]


def _get_period(conn, period_id):
    row = conn.execute(
        "SELECT * FROM billing_period WHERE id = ?", (period_id,)).fetchone()
    assert row is not None
    return dict(row)


def _seed_invoice(conn, env, status="draft", grand_total="50.00"):
    """Insert a sales_invoice row in an arbitrary status (stands in for the
    selling submit/cancel that billing only ever READS)."""
    iid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO sales_invoice (id, customer_id, posting_date, company_id, "
        "grand_total, status) VALUES (?, ?, ?, ?, ?, ?)",
        (iid, env["customer"], "2026-06-15", env["company_id"], grand_total, status))
    conn.commit()
    return iid


def _link(conn, period_id, invoice_id, status="rated", invoiced_at=None):
    """Seed a period→invoice link + own status directly (test setup only)."""
    conn.execute(
        "UPDATE billing_period SET invoice_id = ?, status = ?, invoiced_at = ? "
        "WHERE id = ?", (invoice_id, status, invoiced_at, period_id))
    conn.commit()


def _sync(conn, bp_ids=None, company_id=None):
    return call_action(mod.sync_billing_period_status, conn, ns(
        company_id=company_id,
        billing_period_ids=json.dumps(bp_ids) if bp_ids is not None else None))


# ── multi-period single call: every period invoices (child-lock finding) ────

def test_multi_period_single_call_all_generate(conn, env, db_path, monkeypatch):
    # Re-QA finding: in ONE generate-invoices call spanning several periods, the
    # parent held an uncommitted write transaction (period 1's invoice_id
    # UPDATE) across period 2's child add-sales-invoice subprocess. In WAL only
    # one writer is allowed, so the child hit "database is locked" and period 2
    # was left 'rated' — honest (no false 'invoiced') but un-invoiced in that
    # call. The fix commits each period's link before the next child runs.
    monkeypatch.setenv("OPENCLAW_SKILLS_DIR", _SKILLS_ROOT)
    bp1 = _rated_period(conn, env)
    bp2 = _rated_period(conn, env)

    r = call_action(mod.generate_invoices, conn, ns(
        billing_period_ids=json.dumps([bp1, bp2]), db_path=db_path))
    assert is_ok(r), r
    assert r["generated"] == 2, r          # BOTH invoice in the single call
    assert r["failed"] == 0, r
    inv_ids = set()
    for bp in (bp1, bp2):
        p = _get_period(conn, bp)
        assert p["status"] == "rated"
        assert p["invoice_id"] is not None
        inv_ids.add(p["invoice_id"])
    assert len(inv_ids) == 2               # two distinct real invoices


# ── the state machine walk: generate → submit → sync → cancel → sync ────────

def test_state_machine_walk(conn, env, db_path, monkeypatch):
    monkeypatch.setenv("OPENCLAW_SKILLS_DIR", _SKILLS_ROOT)
    bp_id = _rated_period(conn, env)

    # (1) generate ⇒ 'rated' + invoice_id, no invoiced_at
    r = call_action(mod.generate_invoices, conn, ns(
        billing_period_ids=json.dumps([bp_id]), db_path=db_path))
    assert is_ok(r), r
    assert r["generated"] == 1 and r["failed"] == 0, r
    inv_id = r["results"][0]["invoice_id"]
    assert r["results"][0]["status"] == "generated"
    bp = _get_period(conn, bp_id)
    assert bp["status"] == "rated"
    assert bp["invoice_id"] == inv_id
    assert bp["invoiced_at"] is None
    # the real invoice is a draft
    assert conn.execute("SELECT status FROM sales_invoice WHERE id = ?",
                        (inv_id,)).fetchone()["status"] == "draft"

    # (4) second generate on a generated-not-submitted period ⇒ already_generated,
    #     no second invoice
    r2 = call_action(mod.generate_invoices, conn, ns(
        billing_period_ids=json.dumps([bp_id]), db_path=db_path))
    assert r2["already_generated"] == 1 and r2["generated"] == 0, r2
    assert r2["results"][0]["invoice_id"] == inv_id
    n_inv = conn.execute(
        "SELECT COUNT(*) AS n FROM sales_invoice WHERE customer_id = ?",
        (env["customer"],)).fetchone()["n"]
    assert n_inv == 1, "a second invoice was created — double-generation guard failed"

    # (2) submit the invoice, sync ⇒ 'invoiced' + invoiced_at
    conn.execute("UPDATE sales_invoice SET status = 'submitted' WHERE id = ?",
                 (inv_id,))
    conn.commit()
    sr = _sync(conn, [bp_id])
    assert is_ok(sr), sr
    assert sr["synced"] == 1, sr
    bp = _get_period(conn, bp_id)
    assert bp["status"] == "invoiced"
    assert bp["invoice_id"] == inv_id
    assert bp["invoiced_at"] is not None

    # (3) cancel the invoice, sync ⇒ 'rated', invoice_id NULL, invoiced_at NULL,
    #     audit row written
    conn.execute("UPDATE sales_invoice SET status = 'cancelled' WHERE id = ?",
                 (inv_id,))
    conn.commit()
    sr2 = _sync(conn, [bp_id])
    assert sr2["reverted"] == 1, sr2
    bp = _get_period(conn, bp_id)
    assert bp["status"] == "rated"
    assert bp["invoice_id"] is None
    assert bp["invoiced_at"] is None
    n_audit = conn.execute(
        "SELECT COUNT(*) AS n FROM audit_log "
        "WHERE action = 'sync-billing-period-status' AND entity_id = ?",
        (bp_id,)).fetchone()["n"]
    assert n_audit >= 1, "auto-revert must write an audit row"

    # (5) re-invoiceable but NOT re-ratable
    #  - re-ratable? run-billing skips the still-'rated' period
    run = call_action(mod.run_billing, conn, ns(
        company_id=env["company_id"], billing_date="2026-06-30",
        from_date="2026-06-01", to_date="2026-06-30"))
    assert run["periods_created"] == 0 and run["already_billed"] >= 1, run
    assert _get_period(conn, bp_id)["status"] == "rated"
    #  - re-invoiceable? generate now produces a fresh draft (invoice_id was cleared)
    r3 = call_action(mod.generate_invoices, conn, ns(
        billing_period_ids=json.dumps([bp_id]), db_path=db_path))
    assert r3["generated"] == 1, r3
    assert r3["results"][0]["invoice_id"] != inv_id


# ── correction C7: protected statuses reported, never touched ───────────────

@pytest.mark.parametrize("protected", ["paid", "disputed", "void"])
def test_protected_status_reported_not_touched(conn, env, protected):
    bp_id = _rated_period(conn, env)
    # a hand-set protected period that (mischievously) carries a live invoice
    inv_id = _seed_invoice(conn, env, status="submitted")
    _link(conn, bp_id, inv_id, status=protected)
    before = _get_period(conn, bp_id)

    sr = _sync(conn, [bp_id])
    assert is_ok(sr), sr
    assert sr["synced"] == 0 and sr["reverted"] == 0
    ids = [p["billing_period_id"] for p in sr["protected"]]
    assert bp_id in ids, sr
    after = _get_period(conn, bp_id)
    assert after["status"] == protected           # untouched
    assert after["invoice_id"] == inv_id
    assert after["updated_at"] == before["updated_at"]


def test_invoiced_with_null_link_reported(conn, env):
    bp_id = _rated_period(conn, env)
    _link(conn, bp_id, None, status="invoiced")   # invoiced but no link (C7 anomaly)
    before = _get_period(conn, bp_id)

    sr = _sync(conn, [bp_id])
    assert is_ok(sr), sr
    assert sr["synced"] == 0
    issues = [w["issue"] for w in sr["warnings"]]
    assert "invoiced_with_null_link" in issues, sr
    after = _get_period(conn, bp_id)
    assert after["status"] == "invoiced"          # not touched
    assert after["updated_at"] == before["updated_at"]


# ── pin 6: dangling link warns only, no write ───────────────────────────────

def test_dangling_link_warns_only(conn, env):
    bp_id = _rated_period(conn, env)
    _link(conn, bp_id, "no-such-invoice", status="rated")
    before = _get_period(conn, bp_id)

    sr = _sync(conn, [bp_id])
    assert is_ok(sr), sr
    assert sr["synced"] == 0 and sr["reverted"] == 0
    issues = [w["issue"] for w in sr["warnings"]]
    assert "dangling_invoice_link" in issues, sr
    after = _get_period(conn, bp_id)
    assert after["invoice_id"] == "no-such-invoice"   # untouched
    assert after["updated_at"] == before["updated_at"]


# ── pin 8: read actions surface disagreement but never mutate ───────────────

def test_read_actions_warn_without_mutating(conn, env):
    bp_id = _rated_period(conn, env)
    inv_id = _seed_invoice(conn, env, status="submitted")
    _link(conn, bp_id, inv_id, status="rated")     # stale: should read 'invoiced'
    before = _get_period(conn, bp_id)

    g = call_action(mod.get_billing_period, conn, ns(billing_period_id=bp_id))
    assert is_ok(g), g
    assert any(w["issue"] == "stale_rated" for w in g.get("warnings", [])), g
    assert _get_period(conn, bp_id)["updated_at"] == before["updated_at"]

    lst = call_action(mod.list_billing_periods, conn, ns(
        customer_id=env["customer"], meter_id=None, status=None,
        from_date=None, to_date=None, limit="20", offset="0"))
    assert is_ok(lst), lst
    assert any(w["issue"] == "stale_rated" for w in lst.get("warnings", [])), lst
    assert _get_period(conn, bp_id)["updated_at"] == before["updated_at"]


# ── pin 7: billing-status counters reflect the distribution ─────────────────

def test_billing_status_counts_new_distribution(conn, env):
    # one 'rated'-with-link (generated, not yet submitted) and one 'invoiced'
    bp_rated = _rated_period(conn, env)
    inv_draft = _seed_invoice(conn, env, status="draft")
    _link(conn, bp_rated, inv_draft, status="rated")

    bp_inv = _rated_period(conn, env)
    inv_sub = _seed_invoice(conn, env, status="submitted")
    _link(conn, bp_inv, inv_sub, status="rated")
    assert _sync(conn, [bp_inv])["synced"] == 1

    st = call_action(mod.status_action, conn, ns(company_id=env["company_id"]))
    assert is_ok(st), st
    counts = st["billing_periods"]
    assert counts.get("rated") == 1, counts    # the generated-not-submitted one
    assert counts.get("invoiced") == 1, counts


# ── sync is idempotent (re-run writes nothing new) ──────────────────────────

def test_sync_is_idempotent(conn, env):
    bp_id = _rated_period(conn, env)
    inv_id = _seed_invoice(conn, env, status="submitted")
    _link(conn, bp_id, inv_id, status="rated")
    assert _sync(conn, [bp_id])["synced"] == 1
    after_first = _get_period(conn, bp_id)
    # a second sync finds nothing to change
    sr = _sync(conn, [bp_id])
    assert sr["synced"] == 0 and sr["reverted"] == 0
    assert _get_period(conn, bp_id)["updated_at"] == after_first["updated_at"]


# ── migration 033: re-derive existing installs (D5) ─────────────────────────

def _seed_old_invoiced(conn, env, inv_status, invoiced_at="2026-06-15 00:00:00"):
    """A period in the OLD 'invoiced'-on-draft-creation state: stamped
    'invoiced' + invoice_id + invoiced_at the moment the (any-status) invoice
    was linked."""
    bp_id = _rated_period(conn, env)
    inv_id = _seed_invoice(conn, env, status=inv_status)
    conn.execute("UPDATE billing_period SET status = 'invoiced', invoice_id = ?, "
                 "invoiced_at = ? WHERE id = ?", (inv_id, invoiced_at, bp_id))
    conn.commit()
    return bp_id, inv_id


def test_migration_033_rederives_and_is_idempotent(conn, env, db_path):
    m = _load_migration_033()
    d_bp, d_inv = _seed_old_invoiced(conn, env, "draft")
    s_bp, s_inv = _seed_old_invoiced(conn, env, "submitted",
                                     invoiced_at="2026-06-15 09:00:00")
    c_bp, c_inv = _seed_old_invoiced(conn, env, "cancelled")
    conn.commit()

    m.run_migration(db_path)
    conn.commit()  # end any read snapshot so we observe the migration's writes

    d = _get_period(conn, d_bp)
    assert d["status"] == "rated"          # draft posts no GL
    assert d["invoice_id"] == d_inv        # link kept (double-generation guard)
    assert d["invoiced_at"] is None
    s = _get_period(conn, s_bp)
    assert s["status"] == "invoiced"       # GL-posted stays invoiced
    assert s["invoice_id"] == s_inv
    assert s["invoiced_at"] == "2026-06-15 09:00:00"   # not invented / rewritten
    c = _get_period(conn, c_bp)
    assert c["status"] == "rated"          # cancelled reverts
    assert c["invoice_id"] is None
    assert c["invoiced_at"] is None

    # idempotent: a re-run writes nothing (byte-identical updated_at)
    snap = {p: _get_period(conn, p)["updated_at"] for p in (d_bp, s_bp, c_bp)}
    m.run_migration(db_path)
    conn.commit()
    for p, ua in snap.items():
        assert _get_period(conn, p)["updated_at"] == ua, f"re-run mutated {p}"


def test_migration_033_reports_protected_and_null_link_not_touched(conn, env, db_path):
    m = _load_migration_033()
    # Create both rated periods FIRST (a 'void' period is invisible to the
    # re-bill guard, so mutate only after every period exists).
    v_bp = _rated_period(conn, env)
    n_bp = _rated_period(conn, env)
    v_inv = _seed_invoice(conn, env, status="submitted")
    # a hand-set 'void' period carrying a submitted link: reported, NOT touched
    # (the probe's resurrection bug — correction C7).
    conn.execute("UPDATE billing_period SET status = 'void', invoice_id = ?, "
                 "invoiced_at = ? WHERE id = ?",
                 (v_inv, "2026-06-15 00:00:00", v_bp))
    # 'invoiced' with a NULL link (C7 anomaly): reported, NOT touched
    conn.execute("UPDATE billing_period SET status = 'invoiced', invoice_id = NULL "
                 "WHERE id = ?", (n_bp,))
    conn.commit()

    v_before = _get_period(conn, v_bp)
    n_before = _get_period(conn, n_bp)
    m.run_migration(db_path)
    conn.commit()

    v = _get_period(conn, v_bp)
    assert v["status"] == "void"           # not resurrected
    assert v["invoice_id"] == v_inv
    assert v["updated_at"] == v_before["updated_at"]
    n = _get_period(conn, n_bp)
    assert n["status"] == "invoiced"       # anomaly surfaced, not silently altered
    assert n["invoice_id"] is None
    assert n["updated_at"] == n_before["updated_at"]


# ── migration 033: the audit trail (M102) ───────────────────────────────────
#
# 033 RE-DERIVES a state flag from another table. Without a trail there is no
# record anywhere that the flag was ever anything else: `updated_at` says when,
# not what, and the per-period report goes to a terminal (and, through the
# runner, to stderr — see M99). SIM: planning/simlogs/m102_SIM_2026-08-12.md.

def _stale_invoiced(conn, env, period_id, inv_status,
                    invoiced_at="2026-06-15 00:00:00"):
    """Stamp an EXISTING period into the old 'invoiced'-on-draft-creation state.

    Separate from `_seed_old_invoiced` for a reason worth writing down: that
    helper calls `_rated_period`, which runs `run-billing`, which re-derives
    every period that already exists. So seeding three periods one after another
    leaves only the LAST one stale — the first two arrive at the migration
    already correct, and a test that only asserts the end state cannot tell
    whether the migration did anything. Every period is created first here, and
    stamped afterwards, so all of them are genuinely stale when 033 runs.
    """
    inv_id = _seed_invoice(conn, env, status=inv_status)
    conn.execute("UPDATE billing_period SET status = 'invoiced', invoice_id = ?, "
                 "invoiced_at = ? WHERE id = ?", (inv_id, invoiced_at, period_id))
    conn.commit()
    return inv_id


def _mig033_trail(conn):
    import json as _json
    m = _load_migration_033()
    cur = conn.execute(
        "SELECT skill, entity_type, entity_id, old_values, new_values, description "
        "FROM audit_log WHERE action = ? ORDER BY timestamp, id",
        ("migration:" + m.MIGRATION_ID,))
    columns = [d[0] for d in cur.description]
    rows = [dict(zip(columns, tuple(r))) for r in cur.fetchall()]
    for row in rows:
        for key in ("old_values", "new_values"):
            row[key] = _json.loads(row[key]) if row[key] else None
    return rows


def test_migration_033_trail_describes_exactly_the_periods_it_changed(conn, env, db_path):
    """Compared against the ACTUAL before/after of `billing_period`, not against
    the migration's own report — which is what catches a row that lies."""
    m = _load_migration_033()
    d_bp, c_bp, s_bp = (_rated_period(conn, env) for _ in range(3))
    _stale_invoiced(conn, env, d_bp, "draft")
    _stale_invoiced(conn, env, c_bp, "cancelled")
    _stale_invoiced(conn, env, s_bp, "submitted")
    before = {p: _get_period(conn, p) for p in (d_bp, c_bp, s_bp)}
    assert all(before[p]["status"] == "invoiced" for p in before), before

    m.run_migration(db_path)
    conn.commit()

    after = {p: _get_period(conn, p) for p in (d_bp, c_bp, s_bp)}
    watched = ("status", "invoice_id", "invoiced_at")
    moved = {p: {k: (before[p][k], after[p][k]) for k in watched
                 if before[p][k] != after[p][k]}
             for p in before}
    moved = {p: d for p, d in moved.items() if d}
    assert set(moved) == {d_bp, c_bp}, moved  # the submitted one was already right

    rows = {r["entity_id"]: r for r in _mig033_trail(conn)}
    assert set(rows) == set(moved), (
        f"the trail names {sorted(rows)} but {sorted(moved)} changed")
    for pid, diff in moved.items():
        assert rows[pid]["skill"] == "erpclaw-setup"
        assert rows[pid]["entity_type"] == "billing_period"
        assert rows[pid]["old_values"] == {k: v[0] for k, v in diff.items()}
        assert rows[pid]["new_values"] == {k: v[1] for k, v in diff.items()}
    # the deciding fact travels with the row, so it can be reviewed later
    assert "'draft'" in rows[d_bp]["description"]
    assert "'cancelled'" in rows[c_bp]["description"]
    # the cancelled case drops the link; the draft case keeps it
    assert rows[c_bp]["new_values"]["invoice_id"] is None
    assert "invoice_id" not in rows[d_bp]["new_values"]


def test_migration_033_report_only_writes_no_trail(conn, env, db_path):
    m = _load_migration_033()
    bp = _rated_period(conn, env)
    _stale_invoiced(conn, env, bp, "draft")

    m.run_migration(db_path, report_only=True)
    conn.commit()

    assert _mig033_trail(conn) == []
    assert _get_period(conn, bp)["status"] == "invoiced", "report-only wrote"


def test_migration_033_trail_does_not_duplicate_on_a_re_run(conn, env, db_path):
    """Only rows whose target differs are written, so a migrated install writes
    neither an UPDATE nor an audit row on the next run."""
    m = _load_migration_033()
    bp = _rated_period(conn, env)
    _stale_invoiced(conn, env, bp, "draft")
    m.run_migration(db_path)
    conn.commit()
    first = _mig033_trail(conn)
    assert len(first) == 1

    m.run_migration(db_path)
    conn.commit()

    assert _mig033_trail(conn) == first


def test_migration_033_leaves_no_trail_for_a_period_it_refused_to_touch(conn, env, db_path):
    """The C7 protected classes are reported and never written, so a row for one
    would claim a change that did not happen."""
    m = _load_migration_033()
    v_bp = _rated_period(conn, env)
    v_inv = _seed_invoice(conn, env, status="submitted")
    conn.execute("UPDATE billing_period SET status = 'void', invoice_id = ?, "
                 "invoiced_at = ? WHERE id = ?",
                 (v_inv, "2026-06-15 00:00:00", v_bp))
    conn.commit()

    m.run_migration(db_path)
    conn.commit()

    assert _mig033_trail(conn) == []


def test_migration_033_id_is_the_stem_the_runner_ledgers_it_under():
    """`migration_runner.discover` ledgers the file under `fn[:-3]` and the trail
    is retrieved by `migration:<that stem>`.

    Pinned BY VALUE. `_mig033_trail()` above necessarily derives its query from
    `m.MIGRATION_ID`, so it agrees with whatever the module ends up holding — a
    second assignment overwriting the derived stem passed every trail test here.
    The L0 gate rejects the reassignment; this says what the stem is.
    """
    m = _load_migration_033()
    assert m.MIGRATION_ID == "033_rederive_billing_period_invoice_state"
    assert m.MIGRATION_DATA_CLASS == "rows"


# ── the trail rides the migration's own transaction (M102 §6) ───────────────
#
# Written on the migration's OWN connection, inside the SAME transaction as the
# UPDATE, never after the commit and never on a second connection. Every trail
# test above passes against a migration that collects its rows and flushes them
# from a second connection after committing — the M102 defect wearing the fix's
# clothes — so the ordering is asserted here directly.

class _RecordedCursor:
    def __init__(self, cur, log, tag, trip):
        self._cur, self._log, self._tag, self._trip = cur, log, tag, trip

    def execute(self, sql, params=()):
        self._log.append((self._tag, "execute", sql))
        if self._trip is not None and self._trip(sql, self._log):
            raise _sqlite3.OperationalError("planted failure after the trail row")
        return self._cur.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._cur, name)


class _RecordedConn:
    def __init__(self, conn, log, tag, trip):
        self._conn, self._log, self._tag, self._trip = conn, log, tag, trip

    def cursor(self, *a, **k):
        return _RecordedCursor(self._conn.cursor(*a, **k), self._log, self._tag,
                               self._trip)

    def execute(self, sql, params=()):
        return self.cursor().execute(sql, params)

    def commit(self):
        self._log.append((self._tag, "commit", None))
        return self._conn.commit()

    def rollback(self):
        self._log.append((self._tag, "rollback", None))
        return self._conn.rollback()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _record033(monkeypatch, trip=None):
    """Patch sqlite3.connect for the duration; return (log, opened)."""
    real_connect = _sqlite3.connect
    log, opened = [], []

    def _connect(*a, **k):
        wrapper = _RecordedConn(real_connect(*a, **k), log, len(opened), trip)
        opened.append(wrapper)
        return wrapper

    monkeypatch.setattr(_sqlite3, "connect", _connect)
    return log, opened


def _at(log, needle):
    return [i for i, (_tag, kind, sql) in enumerate(log)
            if kind == "execute" and needle in (sql or "")]


def test_migration_033_writes_its_trail_on_the_same_connection_in_the_transaction(
        conn, env, db_path, monkeypatch):
    m = _load_migration_033()
    bp = _rated_period(conn, env)
    _stale_invoiced(conn, env, bp, "draft")
    conn.commit()

    with monkeypatch.context() as mp:
        log, opened = _record033(mp)
        m.run_migration(db_path)

    assert {tag for tag, _k, _s in log} == {0}, (
        "the migration opened a second connection; a trail written on it cannot "
        "share the UPDATE's transaction")
    upd = _at(log, "UPDATE billing_period")
    trail = _at(log, "INSERT INTO audit_log")
    commits = [i for i, (_t, kind, _s) in enumerate(log) if kind == "commit"]
    print(f"\nM102 033 ordering: UPDATE at {upd}, trail at {trail}, "
          f"commit at {commits}")
    assert len(upd) == 1 and len(trail) == 1 and commits
    assert upd[0] < trail[0] < commits[0]
    assert len(opened) == 1


def test_migration_033_rolls_its_trail_back_with_a_failed_update(
        conn, env, db_path, monkeypatch):
    """Two stale periods; the second UPDATE dies. The first period's audit row was
    already written — the log proves it — and must not survive."""
    m = _load_migration_033()
    one, two = _rated_period(conn, env), _rated_period(conn, env)
    _stale_invoiced(conn, env, one, "draft")
    _stale_invoiced(conn, env, two, "cancelled")
    conn.commit()
    before = {p: _get_period(conn, p) for p in (one, two)}

    def _fail_the_second_update(sql, log):
        return ("UPDATE billing_period" in sql
                and len(_at(log, "UPDATE billing_period")) == 2)

    with monkeypatch.context() as mp:
        log, _opened = _record033(mp, trip=_fail_the_second_update)
        with pytest.raises(_sqlite3.OperationalError):
            m.run_migration(db_path)

    assert _at(log, "INSERT INTO audit_log"), (
        "no trail row was written before the failure, so this proves nothing")
    assert _mig033_trail(conn) == [], "a rolled-back re-derive left its audit row"
    assert {p: _get_period(conn, p) for p in (one, two)} == before
