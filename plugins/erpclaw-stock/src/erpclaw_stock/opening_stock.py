"""Stock-owned opening balances, behind the existing planned-release gate.

The books' first fiscal date is authoritative. This producer only runs within
core's real approved-card commit; it never creates accounts or uses Trusted.
Chat rate grounding and database qualification remain separate activation gates.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated

from pydantic import BeforeValidator, Field
from sqlalchemy import select

from erpclaw_core import integration, ledger, masters, models as m
from erpclaw_core.common import Ctx, Refusal, new_id, now
from erpclaw_core.contracts import Amount, Input, Preview, Result, Shown, WriteContract
from erpclaw_core.money import fmt_qty
from erpclaw_core.numbering import next_number

from . import service as s, tables as t
from .entry_posting import EntryPostingError, entry_posting_lines

NAME = "record_opening_stock"
VOUCHER = "stock_opening"


def _number(value):
    # The stock parser rejects floats, bools, exponents, nonfinite and off-scale
    # values before Pydantic could coerce or round them.
    return s.parse_qty(value)


Number = Annotated[Decimal, BeforeValidator(_number)]


class OpeningLine(Input):
    item: str = Field(min_length=1, max_length=200)
    warehouse: str = Field(min_length=1, max_length=140)
    quantity: Number
    rate: Number  # always supplied by the owner; never the item's selling price


class RecordOpeningStock(Input):
    lines: list[OpeningLine] = Field(min_length=1, max_length=20)
    date: dt.date | None = None  # discrepancy only, never the posting date


@dataclass
class OpeningPlan:
    date: dt.date
    year: dict
    period: dict
    items: list[dict]
    moves: list[s.Move]
    rates: list[Decimal]
    accounts: dict[str, dict]
    lines: list[ledger.Line]
    read_set: list[tuple[str, str]]
    mode: str
    external: str | None
    codes: dict[str, str]


def _books_start(ctx: Ctx) -> tuple[dict, dict]:
    year = ctx.conn.execute(select(m.fiscal_year).where(
        m.fiscal_year.c.company_id == ctx.company_id
    ).order_by(m.fiscal_year.c.start_date, m.fiscal_year.c.id).limit(1)).mappings().first()
    if year is None:
        raise Refusal("These books have no first fiscal year; set up the books before opening stock.",
                      "no_fiscal_year")
    when = year["start_date"]
    try:
        period = ledger.open_period(ctx, when)
    except Refusal as exc:
        if exc.code != "period_closed":
            raise
        raise Refusal(f"Opening stock is dated {when.day} {when:%b %Y}, when these books start, "
                      "and that period is closed; record a count instead.", "opening_period_closed") from None
    return dict(year), dict(period)


def _warehouse(ctx: Ctx, said: str) -> dict:
    # Core's existing raw/exact/ambiguous/near-name rules, without choosing a
    # partial name or silently using the item's/company's default warehouse.
    return masters._match(s.warehouses(ctx), said, "warehouse")


def _never_moved(ctx: Ctx, item: dict, wh: dict, *, lock: bool) -> None:
    head = s.head(ctx, item["id"], wh["id"], lock=lock)
    moved = ctx.conn.execute(select(t.entry.c.id).where(
        t.entry.c.company_id == ctx.company_id, t.entry.c.item_id == item["id"],
        t.entry.c.warehouse_id == wh["id"]
    ).limit(1)).first()
    if head is not None or moved is not None:
        raise Refusal(f"{item['name']} already has stock movements in {wh['name']}, so it can't "
                      "take an opening balance there; record a count instead.", "opening_stock_moved")


def plan_opening(ctx: Ctx, inp: RecordOpeningStock, *, lock: bool = False) -> OpeningPlan:
    """Resolve and preflight the entire voucher; never allocate numbers or write."""
    year, period = _books_start(ctx)
    when = year["start_date"]
    counter = ledger.account_by_type(ctx, "opening_balance")
    accounts = {counter["id"]: counter}
    rows = {("company", ctx.company_id), ("fiscal_year", year["id"]),
            ("fiscal_period", period["id"]), ("account", counter["id"])}
    items, moves, rates, pairs = [], [], [], set()
    for n, line in enumerate(inp.lines, 1):
        item = masters.find_item(ctx, line.item)
        if item["kind"] != "stock":
            raise Refusal(f"{item['name']} is a service item, so it cannot have opening stock.", "item_not_stock")
        wh = _warehouse(ctx, line.warehouse)
        pair = (item["id"], wh["id"])
        if pair in pairs:
            raise Refusal(f"{item['name']} in {wh['name']} appears twice in that opening stock; give one figure for it.", "duplicate_stock_pair")
        pairs.add(pair)
        _never_moved(ctx, item, wh, lock=lock)
        mv = s._receipt_move(ctx, item["id"], wh["id"], line.quantity, line.rate, when, lock=lock)
        mv.line_no = n
        # The existing movement path checks role/company; this lookup also gives
        # the card its stored account name. ledger.check_post checks frozen/group.
        account = s._inventory_account_row(ctx, mv.pair.account, wh["name"])
        accounts[account["id"]] = account
        rows.update({("item", item["id"]), ("stock_item", mv.pair.item["id"]),
                     ("stock_warehouse", wh["id"]), ("account", account["id"])})
        if mv.pair.head is not None:
            rows.add(("stock_head", mv.pair.head["id"]))
        items.append(item)
        moves.append(mv)
        rates.append(line.rate)
    try:
        posting = entry_posting_lines("opening", moves, ctx.currency, {"opening_balance": counter["id"]})
    except EntryPostingError as exc:
        raise Refusal(f"Those quantities and unit costs cannot form an opening-stock posting: {exc}.",
                      "opening_stock_value") from None
    lines = [ledger.Line(p["account"], debit=Decimal(p["debit"]), credit=Decimal(p["credit"])) for p in posting]
    ledger.check_post(ctx, when, VOUCHER, None, lines)
    mode, external = integration.system_of_record(ctx, "accounting")
    codes = {}
    if mode == "external":
        ids = list(accounts)
        codes = integration.account_codes(ctx, external, ids)
        mappings = ctx.conn.execute(select(m.account_map.c.id).where(
            m.account_map.c.company_id == ctx.company_id, m.account_map.c.external_system == external,
            m.account_map.c.account_id.in_(ids)
        )).scalars()
        rows.update(("account_map", key) for key in mappings)
    return OpeningPlan(when, year, period, items, moves, rates, accounts, lines, sorted(rows), mode, external, codes)


def preview_opening(ctx: Ctx, inp: RecordOpeningStock) -> Preview:
    plan = plan_opening(ctx, inp)
    sh = Shown(ctx.currency)
    sh.title("Record opening stock")
    sh.line("Date", f"Opening stock dated {plan.date.day} {plan.date:%b %Y} (when these books start)")
    if inp.date is not None and inp.date != plan.date:
        sh.line("Date you supplied", f"You said {inp.date.day} {inp.date:%b %Y}; opening stock always carries "
                "the date the books start.")
    for item, mv, rate in zip(plan.items, plan.moves, plan.rates):
        sh.line(item["name"], f"{fmt_qty(mv.qty)} {item['unit']} in {mv.pair.wh['name']} at "
                f"{sh.unit_price(rate)} {ctx.currency} per {item['unit']}: {sh.amount(mv.value)} {ctx.currency}")
    total = sum((ln.debit for ln in plan.lines), Decimal(0))
    sh.line("Total", f"{sh.amount(total)} {ctx.currency}")
    # Core digests the actual card, not Preview.state_token. Bind the destination
    # visibly; its setter takes the same company-chain lock as approval/posting.
    sh.line("Accounting destination", "ERPClaw general ledger" if plan.mode == "native" else
            f"{plan.external}: ERPClaw sub-ledger, with the journal staged for this system")
    for ln in plan.lines:
        acc = plan.accounts[ln.account_id]
        side, value = ("debit", ln.debit) if ln.debit else ("credit", ln.credit)
        mapped = f" (external account {plan.codes[ln.account_id]})" if plan.mode == "external" else ""
        sh.book(f"{acc['number']} {acc['name']}{mapped}: {side} {sh.amount(value)} {ctx.currency}")
    card = sh.card()
    token = hashlib.sha256(json.dumps({"card": card.as_dict(), "rows": plan.read_set,
        "moves": [(mv.pair.item["id"], mv.pair.wh["id"], mv.pair.item["method"],
                   str(mv.qty), str(mv.value)) for mv in plan.moves]}, sort_keys=True).encode()).hexdigest()
    return Preview(card, token)


def opening_read_set(ctx: Ctx, inp: RecordOpeningStock) -> list[tuple[str, str]]:
    return plan_opening(ctx, inp).read_set


def write_opening_rows(ctx: Ctx, plan: OpeningPlan, voucher_id: str, number: str,
                       posting_type: str, posting_voucher_id: str) -> None:
    ctx.conn.execute(t.voucher.insert().values(id=voucher_id, company_id=ctx.company_id,
        kind="opening", number=number, posting_date=plan.date, reason=None, created_at=now()))
    for item, mv, rate in zip(plan.items, plan.moves, plan.rates):
        ctx.conn.execute(t.voucher_line.insert().values(id=new_id(), company_id=ctx.company_id,
            voucher_id=voucher_id, line_no=mv.line_no, item_id=item["id"], warehouse_id=mv.pair.wh["id"],
            currency=ctx.currency, qty_moved=mv.qty, counted_qty=None, books_qty=None,
            rate=rate, direction=mv.direction, value=mv.value))
        s._entry_id(ctx, mv, plan.date, posting_type, posting_voucher_id)

def commit_opening(ctx: Ctx, inp: RecordOpeningStock, key: str) -> Result:
    # Core has claimed the card and locked chain/period/read-set before this call.
    s.authorize(ctx, None, NAME)
    plan = plan_opening(ctx, inp, lock=True)
    s.check_fresh(ctx, [mv.pair for mv in plan.moves], plan.read_set)
    voucher_id = new_id()
    ledger.check_post(ctx, plan.date, VOUCHER, voucher_id, plan.lines)
    # First write only after every line, account, period and complete posting is valid.
    # Opening stock has its own series in the company's fiscal year.
    number = next_number(ctx, "OS", plan.date)
    write_opening_rows(ctx, plan, voucher_id, number, VOUCHER, voucher_id)
    ledger.post(ctx, plan.date, VOUCHER, voucher_id, plan.lines, operation_key=key)
    sh = Shown(ctx.currency)
    total = sum((ln.debit for ln in plan.lines), Decimal(0))
    amount = sh.amount(total, "value", number)
    return Result(f"Recorded opening stock {number}, dated {plan.date.day} {plan.date:%b %Y}: "
                  f"{amount} {ctx.currency}.",
                  {"voucher_type": VOUCHER, "voucher_id": voucher_id, "number": number,
                   "posting_date": plan.date.isoformat()}, [amount], sh.amounts)


def find_opening(ctx: Ctx, key: str) -> Result | None:
    # The existing atomic outcome is the replay record, scoped to this company
    # and actual contract; a number prefix never identifies a voucher kind.
    o, a = m.operation_outcome, m.approval
    raw = ctx.conn.execute(select(o.c.result_json).join(a, a.c.id == o.c.approval_id).where(
        o.c.company_id == ctx.company_id, o.c.key == key, o.c.status == "committed", a.c.contract == NAME
    )).scalar_one_or_none()
    if raw is None:
        return None
    result = json.loads(raw)
    return Result(result["text"], result.get("data", {}), result.get("figures", []),
                  [Amount(**a) for a in result.get("amounts", [])])


CONTRACT = WriteContract(NAME, "stock", "write", "Record the goods held when these books began, at the owner's cost.",
    RecordOpeningStock, preview=preview_opening, commit=commit_opening, find_by_operation_key=find_opening,
    read_set=opening_read_set, planned="Core", planned_name="recording opening stock", plain="record opening stock",
    synonyms=("opening stock", "starting inventory", "opening inventory"), amounts=("value",), moves_money=True)
