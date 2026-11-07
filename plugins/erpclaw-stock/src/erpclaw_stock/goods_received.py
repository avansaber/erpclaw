"""Inactive goods-received backend, not a model/user contract.

Requests contain engine-resolved IDs. Their caller must establish owner/supplier
provenance; this module does not. No Contract, public Input or registry entry is
created. Commit additionally requires this exact future stock-owned approval, so
it cannot run through another stock card while that contract remains absent.
Delivery reversal and managed grounding remain separate activation prerequisites.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, localcontext
import hashlib
import json

from sqlalchemy import select

from erpclaw_core import integration, ledger, models as m, payables
from erpclaw_core.common import Ctx, Refusal, new_id, now
from erpclaw_core.contracts import Amount, Preview, Result, Shown
from erpclaw_core.money import MoneyError, fmt_qty, to_amount
from erpclaw_core.numbering import next_number

from . import service as s, tables as t
from .valuation import receipt_value

NAME = "record_goods_received"
VOUCHER = "stock_goods_received"


@dataclass(frozen=True)
class ReceiptLine:
    item_id: str
    quantity: object
    rate: object
    warehouse_id: str | None = None


@dataclass(frozen=True)
class GoodsReceived:
    supplier_id: str
    lines: tuple[ReceiptLine, ...]
    posting_date: date
    settlement: str
    bank_account_id: str | None = None


@dataclass
class LinePlan:
    item: dict
    stock_item: dict
    warehouse: dict
    account: dict
    head: dict | None
    quantity: Decimal
    rate: Decimal
    value: Decimal


@dataclass
class ReceiptPlan:
    request: GoodsReceived
    supplier: dict
    period: dict
    lines: list[LinePlan]
    accounts: dict[str, dict]
    counter: dict
    posting: list[ledger.Line]
    total: Decimal
    owed_after: Decimal | None
    read_set: list[tuple[str, str]]
    mode: str
    external: str | None
    codes: dict[str, str]


def _id(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value or len(value) > 36:
        raise Refusal(f"The {field} must be an engine-resolved record of these books.", "goods_received_input")
    return value


def _destination(ctx: Ctx, stock: dict, warehouse_id: str | None) -> dict:
    """A receipt needs no existing holdings: named ID, item default, company default, question."""
    if warehouse_id is not None:
        return s.warehouse(ctx, _id(warehouse_id, "warehouse"))
    if stock["default_warehouse_id"] is not None:
        return s.warehouse(ctx, stock["default_warehouse_id"])
    warehouses = s.warehouses(ctx)
    default = next((row for row in warehouses if row["is_default"]), None)
    if default is not None:
        return default
    if not warehouses:
        raise Refusal("These books have no warehouses; add one before recording a delivery.", "no_warehouse")
    names = [row["name"] for row in warehouses]
    raise Refusal(f"Which warehouse received the goods: {'; '.join(names)}?", "warehouse_ambiguous",
                  options=names, field="warehouse")


def _bank(ctx: Ctx, account_id: str | None) -> dict:
    if account_id is None:
        raise Refusal("Choose the bank or cash account this delivery was paid from.", "account_missing")
    row = ctx.conn.execute(select(m.account).where(m.account.c.company_id == ctx.company_id,
        m.account.c.id == _id(account_id, "paying account"))).mappings().first()
    if row is None or row["account_type"] != "bank" or row["is_group"]:
        raise Refusal("The delivery's paying account must be a bank-type account of these books.", "account_not_bank")
    if row["frozen"]:
        raise Refusal(f"{row['number']} {row['name']} is frozen.", "account_frozen")
    return dict(row)


def _value(value: Decimal, currency: str) -> Decimal:
    try:
        return to_amount(value, currency)
    except MoneyError as exc:
        raise Refusal(f"Those quantities and unit costs cannot form a goods-received posting: {exc}.",
                      "goods_received_value") from None


def plan_goods_received(ctx: Ctx, request: GoodsReceived, *, lock: bool = False) -> ReceiptPlan:
    """One shared no-write plan; no number allocation, speculative FIFO moves or owner-name resolution."""
    if not isinstance(request, GoodsReceived) or not isinstance(request.lines, (tuple, list)) or not 1 <= len(request.lines) <= 20:
        raise Refusal("A delivery needs one to twenty resolved stock lines.", "goods_received_input")
    if not isinstance(request.posting_date, date) or isinstance(request.posting_date, datetime):
        raise Refusal("A delivery needs its resolved posting date.", "goods_received_input")
    if request.settlement not in ("paid_now", "still_owed"):
        raise Refusal("Say whether this delivery was paid now or is still owed.", "goods_received_settlement")
    if request.settlement == "still_owed" and request.bank_account_id is not None:
        raise Refusal("A delivery still owed cannot also name a paying account.", "goods_received_settlement")
    supplier = payables._supplier(ctx, _id(request.supplier_id, "supplier"))
    period = dict(ledger.open_period(ctx, request.posting_date))
    counter = _bank(ctx, request.bank_account_id) if request.settlement == "paid_now" else payables.payable_account(ctx)
    accounts = {counter["id"]: counter}
    rows = {("company", ctx.company_id), ("party", supplier["id"]), ("fiscal_period", period["id"]),
            ("fiscal_year", period["fiscal_year_id"]), ("account", counter["id"])}
    planned = []
    # Receipt values depend on this line's quantity/rate, not on earlier receipt
    # lines. Real sequential FIFO/head updates happen once in commit below.
    with localcontext() as precision:
        precision.prec = 60
        debit_by_account = {}
        for line in request.lines:
            if not isinstance(line, ReceiptLine):
                raise Refusal("A delivery line must be resolved by the engine.", "goods_received_input")
            item = ctx.conn.execute(select(m.item).where(m.item.c.company_id == ctx.company_id,
                m.item.c.id == _id(line.item_id, "item"))).mappings().first()
            if item is None or item["kind"] != "stock":
                raise Refusal("That delivery item is not a stock item of these books.", "item_not_stock")
            item = dict(item)
            stock = s.stock_item(ctx, item["id"])
            warehouse = _destination(ctx, stock, line.warehouse_id)
            account = s._inventory_account_row(ctx, warehouse["account_id"], warehouse["name"])
            head = s.head(ctx, item["id"], warehouse["id"], lock=lock)
            s.check_date(ctx, request.posting_date, head)
            quantity, rate = s.parse_qty(line.quantity), s.parse_qty(line.rate, "rate")
            value = _value(receipt_value(quantity, rate, ctx.currency), ctx.currency)
            if value == 0:
                raise Refusal(f"That receipt is worth less than {s.smallest_unit(ctx.currency)} at that rate.",
                              "receipt_below_minor_unit", unit=s.smallest_unit(ctx.currency))
            planned.append(LinePlan(item, stock, warehouse, account, head, quantity, rate, value))
            accounts[account["id"]] = account
            debit_by_account[account["id"]] = debit_by_account.get(account["id"], Decimal(0)) + value
            rows.update({("item", item["id"]), ("stock_item", stock["id"]),
                         ("stock_warehouse", warehouse["id"]), ("account", account["id"])})
            if head is not None:
                rows.add(("stock_head", head["id"]))
        total = _value(sum(debit_by_account.values(), Decimal(0)), ctx.currency)
        posting = [ledger.Line(account, debit=_value(value, ctx.currency)) for account, value in debit_by_account.items()]
        posting.append(ledger.Line(counter["id"], credit=total,
                                   party_id=supplier["id"] if request.settlement == "still_owed" else None))
        owed_after = None
        if request.settlement == "still_owed":
            items = payables.open_items(ctx, supplier["id"])
            rows.update(("supplier_payable", row["id"]) for row in items)
            owed_after = sum((to_amount(row["outstanding"], ctx.currency) for row in items), total)
    ledger.check_post(ctx, request.posting_date, VOUCHER, None, posting)
    mode, external = integration.system_of_record(ctx, "accounting")
    codes = {}
    if mode == "external":
        ids = list(accounts)
        codes = integration.account_codes(ctx, external, ids)
        rows.update(("account_map", key) for key in ctx.conn.execute(select(m.account_map.c.id).where(
            m.account_map.c.company_id == ctx.company_id, m.account_map.c.external_system == external,
            m.account_map.c.account_id.in_(ids))).scalars())
    return ReceiptPlan(request, supplier, period, planned, accounts, counter, posting, total, owed_after,
                       sorted(rows), mode, external, codes)


def preview_goods_received(ctx: Ctx, request: GoodsReceived) -> Preview:
    plan = plan_goods_received(ctx, request)
    sh = Shown(ctx.currency)
    sh.title("Record goods received")
    sh.line("Supplier", plan.supplier["name"])
    sh.line("Date", f"{request.posting_date.day} {request.posting_date:%b %Y}")
    for line in plan.lines:
        on_hand = Decimal(0) if line.head is None else line.head["qty"]
        sh.line(line.item["name"], f"{fmt_qty(line.quantity)} {line.item['unit']} at "
                f"{sh.unit_price(line.rate)} {ctx.currency} per {line.item['unit']}; "
                f"into {line.warehouse['name']} ({fmt_qty(on_hand)} on hand now): {sh.amount(line.value)} {ctx.currency}")
    sh.line("Total", f"{sh.amount(plan.total)} {ctx.currency}")
    sh.line("Settlement", f"Paid from {plan.counter['name']} ({plan.counter['number']})" if request.settlement == "paid_now"
            else f"Owed to {plan.supplier['name']}")
    if plan.owed_after is not None:
        sh.line("Total owed", f"{plan.supplier['name']} will be owed {sh.amount(plan.owed_after)} {ctx.currency} in total.")
    sh.line("Accounting destination", "ERPClaw general ledger" if plan.mode == "native" else
            f"{plan.external}: ERPClaw sub-ledger, with the journal staged for this system")
    for line in plan.posting:
        account = plan.accounts[line.account_id]
        mapped = f" (external account {plan.codes[line.account_id]})" if plan.mode == "external" else ""
        party = f" ({plan.supplier['name']})" if line.party_id else ""
        side, value = ("debit", line.debit) if line.debit else ("credit", line.credit)
        sh.book(f"{account['number']} {account['name']}{party}{mapped}: {side} {sh.amount(value)} {ctx.currency}")
    card = sh.card()
    token = hashlib.sha256(json.dumps({"card": card.as_dict(), "rows": plan.read_set}, sort_keys=True).encode()).hexdigest()
    return Preview(card, token)


def goods_received_read_set(ctx: Ctx, request: GoodsReceived) -> list[tuple[str, str]]:
    return plan_goods_received(ctx, request).read_set


def _authorize(ctx: Ctx) -> None:
    from erpclaw_core.plugins import REGISTRY
    from erpclaw_core.approvals import committing

    payables._authorize(ctx)  # live core authority plus company/core-or-stock checks
    approval_id = committing(ctx)
    contract = ctx.conn.execute(select(m.approval.c.contract).where(m.approval.c.id == approval_id,
        m.approval.c.company_id == ctx.company_id)).scalar_one_or_none() if approval_id else None
    # The public contract is deliberately absent. Another registered stock card
    # cannot grant this operation; no caller flag or Trusted argument bypasses it.
    if contract != NAME or REGISTRY.contract_plugin.get(NAME) != "stock":
        raise Refusal("Goods received runs only inside its own approved stock card.", "not_approved")


def commit_goods_received(ctx: Ctx, request: GoodsReceived, operation_key: str) -> Result:
    _authorize(ctx)
    plan = plan_goods_received(ctx, request, lock=True)
    s.check_fresh(ctx, [], plan.read_set)
    voucher_id = new_id()
    ledger.check_post(ctx, request.posting_date, VOUCHER, voucher_id, plan.posting)
    number = next_number(ctx, "GR", request.posting_date)
    ctx.conn.execute(t.voucher.insert().values(id=voucher_id, company_id=ctx.company_id, kind="goods_received",
        number=number, posting_date=request.posting_date, reason=None, created_at=now()))
    ctx.conn.execute(t.goods_received.insert().values(id=new_id(), company_id=ctx.company_id, voucher_id=voucher_id,
        supplier_id=plan.supplier["id"], settlement=request.settlement, account_id=plan.counter["id"], number=number))
    # The next receipt of a repeated pair sees this transaction's prior entry/head,
    # preserving each owner line and the existing FIFO grouping rules.
    for n, line in enumerate(plan.lines, 1):
        move = s._receipt_move(ctx, line.item["id"], line.warehouse["id"], line.quantity, line.rate,
                               request.posting_date, lock=True)
        move.line_no = n
        if move.qty != line.quantity or move.value != line.value:
            raise Refusal("The delivery's stock value changed; nothing was saved. Ask for a fresh card.", "stale")
        s._entry_id(ctx, move, request.posting_date, VOUCHER, voucher_id)
    if request.settlement == "still_owed":
        actual = payables.open_item(ctx, supplier_id=plan.supplier["id"], source_type=VOUCHER, source_id=voucher_id,
            source_number=number, posting_date=request.posting_date, amount=plan.total)
        expected = plan.posting[-1]
        if (actual.account_id, actual.debit, actual.credit, actual.party_id) != (
                expected.account_id, expected.debit, expected.credit, expected.party_id):
            raise Refusal("The delivery's payable changed; nothing was saved. Ask for a fresh card.", "stale")
    ledger.post(ctx, request.posting_date, VOUCHER, voucher_id, plan.posting, operation_key=operation_key)
    sh = Shown(ctx.currency)
    amount = sh.amount(plan.total, "value", number)
    return Result(f"Recorded goods received {number}, dated {request.posting_date.day} {request.posting_date:%b %Y}: "
        f"{amount} {ctx.currency}.", {"voucher_type": VOUCHER, "voucher_id": voucher_id, "number": number,
        "posting_date": request.posting_date.isoformat()}, [amount], sh.amounts)


def find_goods_received(ctx: Ctx, operation_key: str) -> Result | None:
    outcome, approval = m.operation_outcome, m.approval
    raw = ctx.conn.execute(select(outcome.c.result_json).join(approval, approval.c.id == outcome.c.approval_id).where(
        outcome.c.company_id == ctx.company_id, outcome.c.key == operation_key, outcome.c.status == "committed",
        approval.c.contract == NAME)).scalar_one_or_none()
    if raw is None:
        return None
    result = json.loads(raw)
    return Result(result["text"], result.get("data", {}), result.get("figures", []),
                  [Amount(**amount) for amount in result.get("amounts", [])])
