"""Target and result projections for journal entry envelopes."""

import hashlib
import json
from decimal import Decimal

from erpclaw_lib import action_impact
from erpclaw_lib.query import Field, P, Q, Table

_ENTRY_COLS = ("id", "company_id", "naming_series", "posting_date",
               "entry_type", "total_debit", "total_credit", "currency",
               "exchange_rate", "remark", "status", "amended_from",
               "cwip_asset_id", "dimensions_json")

_LINE_COLS = ("id", "account_id", "party_type", "party_id", "debit",
              "credit", "cost_center_id", "project_id", "remark",
              "dimensions_json")

_PAYMENT_ENTRY_COLS = ("id", "company_id", "naming_series", "payment_type",
                       "posting_date", "party_type", "party_id",
                       "paid_from_account", "paid_to_account", "paid_amount",
                       "received_amount", "payment_currency", "exchange_rate",
                       "reference_number", "reference_date", "status",
                       "unallocated_amount", "payment_method",
                       "advance_account_id", "dimensions_json")

_PAYMENT_ALLOC_COLS = ("id", "voucher_type", "voucher_id", "allocated_amount",
                       "exchange_gain_loss", "delinked")

_PAYMENT_DEDUCTION_COLS = ("id", "account_id", "amount", "type",
                           "description")


def journal_entry_state(conn, journal_entry_id):
    """Re-derive one entry and its lines from stored rows only."""
    entry_table = Table("journal_entry")
    entry_query = Q.from_(entry_table).select(
        *[Field(column) for column in _ENTRY_COLS]).where(
        Field("id") == P()).get_sql()
    entry_row = conn.execute(
        entry_query, (journal_entry_id,)).fetchone()
    if entry_row is None:
        return None
    entry = {column: dict(entry_row)[column] for column in _ENTRY_COLS}
    line_table = Table("journal_entry_line")
    line_query = Q.from_(line_table).select(
        *[Field(column) for column in _LINE_COLS]).where(
        Field("journal_entry_id") == P()).orderby(
        Field("id")).get_sql()
    line_rows = conn.execute(line_query, (journal_entry_id,)).fetchall()
    lines = [{column: dict(record)[column] for column in _LINE_COLS}
             for record in line_rows]
    return {"entry": entry, "lines": lines}


def state_digest(state):
    """Digest one stored entry state."""
    text = json.dumps({"entry": state["entry"], "lines": state["lines"]},
                      sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def derive_journal_entry(conn, action, pairs):
    """Derive company, target and amount for one journal entry."""
    found = [(name, value) for name, value in pairs
             if name == "journal-entry-id"]
    if len(found) != 1:
        raise ValueError("AUTHORIZATION_INPUT_INVALID")
    journal_entry_id = found[0][1]
    if type(journal_entry_id) is not str or not journal_entry_id:
        raise ValueError("AUTHORIZATION_INPUT_INVALID")
    state = journal_entry_state(conn, journal_entry_id)
    if state is None:
        raise ValueError("AUTHORIZATION_INPUT_INVALID")
    company_id = state["entry"]["company_id"]
    for name, value in pairs:
        if name == "company-id" and value != company_id:
            raise ValueError("AUTHORIZATION_INPUT_INVALID")
    try:
        number = Decimal(state["entry"]["total_debit"])
        shaped = number.quantize(Decimal("0.01"))
    except Exception:
        raise ValueError("AUTHORIZATION_INPUT_INVALID")
    if shaped != number:
        raise ValueError("AUTHORIZATION_INPUT_INVALID")
    try:
        total = sum((Decimal(line["debit"]) for line in state["lines"]),
                    Decimal("0"))
    except Exception:
        raise ValueError("AUTHORIZATION_INPUT_INVALID")
    if total != number:
        raise ValueError("AUTHORIZATION_INPUT_INVALID")
    return {
        "company_ids": [company_id],
        "targets": [{"kind": "journal-entry", "id": journal_entry_id,
                     "state_digest": state_digest(state)}],
        "amounts": [{"currency": state["entry"]["currency"],
                     "value": format(shaped, "f"), "scale": 2}],
    }


def journal_entry_result(payload):
    """Pick the posted entry id and status from one handler payload."""
    if "new_journal_entry_id" in payload:
        result_id = payload["new_journal_entry_id"]
    else:
        result_id = payload["journal_entry_id"]
    return ("journal-entry", result_id, payload["document_status"])


def payment_entry_state(conn, payment_entry_id):
    """Re-derive one payment and its detail rows from stored rows only."""
    entry_table = Table("payment_entry")
    entry_query = Q.from_(entry_table).select(
        *[Field(column) for column in _PAYMENT_ENTRY_COLS]).where(
        Field("id") == P()).get_sql()
    entry_row = conn.execute(
        entry_query, (payment_entry_id,)).fetchone()
    if entry_row is None:
        return None
    entry = {column: dict(entry_row)[column]
             for column in _PAYMENT_ENTRY_COLS}
    alloc_table = Table("payment_allocation")
    alloc_query = Q.from_(alloc_table).select(
        *[Field(column) for column in _PAYMENT_ALLOC_COLS]).where(
        Field("payment_entry_id") == P()).orderby(
        Field("id")).get_sql()
    alloc_rows = conn.execute(alloc_query, (payment_entry_id,)).fetchall()
    allocations = [{column: dict(record)[column]
                    for column in _PAYMENT_ALLOC_COLS}
                   for record in alloc_rows]
    ded_table = Table("payment_deduction")
    ded_query = Q.from_(ded_table).select(
        *[Field(column) for column in _PAYMENT_DEDUCTION_COLS]).where(
        Field("payment_entry_id") == P()).orderby(
        Field("id")).get_sql()
    ded_rows = conn.execute(ded_query, (payment_entry_id,)).fetchall()
    deductions = [{column: dict(record)[column]
                   for column in _PAYMENT_DEDUCTION_COLS}
                  for record in ded_rows]
    return {"entry": entry, "allocations": allocations,
            "deductions": deductions}


def payment_state_digest(state):
    """Digest one stored payment state."""
    text = json.dumps({"entry": state["entry"],
                       "allocations": state["allocations"],
                       "deductions": state["deductions"]},
                      sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def derive_payment_entry(conn, action, pairs):
    """Derive company, target and amount for one payment entry."""
    found = [(name, value) for name, value in pairs
             if name == "payment-entry-id"]
    if len(found) != 1:
        raise ValueError("AUTHORIZATION_INPUT_INVALID")
    payment_entry_id = found[0][1]
    if type(payment_entry_id) is not str or not payment_entry_id:
        raise ValueError("AUTHORIZATION_INPUT_INVALID")
    state = payment_entry_state(conn, payment_entry_id)
    if state is None:
        raise ValueError("AUTHORIZATION_INPUT_INVALID")
    company_id = state["entry"]["company_id"]
    for name, value in pairs:
        if name == "company-id" and value != company_id:
            raise ValueError("AUTHORIZATION_INPUT_INVALID")
    try:
        number = Decimal(state["entry"]["paid_amount"])
        shaped = number.quantize(Decimal("0.01"))
    except Exception:
        raise ValueError("AUTHORIZATION_INPUT_INVALID")
    if shaped != number:
        raise ValueError("AUTHORIZATION_INPUT_INVALID")
    return {
        "company_ids": [company_id],
        "targets": [{"kind": "payment-entry", "id": payment_entry_id,
                     "state_digest": payment_state_digest(state)}],
        "amounts": [{"currency": state["entry"]["payment_currency"],
                     "value": format(shaped, "f"), "scale": 2}],
    }


def payment_entry_result(payload):
    """Pick the posted payment id and status from one handler payload."""
    return ("payment-entry", payload["payment_entry_id"],
            payload["document_status"])


_SALES_INVOICE_COLS = ("id", "company_id", "naming_series", "customer_id",
                       "posting_date", "due_date", "currency",
                       "exchange_rate", "total_amount", "tax_amount",
                       "grand_total", "outstanding_amount",
                       "rounding_adjustment", "tax_template_id",
                       "payment_terms_id", "status", "sales_order_id",
                       "delivery_note_id", "is_return", "return_against",
                       "update_stock", "amended_from", "is_intercompany",
                       "intercompany_reference_id", "dimensions_json")

_SALES_INVOICE_ITEM_COLS = ("id", "item_id", "quantity", "uom", "rate",
                            "amount", "discount_percentage", "net_amount",
                            "sales_order_item_id", "delivery_note_item_id",
                            "cost_center_id", "project_id")


def sales_invoice_state(conn, sales_invoice_id):
    """Re-derive one sales invoice and its item rows from stored rows."""
    invoice_table = Table("sales_invoice")
    invoice_query = Q.from_(invoice_table).select(
        *[Field(column) for column in _SALES_INVOICE_COLS]).where(
        Field("id") == P()).get_sql()
    invoice_row = conn.execute(
        invoice_query, (sales_invoice_id,)).fetchone()
    if invoice_row is None:
        return None
    invoice = {column: dict(invoice_row)[column]
               for column in _SALES_INVOICE_COLS}
    item_table = Table("sales_invoice_item")
    item_query = Q.from_(item_table).select(
        *[Field(column) for column in _SALES_INVOICE_ITEM_COLS]).where(
        Field("sales_invoice_id") == P()).orderby(
        Field("id")).get_sql()
    item_rows = conn.execute(item_query, (sales_invoice_id,)).fetchall()
    items = [{column: dict(record)[column]
              for column in _SALES_INVOICE_ITEM_COLS}
             for record in item_rows]
    return {"invoice": invoice, "items": items}


def sales_invoice_state_digest(state):
    """Digest one stored sales invoice state."""
    text = json.dumps({"invoice": state["invoice"], "items": state["items"]},
                      sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def derive_sales_invoice(conn, action, pairs):
    """Derive company, target and amount for one sales invoice."""
    found = [(name, value) for name, value in pairs
             if name == "sales-invoice-id"]
    if len(found) != 1:
        raise ValueError("AUTHORIZATION_INPUT_INVALID")
    sales_invoice_id = found[0][1]
    if type(sales_invoice_id) is not str or not sales_invoice_id:
        raise ValueError("AUTHORIZATION_INPUT_INVALID")
    state = sales_invoice_state(conn, sales_invoice_id)
    if state is None:
        raise ValueError("AUTHORIZATION_INPUT_INVALID")
    company_id = state["invoice"]["company_id"]
    for name, value in pairs:
        if name == "company-id" and value != company_id:
            raise ValueError("AUTHORIZATION_INPUT_INVALID")
    company_values = [value for name, value in pairs
                      if name == "company"]
    if company_values:
        company_table = Table("company")
        company_query = Q.from_(company_table).select(
            Field("name")).where(Field("id") == P()).get_sql()
        company_row = conn.execute(
            company_query, (company_id,)).fetchone()
        if company_row is None:
            raise ValueError("AUTHORIZATION_INPUT_INVALID")
        stored_name = dict(company_row)["name"]
        for value in company_values:
            if value != company_id and value != stored_name:
                raise ValueError("AUTHORIZATION_INPUT_INVALID")
    try:
        number = Decimal(state["invoice"]["grand_total"])
        shaped = number.quantize(Decimal("0.01"))
    except Exception:
        raise ValueError("AUTHORIZATION_INPUT_INVALID")
    if shaped != number:
        raise ValueError("AUTHORIZATION_INPUT_INVALID")
    return {
        "company_ids": [company_id],
        "targets": [{"kind": "sales-invoice", "id": sales_invoice_id,
                     "state_digest": sales_invoice_state_digest(state)}],
        "amounts": [{"currency": state["invoice"]["currency"],
                     "value": format(abs(shaped), "f"), "scale": 2}],
    }


def sales_invoice_result(payload):
    """Pick the posted sales invoice id and status from one payload."""
    return ("sales-invoice", payload["sales_invoice_id"],
            payload["document_status"])


_PURCHASE_INVOICE_COLS = ("id", "company_id", "naming_series", "supplier_id",
                         "posting_date", "due_date", "currency",
                         "exchange_rate", "total_amount", "tax_amount",
                         "grand_total", "outstanding_amount",
                         "rounding_adjustment", "tax_template_id",
                         "payment_terms_id", "status", "purchase_order_id",
                         "purchase_receipt_id", "is_return",
                         "return_against", "update_stock", "amended_from",
                         "cwip_asset_id", "is_intercompany",
                         "intercompany_reference_id", "dimensions_json")

_PURCHASE_INVOICE_ITEM_COLS = ("id", "item_id", "quantity", "uom", "rate",
                               "amount", "expense_account_id",
                               "cost_center_id", "project_id",
                               "purchase_order_item_id",
                               "purchase_receipt_item_id",
                               "discount_amount")


def purchase_invoice_state(conn, purchase_invoice_id):
    """Re-derive one purchase invoice and its item rows from stored rows."""
    invoice_table = Table("purchase_invoice")
    invoice_query = Q.from_(invoice_table).select(
        *[Field(column) for column in _PURCHASE_INVOICE_COLS]).where(
        Field("id") == P()).get_sql()
    invoice_row = conn.execute(
        invoice_query, (purchase_invoice_id,)).fetchone()
    if invoice_row is None:
        return None
    invoice = {column: dict(invoice_row)[column]
               for column in _PURCHASE_INVOICE_COLS}
    item_table = Table("purchase_invoice_item")
    item_query = Q.from_(item_table).select(
        *[Field(column) for column in _PURCHASE_INVOICE_ITEM_COLS]).where(
        Field("purchase_invoice_id") == P()).orderby(
        Field("id")).get_sql()
    item_rows = conn.execute(item_query, (purchase_invoice_id,)).fetchall()
    items = [{column: dict(record)[column]
              for column in _PURCHASE_INVOICE_ITEM_COLS}
             for record in item_rows]
    return {"invoice": invoice, "items": items}


def purchase_invoice_state_digest(state):
    """Digest one stored purchase invoice state."""
    text = json.dumps({"invoice": state["invoice"], "items": state["items"]},
                      sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def derive_purchase_invoice(conn, action, pairs):
    """Derive company, target and amount for one purchase invoice."""
    found = [(name, value) for name, value in pairs
             if name == "purchase-invoice-id"]
    if len(found) != 1:
        raise ValueError("AUTHORIZATION_INPUT_INVALID")
    purchase_invoice_id = found[0][1]
    if type(purchase_invoice_id) is not str or not purchase_invoice_id:
        raise ValueError("AUTHORIZATION_INPUT_INVALID")
    state = purchase_invoice_state(conn, purchase_invoice_id)
    if state is None:
        raise ValueError("AUTHORIZATION_INPUT_INVALID")
    company_id = state["invoice"]["company_id"]
    for name, value in pairs:
        if name == "company-id" and value != company_id:
            raise ValueError("AUTHORIZATION_INPUT_INVALID")
    company_values = [value for name, value in pairs
                      if name == "company"]
    if company_values:
        company_table = Table("company")
        company_query = Q.from_(company_table).select(
            Field("name")).where(Field("id") == P()).get_sql()
        company_row = conn.execute(
            company_query, (company_id,)).fetchone()
        if company_row is None:
            raise ValueError("AUTHORIZATION_INPUT_INVALID")
        stored_name = dict(company_row)["name"]
        for value in company_values:
            if value != company_id and value != stored_name:
                raise ValueError("AUTHORIZATION_INPUT_INVALID")
    try:
        number = Decimal(state["invoice"]["grand_total"])
        shaped = number.quantize(Decimal("0.01"))
    except Exception:
        raise ValueError("AUTHORIZATION_INPUT_INVALID")
    if shaped != number:
        raise ValueError("AUTHORIZATION_INPUT_INVALID")
    return {
        "company_ids": [company_id],
        "targets": [{"kind": "purchase-invoice", "id": purchase_invoice_id,
                     "state_digest": purchase_invoice_state_digest(state)}],
        "amounts": [{"currency": state["invoice"]["currency"],
                     "value": format(abs(shaped), "f"), "scale": 2}],
    }


def purchase_invoice_result(payload):
    """Pick the posted purchase invoice id and status from one payload."""
    if payload.get("discount_rederived"):
        raise ValueError("AUTHORIZATION_INPUT_INVALID")
    return ("purchase-invoice", payload["purchase_invoice_id"],
            payload["document_status"])


TARGETS = {"journal-entry-state": derive_journal_entry,
           "payment-entry-state": derive_payment_entry,
           "sales-invoice-state": derive_sales_invoice,
           "purchase-invoice-state": derive_purchase_invoice}
RESULTS = {"journal-entry-result": journal_entry_result,
           "payment-entry-result": payment_entry_result,
           "sales-invoice-result": sales_invoice_result,
           "purchase-invoice-result": purchase_invoice_result}


def product_declarations():
    """Build envelope entries for every projected impact row."""
    out = {}
    for action in sorted(action_impact.IMPACT, key=str):
        row = action_impact.IMPACT[action]
        if row.get("target_projection") is None:
            continue
        try:
            derive = TARGETS[row["target_projection"]]
            result = RESULTS[row["result_projection"]]
        except KeyError:
            raise RuntimeError("unknown projection for " + str(action))
        out[action] = {
            "class": row["class"],
            "money": row["money"],
            "json_args": row["json_args"],
            "money_json_paths": row["money_json_paths"],
            "bound_args": row["bound_args"],
            "derive": derive,
            "result": result,
        }
    return out
