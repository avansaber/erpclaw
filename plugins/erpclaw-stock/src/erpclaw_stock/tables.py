"""Tables owned by the stock plugin (prefix stock_). Only this plugin writes them.

Created by the plugin's own migration branch (`migrations/versions/`), to the core books rules: every reference to a
company-owned row is a composite key with `company_id`; values are `MoneyText` at the company currency's scale, zero
or positive; quantities are canonical decimals of at most four places, never rounded to money scale. The stock ledger,
its FIFO layers and their takes are append only (the branch's triggers refuse an update or a delete, as for
`ledger_entry`); warehouses and stock items are insert only. A head is the one row that changes: the latest sequence,
quantity, value and date of an item in a warehouse, rewritten by the stock service under the chain head's lock.
"""
from sqlalchemy import (Boolean, CheckConstraint, Column, Date, DateTime, ForeignKey, ForeignKeyConstraint, Index,
                        Integer, String, Table, UniqueConstraint)

from erpclaw_core.db import APPEND, MoneyText, Qty, Runtime, metadata, rate_check, scale_check, sign_check

METHODS = ("fifo", "moving_average")


def qty_check(column: str) -> str:
    """CHECK for a quantity left on hand: canonical decimal text (no trailing zeros), zero or positive, at most four
    places."""
    return f"{column} ~ '^(0|[1-9][0-9]*)(\\.[0-9]{{0,3}}[1-9])?$'"


def _value(table: str, column: str) -> list:
    return [CheckConstraint(scale_check(column, "currency"), name=f"ck_{table}_{column}_scale"),
            CheckConstraint(sign_check(column, "not_negative"), name=f"ck_{table}_{column}_sign")]


def _qty(table: str, column: str) -> list:  # a moved quantity: positive, canonical, at most four places
    return [CheckConstraint(rate_check(column), name=f"ck_{table}_{column}_positive"),
            CheckConstraint(qty_check(column), name=f"ck_{table}_{column}_canonical")]


warehouse = Table(
    "stock_warehouse", metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), ForeignKey("company.id"), nullable=False),
    Column("name", String(140), nullable=False),
    Column("account_id", String(36), nullable=False),  # its inventory account (role stock)
    Column("is_default", Boolean, nullable=False),  # the company's default warehouse: at most one
    Column("position", Integer, nullable=False),  # its order of creation in the company: lists and options follow it
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(["company_id", "account_id"], ["account.company_id", "account.id"],
                         name="fk_stock_warehouse_account"),
    UniqueConstraint("company_id", "id", name="uq_stock_warehouse_company_id"),
    UniqueConstraint("company_id", "name", name="uq_stock_warehouse_name"),
    UniqueConstraint("company_id", "position", name="uq_stock_warehouse_position"),
    CheckConstraint("position > 0", name="ck_stock_warehouse_position"),
    Index("uq_stock_warehouse_default", "company_id", unique=True, postgresql_where="is_default"),
)

stock_item = Table(
    "stock_item", metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), ForeignKey("company.id"), nullable=False),
    Column("item_id", String(36), nullable=False),
    Column("method", String(14), nullable=False),
    Column("default_warehouse_id", String(36), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(["company_id", "item_id"], ["item.company_id", "item.id"], name="fk_stock_item_item"),
    ForeignKeyConstraint(["company_id", "default_warehouse_id"], ["stock_warehouse.company_id", "stock_warehouse.id"],
                         name="fk_stock_item_default_warehouse"),
    UniqueConstraint("company_id", "id", name="uq_stock_item_company_id"),
    UniqueConstraint("company_id", "item_id", name="uq_stock_item_item"),
    CheckConstraint(f"method in {METHODS!r}", name="ck_stock_item_method"),
)

head = Table(
    "stock_head", metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), nullable=False),
    Column("item_id", String(36), nullable=False),
    Column("warehouse_id", String(36), nullable=False),
    Column("currency", String(3), ForeignKey("currency.code"), nullable=False),
    Column("last_seq", Integer, nullable=False),
    Column("qty", Qty, nullable=False),
    Column("value", MoneyText, nullable=False),
    Column("last_date", Date, nullable=False),
    ForeignKeyConstraint(["company_id", "item_id"], ["item.company_id", "item.id"], name="fk_stock_head_item"),
    ForeignKeyConstraint(["company_id", "warehouse_id"], ["stock_warehouse.company_id", "stock_warehouse.id"],
                         name="fk_stock_head_warehouse"),
    UniqueConstraint("company_id", "id", name="uq_stock_head_company_id"),
    UniqueConstraint("company_id", "item_id", "warehouse_id", name="uq_stock_head_pair"),
    CheckConstraint("last_seq > 0", name="ck_stock_head_seq"),
    CheckConstraint(qty_check("qty"), name="ck_stock_head_qty"),
    *_value("stock_head", "value"),
    CheckConstraint("qty::numeric > 0 OR value::numeric = 0", name="ck_stock_head_nothing_left_is_worth_nothing"),
)

entry = Table(
    "stock_ledger_entry", metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), nullable=False),
    Column("item_id", String(36), nullable=False),
    Column("warehouse_id", String(36), nullable=False),
    Column("currency", String(3), ForeignKey("currency.code"), nullable=False),
    Column("posting_date", Date, nullable=False),
    Column("seq", Integer, nullable=False),
    Column("direction", String(3), nullable=False),
    Column("qty", Qty, nullable=False),
    Column("value", MoneyText, nullable=False),
    Column("qty_after", Qty, nullable=False),
    Column("value_after", MoneyText, nullable=False),
    Column("voucher_type", String(40), nullable=False),
    Column("voucher_id", String(36), nullable=False),
    Column("line_no", Integer, nullable=False),
    Column("against_entry_id", String(36), nullable=True),  # a return: the issue it comes back against
    Column("reverses_entry_id", String(36), nullable=True),  # a reversal's mirror: the entry it mirrors
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(["company_id", "item_id"], ["item.company_id", "item.id"], name="fk_stock_entry_item"),
    ForeignKeyConstraint(["company_id", "warehouse_id"], ["stock_warehouse.company_id", "stock_warehouse.id"],
                         name="fk_stock_entry_warehouse"),
    ForeignKeyConstraint(["company_id", "against_entry_id"], ["stock_ledger_entry.company_id", "stock_ledger_entry.id"],
                         name="fk_stock_entry_against"),
    ForeignKeyConstraint(["company_id", "reverses_entry_id"],
                         ["stock_ledger_entry.company_id", "stock_ledger_entry.id"], name="fk_stock_entry_reverses"),
    UniqueConstraint("company_id", "id", name="uq_stock_entry_company_id"),
    UniqueConstraint("company_id", "item_id", "warehouse_id", "seq", name="uq_stock_entry_seq"),
    UniqueConstraint("reverses_entry_id", name="uq_stock_entry_reversed_once"),
    CheckConstraint("seq > 0", name="ck_stock_entry_seq"),
    CheckConstraint("line_no > 0", name="ck_stock_entry_line"),
    CheckConstraint("direction in ('in','out')", name="ck_stock_entry_direction"),
    *_qty("stock_ledger_entry", "qty"),
    CheckConstraint(qty_check("qty_after"), name="ck_stock_ledger_entry_qty_after"),
    *_value("stock_ledger_entry", "value"),
    *_value("stock_ledger_entry", "value_after"),
)

layer = Table(  # FIFO only: one per incoming entry
    "stock_layer", metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), nullable=False),
    Column("entry_id", String(36), nullable=False),
    Column("item_id", String(36), nullable=False),
    Column("warehouse_id", String(36), nullable=False),
    Column("currency", String(3), ForeignKey("currency.code"), nullable=False),
    Column("qty", Qty, nullable=False),
    Column("value", MoneyText, nullable=False),
    Column("group_id", String(36), nullable=False),  # the layer it merged into, or its own id
    ForeignKeyConstraint(["company_id", "entry_id"], ["stock_ledger_entry.company_id", "stock_ledger_entry.id"],
                         name="fk_stock_layer_entry"),
    ForeignKeyConstraint(["company_id", "group_id"], ["stock_layer.company_id", "stock_layer.id"],
                         name="fk_stock_layer_group"),
    UniqueConstraint("company_id", "id", name="uq_stock_layer_company_id"),
    UniqueConstraint("entry_id", name="uq_stock_layer_entry"),
    *_qty("stock_layer", "qty"),
    *_value("stock_layer", "value"),
)

take = Table(  # FIFO only: what an issue took from a group, or what a reversal gave back to it
    "stock_layer_take", metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), nullable=False),
    Column("entry_id", String(36), nullable=False),
    Column("group_id", String(36), nullable=False),
    Column("layer_id", String(36), nullable=True),  # a reversed receipt: its own layer
    Column("currency", String(3), ForeignKey("currency.code"), nullable=False),
    Column("qty", Qty, nullable=False),
    Column("value", MoneyText, nullable=False),
    Column("kind", String(9), nullable=False),
    ForeignKeyConstraint(["company_id", "entry_id"], ["stock_ledger_entry.company_id", "stock_ledger_entry.id"],
                         name="fk_stock_take_entry"),
    ForeignKeyConstraint(["company_id", "group_id"], ["stock_layer.company_id", "stock_layer.id"],
                         name="fk_stock_take_group"),
    ForeignKeyConstraint(["company_id", "layer_id"], ["stock_layer.company_id", "stock_layer.id"],
                         name="fk_stock_take_layer"),
    UniqueConstraint("company_id", "id", name="uq_stock_take_company_id"),
    CheckConstraint("kind in ('take','give_back')", name="ck_stock_take_kind"),
    *_qty("stock_layer_take", "qty"),
    *_value("stock_layer_take", "value"),
)

# Stock-owned vouchers. Count-only columns are a
# nullable storage foundation; only opening has a producer in this release slice.
voucher = Table(
    "stock_voucher", metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), ForeignKey("company.id"), nullable=False),
    Column("kind", String(20), nullable=False),
    Column("number", String(40), nullable=False),
    Column("posting_date", Date, nullable=False),
    Column("reason", String(140), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("company_id", "id", name="uq_stock_voucher_company_id"),
    UniqueConstraint("company_id", "number", name="uq_stock_voucher_number"),
    CheckConstraint("kind in ('opening','count','write_off','goods_received')", name="ck_stock_voucher_kind"),
)

voucher_line = Table(
    "stock_voucher_line", metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), nullable=False),
    Column("voucher_id", String(36), nullable=False),
    Column("line_no", Integer, nullable=False),
    Column("item_id", String(36), nullable=False),
    Column("warehouse_id", String(36), nullable=False),
    Column("currency", String(3), ForeignKey("currency.code"), nullable=False),
    Column("qty_moved", Qty, nullable=False),
    Column("counted_qty", Qty, nullable=True),
    Column("books_qty", Qty, nullable=True),
    Column("rate", Qty, nullable=True),
    Column("direction", String(3), nullable=False),
    Column("value", MoneyText, nullable=False),
    ForeignKeyConstraint(["company_id", "voucher_id"], ["stock_voucher.company_id", "stock_voucher.id"],
                         name="fk_stock_voucher_line_voucher"),
    ForeignKeyConstraint(["company_id", "item_id"], ["item.company_id", "item.id"],
                         name="fk_stock_voucher_line_item"),
    ForeignKeyConstraint(["company_id", "warehouse_id"], ["stock_warehouse.company_id", "stock_warehouse.id"],
                         name="fk_stock_voucher_line_warehouse"),
    UniqueConstraint("company_id", "id", name="uq_stock_voucher_line_company_id"),
    UniqueConstraint("voucher_id", "line_no", name="uq_stock_voucher_line_number"),
    UniqueConstraint("voucher_id", "item_id", "warehouse_id", name="uq_stock_voucher_line_pair"),
    CheckConstraint("line_no > 0", name="ck_stock_voucher_line_number"),
    CheckConstraint("direction in ('in','out')", name="ck_stock_voucher_line_direction"),
    CheckConstraint(qty_check("qty_moved"), name="ck_stock_voucher_line_qty"),
    CheckConstraint(qty_check("counted_qty"), name="ck_stock_voucher_line_counted_qty"),
    CheckConstraint(qty_check("books_qty"), name="ck_stock_voucher_line_books_qty"),
    *_qty("stock_voucher_line", "rate"),
    *_value("stock_voucher_line", "value"),
)

# Goods-received header; its ordered lines are stock ledger entries.
goods_received = Table(
    "stock_goods_received", metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), nullable=False),
    Column("voucher_id", String(36), nullable=False),
    Column("supplier_id", String(36), nullable=False),
    Column("settlement", String(12), nullable=False),
    Column("account_id", String(36), nullable=False),
    Column("number", String(40), nullable=False),
    ForeignKeyConstraint(["company_id", "voucher_id"], ["stock_voucher.company_id", "stock_voucher.id"],
                         name="fk_stock_goods_received_voucher"),
    ForeignKeyConstraint(["company_id", "supplier_id"], ["party.company_id", "party.id"],
                         name="fk_stock_goods_received_supplier"),
    ForeignKeyConstraint(["company_id", "account_id"], ["account.company_id", "account.id"],
                         name="fk_stock_goods_received_account"),
    UniqueConstraint("company_id", "id", name="uq_stock_goods_received_company_id"),
    UniqueConstraint("company_id", "voucher_id", name="uq_stock_goods_received_voucher"),
    UniqueConstraint("company_id", "number", name="uq_stock_goods_received_number"),
    CheckConstraint("settlement in ('paid_now','still_owed')", name="ck_stock_goods_received_settlement"),
)

TABLES = (warehouse, stock_item, head, entry, layer, take, voucher, voucher_line, goods_received)
for _table in TABLES:
    _table.info["owner"] = "stock"
    _table.info["runtime"] = APPEND  # the PostgreSQL runtime role's privileges
# the one row that changes; listed in a card's read set, so approval locks it (the runtime UPDATE mark)
head.info["runtime"] = Runtime(insert=True, update=("last_seq", "qty", "value", "last_date"))
