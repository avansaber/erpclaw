"""F4 / M41: link/unlink billing-period ↔ invoice + flag-only detection.

An invoice can be raised without closing its billing period. Two billing-owned
actions manage the period↔invoice link:

  link-billing-period-invoice   attach a submitted/GL-posted invoice to a
                                'rated' period (customer must match; one link
                                only — a second is rejected; period → 'invoiced')
  unlink-billing-period-invoice reset an 'invoiced' period, or a dangling/
                                cancelled link, to 'rated'/NULL (--reason
                                required while the invoice is still live)

And generate-invoices gains flag-only covering-invoice detection: a per-period
`warnings` array that NEVER blocks and NEVER silently skips.

These pin billing's OWN contract. How an invoice reaches 'submitted'/'cancelled'
is selling's concern (submit-/cancel-sales-invoice, tested in erpclaw-selling);
here the sales_invoice status is seeded directly — billing only ever READS it
(a cross-module read; erpclaw-selling is never written, tripwire #4).
"""
import inspect
import json
import os
import uuid

import pytest
import billing_helpers as _bh
from billing_helpers import call_action, ns, is_ok, is_error, load_db_query

mod = load_db_query()

# <root>/erpclaw/scripts/db_query.py must resolve for the un-mocked generate leg.
_SKILLS_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_bh.MODULE_DIR)))


# ── seed helpers (direct inserts — billing's own actions are exercised via the
#    link/unlink/generate calls under test) ──────────────────────────────────

def _seed_rate_plan(conn):
    rp_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO rate_plan (id, name, plan_type, currency, effective_from) "
        "VALUES (?, ?, 'flat', 'USD', '2026-01-01')",
        (rp_id, f"plan {rp_id[:6]}"))
    conn.commit()
    return rp_id


def _seed_period(conn, env, status="rated", invoice_id=None, invoiced_at=None,
                 customer_id=None, period_start="2026-06-01",
                 period_end="2026-06-30", grand_total="100.00"):
    """Insert a billing_period row directly in a chosen state."""
    pid = str(uuid.uuid4())
    rp_id = _seed_rate_plan(conn)
    conn.execute(
        "INSERT INTO billing_period (id, customer_id, rate_plan_id, period_start, "
        "period_end, grand_total, invoice_id, status, invoiced_at, created_at, "
        "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (pid, customer_id or env["customer"], rp_id, period_start, period_end,
         grand_total, invoice_id, status, invoiced_at, "2026-06-01 00:00:00",
         "2026-06-01 00:00:00"))
    conn.commit()
    return pid


def _seed_invoice(conn, env, status="submitted", customer_id=None,
                  posting_date="2026-06-15", grand_total="100.00"):
    """Insert a sales_invoice row in a chosen status (stands in for the selling
    submit/cancel that billing only READS)."""
    iid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO sales_invoice (id, customer_id, posting_date, company_id, "
        "grand_total, status) VALUES (?, ?, ?, ?, ?, ?)",
        (iid, customer_id or env["customer"], posting_date, env["company_id"],
         grand_total, status))
    conn.commit()
    return iid


def _period_row(conn, pid):
    return dict(conn.execute(
        "SELECT * FROM billing_period WHERE id = ?", (pid,)).fetchone())


def _audit_rows(conn, action, entity_id):
    return conn.execute(
        "SELECT * FROM audit_log WHERE action = ? AND entity_id = ?",
        (action, entity_id)).fetchall()


def _bp_rows(conn):
    """The FULL billing_period row set, re-read from the DB and keyed by id.

    The detection pins below compare this, never a dict the action handed back:
    generate-invoices fetches its period row BEFORE detection runs, so an
    in-memory comparison would mask any write detection made (the masking QA
    proved lets a rated→invoiced flip survive the whole suite).
    """
    return {r["id"]: dict(r)
            for r in conn.execute("SELECT * FROM billing_period").fetchall()}


def _row_counts(conn):
    """audit_log / gl_entry row counts. A read-only detection pass adds neither."""
    return {
        "audit_log": conn.execute(
            "SELECT COUNT(*) AS c FROM audit_log").fetchone()["c"],
        "gl_entry": conn.execute(
            "SELECT COUNT(*) AS c FROM gl_entry").fetchone()["c"],
    }


def _bp_audit_count(conn):
    """audit_log rows filed against billing_period. The generate leg's child
    subprocesses file item / sales_invoice rows, so this is the counter that
    stays flat while a real invoice is being created."""
    return conn.execute(
        "SELECT COUNT(*) AS c FROM audit_log WHERE entity_type = ?",
        ("billing_period",)).fetchone()["c"]


def _link(conn, pid, invoice_id):
    return call_action(mod.link_billing_period_invoice, conn, ns(
        billing_period_id=pid, invoice_id=invoice_id))


def _unlink(conn, pid, reason=None):
    return call_action(mod.unlink_billing_period_invoice, conn, ns(
        billing_period_id=pid, reason=reason))


# ── link: happy path ────────────────────────────────────────────────────────

def test_link_submitted_invoice_marks_period_invoiced(conn, env):
    pid = _seed_period(conn, env)
    iid = _seed_invoice(conn, env, status="submitted")
    r = _link(conn, pid, iid)
    assert is_ok(r), r
    row = _period_row(conn, pid)
    assert row["status"] == "invoiced"
    assert row["invoice_id"] == iid
    assert row["invoiced_at"] is not None
    assert r["linked_invoice_id"] == iid and r["invoice_status"] == "submitted"
    # audit row written under the link action
    assert len(_audit_rows(conn, "link-billing-period-invoice", pid)) == 1


def test_link_accepts_any_gl_posted_status(conn, env):
    # 'paid' is GL-posted (past submitted); linking it is valid and consistent
    # with F22's invariant (period 'invoiced' ⟺ linked invoice GL-posted).
    pid = _seed_period(conn, env)
    iid = _seed_invoice(conn, env, status="paid")
    r = _link(conn, pid, iid)
    assert is_ok(r), r
    assert _period_row(conn, pid)["status"] == "invoiced"


def test_link_is_a_sync_noop_afterward(conn, env):
    # After link the state already matches what sync would materialize.
    pid = _seed_period(conn, env)
    iid = _seed_invoice(conn, env, status="submitted")
    assert is_ok(_link(conn, pid, iid))
    before = _period_row(conn, pid)
    s = call_action(mod.sync_billing_period_status, conn, ns(
        company_id=None, billing_period_ids=json.dumps([pid])))
    assert is_ok(s), s
    assert s["synced"] == 0 and s["reverted"] == 0
    assert _period_row(conn, pid) == before  # byte-identical, no re-write


# ── link: refusals ──────────────────────────────────────────────────────────

def test_link_rejects_second_link(conn, env):
    pid = _seed_period(conn, env)
    first = _seed_invoice(conn, env, status="submitted")
    assert is_ok(_link(conn, pid, first))
    second = _seed_invoice(conn, env, status="submitted")
    r = _link(conn, pid, second)
    assert is_error(r)
    assert "unlink" in r.get("message", "").lower()
    # the existing link is untouched — never overwritten
    assert _period_row(conn, pid)["invoice_id"] == first


def test_link_rejects_non_rated_period(conn, env):
    pid = _seed_period(conn, env, status="open")
    iid = _seed_invoice(conn, env, status="submitted")
    r = _link(conn, pid, iid)
    assert is_error(r) and "rated" in r.get("message", "")
    assert _period_row(conn, pid)["invoice_id"] is None


def test_link_rejects_draft_invoice(conn, env):
    pid = _seed_period(conn, env)
    iid = _seed_invoice(conn, env, status="draft")
    r = _link(conn, pid, iid)
    assert is_error(r)
    assert "draft" in r.get("message", "").lower() or "gl-posted" in r.get("message", "").lower()
    assert _period_row(conn, pid)["status"] == "rated"


def test_link_rejects_cancelled_invoice(conn, env):
    pid = _seed_period(conn, env)
    iid = _seed_invoice(conn, env, status="cancelled")
    r = _link(conn, pid, iid)
    assert is_error(r)
    assert _period_row(conn, pid)["invoice_id"] is None


def test_link_rejects_customer_mismatch(conn, env):
    from billing_helpers import seed_customer
    other = seed_customer(conn, env["company_id"], "Other Customer")
    pid = _seed_period(conn, env)  # env customer
    iid = _seed_invoice(conn, env, status="submitted", customer_id=other)
    r = _link(conn, pid, iid)
    assert is_error(r) and "mismatch" in r.get("message", "").lower()
    assert _period_row(conn, pid)["invoice_id"] is None


def test_link_rejects_missing_invoice(conn, env):
    pid = _seed_period(conn, env)
    r = _link(conn, pid, str(uuid.uuid4()))
    assert is_error(r) and "not found" in r.get("message", "").lower()


# ── unlink: live invoice needs a reason ─────────────────────────────────────

def test_unlink_live_invoice_requires_reason(conn, env):
    pid = _seed_period(conn, env)
    iid = _seed_invoice(conn, env, status="submitted")
    assert is_ok(_link(conn, pid, iid))
    # no reason ⇒ refused
    r = _unlink(conn, pid, reason=None)
    assert is_error(r) and "reason" in r.get("message", "").lower()
    assert _period_row(conn, pid)["status"] == "invoiced"  # untouched
    # with reason ⇒ resets to rated/NULL
    r = _unlink(conn, pid, reason="raised in error, re-billing")
    assert is_ok(r), r
    row = _period_row(conn, pid)
    assert row["status"] == "rated"
    assert row["invoice_id"] is None and row["invoiced_at"] is None
    assert r["unlinked_invoice_id"] == iid
    assert len(_audit_rows(conn, "unlink-billing-period-invoice", pid)) == 1


# ── unlink: housekeeping (no reason required) ───────────────────────────────

def test_unlink_dangling_link_no_reason(conn, env):
    # invoice_id points at a row that does not exist
    pid = _seed_period(conn, env, status="invoiced",
                       invoice_id=str(uuid.uuid4()), invoiced_at="2026-06-16")
    r = _unlink(conn, pid, reason=None)
    assert is_ok(r), r
    row = _period_row(conn, pid)
    assert row["status"] == "rated" and row["invoice_id"] is None


def test_unlink_cancelled_link_no_reason(conn, env):
    iid = _seed_invoice(conn, env, status="cancelled")
    pid = _seed_period(conn, env, status="invoiced", invoice_id=iid,
                       invoiced_at="2026-06-16")
    r = _unlink(conn, pid, reason=None)
    assert is_ok(r), r
    assert _period_row(conn, pid)["status"] == "rated"


# ── unlink: refusals ────────────────────────────────────────────────────────

def test_unlink_rated_period_with_live_link_refused(conn, env):
    # a 'rated' period carrying a live (draft) link is the generate/sync
    # machinery's business — unlink does not detach it
    iid = _seed_invoice(conn, env, status="draft")
    pid = _seed_period(conn, env, status="rated", invoice_id=iid)
    r = _unlink(conn, pid, reason="whatever")
    assert is_error(r)
    assert _period_row(conn, pid)["invoice_id"] == iid


def test_unlink_no_link_refused(conn, env):
    pid = _seed_period(conn, env)  # invoice_id is NULL
    r = _unlink(conn, pid, reason="x")
    assert is_error(r) and "no linked invoice" in r.get("message", "").lower()


# ── unlink: the invoiced-with-NULL-link anomaly remedy ──────────────────────
#
# QA bounce condition 3 (pm ruling 2026-08-08): status 'invoiced' + invoice_id
# NULL used to be permanently stuck — sync could only report it, link- refused it
# (it expects 'rated') and unlink refused it (nothing to detach). Unlink now owns
# the remedy: revert to 'rated', --reason required, audit row names the anomaly.

def _plant_anomaly(conn, env):
    """Plant the corrupt row directly. No action produces this state — that is
    the point of the anomaly; C7 only made it visible."""
    return _seed_period(conn, env, status="invoiced", invoice_id=None,
                        invoiced_at="2026-06-16 00:00:00")


def test_unlink_resolves_invoiced_with_null_link(conn, env):
    pid = _plant_anomaly(conn, env)
    # the state sync can only report, never repair
    s = call_action(mod.sync_billing_period_status, conn, ns(
        company_id=None, billing_period_ids=json.dumps([pid])))
    assert is_ok(s), s
    assert any(w["issue"] == "invoiced_with_null_link" for w in s["warnings"]), s
    # the warning must prescribe the remedy that actually executes (QA round-2
    # N8m: the wording is load-bearing — the pre-fix text named an impossible
    # "relink"), so pin the executable prescription, not just the issue code
    anomaly_w = next(w for w in s["warnings"] if w["issue"] == "invoiced_with_null_link")
    assert "unlink (--reason)" in anomaly_w["detail"], anomaly_w

    r = _unlink(conn, pid, reason="books show no invoice was ever raised; re-billing")
    assert is_ok(r), r
    row = _period_row(conn, pid)
    assert row["status"] == "rated"
    assert row["invoice_id"] is None and row["invoiced_at"] is None
    assert r["resolved_anomaly"] == "invoiced_with_null_link"
    assert r["unlinked_invoice_id"] is None

    # the audit row names the anomaly and carries the operator's reason
    rows = _audit_rows(conn, "unlink-billing-period-invoice", pid)
    assert len(rows) == 1, rows
    a = dict(rows[0])
    assert json.loads(a["old_values"]) == {"status": "invoiced", "invoice_id": None}
    assert json.loads(a["new_values"]) == {"status": "rated", "invoice_id": None}
    assert "invoiced_with_null_link" in a["description"], a["description"]
    assert "re-billing" in a["description"], a["description"]

    # and the period is genuinely unstuck: sync no longer reports it, and the
    # covering invoice can now be linked
    s2 = call_action(mod.sync_billing_period_status, conn, ns(
        company_id=None, billing_period_ids=json.dumps([pid])))
    assert is_ok(s2) and s2["warnings"] == [], s2
    iid = _seed_invoice(conn, env, status="submitted")
    assert is_ok(_link(conn, pid, iid))
    assert _period_row(conn, pid)["status"] == "invoiced"


def test_unlink_anomaly_remedy_requires_reason(conn, env):
    pid = _plant_anomaly(conn, env)
    before = _period_row(conn, pid)
    r = _unlink(conn, pid, reason=None)
    assert is_error(r) and "reason" in r.get("message", "").lower()
    assert "invoiced_with_null_link" in r.get("message", ""), r
    assert _period_row(conn, pid) == before          # byte-identical, untouched
    assert _audit_rows(conn, "unlink-billing-period-invoice", pid) == []


@pytest.mark.parametrize("status", ["open", "rated", "paid", "disputed", "void"])
def test_unlink_remedy_touches_no_other_null_link_state(conn, env, status):
    """The remedy fires on 'invoiced' + NULL link and on nothing else: every
    other NULL-link period is refused exactly as before ('rated' with no link is
    the normal pre-generation state, not something to unlink)."""
    pid = _seed_period(conn, env, status=status)   # invoice_id NULL
    before = _period_row(conn, pid)
    r = _unlink(conn, pid, reason="a reason that must not unlock anything")
    assert is_error(r), (status, r)
    assert "no linked invoice" in r.get("message", "").lower(), r
    assert _period_row(conn, pid) == before, status
    assert _audit_rows(conn, "unlink-billing-period-invoice", pid) == []


def test_unlink_never_writes_invoiced(conn, env):
    """N8: the remedy lands 'rated', so unlink adds no third 'invoiced' write
    site. Source-level (the write surface itself) and behavioural (the remedy's
    own result) halves — either alone can be gamed."""
    marker = 'set(T_billing_period.status, ValueWrapper("invoiced"))'
    writers = sorted(name for name, fn in vars(mod).items()
                     if inspect.isfunction(fn) and marker in inspect.getsource(fn))
    assert writers == ["_sync_period_rows", "link_billing_period_invoice"], writers
    assert marker not in inspect.getsource(mod.unlink_billing_period_invoice)
    assert marker not in inspect.getsource(mod._remedy_invoiced_with_null_link)

    pid = _plant_anomaly(conn, env)
    assert is_ok(_unlink(conn, pid, reason="anomaly cleanup"))
    assert _period_row(conn, pid)["status"] == "rated"


# ── flag-only detection in generate-invoices ────────────────────────────────

def test_generate_flags_covering_invoice_without_blocking(conn, env, db_path,
                                                          monkeypatch):
    import os
    import billing_helpers as _bh
    _skills_root = os.path.dirname(os.path.dirname(os.path.dirname(_bh.MODULE_DIR)))
    monkeypatch.setenv("OPENCLAW_SKILLS_DIR", _skills_root)

    pid = _seed_period(conn, env, status="rated", grand_total="100.00")
    # a live covering invoice already posts inside the window (raised outside billing)
    cov = _seed_invoice(conn, env, status="submitted", posting_date="2026-06-10")

    r = call_action(mod.generate_invoices, conn, ns(
        billing_period_ids=json.dumps([pid]), db_path=db_path))
    assert is_ok(r), r
    res = r["results"][0]
    # detection is flag-only: a warning is present AND generation still proceeded
    assert "warnings" in res, res
    assert res["warnings"][0]["issue"] == "possible_existing_invoice"
    assert res["warnings"][0]["invoice_id"] == cov
    # never blocked / skipped: the period still got its own draft invoice
    assert res.get("status") == "generated", res
    assert res["invoice_id"] != cov


def test_generate_ignores_draft_and_out_of_window_and_own_link(conn, env, db_path,
                                                               monkeypatch):
    import os
    import billing_helpers as _bh
    _skills_root = os.path.dirname(os.path.dirname(os.path.dirname(_bh.MODULE_DIR)))
    monkeypatch.setenv("OPENCLAW_SKILLS_DIR", _skills_root)

    pid = _seed_period(conn, env, status="rated")
    _seed_invoice(conn, env, status="draft", posting_date="2026-06-10")     # draft
    _seed_invoice(conn, env, status="submitted", posting_date="2026-07-15")  # out of window
    _seed_invoice(conn, env, status="cancelled", posting_date="2026-06-10")  # cancelled

    r = call_action(mod.generate_invoices, conn, ns(
        billing_period_ids=json.dumps([pid]), db_path=db_path))
    assert is_ok(r), r
    res = r["results"][0]
    # none of the seeded invoices qualify as a covering live invoice
    assert "warnings" not in res, res
    assert res.get("status") == "generated", res


# ── "flag-only" pinned as ACTUALLY flag-only ────────────────────────────────
#
# QA bounce condition 1: nothing asserted that the detection pass writes NOTHING.
# Two mutations survived the whole suite (one flipping rated→invoiced, an N8
# break) because generate-invoices fetches the period row before detection runs,
# so an in-memory check cannot see the write. These pins re-read every
# billing_period row from the DB after the call, on both paths detection takes.

def test_detection_helper_is_read_only(conn, env):
    """Direct pin on the helper: `_covering_invoice_candidates` may not write a
    single row. `total_changes` counts every INSERT/UPDATE/DELETE on this
    connection, so this catches a write to ANY table, not only the ones the
    end-to-end pins below snapshot."""
    pid = _seed_period(conn, env, status="rated")
    cov = _seed_invoice(conn, env, status="submitted", posting_date="2026-06-10")
    before = _bp_rows(conn)
    before_changes = conn.total_changes
    before_counts = _row_counts(conn)

    out = mod._covering_invoice_candidates(
        conn, env["customer"], "2026-06-01", "2026-06-30", exclude_invoice_id=None)

    assert [w["invoice_id"] for w in out] == [cov], out   # detection did run
    assert conn.total_changes == before_changes, "detection wrote rows"
    assert pid in before and _bp_rows(conn) == before
    assert _row_counts(conn) == before_counts


def test_detection_writes_nothing_on_already_generated(conn, env, db_path,
                                                       monkeypatch):
    """already_generated path: generate-invoices writes NOTHING at all, so every
    byte that moves was moved by the detection pass."""
    monkeypatch.setenv("OPENCLAW_SKILLS_DIR", _SKILLS_ROOT)
    # 'rated' + live DRAFT link: the pre-loop sync is a no-op on it (that IS the
    # correct state under N8) and the double-generation guard returns before any
    # write, so no invoice subprocess runs either.
    linked = _seed_invoice(conn, env, status="draft", posting_date="2026-06-05")
    pid = _seed_period(conn, env, status="rated", invoice_id=linked)
    cov = _seed_invoice(conn, env, status="submitted", posting_date="2026-06-10")
    # bystander: same customer AND window, NOT named in the call. A detection
    # write keyed on customer/window rather than on the period id lands here.
    bystander = _seed_period(conn, env, status="rated")

    before = _bp_rows(conn)
    before_counts = _row_counts(conn)
    r = call_action(mod.generate_invoices, conn, ns(
        billing_period_ids=json.dumps([pid]), db_path=db_path))
    assert is_ok(r), r
    res = r["results"][0]
    assert res.get("status") == "already_generated", res
    assert [w["invoice_id"] for w in res["warnings"]] == [cov], res  # detection ran

    after = _bp_rows(conn)
    # column-by-column first so a failure names what moved, then the whole row set
    for p in (pid, bystander):
        for col in ("status", "invoice_id", "invoiced_at", "updated_at"):
            assert after[p][col] == before[p][col], (p, col, before[p][col],
                                                     after[p][col])
    assert after == before, "detection pass wrote to billing_period"
    assert _row_counts(conn) == before_counts, "detection pass wrote audit/GL rows"


def test_detection_writes_nothing_on_generate(conn, env, db_path, monkeypatch):
    """generated path: the ONE write the call was asked to make is the draft
    link. status stays 'rated', invoiced_at stays NULL (a draft posts no GL —
    N8), no other period row moves, and no GL / billing_period audit row appears."""
    monkeypatch.setenv("OPENCLAW_SKILLS_DIR", _SKILLS_ROOT)
    pid = _seed_period(conn, env, status="rated", grand_total="100.00")
    cov = _seed_invoice(conn, env, status="submitted", posting_date="2026-06-10")
    bystander = _seed_period(conn, env, status="rated")

    before = _bp_rows(conn)
    before_counts = _row_counts(conn)
    before_bp_audit = _bp_audit_count(conn)
    r = call_action(mod.generate_invoices, conn, ns(
        billing_period_ids=json.dumps([pid]), db_path=db_path))
    assert is_ok(r), r
    res = r["results"][0]
    assert res.get("status") == "generated", res
    assert [w["invoice_id"] for w in res["warnings"]] == [cov], res  # detection ran

    after = _bp_rows(conn)
    moved = {c for c in after[pid] if after[pid][c] != before[pid][c]}
    assert moved == {"invoice_id", "updated_at"}, moved
    assert after[pid]["status"] == "rated"
    assert after[pid]["invoiced_at"] is None
    assert after[pid]["invoice_id"] == res["invoice_id"]
    assert after[pid]["invoice_id"] != cov
    assert after[bystander] == before[bystander], "detection pass wrote a bystander"
    # a draft invoice posts no GL, and detection posts none either
    assert _row_counts(conn)["gl_entry"] == before_counts["gl_entry"] == 0
    # the child subprocesses file item / sales_invoice audit rows; billing_period
    # gets none, so detection cannot hide one behind them
    assert _bp_audit_count(conn) == before_bp_audit
