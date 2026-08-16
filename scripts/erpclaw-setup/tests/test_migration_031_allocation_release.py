"""Part A — migration 031: payment_allocation.delinked + the release back-heal.

Wave G item F1 (planning/WAVE_G_PLAN_2026-07-31.md §5, row 031). Two halves:
the new column, and the back-heal that repairs installs which already ran the
M46 defect (an invoice cancelled while a payment allocation stood against it).

Every pin runs the REAL migration module against a real SQLite file, on a
DB rebuilt into its genuine pre-031 shape (payment_allocation without the
column), and asserts exact Decimals. The two properties the plan calls
load-bearing are pinned directly:

  C1 — the PLE release pair is written with delinked = 1 on BOTH rows.
  C2 — an allocation whose payment is not 'submitted' is skipped AND reported.

Plus the migration-class properties from ADR-0028 / plan §5: --report-only
leaves the database byte-identical (canonical dump compared, not eyeballed),
and a second real run is a no-op.
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
_MIGRATION = os.path.join(_SETUP_DIR, "migrations",
                          "031_allocation_delink_and_release.py")

D = Decimal


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_031", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mig = _load_migration()


# ── pre-031 shape ────────────────────────────────────────────────────────────

_PRE031_DDL = """
CREATE TABLE payment_allocation (
    id              TEXT PRIMARY KEY,
    payment_entry_id TEXT NOT NULL REFERENCES payment_entry(id) ON DELETE RESTRICT,
    voucher_type    TEXT NOT NULL,
    voucher_id      TEXT NOT NULL,
    allocated_amount TEXT NOT NULL DEFAULT '0',
    exchange_gain_loss TEXT NOT NULL DEFAULT '0',
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP
)
"""


def _rewind_to_pre031(conn):
    """Rebuild payment_allocation exactly as it shipped before this migration.

    Nothing FK-references payment_allocation, so this is a plain rename/copy/drop
    (no legacy_alter_table dance needed — see the migration-FK-preservation L0
    guard for the case where that is not true).
    """
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("ALTER TABLE payment_allocation RENAME TO payment_allocation_pre031")
    conn.execute(_PRE031_DDL)
    conn.execute(
        "INSERT INTO payment_allocation (id, payment_entry_id, voucher_type, "
        " voucher_id, allocated_amount, exchange_gain_loss, created_at) "
        "SELECT id, payment_entry_id, voucher_type, voucher_id, "
        "       allocated_amount, exchange_gain_loss, created_at "
        "  FROM payment_allocation_pre031")
    conn.execute("DROP TABLE payment_allocation_pre031")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_payment_alloc_payment "
                 "ON payment_allocation(payment_entry_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_payment_alloc_voucher "
                 "ON payment_allocation(voucher_type, voucher_id)")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.commit()


def _columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _dump(db_path):
    """Canonical full-database dump — the byte-identical oracle."""
    probe = sqlite3.connect(db_path)
    try:
        return "\n".join(probe.iterdump())
    finally:
        probe.close()


def _run(db_path, report_only=False):
    """Run the real migration, returning its printed report."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        mig.run_migration(db_path, report_only=report_only)
    return buf.getvalue()


# ── fixtures: a real install carrying the M46 defect ─────────────────────────

def _uid():
    return str(uuid.uuid4())


def _seed_party_and_accounts(conn):
    company = _uid()
    conn.execute("INSERT INTO company (id, name, abbr) VALUES (?, ?, ?)",
                 (company, f"Mig Co {company[:6]}", f"MC{company[:4]}"))
    ar = _uid()
    conn.execute(
        "INSERT INTO account (id, name, account_number, root_type, "
        " balance_direction, company_id, depth) "
        "VALUES (?, 'Debtors', ?, 'asset', 'debit_normal', ?, 0)",
        (ar, f"ACC-{ar[:6]}", company))
    bank = _uid()
    conn.execute(
        "INSERT INTO account (id, name, account_number, root_type, "
        " balance_direction, company_id, depth) "
        "VALUES (?, 'Bank', ?, 'asset', 'debit_normal', ?, 0)",
        (bank, f"ACC-{bank[:6]}", company))
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


def _seed_defect_install(conn, env, *, payment_status="submitted"):
    """An invoice cancelled the pre-F1 way while a 300.00 allocation stood.

    Exactly the row set the shipped writers left behind: the invoice's own PLE
    row delinked, its outstanding zeroed, and the payment's allocation + its
    per-allocation PLE row still live with the residual reading 0.
    """
    si_id, pe_id, alloc_id = _uid(), _uid(), _uid()
    conn.execute(
        "INSERT INTO sales_invoice (id, customer_id, posting_date, grand_total, "
        " total_amount, tax_amount, rounding_adjustment, outstanding_amount, "
        " status, company_id) "
        "VALUES (?, ?, '2026-06-01', '1000.00', '1000.00', '0', '0', '0', "
        " 'cancelled', ?)",
        (si_id, env["customer"], env["company"]))
    conn.execute(
        "INSERT INTO payment_entry (id, naming_series, payment_type, posting_date, "
        " party_type, party_id, paid_from_account, paid_to_account, paid_amount, "
        " received_amount, status, unallocated_amount, company_id) "
        "VALUES (?, 'PAY-MIG', 'receive', '2026-06-05', 'customer', ?, ?, ?, "
        " '300.00', '300.00', ?, '0.00', ?)",
        (pe_id, env["customer"], env["ar"], env["bank"], payment_status,
         env["company"]))
    conn.execute(
        "INSERT INTO payment_allocation (id, payment_entry_id, voucher_type, "
        " voucher_id, allocated_amount) VALUES (?, ?, 'sales_invoice', ?, '300.00')",
        (alloc_id, pe_id, si_id))
    _ple(conn, env, voucher_type="sales_invoice", voucher_id=si_id,
         amount="1000.00", delinked=1)                       # invoice, cancelled
    _ple(conn, env, voucher_type="payment_entry", voucher_id=pe_id,
         amount="-300.00")                                   # party-level row
    _ple(conn, env, voucher_type="payment_entry", voucher_id=pe_id,
         amount="-300.00", against_voucher_type="sales_invoice",
         against_voucher_id=si_id)                           # per-allocation row
    conn.commit()
    return {"sales_invoice": si_id, "payment": pe_id, "allocation": alloc_id}


def _alloc_row(conn, alloc_id):
    return conn.execute(
        "SELECT delinked, allocated_amount FROM payment_allocation WHERE id = ?",
        (alloc_id,)).fetchone()


def _release_pair(conn, pe_id, si_id):
    return conn.execute(
        "SELECT amount, delinked FROM payment_ledger_entry "
        "WHERE voucher_type = 'payment_entry' AND voucher_id = ? "
        "  AND against_voucher_id = ? ORDER BY CAST(amount AS NUMERIC), id",
        (pe_id, si_id)).fetchall()


def _unallocated(conn, pe_id):
    return D(conn.execute("SELECT unallocated_amount FROM payment_entry WHERE id = ?",
                          (pe_id,)).fetchone()[0])


# ── the column ───────────────────────────────────────────────────────────────

def test_fresh_install_ships_the_column_and_the_check_holds(conn, db_path):
    """init_schema carries delinked; the CHECK(0,1) rejects anything else."""
    assert "delinked" in _columns(conn, "payment_allocation")
    env = _seed_party_and_accounts(conn)
    ids = _seed_defect_install(conn, env)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE payment_allocation SET delinked = 2 WHERE id = ?",
                     (ids["allocation"],))
    conn.rollback()


def test_adds_the_column_to_a_pre031_install_and_re_run_is_a_no_op(conn, db_path):
    _rewind_to_pre031(conn)
    assert "delinked" not in _columns(conn, "payment_allocation")

    out = _run(db_path)
    assert "delinked: added." in out
    assert "delinked" in _columns(conn, "payment_allocation")
    assert conn.execute(
        "SELECT COUNT(*) FROM pragma_table_info('payment_allocation') "
        "WHERE name = 'delinked'").fetchone()[0] == 1

    dump_after_first = _dump(db_path)
    out2 = _run(db_path)
    assert "already present" in out2
    assert _dump(db_path) == dump_after_first, "the re-run changed the database"


# ── the back-heal ────────────────────────────────────────────────────────────

def test_back_heal_releases_the_stranded_allocation(conn, db_path):
    """The M46 repair, end to end, with exact Decimals and the C1 pair shape."""
    env = _seed_party_and_accounts(conn)
    ids = _seed_defect_install(conn, env)
    _rewind_to_pre031(conn)
    assert _unallocated(conn, ids["payment"]) == D("0.00")

    out = _run(db_path)

    row = _alloc_row(conn, ids["allocation"])
    assert row["delinked"] == 1
    assert D(row["allocated_amount"]) == D("300.00")   # amount never rewritten

    pair = _release_pair(conn, ids["payment"], ids["sales_invoice"])
    assert [D(r["amount"]) for r in pair] == [D("-300.00"), D("300.00")]
    # C1: both sides delinked, so a later cancel-payment cannot re-mirror it.
    assert [r["delinked"] for r in pair] == [1, 1]

    assert _unallocated(conn, ids["payment"]) == D("300.00")

    assert "released 1 payment(s)" in out
    assert "300.00" in out
    assert "unallocated -> 300.00" in out
    assert "skipped: 0 allocation(s)" in out


def test_back_heal_skips_and_reports_a_non_submitted_payment(conn, db_path):
    """C2: a cancelled payment's allocation is left alone — and said so."""
    env = _seed_party_and_accounts(conn)
    ids = _seed_defect_install(conn, env, payment_status="cancelled")
    _rewind_to_pre031(conn)

    out = _run(db_path)

    assert _alloc_row(conn, ids["allocation"])["delinked"] == 0
    assert len(_release_pair(conn, ids["payment"], ids["sales_invoice"])) == 1
    assert _unallocated(conn, ids["payment"]) == D("0.00")   # untouched

    assert "released 0 payment(s)" in out
    assert "skipped: 1 allocation(s)" in out
    assert ids["allocation"] in out
    assert "'cancelled'" in out


def test_live_invoice_allocation_is_not_touched(conn, db_path):
    """Only allocations against CANCELLED documents are released."""
    env = _seed_party_and_accounts(conn)
    ids = _seed_defect_install(conn, env)
    conn.execute("UPDATE sales_invoice SET status = 'partially_paid', "
                 "outstanding_amount = '700.00' WHERE id = ?",
                 (ids["sales_invoice"],))
    conn.commit()
    _rewind_to_pre031(conn)

    out = _run(db_path)

    assert _alloc_row(conn, ids["allocation"])["delinked"] == 0
    assert _unallocated(conn, ids["payment"]) == D("0.00")
    assert "released 0 payment(s)" in out
    assert "skipped: 0 allocation(s)" in out


# ── migration-class properties ───────────────────────────────────────────────

def test_report_only_leaves_the_database_byte_identical(conn, db_path):
    """--report-only enumerates the work and writes NOTHING (dump compared)."""
    env = _seed_party_and_accounts(conn)
    ids = _seed_defect_install(conn, env)
    conn.close()                      # the report opens its own connection

    before = _dump(db_path)
    out = _run(db_path, report_only=True)
    assert _dump(db_path) == before, "--report-only wrote to the database"

    # And it named exactly the work the real run then does.
    assert "would release 1 payment(s)" in out
    assert ids["allocation"] in out
    assert "300.00" in out

    _run(db_path)
    probe = sqlite3.connect(db_path)
    probe.row_factory = sqlite3.Row
    try:
        assert _alloc_row(probe, ids["allocation"])["delinked"] == 1
        assert _unallocated(probe, ids["payment"]) == D("300.00")
    finally:
        probe.close()


def test_second_real_run_is_a_no_op(conn, db_path):
    """Idempotent: nothing left to heal, so nothing is written."""
    env = _seed_party_and_accounts(conn)
    _seed_defect_install(conn, env)
    _rewind_to_pre031(conn)
    _run(db_path)
    conn.close()

    after_first = _dump(db_path)
    out = _run(db_path)
    assert _dump(db_path) == after_first, "the second run changed the database"
    assert "released 0 payment(s)" in out


def test_report_only_on_a_pre_column_install_enumerates_and_writes_nothing(conn, db_path):
    """The real workflow: report FIRST, on an install that still lacks the column.

    Report mode runs no DDL, and it still lists exactly the rows the real run
    will heal (pre-031 every allocation is live by definition, so the liveness
    filter is simply not applied). A report that could not name a row on the
    installs that need the heal would be a report in name only.
    """
    env = _seed_party_and_accounts(conn)
    ids = _seed_defect_install(conn, env)
    skip_ids = _seed_defect_install(conn, env, payment_status="cancelled")
    _rewind_to_pre031(conn)
    conn.close()

    before = _dump(db_path)
    out = _run(db_path, report_only=True)
    assert _dump(db_path) == before
    assert "would be added" in out
    assert "would release 1 payment(s)" in out
    assert ids["allocation"] in out
    assert "300.00" in out
    assert "skipped: 1 allocation(s)" in out
    assert skip_ids["allocation"] in out

    # The real run then does exactly what was reported.
    real = _run(db_path)
    assert "released 1 payment(s)" in real
    assert ids["allocation"] in real
    assert skip_ids["allocation"] in real


# ── the audit trail (M102) ───────────────────────────────────────────────────
#
# This migration delinks allocations and rewrites a residual. Which allocations
# it voided is the one fact a reversal needs and, until M102, the only place it
# lived was a line printed to a terminal.
# SIM: planning/simlogs/m102_SIM_2026-08-12.md.

def _reader(db_path):
    """A read connection through the seam (ADR-0034), not sqlite3 directly.

    The tests above predate the seam and open sqlite3 themselves; the ratchet
    counts those and this file may not add to them.
    """
    from erpclaw_lib.db import get_connection
    return get_connection(db_path)


def _trail(db_path):
    """Every audit_log row this migration wrote, as dicts with parsed JSON."""
    conn = _reader(db_path)
    try:
        cur = conn.execute(
            "SELECT skill, entity_type, entity_id, old_values, new_values, "
            "description FROM audit_log WHERE action = ? ORDER BY timestamp, id",
            ("migration:" + mig.MIGRATION_ID,))
        columns = [d[0] for d in cur.description]
        rows = [dict(zip(columns, tuple(r))) for r in cur.fetchall()]
    finally:
        conn.close()
    for row in rows:
        for key in ("old_values", "new_values"):
            row[key] = json.loads(row[key]) if row[key] else None
    return rows


def test_the_trail_names_the_allocations_it_delinked(conn, db_path):
    """The row-level check: the trail is compared against the ACTUAL delinked
    set, not against what the migration reported."""
    env = _seed_party_and_accounts(conn)
    ids = _seed_defect_install(conn, env)
    _rewind_to_pre031(conn)
    conn.close()

    _run(db_path)

    rows = _trail(db_path)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["skill"] == "erpclaw-setup"
    assert row["entity_type"] == "payment_entry"
    assert row["entity_id"] == ids["payment"]

    check = _reader(db_path)
    try:
        delinked = {r[0] for r in check.execute(
            "SELECT id FROM payment_allocation WHERE delinked = 1").fetchall()}
        residual = check.execute(
            "SELECT unallocated_amount FROM payment_entry WHERE id = ?",
            (ids["payment"],)).fetchone()[0]
    finally:
        check.close()
    assert set(row["new_values"]["delinked_allocations"]) == delinked
    assert row["new_values"]["unallocated_amount"] == residual
    assert row["old_values"]["unallocated_amount"] == "0.00"
    assert row["new_values"]["ple_release_pairs"] == 1


def test_a_skipped_payment_gets_no_trail(conn, db_path):
    """Nothing changed for it, so a row would be a lie about a payment this
    migration deliberately refused to touch."""
    env = _seed_party_and_accounts(conn)
    _seed_defect_install(conn, env, payment_status="cancelled")
    _rewind_to_pre031(conn)
    conn.close()

    _run(db_path)

    assert _trail(db_path) == []


def test_report_only_writes_no_trail(conn, db_path):
    env = _seed_party_and_accounts(conn)
    _seed_defect_install(conn, env)
    _rewind_to_pre031(conn)
    conn.close()

    out = _run(db_path, report_only=True)

    assert _trail(db_path) == []
    assert "report-only: no audit_log row is written" in out


def test_the_trail_does_not_duplicate_on_a_second_run(conn, db_path):
    env = _seed_party_and_accounts(conn)
    _seed_defect_install(conn, env)
    _rewind_to_pre031(conn)
    conn.close()

    _run(db_path)
    first = _trail(db_path)
    assert len(first) == 1

    _run(db_path)

    assert _trail(db_path) == first


def test_the_migration_id_is_the_stem_the_runner_ledgers_it_under():
    """`migration_runner.discover` ledgers this file under `fn[:-3]` and the trail
    is retrieved by `migration:<that stem>`.

    Pinned BY VALUE. `_trail()` above derives its query from `mig.MIGRATION_ID`,
    as it must, so it agrees with ANY value the module ends up holding: a second
    assignment overwriting the derived stem passed every test in this file. The
    L0 gate rejects the reassignment; this says what the stem is.
    """
    assert mig.MIGRATION_ID == "031_allocation_delink_and_release"
    assert mig.MIGRATION_DATA_CLASS == "rows"
