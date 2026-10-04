"""Part A - migration 052: pre-change credit notes to the new ledger shape.

Every legacy note is reshaped (value-neutral) and, where the application can
be proven from the ledger, applied to its original. Books are built with the
real selling/payments actions; a note is put into the pre-change shape by
 `_legacy_note` (submit with the allocator stubbed out, then the single
ledger row is repointed at the invoice and the outstanding reset to the
grand total - exactly what the pre-change writer left).

Tests 9-12, 14, 15 and 17-20 (m784b) fill the gaps: a cancelled original,
a partly refunded note, three notes on one invoice, a currency mismatch, the
install-phase ledger check, PostgreSQL, a legacy note beside a new one, and
the rehearsal-review branches (nothing to apply with mixed notes, note-side
preconditions, a written-off plus side-door original). The numbering is kept.
"""
import importlib.util
import io
import json
import os
import runpy
import shutil
import sqlite3
import sys
from contextlib import redirect_stdout
from decimal import Decimal

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SETUP_DIR = os.path.dirname(_TESTS_DIR)
_MIG_DIR = os.path.join(_SETUP_DIR, "migrations")
_SELL_TESTS = os.path.abspath(os.path.join(
    _SETUP_DIR, "..", "erpclaw-selling", "tests"))
if _SELL_TESTS not in sys.path:
    sys.path.insert(0, _SELL_TESTS)

from selling_helpers import (  # noqa: E402
    build_selling_env,
    call_action,
    get_conn as _selling_get_conn,
    init_all_tables as _selling_init,
    load_db_query,
    ns,
    is_ok,
    seed_account,
)

sell = load_db_query()

_PAY_PATH = os.path.abspath(os.path.join(
    _SETUP_DIR, "..", "erpclaw-payments", "db_query.py"))
_spec = importlib.util.spec_from_file_location("db_query_payments_cna", _PAY_PATH)
pay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pay)

from erpclaw_lib import payment_clearing  # noqa: E402
from erpclaw_lib.db import integrity_error_types  # noqa: E402

try:
    from erpclaw_lib.db import get_dialect  # noqa: E402
except ImportError:
    def get_dialect():
        return os.environ.get("ERPCLAW_DB_DIALECT", "sqlite")


def _load_mig(name="migration_052_credit_note_allocation"):
    path = os.path.join(_MIG_DIR, "052_credit_note_allocation.py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_INV_PATH = os.path.normpath(os.path.join(
    _TESTS_DIR, "..", "..", "..", "..", "..",
    "testing", "invariant_engine.py"))

inv_engine = None


def _require_engine():
    global inv_engine
    if inv_engine is None:
        if not os.path.exists(_INV_PATH):
            pytest.fail("invariant engine not found at %s" % _INV_PATH)
        _spec = importlib.util.spec_from_file_location(
            "invariant_engine_cna", _INV_PATH)
        inv_engine = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(inv_engine)
    return inv_engine


def _green(conn):
    if get_dialect() == "postgresql":
        return
    eng = _require_engine()
    eng._ensure_decimal_sum(conn)
    assert eng._check_inv22_payment_invoice_reconciliation(conn) is None
    assert eng._check_inv25_ar_summary_detail(conn) is None
    assert eng._check_inv27_party_level_residual(conn) is None


def _dump(db_path):
    probe = sqlite3.connect(db_path)
    try:
        return "\n".join(probe.iterdump())
    finally:
        probe.close()


def _run(mod, db_path, report_only=False):
    buf = io.StringIO()
    with redirect_stdout(buf):
        res = mod.run_migration(db_path, report_only=report_only,
                                run_date="2026-09-30")
    return res, buf.getvalue()


def _invoice(conn, env, qty="10"):
    create = call_action(
        sell.create_sales_invoice, conn,
        ns(sales_order_id=None, delivery_note_id=None,
           customer_id=env["customer"], company_id=env["company_id"],
           posting_date="2026-06-20", due_date="2026-07-20",
           items=json.dumps([{"item_id": env["item1"], "qty": qty,
                              "rate": "100.00",
                              "warehouse_id": env["warehouse"]}]),
           tax_template_id=None, payment_terms_id=None))
    assert is_ok(create), create
    si_id = create["sales_invoice_id"]
    r = call_action(sell.submit_sales_invoice, conn,
                    ns(sales_invoice_id=si_id))
    assert is_ok(r), r
    return si_id


def _pay(conn, env, inv, amount):
    created = call_action(
        pay.add_payment, conn,
        ns(company_id=env["company_id"], payment_type="receive",
           posting_date="2026-06-25", party_type="customer",
           party_id=env["customer"], paid_from_account=env["ar"],
           paid_to_account=env["cash"], paid_amount=amount,
           exchange_rate=None, payment_currency=None,
           reference_number=None, reference_date=None,
           allocations=json.dumps([{"voucher_type": "sales_invoice",
                                    "voucher_id": inv,
                                    "allocated_amount": amount}]),
           deductions=None))
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    s = call_action(pay.submit_payment, conn, ns(payment_entry_id=pe_id))
    assert is_ok(s), s
    return pe_id


def _legacy_note(conn, env, inv, qty, monkeypatch,
                 posting_date="2026-06-28"):
    cn = call_action(
        sell.create_credit_note, conn,
        ns(against_invoice_id=inv, reason="Returned goods",
           posting_date=posting_date,
           items=json.dumps([{"item_id": env["item1"], "qty": qty,
                              "rate": "100.00"}])))
    assert is_ok(cn), cn
    cn_id = cn["credit_note_id"]

    def _noop(*args, **kwargs):
        return {}

    monkeypatch.setattr(
        payment_clearing, "allocate_return_to_document", _noop)
    try:
        r = call_action(sell.submit_sales_invoice, conn,
                        ns(sales_invoice_id=cn_id))
    finally:
        monkeypatch.undo()
    assert is_ok(r), r
    rows = conn.execute(
        "SELECT id FROM payment_ledger_entry "
        "WHERE voucher_type = 'credit_note' AND voucher_id = ? "
        "AND delinked = 0", (cn_id,)).fetchall()
    assert len(rows) == 1
    conn.execute(
        "UPDATE payment_ledger_entry SET against_voucher_type = "
        "'sales_invoice', against_voucher_id = ?, currency = 'USD' "
        "WHERE id = ?", (inv, rows[0]["id"]))
    grow = conn.execute(
        "SELECT grand_total FROM sales_invoice WHERE id = ?",
        (cn_id,)).fetchone()
    conn.execute(
        "UPDATE sales_invoice SET outstanding_amount = ? WHERE id = ?",
        (grow["grand_total"], cn_id))
    conn.commit()
    return cn_id


def _doc(conn, doc_id):
    r = conn.execute(
        "SELECT outstanding_amount, status FROM sales_invoice WHERE id = ?",
        (doc_id,)).fetchone()
    return (r["outstanding_amount"], r["status"])


def _note_rows(conn, cn_id):
    rows = conn.execute(
        "SELECT amount, against_voucher_type, against_voucher_id, "
        "posting_date FROM payment_ledger_entry "
        "WHERE voucher_type = 'credit_note' AND voucher_id = ? "
        "AND delinked = 0 ORDER BY posting_date, amount, id",
        (cn_id,)).fetchall()
    return [(r["amount"], r["against_voucher_type"],
             r["against_voucher_id"], r["posting_date"]) for r in rows]


def _party_sums(conn):
    out = {}
    for r in conn.execute(
            "SELECT party_type, party_id, amount, voucher_type, delinked "
            "FROM payment_ledger_entry").fetchall():
        if r["voucher_type"] == "payment_entry" or r["delinked"] == 0:
            key = (r["party_type"], r["party_id"])
            out[key] = out.get(key, Decimal("0")) + Decimal(str(r["amount"]))
    return {k: str(v) for k, v in out.items()}


def _gl_dump(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT * FROM gl_entry ORDER BY id").fetchall()]


def _mig_audit_rows(conn, stem):
    rows = conn.execute(
        "SELECT entity_type, entity_id, old_values, new_values, "
        "description FROM audit_log WHERE action = ?",
        ("migration:" + stem,)).fetchall()
    return [dict(r) for r in rows]


def test_migration_id_is_the_file_stem():
    mig = _load_mig()
    assert mig.MIGRATION_ID == "052_credit_note_allocation"
    assert mig.MIGRATION_DATA_CLASS == "rows"
    names = sorted(os.listdir(_MIG_DIR))
    assert sum(1 for n in names if n.startswith("052_")) == 1


def test_fresh_install_and_new_shape_books_are_untouched(conn, db_path):
    mig = _load_mig()
    before = _dump(db_path)
    res = mig.run_migration(db_path, run_date="2026-09-30")
    assert res["returns"] == []
    assert _dump(db_path) == before
    assert _mig_audit_rows(conn, mig.MIGRATION_ID) == []
    rep = mig.run_migration(db_path, report_only=True,
                            run_date="2026-09-30")
    assert rep["returns"] == []
    assert _dump(db_path) == before

    env = build_selling_env(conn)
    inv = _invoice(conn, env)
    _pay(conn, env, inv, "400.00")
    cn = call_action(
        sell.create_credit_note, conn,
        ns(against_invoice_id=inv, reason="Returned goods",
           posting_date="2026-06-28",
           items=json.dumps([{"item_id": env["item1"], "qty": "2",
                              "rate": "100.00"}])))
    assert is_ok(cn), cn
    r = call_action(sell.submit_sales_invoice, conn,
                    ns(sales_invoice_id=cn["credit_note_id"]))
    assert is_ok(r), r
    _green(conn)
    before = _dump(db_path)
    res = mig.run_migration(db_path, run_date="2026-09-30")
    assert res["returns"] == []
    assert _dump(db_path) == before
    assert _mig_audit_rows(conn, mig.MIGRATION_ID) == []
    rep = mig.run_migration(db_path, report_only=True,
                            run_date="2026-09-30")
    assert rep["returns"] == []
    assert _dump(db_path) == before


def test_legacy_book_is_converted(conn, db_path, monkeypatch):
    mig = _load_mig()
    env = build_selling_env(conn)
    inv = _invoice(conn, env)
    _pay(conn, env, inv, "400.00")
    cn = _legacy_note(conn, env, inv, "2", monkeypatch)
    assert _doc(conn, inv) == ("600.00", "partially_paid")
    assert _doc(conn, cn) == ("-200.00", "submitted")
    msg = _require_engine()._check_inv25_ar_summary_detail(conn)
    assert msg is not None
    assert inv[:8] in msg and cn[:8] in msg
    parties_before = _party_sums(conn)
    gl_before = _gl_dump(conn)

    res, _ = _run(mig, db_path)
    assert len(res["returns"]) == 1
    entry = res["returns"][0]
    assert entry["credit_note_id"] == cn
    assert entry["original_id"] == inv
    assert entry["outcome"] == "applied"
    assert entry["applied"] == "200.00"

    assert _doc(conn, inv) == ("400.00", "partially_paid")
    assert _doc(conn, cn) == ("0", "submitted")
    assert entry["note_after"] == {"outstanding_amount": "0",
                                   "status": "submitted"}
    assert entry["original_after"] == {"outstanding_amount": "400.00",
                                       "status": "partially_paid"}
    rows = _note_rows(conn, cn)
    assert sorted(rows) == sorted(
        [("-200.00", "sales_invoice", inv, "2026-06-28"),
         ("200.00", "sales_invoice", inv, "2026-06-28"),
         ("-200.00", "credit_note", cn, "2026-06-28"),
         ("200.00", "credit_note", cn, "2026-09-30"),
         ("-200.00", "sales_invoice", inv, "2026-09-30")])
    _green(conn)
    assert _party_sums(conn) == parties_before
    assert _gl_dump(conn) == gl_before
    audits = _mig_audit_rows(conn, mig.MIGRATION_ID)
    assert {(a["entity_type"], a["entity_id"]) for a in audits} == {
        ("sales_invoice", cn), ("sales_invoice", inv)}
    assert len(audits) == 2
    by_entity = {a["entity_id"]: a for a in audits}
    for a in audits:
        new_values = json.loads(a["new_values"])
        assert isinstance(new_values["appended_ledger_rows"], list)
        assert new_values["appended_ledger_rows"]
        for item in new_values["appended_ledger_rows"]:
            assert set(item) == {"id", "amount"}
            assert item["id"] not in (a["description"] or "")
    note_new = json.loads(by_entity[cn]["new_values"])
    assert sorted(i["amount"] for i in
                  note_new["appended_ledger_rows"]) == \
        ["-200.00", "-200.00", "200.00", "200.00"]
    orig_new = json.loads(by_entity[inv]["new_values"])
    assert [i["amount"] for i in
            orig_new["appended_ledger_rows"]] == ["-200.00"]
    assert "status" not in orig_new
    assert orig_new["outstanding_amount"] == "400.00"
    orig_old = json.loads(by_entity[inv]["old_values"])
    assert orig_old["outstanding_amount"] == "600.00"
    assert "status" not in orig_old
    appended_ids = [i["id"] for i in
                    note_new["appended_ledger_rows"]]
    shape_remarks = ("Credit note allocation shape "
                     "(migration 052_credit_note_allocation)")
    alloc_remarks = ("Credit note allocation "
                     "(migration 052_credit_note_allocation)")
    live = conn.execute(
        "SELECT id, account_id, party_id, amount, "
        "amount_in_account_currency, currency, remarks "
        "FROM payment_ledger_entry WHERE id IN (?, ?, ?, ?)",
        tuple(appended_ids)).fetchall()
    assert len(live) == 4
    assert len([r for r in live
                if r["remarks"] == shape_remarks]) == 2
    assert len([r for r in live
                if r["remarks"] == alloc_remarks]) == 2
    for r in live:
        assert r["account_id"] == env["ar"]
        assert r["party_id"] == env["customer"]
        assert r["currency"] == "USD"
        assert r["amount_in_account_currency"] == r["amount"]


def test_second_run_writes_nothing(conn, db_path, monkeypatch):
    mig = _load_mig()
    env = build_selling_env(conn)
    inv = _invoice(conn, env)
    _pay(conn, env, inv, "400.00")
    _legacy_note(conn, env, inv, "2", monkeypatch)
    mig.run_migration(db_path, run_date="2026-09-30")
    before = _dump(db_path)
    res = mig.run_migration(db_path, run_date="2026-09-30")
    assert res["returns"] == []
    assert _dump(db_path) == before
    _green(conn)


def test_report_only_writes_nothing_and_predicts_the_run(
        conn, db_path, monkeypatch, tmp_path):
    mig = _load_mig()
    env = build_selling_env(conn)
    inv = _invoice(conn, env)
    _pay(conn, env, inv, "400.00")
    cn = _legacy_note(conn, env, inv, "2", monkeypatch)
    conn.commit()
    copy_path = str(tmp_path / "copy.sqlite")
    _selling_init(copy_path)
    copy_conn = _selling_get_conn(copy_path)
    try:
        copy_env = build_selling_env(copy_conn)
        copy_inv = _invoice(copy_conn, copy_env)
        _pay(copy_conn, copy_env, copy_inv, "400.00")
        copy_cn = _legacy_note(copy_conn, copy_env, copy_inv, "2",
                               monkeypatch)
        copy_conn.commit()
        copy_before = _dump(copy_path)
        rep = mig.run_migration(copy_path, report_only=True,
                                run_date="2026-09-30")
        assert _dump(copy_path) == copy_before
        assert copy_conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = ?",
            ("migration:" + mig.MIGRATION_ID,)).fetchone()[0] == 0
        assert len(rep["returns"]) == 1
        assert rep["returns"][0]["credit_note_id"] == copy_cn
        assert rep["returns"][0]["original_id"] == copy_inv
    finally:
        copy_conn.close()
    assert len(rep["returns"]) == 1
    assert rep["returns"][0]["outcome"] == "applied"
    real = mig.run_migration(db_path, run_date="2026-09-30")
    assert len(real["returns"]) == 1
    assert rep["returns"][0]["outcome"] == real["returns"][0]["outcome"]
    assert rep["returns"][0]["applied"] == real["returns"][0]["applied"]
    assert rep["returns"][0]["note_after"] == real["returns"][0]["note_after"]
    assert (rep["returns"][0]["original_after"] ==
            real["returns"][0]["original_after"])
    assert rep["returns"][0]["note_after"] == {
        "outstanding_amount": _doc(conn, cn)[0],
        "status": _doc(conn, cn)[1]}
    assert rep["returns"][0]["original_after"] == {
        "outstanding_amount": _doc(conn, inv)[0],
        "status": _doc(conn, inv)[1]}
    _green(conn)


def test_paid_before_the_credit_is_reshaped_not_applied(
        conn, db_path, monkeypatch):
    mig = _load_mig()
    env = build_selling_env(conn)
    inv = _invoice(conn, env)
    _pay(conn, env, inv, "1000.00")
    assert _doc(conn, inv) == ("0", "paid")
    cn = _legacy_note(conn, env, inv, "2", monkeypatch)
    res = mig.run_migration(db_path, run_date="2026-09-30")
    assert len(res["returns"]) == 1
    entry = res["returns"][0]
    assert entry["outcome"] == "not_applied"
    assert entry["reason"] == "original_status_paid"
    assert _doc(conn, inv) == ("0", "paid")
    assert _doc(conn, cn) == ("-200.00", "submitted")
    rows = _note_rows(conn, cn)
    assert sorted(rows) == sorted(
        [("-200.00", "sales_invoice", inv, "2026-06-28"),
         ("200.00", "sales_invoice", inv, "2026-06-28"),
         ("-200.00", "credit_note", cn, "2026-06-28")])
    _green(conn)
    if get_dialect() != "postgresql":
        _require_engine()._ensure_decimal_sum(conn)
        assert _require_engine()._check_inv22_payment_invoice_reconciliation(
            conn) is None


def test_side_door_is_reshaped_not_applied(conn, db_path, monkeypatch):
    mig = _load_mig()
    env = build_selling_env(conn)
    inv = _invoice(conn, env)
    _pay(conn, env, inv, "400.00")
    cn = _legacy_note(conn, env, inv, "2", monkeypatch)
    conn.execute(
        "INSERT INTO payment_ledger_entry (id, posting_date, account_id, "
        "party_type, party_id, voucher_type, voucher_id, "
        "against_voucher_type, against_voucher_id, amount, "
        "amount_in_account_currency, currency, delinked, remarks) "
        "VALUES (?, '2026-06-29', ?, 'customer', ?, 'sales_invoice', ?, "
        "'sales_invoice', ?, '-200.00', '-200.00', 'USD', 0, ?)",
        ("00000000-0000-4000-8000-%012d" % 7, env["ar"],
         env["customer"], inv, inv,
         "Payment applied to sales_invoice %s via "
         "update-invoice-outstanding" % inv))
    conn.execute(
        "UPDATE sales_invoice SET outstanding_amount = '400.00', "
        "status = 'partially_paid' WHERE id = ?", (inv,))
    conn.commit()
    res = mig.run_migration(db_path, run_date="2026-09-30")
    assert len(res["returns"]) == 1
    entry = res["returns"][0]
    assert entry["outcome"] == "not_applied"
    assert entry["reason"] == "original_own_row_count_2"
    side_id = "00000000-0000-4000-8000-%012d" % 7
    assert entry["extra_own_rows"] == [{
        "id": side_id, "amount": "-200.00",
        "remarks": ("Payment applied to sales_invoice %s via "
                    "update-invoice-outstanding" % inv),
        "side_door": True}]
    assert _doc(conn, inv) == ("400.00", "partially_paid")
    assert _doc(conn, cn) == ("-200.00", "submitted")
    _green(conn)


def test_written_off_original_is_applied(conn, db_path, monkeypatch):
    mig = _load_mig()
    env = build_selling_env(conn)
    inv = _invoice(conn, env)
    exp = seed_account(conn, env["company_id"], "Bad Debts", "expense")
    w = call_action(pay.write_off_invoice, conn,
                    ns(voucher_type="sales_invoice", voucher_id=inv,
                       write_off_amount="100.00",
                       write_off_account_id=exp,
                       reason="Customer insolvent", posting_date=None,
                       cost_center_id=None))
    assert is_ok(w), w
    cn = _legacy_note(conn, env, inv, "2", monkeypatch)
    res = mig.run_migration(db_path, run_date="2026-09-30")
    assert len(res["returns"]) == 1
    entry = res["returns"][0]
    assert entry["outcome"] == "applied"
    assert entry["applied"] == "200.00"
    assert entry["extra_own_rows"] == []
    assert _doc(conn, inv) == ("700.00", "partially_paid")
    assert _doc(conn, cn) == ("0", "submitted")
    _green(conn)
    if get_dialect() != "postgresql":
        _require_engine()._ensure_decimal_sum(conn)
        assert _require_engine()._check_inv22_payment_invoice_reconciliation(
            conn) is None


def test_runner_applies_it_once(conn, db_path, monkeypatch, tmp_path):
    mig = _load_mig()
    env = build_selling_env(conn)
    inv = _invoice(conn, env)
    _pay(conn, env, inv, "400.00")
    cn = _legacy_note(conn, env, inv, "2", monkeypatch)
    runner_path = os.path.join(_SETUP_DIR, "migration_runner.py")
    spec = importlib.util.spec_from_file_location(
        "migration_runner_cna", runner_path)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    shutil.copy(os.path.join(_MIG_DIR, "052_credit_note_allocation.py"),
                str(mig_dir / "052_credit_note_allocation.py"))
    first = runner.run_pending(db_path, migrations_dir=str(mig_dir))
    assert first["ok"] is True
    assert first["applied"] == ["052_credit_note_allocation"]
    assert _doc(conn, inv) == ("400.00", "partially_paid")
    assert _doc(conn, cn) == ("0", "submitted")
    assert conn.execute(
        "SELECT status FROM erpclaw_schema_migration WHERE id = ?",
        ("052_credit_note_allocation",)).fetchone()[0] == "applied"
    second = runner.run_pending(db_path, migrations_dir=str(mig_dir))
    assert second["ok"] is True
    assert second["applied"] == []
    _green(conn)


def test_a_failed_run_leaves_no_trail(conn, db_path, monkeypatch):
    mig = _load_mig()
    env = build_selling_env(conn)
    inv = _invoice(conn, env)
    _pay(conn, env, inv, "400.00")
    cn1 = _legacy_note(conn, env, inv, "2", monkeypatch,
                       posting_date="2026-06-26")
    cn2 = _legacy_note(conn, env, inv, "3", monkeypatch,
                       posting_date="2026-06-27")
    cn3 = _legacy_note(conn, env, inv, "1.5", monkeypatch,
                       posting_date="2026-06-28")
    before = _dump(db_path)
    real_new_id = mig._new_id
    seen = []

    def _failing_new_id():
        if len(seen) >= 4:
            return seen[0]
        value = real_new_id()
        seen.append(value)
        return value

    monkeypatch.setattr(mig, "_new_id", _failing_new_id)
    with pytest.raises(integrity_error_types()):
        mig.run_migration(db_path, run_date="2026-09-30")
    assert len(seen) == 4
    assert _dump(db_path) == before
    assert _doc(conn, inv) == ("600.00", "partially_paid")
    assert _doc(conn, cn1) == ("-200.00", "submitted")
    assert _doc(conn, cn2) == ("-300.00", "submitted")
    assert _doc(conn, cn3) == ("-150.00", "submitted")


def _refund(conn, env, cn, amount):
    if "bank" not in env:
        env["bank"] = seed_account(
            conn, env["company_id"], "Bank", "asset", "bank", "1010")
    created = call_action(
        pay.add_payment, conn,
        ns(company_id=env["company_id"], payment_type="pay",
           posting_date="2026-06-29", party_type="customer",
           party_id=env["customer"],
           paid_from_account=env["bank"],
           paid_to_account=env["ar"],
           paid_amount=amount,
           exchange_rate=None, payment_currency=None,
           reference_number=None, reference_date=None,
           allocations=json.dumps(
               [{"voucher_type": "credit_note", "voucher_id": cn,
                 "allocated_amount": amount}]),
           deductions=None))
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    s = call_action(pay.submit_payment, conn, ns(payment_entry_id=pe_id))
    assert is_ok(s), s
    return pe_id


def test_cancelled_original_is_reshaped_only(conn, db_path, monkeypatch):
    mig = _load_mig()
    env = build_selling_env(conn)
    inv = _invoice(conn, env)
    cn = _legacy_note(conn, env, inv, "2", monkeypatch)
    refused = call_action(sell.cancel_sales_invoice, conn,
                          ns(sales_invoice_id=inv))
    assert not is_ok(refused), refused
    conn.execute(
        "UPDATE sales_invoice SET status = 'cancelled', "
        "outstanding_amount = '0' WHERE id = ?", (inv,))
    conn.execute(
        "UPDATE payment_ledger_entry SET delinked = 1 "
        "WHERE voucher_type = 'sales_invoice' AND voucher_id = ? "
        "AND delinked = 0", (inv,))
    conn.commit()
    assert _doc(conn, inv) == ("0", "cancelled")
    res = mig.run_migration(db_path, run_date="2026-09-30")
    assert len(res["returns"]) == 1
    entry = res["returns"][0]
    assert entry["outcome"] == "not_applied"
    assert entry["reason"] == "original_status_cancelled"
    assert _doc(conn, inv) == ("0", "cancelled")
    assert _doc(conn, cn) == ("-200.00", "submitted")
    rows = _note_rows(conn, cn)
    assert sorted(rows) == sorted(
        [("-200.00", "sales_invoice", inv, "2026-06-28"),
         ("200.00", "sales_invoice", inv, "2026-06-28"),
         ("-200.00", "credit_note", cn, "2026-06-28")])
    _green(conn)


def test_partly_refunded_legacy_note(conn, db_path, monkeypatch):
    mig = _load_mig()
    env = build_selling_env(conn)
    inv = _invoice(conn, env)
    _pay(conn, env, inv, "900.00")
    cn = _legacy_note(conn, env, inv, "2", monkeypatch)
    _refund(conn, env, cn, "50.00")
    assert _doc(conn, cn) == ("-150.00", "partially_paid")
    res = mig.run_migration(db_path, run_date="2026-09-30")
    assert len(res["returns"]) == 1
    entry = res["returns"][0]
    assert entry["outcome"] == "applied"
    assert entry["applied"] == "100.00"
    assert _doc(conn, inv) == ("0", "paid")
    assert _doc(conn, cn) == ("-50.00", "partially_paid")
    _green(conn)


def test_three_legacy_notes_on_one_invoice(conn, db_path, monkeypatch):
    mig = _load_mig()
    env = build_selling_env(conn)
    inv = _invoice(conn, env)
    _pay(conn, env, inv, "400.00")
    cn1 = _legacy_note(conn, env, inv, "2", monkeypatch,
                       posting_date="2026-06-26")
    cn2 = _legacy_note(conn, env, inv, "3", monkeypatch,
                       posting_date="2026-06-27")
    cn3 = _legacy_note(conn, env, inv, "1.5", monkeypatch,
                       posting_date="2026-06-28")
    res = mig.run_migration(db_path, run_date="2026-09-30")
    assert len(res["returns"]) == 3
    by_id = {entry["credit_note_id"]: entry for entry in res["returns"]}
    assert by_id[cn1]["outcome"] == "applied"
    assert by_id[cn1]["applied"] == "200.00"
    assert by_id[cn2]["outcome"] == "applied"
    assert by_id[cn2]["applied"] == "300.00"
    assert by_id[cn3]["outcome"] == "applied"
    assert by_id[cn3]["applied"] == "100.00"
    assert _doc(conn, inv) == ("0", "paid")
    assert _doc(conn, cn1) == ("0", "submitted")
    assert _doc(conn, cn2) == ("0", "submitted")
    assert _doc(conn, cn3) == ("-50.00", "submitted")
    assert by_id[cn1]["original_before"] == {
        "outstanding_amount": "600.00", "status": "partially_paid"}
    assert by_id[cn1]["original_after"] == {
        "outstanding_amount": "400.00", "status": "partially_paid"}
    assert by_id[cn2]["original_before"] == by_id[cn1]["original_after"]
    assert by_id[cn2]["original_after"] == {
        "outstanding_amount": "100.00", "status": "partially_paid"}
    assert by_id[cn3]["original_before"] == by_id[cn2]["original_after"]
    assert by_id[cn3]["original_after"] == {
        "outstanding_amount": "0", "status": "paid"}
    audits = conn.execute(
        "SELECT entity_type, entity_id FROM audit_log WHERE action = ? "
        "ORDER BY rowid",
        ("migration:" + mig.MIGRATION_ID,)).fetchall()
    pairs = [(row["entity_type"], row["entity_id"]) for row in audits]
    assert set(pairs) == {
        ("sales_invoice", cn1), ("sales_invoice", cn2),
        ("sales_invoice", cn3), ("sales_invoice", inv)}
    assert len(pairs) == 4
    assert pairs.index(("sales_invoice", inv)) > max(
        pairs.index(("sales_invoice", cn1)),
        pairs.index(("sales_invoice", cn2)),
        pairs.index(("sales_invoice", cn3)))
    _green(conn)


def test_mismatched_currency_is_not_applied(conn, db_path, monkeypatch):
    mig = _load_mig()
    env = build_selling_env(conn)
    inv = _invoice(conn, env)
    _pay(conn, env, inv, "400.00")
    cn = _legacy_note(conn, env, inv, "2", monkeypatch)
    conn.execute(
        "UPDATE sales_invoice SET currency = 'EUR' WHERE id = ?", (inv,))
    conn.commit()
    res = mig.run_migration(db_path, run_date="2026-09-30")
    assert len(res["returns"]) == 1
    entry = res["returns"][0]
    assert entry["outcome"] == "not_applied"
    assert entry["reason"] == "currency_mismatch"
    assert _doc(conn, inv) == ("600.00", "partially_paid")
    assert _doc(conn, cn) == ("-200.00", "submitted")
    _green(conn)


def test_ledger_check_by_install_phase(conn, db_path, monkeypatch):
    from erpclaw_lib import authority_gate as _authority_gate
    from erpclaw_lib import authority_sink as _authority_sink
    from erpclaw_lib.db import get_connection as _fresh_connection
    assert "payment" not in _authority_sink.ENFORCED_FAMILIES
    mig = _load_mig()
    env = build_selling_env(conn)
    inv = _invoice(conn, env)
    _pay(conn, env, inv, "400.00")
    cn = _legacy_note(conn, env, inv, "2", monkeypatch)
    conn.commit()

    def _fresh_doc(doc_id):
        handle = _fresh_connection(db_path)
        try:
            row = handle.execute(
                "SELECT outstanding_amount, status FROM sales_invoice "
                "WHERE id = ?", (doc_id,)).fetchone()
            return (row["outstanding_amount"], row["status"])
        finally:
            try:
                handle.close()
            except Exception:
                pass

    handle = _fresh_connection(db_path)
    try:
        assert handle.execute(
            "SELECT COUNT(*) AS n FROM authority_install").fetchone()["n"] == 1
        assert _authority_gate.install_phase(handle)[0] == "STAGED"
    finally:
        try:
            handle.close()
        except Exception:
            pass
    flip_handle = _fresh_connection(db_path)
    try:
        flip_handle.execute(
            "UPDATE authority_install SET phase = ?", ("ACTIVE",))
        flip_handle.commit()
    finally:
        try:
            flip_handle.close()
        except Exception:
            pass
    try:
        res = mig.run_migration(db_path, run_date="2026-09-30")
    except Exception as exc:
        probe_handle = _fresh_connection(db_path)
        try:
            phase_now = _authority_gate.install_phase(probe_handle)[0]
        finally:
            try:
                probe_handle.close()
            except Exception:
                pass
        pytest.fail("migration refused at phase %s: %r" % (phase_now, exc))
    assert len(res["returns"]) == 1
    entry = res["returns"][0]
    assert entry["outcome"] == "applied"
    assert entry["applied"] == "200.00"
    assert _fresh_doc(inv) == ("400.00", "partially_paid")
    assert _fresh_doc(cn) == ("0", "submitted")
    green_handle = _fresh_connection(db_path)
    try:
        _green(green_handle)
    finally:
        try:
            green_handle.close()
        except Exception:
            pass


_PG_URL = os.environ.get("ERPCLAW_PG_TEST_URL")


def _pg_table_snapshot(target, table):
    from erpclaw_lib.db import get_connection as _snapshot_connection
    if table == "payment_ledger_entry":
        statement = "SELECT * FROM payment_ledger_entry"
    elif table == "sales_invoice":
        statement = "SELECT * FROM sales_invoice"
    else:
        statement = "SELECT * FROM audit_log"
    handle = _snapshot_connection(target)
    try:
        rows = handle.execute(statement).fetchall()
        return (len(rows), sorted(
            tuple("" if value is None else str(value)
                  for value in tuple(row.values()))
            for row in rows))
    finally:
        try:
            handle.close()
        except Exception:
            pass


def _pg_snapshot(target):
    return {table: _pg_table_snapshot(target, table)
            for table in ("payment_ledger_entry", "sales_invoice",
                          "audit_log")}


@pytest.mark.skipif(not _PG_URL,
                    reason="ERPCLAW_PG_TEST_URL not set (live PostgreSQL required)")
def test_conversion_on_postgresql(monkeypatch):
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", _PG_URL)
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    _selling_init(None)
    conn = _selling_get_conn(None)
    try:
        env = build_selling_env(conn)
        inv = _invoice(conn, env)
        _pay(conn, env, inv, "400.00")
        cn = _legacy_note(conn, env, inv, "2", monkeypatch)
        monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
        monkeypatch.setenv("ERPCLAW_DB_URL", _PG_URL)
        assert _doc(conn, inv) == ("600.00", "partially_paid")
        assert _doc(conn, cn) == ("-200.00", "submitted")
        mig = _load_mig()
        res = mig.run_migration(_PG_URL, run_date="2026-09-30")
        assert len(res["returns"]) == 1
        entry = res["returns"][0]
        assert entry["outcome"] == "applied"
        assert entry["applied"] == "200.00"
        assert _doc(conn, inv) == ("400.00", "partially_paid")
        assert _doc(conn, cn) == ("0", "submitted")
        before = _pg_snapshot(_PG_URL)
        second = mig.run_migration(_PG_URL, run_date="2026-09-30")
        assert second["returns"] == []
        assert _pg_snapshot(_PG_URL) == before
    finally:
        conn.close()


def test_legacy_note_on_an_invoice_reduced_by_a_new_note(
        conn, db_path, monkeypatch):
    mig = _load_mig()
    env = build_selling_env(conn)
    inv = _invoice(conn, env)
    _pay(conn, env, inv, "400.00")
    legacy = _legacy_note(conn, env, inv, "2", monkeypatch)
    created = call_action(
        sell.create_credit_note, conn,
        ns(against_invoice_id=inv, reason="Returned goods",
           posting_date="2026-06-28",
           items=json.dumps([{"item_id": env["item1"], "qty": "1",
                              "rate": "100.00"}])))
    assert is_ok(created), created
    new_id = created["credit_note_id"]
    r = call_action(sell.submit_sales_invoice, conn,
                    ns(sales_invoice_id=new_id))
    assert is_ok(r), r
    assert _doc(conn, inv) == ("500.00", "partially_paid")
    assert _doc(conn, new_id) == ("0", "submitted")
    res = mig.run_migration(db_path, run_date="2026-09-30")
    assert len(res["returns"]) == 1
    entry = res["returns"][0]
    assert entry["credit_note_id"] == legacy
    assert entry["outcome"] == "not_applied"
    assert entry["reason"] == "original_outstanding_mismatch"
    assert _doc(conn, inv) == ("500.00", "partially_paid")
    assert _doc(conn, legacy) == ("-200.00", "submitted")
    assert _doc(conn, new_id) == ("0", "submitted")
    assert len(entry["appended"]) == 2
    assert len(_note_rows(conn, legacy)) == 3
    _green(conn)


def test_nothing_to_apply_and_mixed_notes(conn, db_path, monkeypatch,
                                          tmp_path):
    mig = _load_mig()
    env = build_selling_env(conn)
    inv = _invoice(conn, env)
    _pay(conn, env, inv, "400.00")
    cn = _legacy_note(conn, env, inv, "2", monkeypatch)
    _refund(conn, env, cn, "200.00")
    assert _doc(conn, cn) == ("0", "paid")
    res = mig.run_migration(db_path, run_date="2026-09-30")
    assert len(res["returns"]) == 1
    entry = res["returns"][0]
    assert entry["outcome"] == "nothing_to_apply"
    assert entry["applied"] == "0.00"
    assert len(entry["appended"]) == 2
    assert _doc(conn, inv) == ("600.00", "partially_paid")
    assert _doc(conn, cn) == ("0", "paid")
    assert sorted(_note_rows(conn, cn)) == sorted(
        [("-200.00", "sales_invoice", inv, "2026-06-28"),
         ("200.00", "sales_invoice", inv, "2026-06-28"),
         ("-200.00", "credit_note", cn, "2026-06-28")])
    audits = _mig_audit_rows(conn, mig.MIGRATION_ID)
    assert len(audits) == 1
    assert (audits[0]["entity_type"], audits[0]["entity_id"]) == (
        "sales_invoice", cn)
    assert audits[0]["old_values"] is None
    assert len(
        json.loads(audits[0]["new_values"])["appended_ledger_rows"]) == 2
    _green(conn)
    mixed_path = str(tmp_path / "mixed.sqlite")
    _selling_init(mixed_path)
    mixed_conn = _selling_get_conn(mixed_path)
    try:
        mixed_env = build_selling_env(mixed_conn)
        mixed_inv = _invoice(mixed_conn, mixed_env)
        _pay(mixed_conn, mixed_env, mixed_inv, "400.00")
        mixed_cn1 = _legacy_note(mixed_conn, mixed_env, mixed_inv, "2",
                                 monkeypatch, posting_date="2026-06-26")
        mixed_cn2 = _legacy_note(mixed_conn, mixed_env, mixed_inv, "2",
                                 monkeypatch, posting_date="2026-06-28")
        mixed_conn.execute(
            "INSERT INTO payment_ledger_entry (id, posting_date, account_id, "
            "party_type, party_id, voucher_type, voucher_id, "
            "against_voucher_type, against_voucher_id, amount, "
            "amount_in_account_currency, currency, delinked, remarks) "
            "VALUES ('00000000-0000-4000-8000-000000000021', '2026-06-28', ?, "
            "'customer', ?, 'credit_note', ?, NULL, NULL, '-10.00', "
            "'-10.00', 'USD', 0, 'extra own row without against')",
            (mixed_env["ar"], mixed_env["customer"], mixed_cn1))
        mixed_conn.execute(
            "UPDATE sales_invoice SET outstanding_amount = '-210.00' "
            "WHERE id = ?", (mixed_cn1,))
        mixed_conn.commit()
        mixed_res = mig.run_migration(mixed_path, run_date="2026-09-30")
        assert len(mixed_res["returns"]) == 2
        first, second = mixed_res["returns"]
        assert first["credit_note_id"] == mixed_cn1
        assert first["outcome"] == "not_applied"
        assert first["reason"] == "note_own_row_count_2"
        assert second["credit_note_id"] == mixed_cn2
        assert second["outcome"] == "applied"
        assert second["applied"] == "200.00"
        assert _doc(mixed_conn, mixed_inv) == ("400.00", "partially_paid")
        assert _doc(mixed_conn, mixed_cn1) == ("-210.00", "submitted")
        assert _doc(mixed_conn, mixed_cn2) == ("0", "submitted")
        assert len(first["appended"]) == 2
        _green(mixed_conn)
    finally:
        mixed_conn.close()
    overdue_path = str(tmp_path / "overdue.sqlite")
    _selling_init(overdue_path)
    overdue_conn = _selling_get_conn(overdue_path)
    try:
        overdue_env = build_selling_env(overdue_conn)
        overdue_inv = _invoice(overdue_conn, overdue_env)
        _pay(overdue_conn, overdue_env, overdue_inv, "400.00")
        overdue_cn = _legacy_note(overdue_conn, overdue_env, overdue_inv, "2",
                                  monkeypatch)
        overdue_conn.execute(
            "UPDATE sales_invoice SET status = 'overdue' WHERE id = ?",
            (overdue_inv,))
        overdue_conn.commit()
        overdue_res = mig.run_migration(overdue_path, run_date="2026-09-30")
        assert len(overdue_res["returns"]) == 1
        overdue_entry = overdue_res["returns"][0]
        assert overdue_entry["outcome"] == "applied"
        assert overdue_entry["applied"] == "200.00"
        assert _doc(overdue_conn, overdue_inv) == ("400.00", "partially_paid")
        assert _doc(overdue_conn, overdue_cn) == ("0", "submitted")
        _green(overdue_conn)
    finally:
        overdue_conn.close()


def test_note_side_preconditions_are_not_applied(conn, db_path, monkeypatch,
                                                 tmp_path):
    mig = _load_mig()
    env = build_selling_env(conn)
    inv_a = _invoice(conn, env)
    cn_a = _legacy_note(conn, env, inv_a, "2", monkeypatch)
    conn.execute(
        "UPDATE sales_invoice SET outstanding_amount = '-100.00' "
        "WHERE id = ?", (cn_a,))
    conn.commit()
    res = mig.run_migration(db_path, run_date="2026-09-30")
    assert len(res["returns"]) == 1
    entry_a = res["returns"][0]
    assert entry_a["outcome"] == "not_applied"
    assert entry_a["reason"] == "note_outstanding_mismatch"
    assert _doc(conn, inv_a) == ("1000.00", "submitted")
    assert _doc(conn, cn_a) == ("-100.00", "submitted")
    assert len(entry_a["appended"]) == 2
    assert len(_note_rows(conn, cn_a)) == 3
    if get_dialect() == "postgresql":
        return
    eng = _require_engine()
    eng._ensure_decimal_sum(conn)
    assert eng._check_inv22_payment_invoice_reconciliation(conn) is None
    msg25 = eng._check_inv25_ar_summary_detail(conn)
    assert msg25 is not None
    assert msg25.startswith("1 summary/detail divergence(s):")
    assert cn_a[:8] in msg25
    assert "outstanding=-100.00" in msg25
    assert "PLE net=-200.00" in msg25
    assert "diff=100.00" in msg25
    extra_path = str(tmp_path / "note-extra-row.sqlite")
    _selling_init(extra_path)
    extra_conn = _selling_get_conn(extra_path)
    try:
        extra_env = build_selling_env(extra_conn)
        inv_b = _invoice(extra_conn, extra_env)
        cn_b = _legacy_note(extra_conn, extra_env, inv_b, "2", monkeypatch)
        extra_conn.execute(
            "INSERT INTO payment_ledger_entry (id, posting_date, account_id, "
            "party_type, party_id, voucher_type, voucher_id, "
            "against_voucher_type, against_voucher_id, amount, "
            "amount_in_account_currency, currency, delinked, remarks) "
            "VALUES ('00000000-0000-4000-8000-000000000022', '2026-06-28', ?, "
            "'customer', ?, 'credit_note', ?, NULL, NULL, '-10.00', "
            "'-10.00', 'USD', 0, 'extra own row without against')",
            (extra_env["ar"], extra_env["customer"], cn_b))
        extra_conn.execute(
            "UPDATE sales_invoice SET outstanding_amount = '-210.00' "
            "WHERE id = ?", (cn_b,))
        extra_conn.commit()
        extra_res = mig.run_migration(extra_path, run_date="2026-09-30")
        assert len(extra_res["returns"]) == 1
        entry_b = extra_res["returns"][0]
        assert entry_b["outcome"] == "not_applied"
        assert entry_b["reason"] == "note_own_row_count_2"
        assert _doc(extra_conn, inv_b) == ("1000.00", "submitted")
        assert _doc(extra_conn, cn_b) == ("-210.00", "submitted")
        assert len(entry_b["appended"]) == 2
        assert len(_note_rows(extra_conn, cn_b)) == 4
        _green(extra_conn)
    finally:
        extra_conn.close()
    amount_path = str(tmp_path / "note-amount.sqlite")
    _selling_init(amount_path)
    amount_conn = _selling_get_conn(amount_path)
    try:
        amount_env = build_selling_env(amount_conn)
        inv_c = _invoice(amount_conn, amount_env)
        cn_c = _legacy_note(amount_conn, amount_env, inv_c, "2", monkeypatch)
        amount_conn.execute(
            "UPDATE sales_invoice SET grand_total = '-250.00' WHERE id = ?",
            (cn_c,))
        amount_conn.commit()
        amount_res = mig.run_migration(amount_path, run_date="2026-09-30")
        assert len(amount_res["returns"]) == 1
        entry_c = amount_res["returns"][0]
        assert entry_c["outcome"] == "not_applied"
        assert entry_c["reason"] == "note_amount_mismatch"
        assert _doc(amount_conn, inv_c) == ("1000.00", "submitted")
        assert _doc(amount_conn, cn_c) == ("-200.00", "submitted")
        assert len(entry_c["appended"]) == 2
        assert len(_note_rows(amount_conn, cn_c)) == 3
        _green(amount_conn)
    finally:
        amount_conn.close()


def test_written_off_and_side_door_original_is_not_applied(
        conn, db_path, monkeypatch):
    mig = _load_mig()
    env = build_selling_env(conn)
    inv = _invoice(conn, env)
    exp = seed_account(conn, env["company_id"], "Bad Debts", "expense")
    w = call_action(pay.write_off_invoice, conn,
                    ns(voucher_type="sales_invoice", voucher_id=inv,
                       write_off_amount="100.00",
                       write_off_account_id=exp,
                       reason="Customer insolvent",
                       posting_date="2026-07-01",
                       cost_center_id=None))
    assert is_ok(w), w
    conn.execute(
        "INSERT INTO payment_ledger_entry (id, posting_date, account_id, "
        "party_type, party_id, voucher_type, voucher_id, "
        "against_voucher_type, against_voucher_id, amount, "
        "amount_in_account_currency, currency, delinked, remarks) "
        "VALUES (?, '2026-06-29', ?, 'customer', ?, 'sales_invoice', ?, "
        "'sales_invoice', ?, '-100.00', '-100.00', 'USD', 0, ?)",
        ("00000000-0000-4000-8000-%012d" % 7, env["ar"],
         env["customer"], inv, inv,
         "Payment applied to sales_invoice %s via "
         "update-invoice-outstanding" % inv))
    conn.execute(
        "UPDATE sales_invoice SET outstanding_amount = '800.00', "
        "status = 'partially_paid' WHERE id = ?", (inv,))
    conn.commit()
    cn = _legacy_note(conn, env, inv, "2", monkeypatch)
    res = mig.run_migration(db_path, run_date="2026-09-30")
    assert len(res["returns"]) == 1
    entry = res["returns"][0]
    assert entry["outcome"] == "not_applied"
    assert entry["reason"] == "original_own_row_count_3"
    side_id = "00000000-0000-4000-8000-%012d" % 7
    side_remarks = ("Payment applied to sales_invoice %s via "
                    "update-invoice-outstanding" % inv)
    assert entry["extra_own_rows"] == [{
        "id": side_id, "amount": "-100.00",
        "remarks": side_remarks, "side_door": True}]
    own = conn.execute(
        "SELECT id, remarks FROM payment_ledger_entry "
        "WHERE voucher_type = 'sales_invoice' AND voucher_id = ? "
        "AND delinked = 0", (inv,)).fetchall()
    write_off_ids = [r["id"] for r in own
                     if (r["remarks"] or "").startswith("Write-off: ")]
    assert len(write_off_ids) == 1
    assert all(r["id"] != write_off_ids[0]
               for r in entry["extra_own_rows"])
    assert _doc(conn, inv) == ("800.00", "partially_paid")
    assert _doc(conn, cn) == ("-200.00", "submitted")
    assert len(entry["appended"]) == 2
    assert len(_note_rows(conn, cn)) == 3
    _green(conn)


def test_non_invoice_against_is_reported_not_missing(
        conn, db_path, monkeypatch):
    mig = _load_mig()
    env = build_selling_env(conn)
    inv = _invoice(conn, env)
    _pay(conn, env, inv, "400.00")
    cn = _legacy_note(conn, env, inv, "2", monkeypatch)
    conn.execute(
        "UPDATE payment_ledger_entry SET against_voucher_type = "
        "'journal_entry' WHERE voucher_type = 'credit_note' "
        "AND voucher_id = ? AND delinked = 0", (cn,))
    conn.commit()
    res = mig.run_migration(db_path, run_date="2026-09-30")
    assert len(res["returns"]) == 1
    entry = res["returns"][0]
    assert entry["outcome"] == "not_applied"
    assert entry["reason"] == "original_not_a_sales_invoice"
    assert entry["applied"] == "0.00"
    assert sorted(_note_rows(conn, cn)) == sorted(
        [("-200.00", "journal_entry", inv, "2026-06-28"),
         ("200.00", "journal_entry", inv, "2026-06-28"),
         ("-200.00", "credit_note", cn, "2026-06-28")])
    assert _doc(conn, inv) == ("600.00", "partially_paid")
    assert _doc(conn, cn) == ("-200.00", "submitted")
    audits = _mig_audit_rows(conn, mig.MIGRATION_ID)
    assert {(a["entity_type"], a["entity_id"]) for a in audits} == {
        ("sales_invoice", cn)}
    _green(conn)


def test_report_only_preview_counts_side_door_and_extra_rows(
        conn, db_path, monkeypatch, capsys):
    mig = _load_mig()
    env = build_selling_env(conn)
    inv = _invoice(conn, env)
    exp = seed_account(conn, env["company_id"], "Bad Debts", "expense")
    w = call_action(pay.write_off_invoice, conn,
                    ns(voucher_type="sales_invoice", voucher_id=inv,
                       write_off_amount="100.00",
                       write_off_account_id=exp,
                       reason="Customer insolvent",
                       posting_date="2026-07-01",
                       cost_center_id=None))
    assert is_ok(w), w
    conn.execute(
        "INSERT INTO payment_ledger_entry (id, posting_date, account_id, "
        "party_type, party_id, voucher_type, voucher_id, "
        "against_voucher_type, against_voucher_id, amount, "
        "amount_in_account_currency, currency, delinked, remarks) "
        "VALUES (?, '2026-06-29', ?, 'customer', ?, 'sales_invoice', ?, "
        "'sales_invoice', ?, '-100.00', '-100.00', 'USD', 0, ?)",
        ("00000000-0000-4000-8000-%012d" % 7, env["ar"],
         env["customer"], inv, inv,
         "Payment applied to sales_invoice %s via "
         "update-invoice-outstanding" % inv))
    conn.execute(
        "UPDATE sales_invoice SET outstanding_amount = '800.00', "
        "status = 'partially_paid' WHERE id = ?", (inv,))
    conn.commit()
    cn = _legacy_note(conn, env, inv, "2", monkeypatch)
    conn.execute(
        "INSERT INTO payment_ledger_entry (id, posting_date, account_id, "
        "party_type, party_id, voucher_type, voucher_id, "
        "against_voucher_type, against_voucher_id, amount, "
        "amount_in_account_currency, currency, delinked, remarks) "
        "VALUES (?, '2026-06-30', ?, 'customer', ?, 'sales_invoice', ?, "
        "NULL, NULL, '-10.00', '-10.00', 'USD', 0, ?)",
        ("00000000-0000-4000-8000-000000000031", env["ar"],
         env["customer"], inv, "Manual adjustment"))
    conn.execute(
        "UPDATE sales_invoice SET outstanding_amount = '790.00' "
        "WHERE id = ?", (inv,))
    conn.commit()
    assert _doc(conn, inv) == ("790.00", "partially_paid")
    before = _dump(db_path)
    mig_path = os.path.join(_MIG_DIR, "052_credit_note_allocation.py")
    monkeypatch.setattr(sys, "argv", [mig_path, db_path, "--report-only"])
    runpy.run_path(mig_path, run_name="__main__")
    out = capsys.readouterr().out
    assert out == (
        "  %s -> %s: not_applied applied 0.00 "
        "reason=original_own_row_count_4 "
        "side_door_rows=1 extra_own_rows=1\n"
        "Migration 052_credit_note_allocation report complete (no writes).\n"
        % (cn, inv))
    assert _dump(db_path) == before
