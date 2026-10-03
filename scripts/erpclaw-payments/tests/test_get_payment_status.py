"""get-payment surfaces the document state as document_status; envelope status stays ok."""
import json

import pytest

from payments_helpers import (
    build_ar_env, call_action, is_ok, load_db_query, ns,
)

pay = load_db_query()


def _add_draft_payment(conn, env):
    created = call_action(pay.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="receive",
        posting_date="2026-06-01", party_type="customer",
        party_id=env["customer"], paid_from_account=env["ar"],
        paid_to_account=env["bank"], paid_amount="100.00",
        exchange_rate=None, payment_currency=None,
        reference_number=None, reference_date=None,
        allocations=None, deductions=None))
    assert is_ok(created), created
    return created["payment_entry_id"]


def test_get_payment_reports_draft_then_submitted(conn):
    env = build_ar_env(conn)
    pe_id = _add_draft_payment(conn, env)

    draft = call_action(pay.get_payment, conn, ns(payment_entry_id=pe_id))
    assert draft["status"] == "ok"
    assert draft["document_status"] == "draft"

    submitted = call_action(pay.submit_payment, conn, ns(payment_entry_id=pe_id))
    assert is_ok(submitted), submitted

    fetched = call_action(pay.get_payment, conn, ns(payment_entry_id=pe_id))
    assert fetched["status"] == "ok"
    assert fetched["document_status"] == "submitted"
