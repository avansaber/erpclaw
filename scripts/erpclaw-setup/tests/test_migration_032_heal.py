"""Part A — migration 032: the party-level PLE heal (Wave G F2 / M38).

Every pin runs the REAL migration module against a real SQLite file and asserts
exact Decimals. The four properties the plan and ADR-0032 call load-bearing are
pinned directly, because each one is what stops the migration from laundering a
wrong number into a consistent-looking one:

  APPEND-ONLY        no existing ``amount`` is ever rewritten (dump-compared).
  DELTA ARITHMETIC   the target set is EVERY submitted payment and the amount is
                     ``Σ live allocations + Σ deductions − Σ existing comp``;
                     delta 0 writes NOTHING. This is what makes a re-run a true
                     no-op and a MIS-HEALED install self-repair — the discarded
                     "payments lacking a compensation" reading made a mis-heal
                     permanent (correction C5).
  PRECONDITION       deduction-aware: heal only when ``unallocated_amount ==
                     paid − Σ live allocations − Σ deductions``. Otherwise SKIP
                     AND REPORT. Without the deduction term every
                     deduction-carrying payment is skipped and the always-on
                     invariant reds on live data (correction B4).
  ORDERING           031 first. 032 reads LIVE allocations and 031 is what makes
                     an allocation against a cancelled document non-live, so the
                     wrong order mis-heals exactly the installs carrying the F1
                     defect. Pinned by demonstration, then repaired by re-run.

Plus the migration-class properties from ADR-0028: ``--report-only`` leaves the
database byte-identical (canonical dump compared, not eyeballed), and a second
real run is a no-op.
"""
import importlib.util
import io
import json
import os
import sqlite3
import uuid
from contextlib import redirect_stdout
from decimal import Decimal

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SETUP_DIR = os.path.dirname(_TESTS_DIR)
_MIG_DIR = os.path.join(_SETUP_DIR, "migrations")

D = Decimal


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_MIG_DIR, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mig032 = _load("migration_032", "032_heal_party_level_ple.py")
mig031 = _load("migration_031_for_032", "031_allocation_delink_and_release.py")


def _dump(db_path):
    """Canonical full-database dump — the byte-identical oracle."""
    probe = sqlite3.connect(db_path)
    try:
        return "\n".join(probe.iterdump())
    finally:
        probe.close()


def _run(module, db_path, report_only=False):
    buf = io.StringIO()
    with redirect_stdout(buf):
        module.run_migration(db_path, report_only=report_only)
    return buf.getvalue()


def _uid():
    return str(uuid.uuid4())


# ── fixtures: a real install carrying the M38 defect ─────────────────────────

def _seed_party_and_accounts(conn):
    company = _uid()
    conn.execute("INSERT INTO company (id, name, abbr) VALUES (?, ?, ?)",
                 (company, f"Mig Co {company[:6]}", f"MC{company[:4]}"))
    ar, bank = _uid(), _uid()
    for aid, nm in ((ar, "Debtors"), (bank, "Bank")):
        conn.execute(
            "INSERT INTO account (id, name, account_number, root_type, "
            " balance_direction, company_id, depth) "
            "VALUES (?, ?, ?, 'asset', 'debit_normal', ?, 0)",
            (aid, nm, f"ACC-{aid[:6]}", company))
    customer = _uid()
    conn.execute("INSERT INTO customer (id, name, company_id) VALUES (?, 'Wayne', ?)",
                 (customer, company))
    conn.commit()
    return {"company": company, "ar": ar, "bank": bank, "customer": customer}


def _ple(conn, env, *, voucher_type, voucher_id, amount,
         against_voucher_type=None, against_voucher_id=None, delinked=0):
    conn.execute(
        "INSERT INTO payment_ledger_entry (id, posting_date, account_id, "
        " party_type, party_id, voucher_type, voucher_id, against_voucher_type, "
        " against_voucher_id, amount, amount_in_account_currency, currency, "
        " delinked) VALUES (?, '2026-06-01', ?, 'customer', ?, ?, ?, ?, ?, ?, ?, "
        " 'USD', ?)",
        (_uid(), env["ar"], env["customer"], voucher_type, voucher_id,
         against_voucher_type, against_voucher_id, str(amount), str(amount),
         delinked))


def _seed_m38_install(conn, env, *, invoice_status="submitted",
                      residual="0.00", status="submitted", party=True):
    """A 1,000.00 invoice with 300.00 allocated — the pre-F2 row set.

    Exactly what the shipped writers left behind: the invoice's own ledger row,
    a FULL-AMOUNT party-level row, and a per-allocation row. The party nets 400
    where the truth is 700.
    """
    si_id, pe_id, alloc_id = _uid(), _uid(), _uid()
    conn.execute(
        "INSERT INTO sales_invoice (id, customer_id, posting_date, grand_total, "
        " total_amount, tax_amount, rounding_adjustment, outstanding_amount, "
        " status, company_id) "
        "VALUES (?, ?, '2026-06-01', '1000.00', '1000.00', '0', '0', '700.00', "
        " ?, ?)",
        (si_id, env["customer"], invoice_status, env["company"]))
    conn.execute(
        "INSERT INTO payment_entry (id, naming_series, payment_type, posting_date, "
        " party_type, party_id, paid_from_account, paid_to_account, paid_amount, "
        " received_amount, status, unallocated_amount, company_id) "
        "VALUES (?, 'PAY-MIG', 'receive', '2026-06-05', ?, ?, ?, ?, '300.00', "
        " '300.00', ?, ?, ?)",
        (pe_id, "customer" if party else None, env["customer"] if party else None,
         env["ar"], env["bank"], status, residual, env["company"]))
    conn.execute(
        "INSERT INTO payment_allocation (id, payment_entry_id, voucher_type, "
        " voucher_id, allocated_amount) VALUES (?, ?, 'sales_invoice', ?, '300.00')",
        (alloc_id, pe_id, si_id))
    _ple(conn, env, voucher_type="sales_invoice", voucher_id=si_id,
         amount="1000.00")
    _ple(conn, env, voucher_type="payment_entry", voucher_id=pe_id,
         amount="-300.00")                                    # the party-level row
    _ple(conn, env, voucher_type="payment_entry", voucher_id=pe_id,
         amount="-300.00", against_voucher_type="sales_invoice",
         against_voucher_id=si_id)                            # per-allocation row
    conn.commit()
    return {"sales_invoice": si_id, "payment": pe_id, "allocation": alloc_id}


def _party_net(conn, env):
    """The canonical party reading (payment rows reversal-inclusive)."""
    net = D("0")
    for vt, amount, delinked in conn.execute(
            "SELECT voucher_type, amount, delinked FROM payment_ledger_entry "
            "WHERE party_type = 'customer' AND party_id = ?", (env["customer"],)):
        if vt == "payment_entry" or delinked == 0:
            net += D(amount)
    return net


def _comp_rows(conn, pe_id):
    return conn.execute(
        "SELECT amount FROM payment_ledger_entry "
        "WHERE voucher_type = 'payment_entry' AND voucher_id = ? "
        "  AND against_voucher_type = 'payment_entry' AND against_voucher_id = ? "
        "ORDER BY CAST(amount AS NUMERIC), id", (pe_id, pe_id)).fetchall()


# ── the heal ─────────────────────────────────────────────────────────────────

def test_heals_the_party_double_count(conn, db_path):
    env = _seed_party_and_accounts(conn)
    ids = _seed_m38_install(conn, env)
    assert _party_net(conn, env) == D("400.00"), "fixture must carry the defect"

    out = _run(mig032, db_path)

    assert [D(r["amount"]) for r in _comp_rows(conn, ids["payment"])] == [D("300.00")]
    assert _party_net(conn, env) == D("700.00")
    assert "appended 1 compensation row(s)" in out
    assert "300.00" in out


def test_report_only_leaves_the_database_byte_identical(conn, db_path):
    env = _seed_party_and_accounts(conn)
    ids = _seed_m38_install(conn, env)
    before = _dump(db_path)

    out = _run(mig032, db_path, report_only=True)

    assert _dump(db_path) == before, "--report-only wrote to the database"
    assert "would append 1 compensation row(s)" in out
    # The report must name the payment and the arithmetic, not just a count:
    # it is the operator's review artifact before the real run.
    assert ids["payment"] in out
    assert "allocations 300.00 + deductions 0.00 − existing 0.00" in out


def test_re_run_on_a_healed_install_writes_nothing(conn, db_path):
    """Delta 0 ⇒ no row (correction C5). Idempotency by dump, not by count."""
    env = _seed_party_and_accounts(conn)
    _seed_m38_install(conn, env)
    _run(mig032, db_path)
    after_first = _dump(db_path)

    out = _run(mig032, db_path)

    assert _dump(db_path) == after_first, "the re-run changed the database"
    assert "appended 0 compensation row(s)" in out


def test_append_only_never_rewrites_an_existing_amount(conn, db_path):
    env = _seed_party_and_accounts(conn)
    _seed_m38_install(conn, env)
    before = {r[0]: r[1] for r in conn.execute(
        "SELECT id, amount FROM payment_ledger_entry")}

    _run(mig032, db_path)

    after = {r[0]: r[1] for r in conn.execute(
        "SELECT id, amount FROM payment_ledger_entry")}
    for ple_id, amount in before.items():
        assert after[ple_id] == amount, f"row {ple_id} was rewritten"
    assert len(after) == len(before) + 1


def test_a_draft_or_cancelled_payment_is_never_compensated(conn, db_path):
    """Only 'submitted' payments carry a party-level residual (correction C3)."""
    env = _seed_party_and_accounts(conn)
    draft = _seed_m38_install(conn, env, status="draft", residual="300.00")
    before = _dump(db_path)

    _run(mig032, db_path)

    assert _comp_rows(conn, draft["payment"]) == []
    assert _dump(db_path) == before


def test_a_party_less_payment_is_skipped_and_reported(conn, db_path):
    """C9: an internal transfer has no party and no party-level row to fix."""
    env = _seed_party_and_accounts(conn)
    ids = _seed_m38_install(conn, env, party=False)

    out = _run(mig032, db_path)

    assert _comp_rows(conn, ids["payment"]) == []
    assert "carries no party" in out
    assert ids["payment"] in out


# ── the precondition (deduction-aware) ───────────────────────────────────────

def test_precondition_holds_for_a_deduction_carrying_payment(conn, db_path):
    """paid 300 = alloc 200 + deduction 50 + residual 50.

    The deduction term is what makes this payment healable at all; the
    deduction-blind formula would compute 300 − 200 = 100 ≠ 50 and skip it,
    leaving the always-on invariant red on ordinary live data.
    """
    env = _seed_party_and_accounts(conn)
    ids = _seed_m38_install(conn, env, residual="50.00")
    conn.execute(
        "UPDATE payment_allocation SET allocated_amount = '200.00' WHERE id = ?",
        (ids["allocation"],))
    conn.execute(
        "INSERT INTO payment_deduction (id, payment_entry_id, type, amount, "
        " account_id) VALUES (?, ?, 'early_payment_discount', '50.00', ?)",
        (_uid(), ids["payment"], env["ar"]))
    conn.commit()

    out = _run(mig032, db_path)

    assert [D(r["amount"]) for r in _comp_rows(conn, ids["payment"])] == [D("250.00")]
    assert "allocations 200.00 + deductions 50.00" in out
    assert "skipped: 0 payment" in out


def test_precondition_skips_and_reports_a_corrupted_residual(conn, db_path):
    """The migration REFUSES to heal what it cannot prove.

    A payment whose stored residual already disagrees with its detail rows has a
    different problem; appending a compensation would launder it into a
    wrong-but-consistent number. It is skipped, reported, and left untouched —
    so the always-on invariant keeps reddening it instead of going quiet.
    """
    env = _seed_party_and_accounts(conn)
    ids = _seed_m38_install(conn, env, residual="123.45")
    before = _dump(db_path)

    out = _run(mig032, db_path)

    assert _comp_rows(conn, ids["payment"]) == []
    assert _dump(db_path) == before, "a skipped payment was touched"
    assert "skipped: 1 payment" in out
    assert "residual 123.45 disagrees with paid 300.00" in out
    assert ids["payment"] in out


# ── ordering: 031 before 032 (demonstrated, then repaired) ───────────────────

def _seed_f1_defect_install(conn, env):
    """The F1 defect: the invoice was cancelled while the allocation stood.

    031 has not run, so the allocation is still LIVE against a cancelled
    document and the payment's residual still reads 0.
    """
    ids = _seed_m38_install(conn, env, invoice_status="cancelled")
    conn.execute("UPDATE sales_invoice SET outstanding_amount = '0' WHERE id = ?",
                 (ids["sales_invoice"],))
    conn.execute("UPDATE payment_ledger_entry SET delinked = 1 "
                 "WHERE voucher_type = 'sales_invoice' AND voucher_id = ?",
                 (ids["sales_invoice"],))
    conn.commit()
    return ids


def test_right_order_031_then_032_heals_green(conn, db_path):
    env = _seed_party_and_accounts(conn)
    ids = _seed_f1_defect_install(conn, env)

    _run(mig031, db_path)
    _run(mig032, db_path)

    # 031 released the allocation, so 032 sees zero live allocations and the
    # residual is back to 300: the party owes −300 (cash applied to nothing).
    assert _comp_rows(conn, ids["payment"]) == []
    assert _party_net(conn, env) == D("-300.00")


def test_wrong_order_mis_heals_and_a_re_run_self_repairs(conn, db_path):
    """The ordering claim, demonstrated rather than argued — then C5's
    repairability property, which is the reason the delta reading was chosen.

    Running 032 first passes its own precondition on a defect install
    (0 == 300 − 300 − 0, because the allocation is still live) and appends a
    compensation for an allocation that 031 then voids. Under the discarded
    "payments lacking a compensation" reading that mis-heal is PERMANENT: the
    payment no longer lacks a row, so re-running is a no-op. Under the delta
    reading the same re-run appends the correcting −300.00 and the install goes
    green.
    """
    env = _seed_party_and_accounts(conn)
    ids = _seed_f1_defect_install(conn, env)

    _run(mig032, db_path)                      # wrong order
    assert [D(r["amount"]) for r in _comp_rows(conn, ids["payment"])] == [D("300.00")]

    _run(mig031, db_path)                      # 031 now voids that allocation
    mis_healed = _party_net(conn, env)
    assert mis_healed == D("0.00"), "the mis-heal should be visible"

    out = _run(mig032, db_path)                # the self-repair

    assert [D(r["amount"]) for r in _comp_rows(conn, ids["payment"])] == \
        [D("-300.00"), D("300.00")]
    assert _party_net(conn, env) == D("-300.00")
    assert "appended 1 compensation row(s)" in out
    assert "-300.00" in out


def test_refuses_to_run_before_031(conn, db_path):
    """032 reads LIVE allocations. Without 031's column there is no such thing,
    so it stops with an actionable message instead of guessing."""
    env = _seed_party_and_accounts(conn)
    _seed_m38_install(conn, env)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("ALTER TABLE payment_allocation RENAME TO pa_tmp")
    conn.execute(
        "CREATE TABLE payment_allocation ("
        " id TEXT PRIMARY KEY, payment_entry_id TEXT NOT NULL, "
        " voucher_type TEXT NOT NULL, voucher_id TEXT NOT NULL, "
        " allocated_amount TEXT NOT NULL DEFAULT '0', "
        " exchange_gain_loss TEXT NOT NULL DEFAULT '0', "
        " created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
    conn.execute(
        "INSERT INTO payment_allocation (id, payment_entry_id, voucher_type, "
        " voucher_id, allocated_amount, exchange_gain_loss, created_at) "
        "SELECT id, payment_entry_id, voucher_type, voucher_id, "
        "       allocated_amount, exchange_gain_loss, created_at FROM pa_tmp")
    conn.execute("DROP TABLE pa_tmp")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.commit()
    before = _dump(db_path)

    out = _run(mig032, db_path)

    assert "migration 031 has not run" in out
    assert _dump(db_path) == before


def test_missing_tables_are_a_clean_no_op(conn, db_path):
    conn.execute("DROP TABLE payment_allocation")
    conn.commit()
    before = _dump(db_path)
    out = _run(mig032, db_path)
    assert "table absent" in out
    assert _dump(db_path) == before


@pytest.mark.skipif(not os.environ.get("ERPCLAW_PG_TEST_URL"),
                    reason="ERPCLAW_PG_TEST_URL not set (live Postgres required; "
                           "the PG lane runs on the box leg, plan §8.3)")
def test_postgres_lane():
    pytest.skip("PG lane executes on the box leg (plan §8.3 / SIM §12.4 item 4)")


# ── the audit trail (M102) ───────────────────────────────────────────────────
#
# This migration APPENDS ledger rows computed from the install's own allocations
# and deductions. Nothing else records that those rows came from a migration
# rather than from a payment, which is precisely what a later reader needs to
# know. SIM: planning/simlogs/m102_SIM_2026-08-12.md.

def _trail(conn):
    cur = conn.execute(
        "SELECT skill, entity_type, entity_id, old_values, new_values, description "
        "FROM audit_log WHERE action = ? ORDER BY timestamp, id",
        ("migration:" + mig032.MIGRATION_ID,))
    columns = [d[0] for d in cur.description]
    rows = [dict(zip(columns, r)) for r in cur.fetchall()]
    for row in rows:
        for key in ("old_values", "new_values"):
            row[key] = json.loads(row[key]) if row[key] else None
    return rows


def test_the_trail_names_the_row_it_appended_and_the_arithmetic(conn, db_path):
    """Compared against the PLE row that actually landed, not against the report."""
    env = _seed_party_and_accounts(conn)
    ids = _seed_m38_install(conn, env)

    _run(mig032, db_path)

    rows = _trail(conn)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["skill"] == "erpclaw-setup"
    assert row["entity_type"] == "payment_entry"
    assert row["entity_id"] == ids["payment"]
    assert row["old_values"] is None, "nothing pre-existing changed; this is an append"

    appended = conn.execute(
        "SELECT id, amount FROM payment_ledger_entry WHERE id = ?",
        (row["new_values"]["payment_ledger_entry_id"],)).fetchone()
    assert appended is not None, "the trail names a payment_ledger_entry that does not exist"
    assert appended[1] == row["new_values"]["amount"]
    assert D(row["new_values"]["amount"]) == D("300.00")
    assert row["new_values"]["live_allocations"] == "300.00"
    assert row["new_values"]["deductions"] == "0.00"
    assert row["new_values"]["existing_compensation"] == "0.00"


def test_report_only_writes_no_trail(conn, db_path):
    env = _seed_party_and_accounts(conn)
    _seed_m38_install(conn, env)

    out = _run(mig032, db_path, report_only=True)

    assert _trail(conn) == []
    assert "report-only: no audit_log row is written" in out


def test_a_skipped_payment_gets_no_trail(conn, db_path):
    """A payment the precondition refuses is untouched, so a row would be a lie."""
    env = _seed_party_and_accounts(conn)
    _seed_m38_install(conn, env, party=False)

    _run(mig032, db_path)

    assert _trail(conn) == []


def test_a_re_run_writes_no_second_trail_row(conn, db_path):
    """Delta 0 writes no PLE row, so it writes no audit row either: the trail is
    idempotent for exactly the reason the heal is."""
    env = _seed_party_and_accounts(conn)
    _seed_m38_install(conn, env)
    _run(mig032, db_path)
    first = _trail(conn)
    assert len(first) == 1

    _run(mig032, db_path)

    assert _trail(conn) == first


def test_the_migration_id_is_the_stem_the_runner_ledgers_it_under():
    """`migration_runner.discover` ledgers this file under `fn[:-3]` and the trail
    is retrieved by `migration:<that stem>`.

    Pinned BY VALUE. Deriving it from `mig032.MIGRATION_ID` — which is what
    `_trail()` above does, necessarily — agrees with ANY value the module ends up
    holding, so a second assignment overwriting the derived stem passed every
    test in this file. The L0 gate's other half rejects a reassignment; this half
    says what the stem is.
    """
    assert mig032.MIGRATION_ID == "032_heal_party_level_ple"
    assert mig032.MIGRATION_DATA_CLASS == "rows"


# ── the trail rides the migration's own transaction (M102 §6) ────────────────
#
# The property: written on the migration's OWN connection, inside the SAME
# transaction as the change, never after the commit and never on a second
# connection. Every trail test above passes against a migration that collects its
# rows and flushes them from a second connection after committing — which is the
# M102 defect wearing the fix's clothes, and which is what these two catch.

class _RecordedCursor:
    def __init__(self, cur, log, tag, trip):
        self._cur, self._log, self._tag, self._trip = cur, log, tag, trip

    def execute(self, sql, params=()):
        self._log.append((self._tag, "execute", sql))
        if self._trip is not None and self._trip(sql, self._log):
            raise sqlite3.OperationalError("planted failure after the trail row")
        return self._cur.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._cur, name)


class _RecordedConn:
    """A sqlite3 connection that remembers which statements ran on IT, and when
    it committed, so ordering can be asserted instead of assumed."""

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


def _record(monkeypatch, trip=None):
    """Patch sqlite3.connect for the duration; return (log, opened_connections)."""
    real_connect = sqlite3.connect
    log, opened = [], []

    def _connect(*a, **k):
        wrapper = _RecordedConn(real_connect(*a, **k), log, len(opened), trip)
        opened.append(wrapper)
        return wrapper

    monkeypatch.setattr(sqlite3, "connect", _connect)
    return log, opened


def _positions(log, needle):
    return [i for i, (_tag, kind, sql) in enumerate(log)
            if kind == "execute" and needle in (sql or "")]


def test_the_trail_is_written_on_the_same_connection_inside_the_transaction(
        conn, db_path, monkeypatch):
    env = _seed_party_and_accounts(conn)
    _seed_m38_install(conn, env)
    conn.commit()

    with monkeypatch.context() as mp:
        log, opened = _record(mp)
        _run(mig032, db_path)

    assert {tag for tag, _k, _s in log} == {0}, (
        "the migration opened a second connection; a trail written on it cannot "
        "share the heal's transaction")
    ple = _positions(log, "INSERT INTO payment_ledger_entry")
    trail = _positions(log, "INSERT INTO audit_log")
    commits = [i for i, (_t, kind, _s) in enumerate(log) if kind == "commit"]
    print(f"\nM102 032 ordering: PLE at {ple}, trail at {trail}, commit at {commits}")
    assert len(ple) == 1 and len(trail) == 1 and commits
    assert ple[0] < trail[0] < commits[0], (
        "the trail row must be written after its own change and BEFORE the "
        "commit that makes both durable")
    assert len(opened) == 1


def test_a_failed_heal_takes_its_trail_row_with_it(conn, db_path, monkeypatch):
    """Two payments to heal; the second INSERT dies. The first one's audit row was
    already written — the log proves it — and must not survive the rollback."""
    env = _seed_party_and_accounts(conn)
    _seed_m38_install(conn, env)
    _seed_m38_install(conn, env)
    conn.commit()
    before = _dump(db_path)

    def _fail_the_second_ple(sql, log):
        return ("INSERT INTO payment_ledger_entry" in sql
                and len(_positions(log, "INSERT INTO payment_ledger_entry")) == 2)

    with monkeypatch.context() as mp:
        log, opened = _record(mp, trip=_fail_the_second_ple)
        try:
            with pytest.raises(sqlite3.OperationalError):
                _run(mig032, db_path)
        finally:
            for c in opened:
                c.close()

    assert _positions(log, "INSERT INTO audit_log"), (
        "no trail row was written before the failure, so this proves nothing")
    assert _trail(conn) == [], "a rolled-back heal left its audit row behind"
    assert _dump(db_path) == before, "the failed run changed the database"
