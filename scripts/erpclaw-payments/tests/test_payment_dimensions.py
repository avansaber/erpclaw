"""Payments carry accounting dimensions from draft to ledger (m715).

Tagged-invoice note: the seeded invoices below never pass through selling,
and selling does not yet write sales_invoice.dimensions_json on this base
(a parallel change does), so each test that needs a tagged invoice sets
that column directly with a PyPika UPDATE on the submitted seeded invoice
before creating the payment (see _set_invoice_dims).
"""
import importlib.util
import json
import os
import uuid
from decimal import Decimal

import pytest

from payments_helpers import (
    call_action, ns, is_error, is_ok, load_db_query, _uuid,
    build_ar_env, build_ap_env, seed_sales_invoice, seed_purchase_invoice,
    seed_account,
)
from erpclaw_lib.query import Q, P, Table, Field

mod = load_db_query()

E = '{"department": "Engineering"}'
Sv = '{"department": "Sales"}'
Op = '{"department": "Ops"}'

DIM_NOTE = ("Referenced invoices carry different dimensions; the payment was "
            "stored untagged. Pass --dimensions to tag it.")


def _reports_module():
    here = os.path.dirname(os.path.abspath(__file__))
    scripts = os.path.dirname(os.path.dirname(here))
    spec = importlib.util.spec_from_file_location(
        "db_query_reports_dims",
        os.path.join(scripts, "erpclaw-reports", "db_query.py"))
    rep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rep)
    return rep


REP = _reports_module()


def _base_ns(**kw):
    base = {"company_id": None, "company_name": None, "from_date": None,
            "to_date": None, "as_of_date": None, "account_id": None,
            "cost_center_id": None, "project_id": None,
            "fiscal_year_id": None, "party_type": None, "party_id": None,
            "voucher_type": None, "periods": None, "group_by": None,
            "dimension_key": None, "dimension_value": None,
            "limit": "100", "offset": "0", "aging_buckets": "30,60,90,120"}
    base.update(kw)
    return ns(**base)


def _add_payment(conn, env, *, payment_type, party_type, party_id,
                 paid_from, paid_to, paid_amount,
                 allocations=None, deductions=None,
                 dimensions=None, dimension_key=None, dimension_value=None):
    return call_action(mod.add_payment, conn, ns(
        company_id=env["company_id"], payment_type=payment_type,
        posting_date="2026-06-01", party_type=party_type, party_id=party_id,
        paid_from_account=paid_from, paid_to_account=paid_to,
        paid_amount=paid_amount, exchange_rate=None, payment_currency=None,
        reference_number=None, reference_date=None,
        allocations=json.dumps(allocations) if allocations is not None else None,
        deductions=json.dumps(deductions) if deductions is not None else None,
        dimensions=dimensions, dimension_key=dimension_key,
        dimension_value=dimension_value))


def _add_receive(conn, env, paid_amount, allocations=None, deductions=None,
                 dimensions=None, dimension_key=None, dimension_value=None):
    return _add_payment(conn, env, payment_type="receive",
                        party_type="customer", party_id=env["customer"],
                        paid_from=env["ar"], paid_to=env["bank"],
                        paid_amount=paid_amount,
                        allocations=allocations, deductions=deductions,
                        dimensions=dimensions, dimension_key=dimension_key,
                        dimension_value=dimension_value)


def _add_pay(conn, env, paid_amount, allocations=None, deductions=None,
             dimensions=None, dimension_key=None, dimension_value=None):
    return _add_payment(conn, env, payment_type="pay",
                        party_type="supplier", party_id=env["supplier"],
                        paid_from=env["bank"], paid_to=env["ap"],
                        paid_amount=paid_amount,
                        allocations=allocations, deductions=deductions,
                        dimensions=dimensions, dimension_key=dimension_key,
                        dimension_value=dimension_value)


def _submit(conn, pe_id):
    return call_action(mod.submit_payment, conn, ns(payment_entry_id=pe_id))


def _cancel(conn, pe_id):
    return call_action(mod.cancel_payment, conn, ns(payment_entry_id=pe_id))


def _gl(conn, voucher_type, voucher_id, entry_set="primary"):
    t = Table("gl_entry")
    q = (Q.from_(t).select(Field("account_id"), Field("debit"),
                           Field("credit"), Field("dimensions_json"))
         .where(Field("voucher_type") == P())
         .where(Field("voucher_id") == P())
         .where(Field("entry_set") == P())
         .where(Field("is_cancelled") == P()))
    rows = conn.execute(
        q.get_sql(), (voucher_type, voucher_id, entry_set, 0)).fetchall()
    return sorted([(r["account_id"], r["debit"], r["credit"],
                    r["dimensions_json"]) for r in rows])


def _set_invoice_dims(conn, table, inv_id, text):
    t = Table(table)
    conn.execute(
        Q.update(t).set(Field("dimensions_json"), P())
        .where(Field("id") == P()).get_sql(), (text, inv_id))
    conn.commit()


def _stored_dims(conn, pe_id):
    t = Table("payment_entry")
    row = conn.execute(
        Q.from_(t).select(Field("dimensions_json"))
        .where(Field("id") == P()).get_sql(), (pe_id,)).fetchone()
    return row["dimensions_json"]


def _register_dim(conn, key, data_type="text", referenced_table=None,
                  allowed=None, required=None, is_active=1):
    t = Table("dimension_registry")
    conn.execute(
        Q.into(t).columns(
            "id", "key", "label", "data_type", "referenced_table",
            "allowed_values_json", "is_required_on_account_types_json",
            "is_active")
        .insert(P(), P(), P(), P(), P(), P(), P(), P()).get_sql(),
        (str(uuid.uuid4()), key, key, data_type, referenced_table, allowed,
         required, is_active))
    conn.commit()


def _set_dim_active(conn, key, is_active):
    t = Table("dimension_registry")
    conn.execute(
        Q.update(t).set(Field("is_active"), P())
        .where(Field("key") == P()).get_sql(), (is_active, key))
    conn.commit()


def _counts(conn, company_id):
    out = {}
    for table in ("payment_entry", "payment_allocation", "payment_deduction",
                  "audit_log"):
        t = Table(table)
        out[table] = len(conn.execute(
            Q.from_(t).select(Field("id")).get_sql()).fetchall())
    n = Table("naming_series")
    nrows = conn.execute(
        Q.from_(n).select(Field("entity_type"), Field("prefix"),
                          Field("current_value"))
        .where(Field("company_id") == P())
        .orderby(Field("entity_type")).orderby(Field("prefix")).get_sql(),
        (company_id,)).fetchall()
    out["naming"] = sorted([(r["entity_type"], r["prefix"],
                             r["current_value"]) for r in nrows])
    return out


def _alloc(voucher_type, voucher_id, amount):
    return {"voucher_type": voucher_type, "voucher_id": voucher_id,
            "allocated_amount": amount}


def test_1_tagged_payment_with_deduction(conn):
    env = build_ar_env(conn)
    si = seed_sales_invoice(conn, env, "1000")
    created = _add_receive(
        conn, env, "1000", allocations=[_alloc("sales_invoice", si, "980")],
        deductions=[{"account_id": env["commission"], "amount": "20",
                     "type": "commission"}],
        dimensions=E)
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    assert _stored_dims(conn, pe_id) == E
    got = call_action(mod.get_payment, conn, ns(payment_entry_id=pe_id))
    assert is_ok(got), got
    assert got["dimensions_json"] == E
    sub = _submit(conn, pe_id)
    assert is_ok(sub), sub
    assert sub["gl_entries_created"] == 3
    assert _gl(conn, "payment_entry", pe_id) == sorted([
        (env["bank"], "980.00", "0.00", E),
        (env["commission"], "20.00", "0.00", E),
        (env["ar"], "0.00", "1000.00", E),
    ])


def test_2_inherited_from_invoice(conn):
    env = build_ar_env(conn)
    si = seed_sales_invoice(conn, env, "1000")
    _set_invoice_dims(conn, "sales_invoice", si, Sv)
    created = _add_receive(
        conn, env, "1000", allocations=[_alloc("sales_invoice", si, "1000")])
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    assert _stored_dims(conn, pe_id) == Sv
    assert "dimensions_note" not in created
    sub = _submit(conn, pe_id)
    assert is_ok(sub), sub
    assert _gl(conn, "payment_entry", pe_id) == sorted([
        (env["bank"], "1000.00", "0.00", Sv),
        (env["ar"], "0.00", "1000.00", Sv),
    ])


def test_2_inherited_ap_side(conn):
    env = build_ap_env(conn)
    pi = seed_purchase_invoice(conn, env, "500")
    _set_invoice_dims(conn, "purchase_invoice", pi, Sv)
    created = _add_pay(
        conn, env, "500", allocations=[_alloc("purchase_invoice", pi, "500")])
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    assert _stored_dims(conn, pe_id) == Sv
    sub = _submit(conn, pe_id)
    assert is_ok(sub), sub
    assert _gl(conn, "payment_entry", pe_id) == sorted([
        (env["ap"], "500.00", "0.00", Sv),
        (env["bank"], "0.00", "500.00", Sv),
    ])


def test_2_input_wins_over_inheritance(conn):
    env = build_ar_env(conn)
    si = seed_sales_invoice(conn, env, "1000")
    _set_invoice_dims(conn, "sales_invoice", si, Sv)
    created = _add_receive(
        conn, env, "1000", allocations=[_alloc("sales_invoice", si, "1000")],
        dimension_key=["department"], dimension_value=["Ops"])
    assert is_ok(created), created
    assert _stored_dims(conn, created["payment_entry_id"]) == Op


def test_2_deactivated_key_still_inherits(conn):
    env = build_ar_env(conn)
    _register_dim(conn, "fund")
    si = seed_sales_invoice(conn, env, "1000")
    want = '{"department": "Sales", "fund": "F1"}'
    _set_invoice_dims(conn, "sales_invoice", si, want)
    _set_dim_active(conn, "fund", 0)
    created = _add_receive(
        conn, env, "1000", allocations=[_alloc("sales_invoice", si, "1000")])
    assert is_ok(created), created
    assert _stored_dims(conn, created["payment_entry_id"]) == want


def test_3_advance_leg_is_tagged(conn):
    env = build_ar_env(conn)
    adv = seed_account(conn, env["company_id"], "Adv Cust", "liability")
    t = Table("company")
    conn.execute(
        Q.update(t).set(Field("advance_from_customer_account_id"), P())
        .where(Field("id") == P()).get_sql(), (adv, env["company_id"]))
    conn.commit()
    si = seed_sales_invoice(conn, env, "600")
    _set_invoice_dims(conn, "sales_invoice", si, Sv)
    created = _add_receive(
        conn, env, "1000", allocations=[_alloc("sales_invoice", si, "600")])
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    assert _stored_dims(conn, pe_id) == Sv
    sub = _submit(conn, pe_id)
    assert is_ok(sub), sub
    assert _gl(conn, "payment_entry", pe_id) == sorted([
        (env["bank"], "1000.00", "0.00", Sv),
        (env["ar"], "0.00", "600.00", Sv),
        (adv, "0.00", "400.00", Sv),
    ])


def test_4_mixed_references_stored_untagged(conn):
    env = build_ar_env(conn)
    a = seed_sales_invoice(conn, env, "300")
    b = seed_sales_invoice(conn, env, "200")
    _set_invoice_dims(conn, "sales_invoice", a, Sv)
    created = _add_receive(
        conn, env, "500",
        allocations=[_alloc("sales_invoice", a, "300"),
                     _alloc("sales_invoice", b, "200")])
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    assert _stored_dims(conn, pe_id) == "{}"
    assert created["dimensions_note"] == DIM_NOTE
    sub = _submit(conn, pe_id)
    assert is_ok(sub), sub
    rows = _gl(conn, "payment_entry", pe_id)
    assert rows, "expected posted legs"
    assert all(dims == "{}" for (_, _, _, dims) in rows)


def test_4_mixed_references_given_input_wins(conn):
    env = build_ar_env(conn)
    a = seed_sales_invoice(conn, env, "300")
    b = seed_sales_invoice(conn, env, "200")
    _set_invoice_dims(conn, "sales_invoice", a, Sv)
    created = _add_receive(
        conn, env, "500",
        allocations=[_alloc("sales_invoice", a, "300"),
                     _alloc("sales_invoice", b, "200")],
        dimensions=E)
    assert is_ok(created), created
    assert _stored_dims(conn, created["payment_entry_id"]) == E
    assert "dimensions_note" not in created


def test_4_equal_objects_different_spellings_agree(conn):
    env = build_ar_env(conn)
    a = seed_sales_invoice(conn, env, "300")
    b = seed_sales_invoice(conn, env, "200")
    _set_invoice_dims(conn, "sales_invoice", a, '{"department":"Sales"}')
    _set_invoice_dims(conn, "sales_invoice", b, Sv)
    created = _add_receive(
        conn, env, "500",
        allocations=[_alloc("sales_invoice", a, "300"),
                     _alloc("sales_invoice", b, "200")])
    assert is_ok(created), created
    assert _stored_dims(conn, created["payment_entry_id"]) == Sv
    assert "dimensions_note" not in created


def test_5_reports_group_tagged_and_untagged(conn):
    env = build_ar_env(conn)
    si1 = seed_sales_invoice(conn, env, "1000")
    created = _add_receive(
        conn, env, "1000", allocations=[_alloc("sales_invoice", si1, "980")],
        deductions=[{"account_id": env["commission"], "amount": "20",
                     "type": "commission"}],
        dimensions=E)
    assert is_ok(created), created
    assert is_ok(_submit(conn, created["payment_entry_id"])), created
    si2 = seed_sales_invoice(conn, env, "500")
    created2 = _add_receive(
        conn, env, "500", allocations=[_alloc("sales_invoice", si2, "500")])
    assert is_ok(created2), created2
    assert is_ok(_submit(conn, created2["payment_entry_id"])), created2
    trial = call_action(
        REP.multi_dim_trial_balance, conn,
        _base_ns(company_id=env["company_id"], to_date="2026-12-31",
                 group_by="department"))
    assert is_ok(trial), trial
    assert sorted(trial["groups"],
                  key=lambda g: str(g["department"])) == sorted([
        {"department": "Engineering", "debit": "1000.00",
         "credit": "1000.00", "balance": "0.00"},
        {"department": None, "debit": "500.00",
         "credit": "500.00", "balance": "0.00"},
    ], key=lambda g: str(g["department"]))
    pl = call_action(
        REP.profit_and_loss, conn,
        _base_ns(company_id=env["company_id"], from_date="2026-01-01",
                 to_date="2026-12-31", group_by="department"))
    assert is_ok(pl), pl
    assert pl["groups"] == [{"department": "Engineering", "revenue": "0.00",
                             "expenses": "20.00", "net": "-20.00"}]
    assert pl["income_total"] == "0.00"
    assert pl["expense_total"] == "20.00"


def test_6_cancel_nets_to_zero_per_value(conn):
    env = build_ar_env(conn)
    si = seed_sales_invoice(conn, env, "1000")
    created = _add_receive(
        conn, env, "1000", allocations=[_alloc("sales_invoice", si, "980")],
        deductions=[{"account_id": env["commission"], "amount": "20",
                     "type": "commission"}],
        dimensions=E)
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    assert is_ok(_submit(conn, pe_id)), pe_id
    assert is_ok(_cancel(conn, pe_id)), pe_id
    t = Table("gl_entry")
    rows = conn.execute(
        Q.from_(t).select(Field("account_id"), Field("debit"),
                          Field("credit"), Field("dimensions_json"),
                          Field("is_cancelled"))
        .where(Field("voucher_type") == P())
        .where(Field("voucher_id") == P()).get_sql(),
        ("payment_entry", pe_id)).fetchall()
    assert len(rows) == 6, "three originals plus three mirrors"
    assert all(r["dimensions_json"] == E for r in rows)
    nets = {}
    for r in rows:
        key = (r["account_id"], r["dimensions_json"])
        nets[key] = (nets.get(key, Decimal("0.00"))
                     + Decimal(r["debit"]) - Decimal(r["credit"]))
    assert nets, "expected rows to net"
    assert all(net == Decimal("0.00") for net in nets.values())


def test_7_untagged_is_unchanged(conn):
    env = build_ar_env(conn)
    si = seed_sales_invoice(conn, env, "1000")
    created = _add_receive(
        conn, env, "1000", allocations=[_alloc("sales_invoice", si, "1000")])
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    assert _stored_dims(conn, pe_id) == "{}"
    sub = _submit(conn, pe_id)
    assert is_ok(sub), sub
    assert sub["gl_entries_created"] == 2
    assert _gl(conn, "payment_entry", pe_id) == sorted([
        (env["bank"], "1000.00", "0.00", "{}"),
        (env["ar"], "0.00", "1000.00", "{}"),
    ])


def _refused_add(conn, env, si, **dim_kw):
    before = _counts(conn, env["company_id"])
    res = _add_receive(
        conn, env, "1000", allocations=[_alloc("sales_invoice", si, "980")],
        deductions=[{"account_id": env["commission"], "amount": "20",
                     "type": "commission"}],
        **dim_kw)
    assert is_error(res), res
    assert _counts(conn, env["company_id"]) == before
    return res


def test_8_unknown_dimension_key_refused(conn):
    env = build_ar_env(conn)
    si = seed_sales_invoice(conn, env, "1000")
    res = _refused_add(conn, env, si, dimensions='{"region": "West"}')
    assert res["message"] == ("Unknown or inactive dimension 'region'; "
                              "run list-dimensions")


def test_8_unpaired_key_value_refused(conn):
    env = build_ar_env(conn)
    si = seed_sales_invoice(conn, env, "1000")
    res = _refused_add(conn, env, si, dimension_key=["department"],
                       dimension_value=["Sales", "Ops"])
    assert res["message"] == ("--dimension-key and --dimension-value must be "
                              "given in pairs")


def test_8_conflicting_double_given_refused(conn):
    env = build_ar_env(conn)
    si = seed_sales_invoice(conn, env, "1000")
    res = _refused_add(conn, env, si, dimensions='{"department": "A"}',
                       dimension_key=["department"], dimension_value=["B"])
    assert res["message"] == ("Dimension 'department' given twice with "
                              "different values")


def test_8_non_object_dimensions_refused(conn):
    env = build_ar_env(conn)
    si = seed_sales_invoice(conn, env, "1000")
    res = _refused_add(conn, env, si, dimensions="[1]")
    assert res["message"] == "--dimensions must be a JSON object"


def test_8_update_refusal_writes_nothing(conn):
    env = build_ar_env(conn)
    si = seed_sales_invoice(conn, env, "1000")
    created = _add_receive(
        conn, env, "1000", allocations=[_alloc("sales_invoice", si, "1000")])
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    t = Table("payment_entry")
    before = conn.execute(
        Q.from_(t).select(Field("dimensions_json"), Field("updated_at"))
        .where(Field("id") == P()).get_sql(), (pe_id,)).fetchone()
    audit_count = _counts(conn, env["company_id"])["audit_log"]
    res = call_action(mod.update_payment, conn, ns(
        payment_entry_id=pe_id, paid_amount=None, reference_number=None,
        allocations=None, dimensions='{"region": "West"}',
        dimension_key=None, dimension_value=None))
    assert is_error(res), res
    assert res["message"] == ("Unknown or inactive dimension 'region'; "
                              "run list-dimensions")
    after = conn.execute(
        Q.from_(t).select(Field("dimensions_json"), Field("updated_at"))
        .where(Field("id") == P()).get_sql(), (pe_id,)).fetchone()
    assert dict(after) == dict(before)
    assert _counts(conn, env["company_id"])["audit_log"] == audit_count


def _audit_dim_change(conn, pe_id):
    t = Table("audit_log")
    rows = conn.execute(
        Q.from_(t).select(Field("old_values"), Field("new_values"))
        .where(Field("action") == P())
        .where(Field("entity_id") == P()).get_sql(),
        ("update-payment", pe_id)).fetchall()
    for r in rows:
        try:
            newv = json.loads(r["new_values"] or "{}")
        except (ValueError, TypeError):
            continue
        if "dimensions_json" in newv:
            try:
                oldv = json.loads(r["old_values"] or "{}")
            except (ValueError, TypeError):
                oldv = {}
            return oldv, newv
    return None, None


def test_9_update_sets_and_clears_dimensions(conn):
    env = build_ar_env(conn)
    si = seed_sales_invoice(conn, env, "1000")
    created = _add_receive(
        conn, env, "1000", allocations=[_alloc("sales_invoice", si, "1000")])
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    res = call_action(mod.update_payment, conn, ns(
        payment_entry_id=pe_id, paid_amount=None, reference_number=None,
        allocations=None, dimensions=E,
        dimension_key=None, dimension_value=None))
    assert is_ok(res), res
    assert res["updated_fields"] == ["dimensions_json"]
    assert _stored_dims(conn, pe_id) == E
    oldv, newv = _audit_dim_change(conn, pe_id)
    assert oldv is not None and newv is not None, "audit row carries change"
    assert oldv["dimensions_json"] == "{}"
    assert newv["dimensions_json"] == E
    clear = call_action(mod.update_payment, conn, ns(
        payment_entry_id=pe_id, paid_amount=None, reference_number=None,
        allocations=None, dimensions="{}",
        dimension_key=None, dimension_value=None))
    assert is_ok(clear), clear
    assert _stored_dims(conn, pe_id) == "{}"


def test_9_update_refused_once_submitted(conn):
    env = build_ar_env(conn)
    si = seed_sales_invoice(conn, env, "1000")
    created = _add_receive(
        conn, env, "1000", allocations=[_alloc("sales_invoice", si, "1000")])
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    assert is_ok(_submit(conn, pe_id)), pe_id
    res = call_action(mod.update_payment, conn, ns(
        payment_entry_id=pe_id, paid_amount=None, reference_number=None,
        allocations=None, dimensions=E,
        dimension_key=None, dimension_value=None))
    assert is_error(res), res
    assert res["message"] == ("Cannot update: payment is 'submitted' "
                              "(must be 'draft')")


def _write_off(conn, *, voucher_type, voucher_id, amount, account):
    return call_action(mod.write_off_invoice, conn, ns(
        voucher_type=voucher_type, voucher_id=voucher_id,
        write_off_amount=amount, write_off_account_id=account,
        reason="Customer insolvent, 2026 review", posting_date="2026-06-01",
        cost_center_id=None))


def test_10_write_off_carries_invoice_tags(conn):
    env = build_ar_env(conn)
    bad_debt = seed_account(conn, env["company_id"], "Bad Debt", "expense")
    si = seed_sales_invoice(conn, env, "1000")
    _set_invoice_dims(conn, "sales_invoice", si, Sv)
    res = _write_off(conn, voucher_type="sales_invoice", voucher_id=si,
                     amount="340.00", account=bad_debt)
    assert is_ok(res), res
    assert _gl(conn, "sales_invoice", si, "write_off") == sorted([
        (bad_debt, "340.00", "0.00", Sv),
        (env["ar"], "0.00", "340.00", Sv),
    ])


def test_10_write_off_untagged_stays_untagged(conn):
    env = build_ar_env(conn)
    bad_debt = seed_account(conn, env["company_id"], "Bad Debt", "expense")
    si = seed_sales_invoice(conn, env, "200")
    res = _write_off(conn, voucher_type="sales_invoice", voucher_id=si,
                     amount="50.00", account=bad_debt)
    assert is_ok(res), res
    rows = _gl(conn, "sales_invoice", si, "write_off")
    assert rows, "expected the write-off pair"
    assert all(dims == "{}" for (_, _, _, dims) in rows)
