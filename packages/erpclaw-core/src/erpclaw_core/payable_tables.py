"""Core's supplier payables tables Made by core's revision
`0015_supplier_payables`; the database refuses every change but the ones named here (triggers in that revision).

- `supplier_payable`: one item per still-owed delivery (or, from `record_opening_balances`, per opening balance):
  insert only, except `outstanding` and `status`, which move only through core's own commits under the read set.
  It names its source voucher by type, id and number, so core never reads a plugin's table.
- `supplier_payment`: one row per payment, `unallocated` held at zero in v3.0; only
  `status` and `reverse_operation_key` change, once, when it is reversed.
- `supplier_payment_allocation`: insert only; a reversal restores each item from these rows, never recomputed.

Kept apart from `payables.py` so `models.py` can register them without importing the ledger.
"""
from __future__ import annotations

from sqlalchemy import CheckConstraint, Column, Date, DateTime, ForeignKey, ForeignKeyConstraint, String, Table, \
    UniqueConstraint

from .db import APPEND, MoneyText, Runtime, metadata, scale_check, sign_check

ITEM_STATUS = ("open", "paid", "reversed")
PAYMENT_STATUS = ("posted", "reversed")


def _amount(table: str, column: str, sign: str) -> list:
    return [CheckConstraint(scale_check(column, "currency"), name=f"ck_{table}_{column}_scale"),
            CheckConstraint(sign_check(column, sign), name=f"ck_{table}_{column}_sign")]


def _quoted(values) -> str:
    return ",".join(f"'{v}'" for v in values)


supplier_payable = Table(
    "supplier_payable", metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), ForeignKey("company.id"), nullable=False),
    Column("supplier_id", String(36), nullable=False),
    Column("source_type", String(40), nullable=False),
    Column("source_id", String(36), nullable=False),
    Column("source_number", String(30), nullable=False),
    Column("posting_date", Date, nullable=False),
    Column("currency", String(3), ForeignKey("currency.code"), nullable=False),
    Column("amount", MoneyText, nullable=False),
    Column("outstanding", MoneyText, nullable=False),
    Column("status", String(10), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(["company_id", "supplier_id"], ["party.company_id", "party.id"],
                         name="fk_supplier_payable_supplier"),
    UniqueConstraint("company_id", "id", "currency", name="uq_supplier_payable_currency_key"),
    UniqueConstraint("company_id", "source_type", "source_id", name="uq_supplier_payable_source"),
    CheckConstraint(f"status in ({_quoted(ITEM_STATUS)})", name="ck_supplier_payable_status"),
    *_amount("supplier_payable", "amount", "positive"),
    *_amount("supplier_payable", "outstanding", "not_negative"),
    CheckConstraint("outstanding::numeric <= amount::numeric", name="ck_supplier_payable_within_amount"),
    CheckConstraint("(status = 'open') = (outstanding::numeric > 0)", name="ck_supplier_payable_open_owes"),
    info={"owner": "core", "runtime": Runtime(insert=True, update=("outstanding", "status"))},
)

supplier_payment = Table(
    "supplier_payment", metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), ForeignKey("company.id"), nullable=False),
    Column("number", String(30), nullable=False),
    Column("supplier_id", String(36), nullable=False),
    Column("posting_date", Date, nullable=False),
    Column("currency", String(3), ForeignKey("currency.code"), nullable=False),
    Column("amount", MoneyText, nullable=False),
    Column("unallocated", MoneyText, nullable=False),
    Column("account_id", String(36), nullable=False),
    Column("status", String(10), nullable=False),
    Column("operation_key", String(36), nullable=True),
    Column("reverse_operation_key", String(36), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(["company_id", "supplier_id"], ["party.company_id", "party.id"],
                         name="fk_supplier_payment_supplier"),
    ForeignKeyConstraint(["company_id", "account_id"], ["account.company_id", "account.id"],
                         name="fk_supplier_payment_account"),
    UniqueConstraint("company_id", "id", "currency", name="uq_supplier_payment_currency_key"),
    UniqueConstraint("company_id", "number", name="uq_supplier_payment_number"),
    UniqueConstraint("operation_key", name="uq_supplier_payment_operation_key"),
    UniqueConstraint("reverse_operation_key", name="uq_supplier_payment_reverse_key"),
    CheckConstraint(f"status in ({_quoted(PAYMENT_STATUS)})", name="ck_supplier_payment_status"),
    *_amount("supplier_payment", "amount", "positive"),
    CheckConstraint(scale_check("unallocated", "currency"), name="ck_supplier_payment_unallocated_scale"),
    CheckConstraint(sign_check("unallocated", "zero"), name="ck_supplier_payment_unallocated_zero"),  # v3.0
    info={"owner": "core", "runtime": Runtime(insert=True, update=("status", "reverse_operation_key"))},
)

supplier_payment_allocation = Table(
    "supplier_payment_allocation", metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), nullable=False),
    Column("payment_id", String(36), nullable=False),
    Column("payable_id", String(36), nullable=False),
    Column("currency", String(3), nullable=False),
    Column("amount", MoneyText, nullable=False),
    ForeignKeyConstraint(["company_id", "payment_id", "currency"],
                         ["supplier_payment.company_id", "supplier_payment.id", "supplier_payment.currency"],
                         name="fk_supplier_allocation_payment"),
    ForeignKeyConstraint(["company_id", "payable_id", "currency"],
                         ["supplier_payable.company_id", "supplier_payable.id", "supplier_payable.currency"],
                         name="fk_supplier_allocation_payable"),
    UniqueConstraint("payment_id", "payable_id", name="uq_supplier_allocation_item"),
    *_amount("supplier_payment_allocation", "amount", "positive"),
    info={"owner": "core", "runtime": APPEND},
)

PAYABLE_TABLES = frozenset({"supplier_payable", "supplier_payment", "supplier_payment_allocation"})
