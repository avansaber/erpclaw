"""Selling postings stamp the posting company's own fiscal year.

A decoy company B owns FY-DECOY (2025-07-01..2026-12-31), inserted first;
company A (built in-test) owns FY-A-2026 (2026-01-01..2026-12-31). A posting
for A dated 2026-03-15 must carry FY-A-2026 on every gl_entry row (and every
stock_ledger_entry row where the action writes stock). The company-blind
lookup returns B's year first on SQLite, so each test fails on the base.
"""
import json
from decimal import Decimal

from selling_helpers import (
    build_selling_env,
    call_action,
    is_error,
    is_ok,
    load_db_query,
    ns,
    seed_company,
    seed_fiscal_year,
)

mod = load_db_query()

POSTING_DATE = "2026-03-15"
OWN_YEAR = "FY-A-2026"


def _setup_two_companies(conn):
    """Seed decoy B first, then build A in-test and rename A's year."""
    b = seed_company(conn, name="Decoy Co", abbr="DC")
    seed_fiscal_year(conn, b, name="FY-DECOY",
                     start="2025-07-01", end="2026-12-31")
    env = build_selling_env(conn)
    conn.execute(
        "UPDATE fiscal_year SET name = 'FY-A-2026' WHERE company_id = ?",
        (env["company_id"],),
    )
    conn.commit()
    return b, env


def _gl_rows(conn, voucher_id):
    return conn.execute(
        "SELECT fiscal_year, debit, credit FROM gl_entry WHERE voucher_id = ?",
        (voucher_id,),
    ).fetchall()


def _sle_rows(conn, voucher_id):
    return conn.execute(
        "SELECT fiscal_year FROM stock_ledger_entry "
        "WHERE voucher_id = ? AND is_cancelled = 0",
        (voucher_id,),
    ).fetchall()


def _assert_own_year(rows):
    assert len(rows) >= 1
    for row in rows:
        assert row["fiscal_year"] == OWN_YEAR


def _create_confirmed_so(conn, env):
    items = json.dumps([{
        "item_id": env["item1"], "qty": "10", "rate": "100.00",
        "warehouse_id": env["warehouse"],
    }])
    so = call_action(mod.add_sales_order, conn, ns(
        customer_id=env["customer"], company_id=env["company_id"],
        posting_date=POSTING_DATE, items=items,
        delivery_date="2026-03-20", tax_template_id=None,
    ))
    assert is_ok(so), so
    submit = call_action(mod.submit_sales_order, conn, ns(
        sales_order_id=so["sales_order_id"],
    ))
    assert is_ok(submit), submit
    return so["sales_order_id"]


def test_delivery_note_stamps_own_company_year(conn):
    _, env = _setup_two_companies(conn)
    so_id = _create_confirmed_so(conn, env)
    dn = call_action(mod.create_delivery_note, conn, ns(
        sales_order_id=so_id, posting_date=POSTING_DATE, items=None,
    ))
    assert is_ok(dn), dn
    result = call_action(mod.submit_delivery_note, conn, ns(
        delivery_note_id=dn["delivery_note_id"],
    ))
    assert is_ok(result), result

    gl_rows = _gl_rows(conn, dn["delivery_note_id"])
    _assert_own_year(gl_rows)
    total_debit = sum(Decimal(r["debit"]) for r in gl_rows)
    total_credit = sum(Decimal(r["credit"]) for r in gl_rows)
    assert total_debit == Decimal("100.00")
    assert total_credit == Decimal("100.00")

    sle_rows = _sle_rows(conn, dn["delivery_note_id"])
    _assert_own_year(sle_rows)


def _standalone_invoice(conn, env):
    items = json.dumps([{
        "item_id": env["item1"], "qty": "5", "rate": "100.00",
        "warehouse_id": env["warehouse"],
    }])
    create = call_action(mod.create_sales_invoice, conn, ns(
        sales_order_id=None, delivery_note_id=None,
        customer_id=env["customer"], company_id=env["company_id"],
        posting_date=POSTING_DATE, due_date="2026-04-15",
        items=items, tax_template_id=None, payment_terms_id=None,
    ))
    assert is_ok(create), create
    return create["sales_invoice_id"]


def test_sales_invoice_stamps_own_company_year(conn):
    _, env = _setup_two_companies(conn)
    si_id = _standalone_invoice(conn, env)
    result = call_action(mod.submit_sales_invoice, conn, ns(
        sales_invoice_id=si_id,
    ))
    assert is_ok(result), result

    gl_rows = _gl_rows(conn, si_id)
    _assert_own_year(gl_rows)
    debits = sorted(Decimal(r["debit"]) for r in gl_rows)
    credits = sorted(Decimal(r["credit"]) for r in gl_rows)
    assert debits == [Decimal("0.00"), Decimal("0.00"),
                      Decimal("50.00"), Decimal("500.00")]
    assert credits == [Decimal("0.00"), Decimal("0.00"),
                       Decimal("50.00"), Decimal("500.00")]

    sle_rows = _sle_rows(conn, si_id)
    _assert_own_year(sle_rows)


def test_recurring_invoice_stamps_own_company_year(conn):
    _, env = _setup_two_companies(conn)
    items = json.dumps([{"item_id": env["item1"], "qty": "1", "rate": "500.00"}])
    result = call_action(mod.add_recurring_template, conn, ns(
        customer_id=env["customer"], company_id=env["company_id"],
        items=items, frequency="monthly",
        start_date=POSTING_DATE, end_date="2026-12-31",
        tax_template_id=None, payment_terms_id=None,
    ))
    assert is_ok(result), result
    template_id = result["template_id"]
    activated = call_action(mod.update_recurring_template, conn, ns(
        template_id=template_id, template_status="active",
        frequency=None, items=None,
    ))
    assert is_ok(activated), activated

    gen = call_action(mod.generate_recurring_invoices, conn, ns(
        as_of_date=POSTING_DATE, company_id=env["company_id"],
    ))
    assert is_ok(gen), gen
    assert gen["invoices_generated"] == 1
    assert Decimal(gen["invoices"][0]["amount"]) == Decimal("500.00")
    inv_id = gen["invoices"][0]["invoice_id"]

    gl_rows = _gl_rows(conn, inv_id)
    _assert_own_year(gl_rows)
    total_debit = sum(Decimal(r["debit"]) for r in gl_rows)
    total_credit = sum(Decimal(r["credit"]) for r in gl_rows)
    assert total_debit == Decimal("500.00")
    assert total_credit == Decimal("500.00")


def test_missing_company_year_is_refused(conn):
    _, env = _setup_two_companies(conn)
    conn.execute(
        "DELETE FROM fiscal_year WHERE company_id = ?",
        (env["company_id"],),
    )
    conn.commit()
    si_id = _standalone_invoice(conn, env)
    conn.execute(
        "UPDATE sales_invoice SET update_stock = 0 WHERE id = ?", (si_id,))
    conn.commit()
    result = call_action(mod.submit_sales_invoice, conn, ns(
        sales_invoice_id=si_id,
    ))
    assert is_error(result), result
    message = str(result.get("message", "")) + str(result.get("error", ""))
    assert ("GL Validation Step 9 Failed: No open fiscal year found "
            "for posting date 2026-03-15") in message
    assert _gl_rows(conn, si_id) == []
