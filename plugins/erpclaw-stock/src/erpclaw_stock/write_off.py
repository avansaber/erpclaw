"""Stock write-offs, behind the existing planned-release gate.

One owner-approved stock voucher records the actual FIFO/moving-average issue.
No selling price is a cost, no default warehouse settles an ambiguous holder,
and no zero-cost movement creates an empty ledger posting.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated

from pydantic import Field, StringConstraints
from sqlalchemy import select

from erpclaw_core import integration, ledger, masters, models as m
from erpclaw_core.common import Ctx, Refusal, new_id, now
from erpclaw_core.contracts import Amount, Input, Preview, Result, Shown, WriteContract
from erpclaw_core.money import fmt_qty
from erpclaw_core.numbering import next_number

from . import service as s, tables as t
from .entry_posting import EntryPostingError, entry_posting_lines
from .opening_stock import Number, _warehouse

NAME = "write_off_stock"
VOUCHER = "stock_write_off"
Reason = Annotated[str, StringConstraints(strip_whitespace=False, max_length=140)]


class WriteOffStock(Input):
    item: str = Field(min_length=1, max_length=200)
    quantity: Number
    warehouse: str | None = Field(default=None, min_length=1, max_length=140)
    reason: Reason | None = None  # opaque owner text, including its spacing
    date: dt.date | None = None


@dataclass
class WriteOffPlan:
    date: dt.date
    period: dict
    item: dict
    move: s.Move
    accounts: dict[str, dict]
    lines: list[ledger.Line]
    read_set: list[tuple[str, str]]
    mode: str
    external: str | None
    codes: dict[str, str]


def holder_warehouse(ctx: Ctx, item: dict, said: str | None) -> dict:
    """Count/write-off rule: one positive holder, or ask; never sales defaults."""
    s.stock_item(ctx, item["id"])
    if said is not None:
        return _warehouse(ctx, said)
    warehouses = s.warehouses(ctx)
    if not warehouses:
        raise Refusal("These books have no warehouses; add a warehouse before recording stock movements.", "no_warehouse")
    quantities = {wh["id"]: s.on_hand(ctx, item["id"], wh["id"])[0] for wh in warehouses}
    holders = [wh for wh in warehouses if quantities[wh["id"]] > 0]
    if len(holders) == 1:
        return holders[0]
    choices = holders or warehouses
    labels = [f"{wh['name']} ({fmt_qty(quantities[wh['id']])} on hand)" for wh in choices]
    lead = f"{item['name']} is held in more than one warehouse" if holders else f"No warehouse holds {item['name']} on the books"
    raise Refusal(f"{lead}: {'; '.join(labels)}. Which warehouse?", "warehouse_ambiguous" if holders else "warehouse_missing",
                  options=labels, field="warehouse", choices=[{"label": label, "ref": wh["name"]}
                                                              for label, wh in zip(labels, choices)])


def plan_write_off(ctx: Ctx, inp: WriteOffStock, *, lock: bool = False) -> WriteOffPlan:
    """Resolve and value the complete write-off without writing or allocating."""
    item = masters.find_item(ctx, inp.item)
    if item["kind"] != "stock":
        raise Refusal(f"{item['name']} is not kept in stock, so it cannot be written off as stock.", "item_not_stock")
    wh = holder_warehouse(ctx, item, inp.warehouse)
    when = inp.date or ctx.today
    period = ledger.open_period(ctx, when)
    counter = ledger.account_by_type(ctx, "stock_write_off")
    [move] = s._issue_moves(ctx, [{"item": item["id"], "warehouse": wh["id"], "qty": inp.quantity}], when, lock=lock)
    inventory = s._inventory_account_row(ctx, move.pair.account, wh["name"])
    accounts = {inventory["id"]: inventory, counter["id"]: counter}
    try:
        posting = entry_posting_lines("write_off", [move], ctx.currency, {"stock_write_off": counter["id"]})
    except EntryPostingError as exc:
        raise Refusal(f"That quantity and its book cost cannot form a stock write-off: {exc}.", "write_off_value") from None
    lines = [ledger.Line(row["account"], debit=Decimal(row["debit"]), credit=Decimal(row["credit"])) for row in posting]
    if lines:
        ledger.check_post(ctx, when, VOUCHER, None, lines)
    rows = {("company", ctx.company_id), ("fiscal_year", period["fiscal_year_id"]), ("fiscal_period", period["id"]),
            ("item", item["id"]), ("stock_item", move.pair.item["id"]), ("stock_warehouse", wh["id"]),
            ("account", inventory["id"]), ("account", counter["id"])}
    if move.pair.head is not None:
        rows.add(("stock_head", move.pair.head["id"]))
    mode, external = integration.system_of_record(ctx, "accounting")
    codes = {}
    if mode == "external" and lines:
        ids = list(accounts)
        codes = integration.account_codes(ctx, external, ids)
        rows.update(("account_map", key) for key in ctx.conn.execute(select(m.account_map.c.id).where(
            m.account_map.c.company_id == ctx.company_id, m.account_map.c.external_system == external,
            m.account_map.c.account_id.in_(ids)
        )).scalars())
    return WriteOffPlan(when, dict(period), item, move, accounts, lines, sorted(rows), mode, external, codes)


def preview_write_off(ctx: Ctx, inp: WriteOffStock) -> Preview:
    plan = plan_write_off(ctx, inp)
    mv = plan.move
    sh = Shown(ctx.currency)
    sh.title("Write off stock")
    sh.line("Date", f"{plan.date.day} {plan.date:%b %Y}")
    sh.line(plan.item["name"], f"{fmt_qty(mv.qty)} {plan.item['unit']} from {mv.pair.wh['name']} "
            f"({fmt_qty(mv.qty_after + mv.qty)} on hand)")
    sh.line("Reason", inp.reason if inp.reason is not None else "No reason given")
    method = "FIFO" if mv.pair.item["method"] == "fifo" else "Moving average"
    sh.line("Stock cost", f"{method}: {sh.amount(mv.value)} {ctx.currency}")
    if not plan.lines:
        sh.line("Accounting destination", "No ledger posting")
        sh.book(f"The stock cost is less than {s.smallest_unit(ctx.currency)}; stock moves, but nothing posts to the ledger.")
    else:
        # Actual card fields enter core's approval digest; a state token alone does not.
        sh.line("Accounting destination", "ERPClaw general ledger" if plan.mode == "native" else
                f"{plan.external}: ERPClaw sub-ledger, with the journal staged for this system")
        for line in plan.lines:
            account = plan.accounts[line.account_id]
            side, value = ("debit", line.debit) if line.debit else ("credit", line.credit)
            mapped = f" (external account {plan.codes[line.account_id]})" if plan.mode == "external" else ""
            sh.book(f"{account['number']} {account['name']}{mapped}: {side} {sh.amount(value)} {ctx.currency}")
    card = sh.card()
    token = hashlib.sha256(json.dumps({"card": card.as_dict(), "rows": plan.read_set}, sort_keys=True).encode()).hexdigest()
    return Preview(card, token)


def write_off_read_set(ctx: Ctx, inp: WriteOffStock) -> list[tuple[str, str]]:
    return plan_write_off(ctx, inp).read_set


def commit_write_off(ctx: Ctx, inp: WriteOffStock, key: str) -> Result:
    s.authorize(ctx, None, NAME)
    plan = plan_write_off(ctx, inp, lock=True)
    s.check_fresh(ctx, [plan.move.pair], plan.read_set)
    voucher_id = new_id()
    if plan.lines:
        ledger.check_post(ctx, plan.date, VOUCHER, voucher_id, plan.lines)
    # First write follows all quantity, role, date, cost, destination and freshness checks.
    number = next_number(ctx, "SW", plan.date)
    ctx.conn.execute(t.voucher.insert().values(id=voucher_id, company_id=ctx.company_id, kind="write_off",
        number=number, posting_date=plan.date, reason=inp.reason, created_at=now()))
    mv = plan.move
    ctx.conn.execute(t.voucher_line.insert().values(id=new_id(), company_id=ctx.company_id,
        voucher_id=voucher_id, line_no=mv.line_no, item_id=plan.item["id"], warehouse_id=mv.pair.wh["id"],
        currency=ctx.currency, qty_moved=s.qty_text(mv.qty), counted_qty=None, books_qty=None,
        rate=None, direction="out", value=mv.value))
    s._entry_id(ctx, mv, plan.date, VOUCHER, voucher_id)
    if plan.lines:
        ledger.post(ctx, plan.date, VOUCHER, voucher_id, plan.lines, operation_key=key)
    sh = Shown(ctx.currency)
    amount = sh.amount(mv.value, "value", number)
    no_post = " No ledger posting was needed." if not plan.lines else ""
    return Result(f"Wrote off {fmt_qty(mv.qty)} {plan.item['unit']} of {plan.item['name']} from {mv.pair.wh['name']}: "
                  f"{amount} {ctx.currency} at book cost, {number}, dated {plan.date.day} {plan.date:%b %Y}.{no_post}",
                  {"voucher_type": VOUCHER, "voucher_id": voucher_id, "number": number,
                   "posting_date": plan.date.isoformat()}, [amount], sh.amounts)


def find_write_off(ctx: Ctx, key: str) -> Result | None:
    outcome, approval = m.operation_outcome, m.approval
    raw = ctx.conn.execute(select(outcome.c.result_json).join(approval, approval.c.id == outcome.c.approval_id).where(
        outcome.c.company_id == ctx.company_id, outcome.c.key == key, outcome.c.status == "committed",
        approval.c.contract == NAME
    )).scalar_one_or_none()
    if raw is None:
        return None
    result = json.loads(raw)
    return Result(result["text"], result.get("data", {}), result.get("figures", []),
                  [Amount(**a) for a in result.get("amounts", [])])


CONTRACT = WriteContract(NAME, "stock", "write", "Write off damaged, expired or lost stock at its actual book cost.",
    WriteOffStock, preview=preview_write_off, commit=commit_write_off, find_by_operation_key=find_write_off,
    read_set=write_off_read_set, planned="Core", planned_name="writing off stock", plain="write off stock",
    synonyms=("write off stock", "damaged stock", "expired stock", "lost stock"), amounts=("value",), moves_money=True)
