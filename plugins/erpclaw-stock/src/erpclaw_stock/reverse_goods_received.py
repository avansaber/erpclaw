"""Inactive delivery reversal, using stored delivery facts and exact mirrors.

No public Contract/Input/registry or owner-name resolver is supplied. The caller
owns provenance, the company chain, approved read-set/recompute and savepoint.
An exact future stock approval is required; another card or a caller flag cannot
activate this private backend while that contract remains absent.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, localcontext
import hashlib
import json

from sqlalchemy import select

from erpclaw_core import integration, ledger, models as m, payables
from erpclaw_core.common import Ctx, Refusal
from erpclaw_core.contracts import Amount, Preview, Result, Shown
from erpclaw_core.money import fmt_qty, to_amount

from . import service as s, tables as t

NAME = "reverse_goods_received"
VOUCHER = "stock_goods_received"


@dataclass(frozen=True)
class ReverseGoodsReceived:
    voucher_id: str
    reversal_date: date


@dataclass
class ReversalPlan:
    request: ReverseGoodsReceived
    voucher: dict
    header: dict
    supplier: dict
    posting: dict
    moves: list[s.Move]
    mirror: list[ledger.Line]
    total: Decimal
    payable: dict | None
    items: dict[str, dict]
    accounts: dict[str, dict]
    read_set: list[tuple[str, str]]
    mode: str
    external: str | None
    codes: dict[str, str]


def _inconsistent() -> Refusal:
    return Refusal("That delivery's stored stock, settlement and posting do not agree; nothing was saved.",
                   "goods_received_inconsistent")


def _document(ctx: Ctx, request: ReverseGoodsReceived) -> tuple[dict, dict]:
    if (not isinstance(request, ReverseGoodsReceived) or not isinstance(request.voucher_id, str)
            or not request.voucher_id or request.voucher_id.strip() != request.voucher_id
            or len(request.voucher_id) > 36 or not isinstance(request.reversal_date, date)
            or isinstance(request.reversal_date, datetime)):
        raise Refusal("A delivery reversal needs its resolved document and reversal date.", "goods_received_input")
    voucher = ctx.conn.execute(select(t.voucher).where(t.voucher.c.company_id == ctx.company_id,
        t.voucher.c.id == request.voucher_id)).mappings().first()
    if voucher is None or voucher["kind"] != "goods_received":
        raise Refusal("There is no such goods-received document in these books.", "goods_received_missing")
    header = ctx.conn.execute(select(t.goods_received).where(t.goods_received.c.company_id == ctx.company_id,
        t.goods_received.c.voucher_id == request.voucher_id)).mappings().first()
    if header is None or header["number"] != voucher["number"] or header["settlement"] not in ("paid_now", "still_owed"):
        raise _inconsistent()
    return dict(voucher), dict(header)


def plan_reverse_goods_received(ctx: Ctx, request: ReverseGoodsReceived, *, lock: bool = False) -> ReversalPlan:
    """Recompute before effects, locking stock heads before the owed item's row."""
    voucher, header = _document(ctx, request)
    supplier = payables._supplier(ctx, header["supplier_id"])
    moves = s._reverse_moves(ctx, VOUCHER, voucher["id"], request.reversal_date, lock=lock)
    posting = ledger.posting_for_voucher(ctx, VOUCHER, voucher["id"])
    if posting["posting_date"] != voucher["posting_date"]:
        raise _inconsistent()
    mirror = ledger.check_reverse(ctx, posting["id"], request.reversal_date)
    period = dict(ledger.open_period(ctx, request.reversal_date))
    rows = {("company", ctx.company_id), ("party", supplier["id"]), ("stock_voucher", voucher["id"]),
            ("stock_goods_received", header["id"]), ("posting", posting["id"]),
            ("fiscal_period", period["id"]), ("fiscal_year", period["fiscal_year_id"])}
    rows.update(("ledger_entry", key) for key in ctx.conn.execute(select(m.ledger_entry.c.id).where(
        m.ledger_entry.c.company_id == ctx.company_id, m.ledger_entry.c.posting_id == posting["id"])).scalars())
    items, inventory = {}, {}
    with localcontext() as precision:
        precision.prec = 60
        for move in moves:
            original, pair = move.original, move.pair
            if (original is None or original["direction"] != "in" or original["against_entry_id"] is not None
                    or move.direction != "out" or move.value <= 0):
                raise _inconsistent()
            inventory[pair.account] = inventory.get(pair.account, Decimal(0)) + move.value
            if pair.item["item_id"] not in items:
                items[pair.item["item_id"]] = s.company_item(ctx, pair.item["item_id"])
            rows.update({("item", pair.item["item_id"]), ("stock_item", pair.item["id"]),
                         ("stock_warehouse", pair.wh["id"]), ("stock_ledger_entry", original["id"])})
            if pair.head is not None:
                rows.add(("stock_head", pair.head["id"]))
            if move.take_layer is not None:
                rows.add(("stock_layer", move.take_layer))
        total = to_amount(sum(inventory.values(), Decimal(0)), ctx.currency)
    # The forward producer stores one inventory debit per account and one
    # settlement credit. Mirror those immutable lines, never reselect today's
    # payable/bank/counter role. Refuse inconsistent restored headers/rows.
    party = supplier["id"] if header["settlement"] == "still_owed" else None
    expected = [(key, Decimal(0), value, None) for key, value in inventory.items()]
    expected.append((header["account_id"], total, Decimal(0), party))
    actual = [(line.account_id, line.debit, line.credit, line.party_id) for line in mirror]
    if sorted(actual, key=repr) != sorted(expected, key=repr):
        raise _inconsistent()
    payable = None
    if header["settlement"] == "still_owed":
        payable = payables.check_close_item(ctx, VOUCHER, voucher["id"], lock=lock)
        if (payable["supplier_id"] != supplier["id"] or payable["source_number"] != voucher["number"]
                or payable["currency"] != ctx.currency or to_amount(payable["amount"], ctx.currency) != total):
            raise _inconsistent()
        rows.add(("supplier_payable", payable["id"]))
    accounts = {}
    for line in mirror:
        row = ctx.conn.execute(select(m.account).where(m.account.c.company_id == ctx.company_id,
            m.account.c.id == line.account_id)).mappings().first()
        if row is None:
            raise _inconsistent()
        accounts[line.account_id] = dict(row)
        rows.add(("account", line.account_id))
    mode, external = integration.system_of_record(ctx, "accounting")
    codes = {}
    if mode == "external":
        ids = list(accounts)
        codes = integration.account_codes(ctx, external, ids)
        rows.update(("account_map", key) for key in ctx.conn.execute(select(m.account_map.c.id).where(
            m.account_map.c.company_id == ctx.company_id, m.account_map.c.external_system == external,
            m.account_map.c.account_id.in_(ids))).scalars())
    return ReversalPlan(request, voucher, header, supplier, posting, moves, mirror, total, payable,
                        items, accounts, sorted(rows), mode, external, codes)


def preview_reverse_goods_received(ctx: Ctx, request: ReverseGoodsReceived) -> Preview:
    plan = plan_reverse_goods_received(ctx, request)
    sh = Shown(ctx.currency)
    sh.title("Reverse goods received")
    sh.line("Delivery", f"{plan.voucher['number']} from {plan.supplier['name']}")
    sh.line("Original date", f"{plan.voucher['posting_date'].day} {plan.voucher['posting_date']:%b %Y}")
    sh.line("Reversal date", f"{request.reversal_date.day} {request.reversal_date:%b %Y}")
    counter = plan.accounts[plan.header["account_id"]]
    sh.line("Settlement", f"Paid from {counter['name']} ({counter['number']})" if plan.payable is None else
            f"Still owed to {plan.supplier['name']}; this delivery's untouched payable will be closed.")
    for move in plan.moves:
        item = plan.items[move.pair.item["item_id"]]
        sh.line(item["name"], f"Remove {fmt_qty(move.qty)} {item['unit']} from {move.pair.wh['name']}: "
                f"{sh.amount(move.value)} {ctx.currency}; {fmt_qty(move.qty_after)} {item['unit']} remain.")
    sh.line("Total reversed", f"{sh.amount(plan.total)} {ctx.currency}")
    if plan.payable is not None:
        sh.line("Delivery payable", f"{sh.amount(plan.payable['outstanding'])} {ctx.currency} outstanding becomes "
                f"{sh.amount(Decimal(0))} {ctx.currency}; status reversed.")
    sh.line("Accounting destination", "ERPClaw general ledger" if plan.mode == "native" else
            f"{plan.external}: ERPClaw sub-ledger, with the journal staged for this system")
    for line in plan.mirror:
        account = plan.accounts[line.account_id]
        mapped = f" (external account {plan.codes[line.account_id]})" if plan.mode == "external" else ""
        party = f" ({plan.supplier['name']})" if line.party_id else ""
        side, value = ("debit", line.debit) if line.debit else ("credit", line.credit)
        sh.book(f"{account['number']} {account['name']}{party}{mapped}: {side} {sh.amount(value)} {ctx.currency}")
    card = sh.card()
    token = hashlib.sha256(json.dumps({"card": card.as_dict(), "rows": plan.read_set}, sort_keys=True).encode()).hexdigest()
    return Preview(card, token)


def reverse_goods_received_read_set(ctx: Ctx, request: ReverseGoodsReceived) -> list[tuple[str, str]]:
    return plan_reverse_goods_received(ctx, request).read_set


def _authorize(ctx: Ctx) -> None:
    from erpclaw_core.plugins import REGISTRY
    from erpclaw_core.approvals import committing

    payables._authorize(ctx)
    approval_id = committing(ctx)
    contract = ctx.conn.execute(select(m.approval.c.contract).where(m.approval.c.id == approval_id,
        m.approval.c.company_id == ctx.company_id)).scalar_one_or_none() if approval_id else None
    if contract != NAME or REGISTRY.contract_plugin.get(NAME) != "stock":
        raise Refusal("A delivery reverses only inside its own approved stock card.", "not_approved")


def commit_reverse_goods_received(ctx: Ctx, request: ReverseGoodsReceived, operation_key: str) -> Result:
    _authorize(ctx)
    plan = plan_reverse_goods_received(ctx, request, lock=True)
    s.check_fresh(ctx, [], plan.read_set)
    # One caller-owned savepoint: immutable stock mirrors, exact original ledger
    # mirror, and (only when owed) core's locked, conditional payable close.
    for move in plan.moves:
        s._entry_id(ctx, move, request.reversal_date, VOUCHER + ":reversal", plan.voucher["id"])
    ledger.reverse(ctx, plan.posting["id"], request.reversal_date)
    if plan.payable is not None:
        payables.close_item(ctx, VOUCHER, plan.voucher["id"])
    sh = Shown(ctx.currency)
    amount = sh.amount(plan.total, "value", plan.voucher["number"])
    return Result(f"Reversed goods received {plan.voucher['number']}, dated {request.reversal_date.day} "
        f"{request.reversal_date:%b %Y}: {amount} {ctx.currency}.",
        {"voucher_type": VOUCHER + ":reversal", "voucher_id": plan.voucher["id"],
         "number": plan.voucher["number"], "posting_date": request.reversal_date.isoformat()}, [amount], sh.amounts)


def find_reverse_goods_received(ctx: Ctx, operation_key: str) -> Result | None:
    outcome, approval = m.operation_outcome, m.approval
    raw = ctx.conn.execute(select(outcome.c.result_json).join(approval, approval.c.id == outcome.c.approval_id).where(
        outcome.c.company_id == ctx.company_id, outcome.c.key == operation_key, outcome.c.status == "committed",
        approval.c.contract == NAME)).scalar_one_or_none()
    if raw is None:
        return None
    result = json.loads(raw)
    return Result(result["text"], result.get("data", {}), result.get("figures", []),
                  [Amount(**amount) for amount in result.get("amounts", [])])
