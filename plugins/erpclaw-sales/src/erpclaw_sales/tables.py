"""Tables owned by the sales plugin (prefix sales_). Only this plugin writes them.

Created by the plugin's own migration branch (`migrations/versions/`), to the
core books rules: every reference to a
company-owned row is a composite key with `company_id`; lines and
allocations carry their document's currency through that key; amounts are
`MoneyText` at the currency's scale; numbers are unique per company. The
branch's triggers keep a submitted document fixed: status only moves
forward, lines go only into a draft, and nothing is replaced or deleted
once submitted.
"""
from sqlalchemy import (CheckConstraint, Column, Date, DateTime, ForeignKey, ForeignKeyConstraint, Integer, String,
                        Table, Text, UniqueConstraint)

from erpclaw_core.db import APPEND, Money, MoneyText, Qty, Runtime, metadata, rate_check, scale_check, sign_check

INVOICE_STATUS = "status in ('draft','submitted','partly_paid','paid','credited','cancelled')"
ORDER_STATUS = "status in ('submitted','partly_delivered','delivered','completed')"
PAYMENT_STATUS = "status in ('submitted','cancelled')"
def _sign(column: str, sign: str) -> str:
    return sign_check(column, sign)


def _amount(table: str, column: str, sign: str) -> list:
    return [CheckConstraint(scale_check(column, "currency"), name=f"ck_{table}_{column}_scale"),
            CheckConstraint(_sign(column, sign), name=f"ck_{table}_{column}_sign")]


invoice = Table(
    "sales_invoice", metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), ForeignKey("company.id"), nullable=False),
    Column("number", String(30), nullable=False),
    Column("customer_id", String(36), nullable=False),
    Column("posting_date", Date, nullable=False),
    Column("due_date", Date, nullable=False),
    Column("currency", String(3), ForeignKey("currency.code"), nullable=False),
    Column("total", MoneyText, nullable=False),
    Column("outstanding", MoneyText, nullable=False),
    Column("status", String(14), nullable=False),
    Column("operation_key", String(36), nullable=True),
    Column("submit_operation_key", String(36), nullable=True),
    Column("cancel_operation_key", String(36), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(["company_id", "customer_id"], ["party.company_id", "party.id"],
                         name="fk_sales_invoice_customer"),
    UniqueConstraint("company_id", "id", name="uq_sales_invoice_company_id"),
    UniqueConstraint("company_id", "id", "currency", name="uq_sales_invoice_currency_key"),
    UniqueConstraint("company_id", "number", name="uq_sales_invoice_number"),
    UniqueConstraint("operation_key", name="uq_sales_invoice_operation_key"),
    UniqueConstraint("submit_operation_key", name="uq_sales_invoice_submit_key"),
    UniqueConstraint("cancel_operation_key", name="uq_sales_invoice_cancel_key"),
    CheckConstraint(INVOICE_STATUS, name="ck_inv_status"),
    *_amount("sales_invoice", "total", "positive"),
    *_amount("sales_invoice", "outstanding", "not_negative"),
    # a draft owes nothing: a state rule, so it holds for restored rows too (amendment r4, 2.4)
    CheckConstraint(f"status <> 'draft' OR ({_sign('outstanding', 'zero')})", name="ck_sales_invoice_draft_owes_nothing"),
    # status follows outstanding (the reversal revision, sales_0002; credit notes, sales_0003)
    *[CheckConstraint(check, name=name) for name, check in (
        ("ck_sales_invoice_paid_owes_nothing", "status <> 'paid' OR outstanding::numeric = 0"),
        ("ck_sales_invoice_partly_paid_between",
         "status <> 'partly_paid' OR (outstanding::numeric > 0 AND outstanding::numeric < total::numeric)"),
        ("ck_sales_invoice_submitted_owes",
         "status <> 'submitted' OR (outstanding::numeric > 0 AND outstanding::numeric <= total::numeric)"),
        ("ck_sales_invoice_credited_owes_nothing", "status <> 'credited' OR outstanding::numeric = 0"),
        ("ck_sales_invoice_cancelled_owes_nothing", "status <> 'cancelled' OR outstanding::numeric = 0"))],
)

invoice_line = Table(
    "sales_invoice_line", metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), nullable=False),
    Column("invoice_id", String(36), nullable=False),
    Column("currency", String(3), nullable=False),
    Column("item_id", String(36), nullable=False),
    Column("quantity", Qty, nullable=False),
    Column("rate", Money, nullable=False),
    Column("amount", MoneyText, nullable=False),
    Column("line_no", Integer, nullable=True),  # its place on the invoice, from sales_0003 (older lines: none)
    Column("named_warehouse_id", String(36), nullable=True),  # a stock line's warehouse, as the owner named it
    Column("item_kind", String(8), nullable=True),  # the item's kind when the line was written (older lines: none)
    ForeignKeyConstraint(["company_id", "invoice_id", "currency"],
                         ["sales_invoice.company_id", "sales_invoice.id", "sales_invoice.currency"],
                         name="fk_sales_invoice_line_invoice"),
    ForeignKeyConstraint(["company_id", "item_id"], ["item.company_id", "item.id"], name="fk_sales_invoice_line_item"),
    UniqueConstraint("company_id", "id", name="uq_sales_invoice_line_company_id"),
    CheckConstraint(rate_check("quantity"), name="ck_sales_invoice_line_quantity"),
    CheckConstraint(rate_check("rate"), name="ck_sales_invoice_line_rate"),
    *_amount("sales_invoice_line", "amount", "positive"),
)

payment = Table(
    "sales_payment", metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), ForeignKey("company.id"), nullable=False),
    Column("number", String(30), nullable=False),
    Column("customer_id", String(36), nullable=False),
    Column("posting_date", Date, nullable=False),
    Column("currency", String(3), ForeignKey("currency.code"), nullable=False),
    Column("amount", MoneyText, nullable=False),
    Column("unallocated", MoneyText, nullable=False),
    Column("method", String(60), nullable=True),
    Column("reference", Text, nullable=True),
    Column("status", String(12), nullable=False),
    Column("operation_key", String(36), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("cancel_operation_key", String(36), nullable=True),
    ForeignKeyConstraint(["company_id", "customer_id"], ["party.company_id", "party.id"],
                         name="fk_sales_payment_customer"),
    UniqueConstraint("company_id", "id", name="uq_sales_payment_company_id"),
    UniqueConstraint("company_id", "id", "currency", name="uq_sales_payment_currency_key"),
    UniqueConstraint("company_id", "number", name="uq_sales_payment_number"),
    UniqueConstraint("operation_key", name="uq_sales_payment_operation_key"),
    UniqueConstraint("cancel_operation_key", name="uq_sales_payment_cancel_key"),
    CheckConstraint(PAYMENT_STATUS, name="ck_pay_status"),
    *_amount("sales_payment", "amount", "positive"),
    *_amount("sales_payment", "unallocated", "not_negative"),
)

allocation = Table(
    "sales_payment_allocation", metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), nullable=False),
    Column("payment_id", String(36), nullable=False),
    Column("invoice_id", String(36), nullable=False),
    Column("currency", String(3), nullable=False),
    Column("amount", MoneyText, nullable=False),
    ForeignKeyConstraint(["company_id", "payment_id", "currency"],
                         ["sales_payment.company_id", "sales_payment.id", "sales_payment.currency"],
                         name="fk_sales_allocation_payment"),
    ForeignKeyConstraint(["company_id", "invoice_id", "currency"],
                         ["sales_invoice.company_id", "sales_invoice.id", "sales_invoice.currency"],
                         name="fk_sales_allocation_invoice"),
    UniqueConstraint("company_id", "id", name="uq_sales_allocation_company_id"),
    *_amount("sales_payment_allocation", "amount", "positive"),
)

# ---------------------------------------------------------------- stock-backed documents (sales_0003)
# A warehouse or stock entry is the stock plugin's id, kept as the stock service returned it: sales never reads a
# stock table, and its dependency on stock is optional, so there is no key into one.

def _line_key(table: str, column: str, target: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(["company_id", column], [f"{target}.company_id", f"{target}.id"],
                                name=f"fk_{table}_{column.removesuffix('_id')}")


def _stock_cost(table: str, issued: bool = True, stocked: bool = True) -> list:
    """What moved, at what cost, and the stock entry; an issue also keeps its line on the stock voucher, which a
    return names to come back against. A line that need not move stock (`stocked` false) may have no entry."""
    return [Column("quantity", Qty, nullable=False), Column("cost", MoneyText, nullable=False),
            Column("stock_entry_id", String(36), nullable=not stocked),
            *([Column("stock_line", Integer, nullable=not stocked)] if issued else []),
            CheckConstraint(rate_check("quantity"), name=f"ck_{table}_quantity"), *_amount(table, "cost", "not_negative")]


invoice_issue = Table(  # where each stock line of an invoice was taken from, at its submission
    "sales_invoice_issue", metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), nullable=False),
    Column("invoice_line_id", String(36), nullable=False),
    Column("currency", String(3), nullable=False),
    Column("warehouse_id", String(36), nullable=False),
    *_stock_cost("sales_invoice_issue"),
    _line_key("sales_invoice_issue", "invoice_line_id", "sales_invoice_line"),
    UniqueConstraint("company_id", "id", name="uq_sales_invoice_issue_company_id"),
    UniqueConstraint("invoice_line_id", name="uq_sales_invoice_issue_line"),
)

order = Table(
    "sales_order", metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), ForeignKey("company.id"), nullable=False),
    Column("number", String(30), nullable=False),
    Column("customer_id", String(36), nullable=False),
    Column("posting_date", Date, nullable=False),
    Column("delivery_date", Date, nullable=False),
    Column("currency", String(3), ForeignKey("currency.code"), nullable=False),
    Column("total", MoneyText, nullable=False),
    Column("status", String(16), nullable=False),
    Column("operation_key", String(36), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(["company_id", "customer_id"], ["party.company_id", "party.id"],
                         name="fk_sales_order_customer"),
    UniqueConstraint("company_id", "id", name="uq_sales_order_company_id"),
    UniqueConstraint("company_id", "id", "currency", name="uq_sales_order_currency_key"),
    UniqueConstraint("company_id", "number", name="uq_sales_order_number"),
    UniqueConstraint("operation_key", name="uq_sales_order_operation_key"),
    CheckConstraint(ORDER_STATUS, name="ck_sales_order_status"),
    *_amount("sales_order", "total", "positive"),
)

order_line = Table(
    "sales_order_line", metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), nullable=False),
    Column("order_id", String(36), nullable=False),
    Column("currency", String(3), nullable=False),
    Column("line_no", Integer, nullable=False),
    Column("item_id", String(36), nullable=False),
    Column("quantity", Qty, nullable=False),
    Column("rate", Money, nullable=False),
    Column("amount", MoneyText, nullable=False),
    ForeignKeyConstraint(["company_id", "order_id", "currency"],
                         ["sales_order.company_id", "sales_order.id", "sales_order.currency"],
                         name="fk_sales_order_line_order"),
    ForeignKeyConstraint(["company_id", "item_id"], ["item.company_id", "item.id"], name="fk_sales_order_line_item"),
    UniqueConstraint("company_id", "id", name="uq_sales_order_line_company_id"),
    UniqueConstraint("order_id", "line_no", name="uq_sales_order_line_no"),
    CheckConstraint(rate_check("quantity"), name="ck_sales_order_line_quantity"),
    CheckConstraint(rate_check("rate"), name="ck_sales_order_line_rate"),
    *_amount("sales_order_line", "amount", "positive"),
)

delivery = Table(
    "sales_delivery", metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), ForeignKey("company.id"), nullable=False),
    Column("number", String(30), nullable=False),
    Column("order_id", String(36), nullable=False),
    Column("posting_date", Date, nullable=False),
    Column("operation_key", String(36), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    _line_key("sales_delivery", "order_id", "sales_order"),
    UniqueConstraint("company_id", "id", name="uq_sales_delivery_company_id"),
    UniqueConstraint("company_id", "number", name="uq_sales_delivery_number"),
    UniqueConstraint("operation_key", name="uq_sales_delivery_operation_key"),
)

delivery_line = Table(
    "sales_delivery_line", metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), nullable=False),
    Column("delivery_id", String(36), nullable=False),
    Column("order_line_id", String(36), nullable=False),
    Column("currency", String(3), nullable=False),
    Column("warehouse_id", String(36), nullable=True),  # none for a service line
    *_stock_cost("sales_delivery_line", stocked=False),
    _line_key("sales_delivery_line", "delivery_id", "sales_delivery"),
    _line_key("sales_delivery_line", "order_line_id", "sales_order_line"),
    UniqueConstraint("company_id", "id", name="uq_sales_delivery_line_company_id"),
)

invoice_delivery = Table(  # the delivery lines an invoice line made from an order covers, oldest delivery first
    "sales_invoice_delivery", metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), nullable=False),
    Column("invoice_line_id", String(36), nullable=False),
    Column("delivery_line_id", String(36), nullable=False),
    Column("quantity", Qty, nullable=False),
    _line_key("sales_invoice_delivery", "invoice_line_id", "sales_invoice_line"),
    _line_key("sales_invoice_delivery", "delivery_line_id", "sales_delivery_line"),
    UniqueConstraint("company_id", "id", name="uq_sales_invoice_delivery_company_id"),
    CheckConstraint(rate_check("quantity"), name="ck_sales_invoice_delivery_quantity"),
)

credit_note = Table(
    "sales_credit_note", metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), ForeignKey("company.id"), nullable=False),
    Column("number", String(30), nullable=False),
    Column("invoice_id", String(36), nullable=False),
    Column("customer_id", String(36), nullable=False),
    Column("posting_date", Date, nullable=False),
    Column("currency", String(3), ForeignKey("currency.code"), nullable=False),
    Column("total", MoneyText, nullable=False),
    Column("operation_key", String(36), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(["company_id", "invoice_id", "currency"],
                         ["sales_invoice.company_id", "sales_invoice.id", "sales_invoice.currency"],
                         name="fk_sales_credit_note_invoice"),
    ForeignKeyConstraint(["company_id", "customer_id"], ["party.company_id", "party.id"],
                         name="fk_sales_credit_note_customer"),
    UniqueConstraint("company_id", "id", name="uq_sales_credit_note_company_id"),
    UniqueConstraint("company_id", "id", "currency", name="uq_sales_credit_note_currency_key"),
    UniqueConstraint("company_id", "number", name="uq_sales_credit_note_number"),
    UniqueConstraint("operation_key", name="uq_sales_credit_note_operation_key"),
    *_amount("sales_credit_note", "total", "positive"),
)

credit_note_line = Table(
    "sales_credit_note_line", metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), nullable=False),
    Column("credit_note_id", String(36), nullable=False),
    Column("currency", String(3), nullable=False),
    Column("line_no", Integer, nullable=False),
    Column("invoice_line_id", String(36), nullable=False),
    Column("item_id", String(36), nullable=False),
    Column("quantity", Qty, nullable=False),
    Column("rate", Money, nullable=False),
    Column("amount", MoneyText, nullable=False),
    ForeignKeyConstraint(["company_id", "credit_note_id", "currency"],
                         ["sales_credit_note.company_id", "sales_credit_note.id", "sales_credit_note.currency"],
                         name="fk_sales_credit_note_line_note"),
    _line_key("sales_credit_note_line", "invoice_line_id", "sales_invoice_line"),
    ForeignKeyConstraint(["company_id", "item_id"], ["item.company_id", "item.id"],
                         name="fk_sales_credit_note_line_item"),
    UniqueConstraint("company_id", "id", name="uq_sales_credit_note_line_company_id"),
    UniqueConstraint("credit_note_id", "line_no", name="uq_sales_credit_note_line_no"),
    CheckConstraint(rate_check("quantity"), name="ck_sales_credit_note_line_quantity"),
    CheckConstraint(rate_check("rate"), name="ck_sales_credit_note_line_rate"),
    *_amount("sales_credit_note_line", "amount", "not_negative"),
)

credit_note_return = Table(  # each part of a credit line's stock, against the issue it came from, at that cost
    "sales_credit_note_return", metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), nullable=False),
    Column("credit_note_line_id", String(36), nullable=False),
    Column("currency", String(3), nullable=False),
    Column("original_voucher_type", String(40), nullable=False),
    Column("original_voucher_id", String(36), nullable=False),
    Column("original_line", Integer, nullable=False),
    Column("warehouse_id", String(36), nullable=False),
    *_stock_cost("sales_credit_note_return", issued=False),
    _line_key("sales_credit_note_return", "credit_note_line_id", "sales_credit_note_line"),
    UniqueConstraint("company_id", "id", name="uq_sales_credit_note_return_company_id"),
)

STOCK_TABLES = (invoice_issue, order, order_line, delivery, delivery_line, invoice_delivery, credit_note,
                credit_note_line, credit_note_return)

# Ownership is a mark on each table (plugin interface: not inferred from import order).
for _table in (invoice, invoice_line, payment, allocation, *STOCK_TABLES):
    _table.info["owner"] = "sales"
    _table.info["runtime"] = APPEND  # the PostgreSQL runtime role's privileges
invoice.info["runtime"] = Runtime(insert=True, update=("status", "outstanding", "submit_operation_key",
                                                       "cancel_operation_key"))
payment.info["runtime"] = Runtime(insert=True, update=("status", "cancel_operation_key"))  # a reversal (sales_0002)
order.info["runtime"] = Runtime(insert=True, update=("status",))  # delivered and invoiced (sales_0003)
