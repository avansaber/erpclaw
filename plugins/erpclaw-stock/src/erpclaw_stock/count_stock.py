"""Physical stock counts, behind the planned-release gate.

Every observation is retained in the approved card/args and every counted head
in its read set. Only differences become movement/voucher rows; line numbers
retain their original positions. No zero direction or schema extension is used.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
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
from .opening_stock import Number
from .valuation import _rounded
from .write_off import holder_warehouse

NAME = "record_stock_count"
VOUCHER = "stock_count"
ZERO = Decimal(0)


def _counted(value):
    # Reuse the positive stock domain; add only physical zero and its specified
    # negative-count refusal. No float, bool or exponent coercion is introduced.
    if isinstance(value, (str, int, Decimal)) and not isinstance(value, bool):
        text = value.strip() if isinstance(value, str) else format(value, "f") if isinstance(value, Decimal) else str(value)
        if re.fullmatch(r"-[0-9]+(?:\.[0-9]+)?", text) and Decimal(text) < 0:
            raise Refusal("A count can't be below zero.", "count_negative")
        if re.fullmatch(r"0(?:\.0+)?", text):
            return ZERO
    return s.parse_qty(value, "counted")


Counted = Annotated[Decimal, BeforeValidator(_counted)]


class CountLine(Input):
    item: str = Field(min_length=1, max_length=200)
    warehouse: str | None = Field(default=None, min_length=1, max_length=140)
    counted: Counted
    rate: Number | None = None


class RecordStockCount(Input):
    lines: list[CountLine] = Field(min_length=1, max_length=20)
    date: dt.date | None = None


@dataclass
class CountObservation:
    number: int
    item: dict
    pair: s.Pair
    books_qty: Decimal
    counted: Decimal
    difference: Decimal
    move: s.Move | None
    rate_used: Decimal | None
    supplied_rate: Decimal | None


@dataclass
class CountPlan:
    date: dt.date
    period: dict
    observations: list[CountObservation]
    moves: list[s.Move]
    accounts: dict[str, dict]
    lines: list[ledger.Line]
    read_set: list[tuple[str, str]]
    mode: str
    external: str | None
    codes: dict[str, str]


def _same_head(first: s.Pair, later: s.Pair) -> None:
    # _issue_moves/_receipt_move own valuation and may reread this pair. A preview
    # must not mix an earlier count difference with a later stock snapshot.
    if first.head != later.head:
        raise Refusal("Stock changed while this count was being prepared; ask again.", "stale")


def _rate_question(item: dict, pair: s.Pair, gain: Decimal, index: int) -> Refusal:
    # Use exact stored labels; never guess a singular form from a plural name.
    each = item["unit"].casefold() == "each"
    question = f"What did you pay for {'them' if each else 'it'} per {item['name'] if each else item['unit']}?"
    return Refusal(f"{pair.wh['name']} holds no {item['name']} on the books, so there is no cost to value the "
                   f"{fmt_qty(gain)} found at. {question}", "rate_missing", field=f"lines.{index}.rate")


def plan_count(ctx: Ctx, inp: RecordStockCount, *, lock: bool = False) -> CountPlan:
    when = inp.date or ctx.today
    period = ledger.open_period(ctx, when)
    observations, moves, accounts, seen = [], [], {}, set()
    rows = {("company", ctx.company_id), ("fiscal_year", period["fiscal_year_id"]), ("fiscal_period", period["id"])}
    for index, line in enumerate(inp.lines):
        item = masters.find_item(ctx, line.item)
        if item["kind"] != "stock":
            raise Refusal(f"{item['name']} is a service item, so it cannot have a stock count.", "item_not_stock")
        try:
            wh = holder_warehouse(ctx, item, line.warehouse)
        except Refusal as exc:
            if exc.code.startswith("warehouse_"):
                exc.data["field"] = f"lines.{index}.warehouse"
            raise
        key = (item["id"], wh["id"])
        if key in seen:
            raise Refusal(f"{item['name']} in {wh['name']} appears twice in that count; give one figure for it.", "count_duplicate")
        seen.add(key)
        pair = s._pair(ctx, {}, item["id"], wh["id"], when, lock=lock)
        books_qty, difference = pair.qty, line.counted - pair.qty
        number, move, rate_used = index + 1, None, None
        if difference < 0:
            [move] = s._issue_moves(ctx, [{"item": item["id"], "warehouse": wh["id"], "qty": -difference}],
                                    when, lock=lock, first_line=number)
            _same_head(pair, move.pair)
            pair = move.pair
        elif difference > 0:
            if pair.qty == 0:
                if line.rate is None:
                    raise _rate_question(item, pair, difference, index)
                rate_used = line.rate
                move = s._receipt_move(ctx, item["id"], wh["id"], difference, rate_used, when, lock=lock)
                _same_head(pair, move.pair)
                pair = move.pair
                move.line_no = number
            else:
                value = _rounded(pair.value, difference, pair.qty, ctx.currency)
                if value == 0:
                    raise Refusal(f"That count gain is worth less than {s.smallest_unit(ctx.currency)} at book cost.",
                                  "receipt_below_minor_unit", unit=s.smallest_unit(ctx.currency))
                move = s.Move(pair, "in", difference, value, number, layer=pair.item["method"] == "fifo")
                if move.layer:
                    move.merge_into = s._merge_target(pair, difference, value)
                s._apply(pair, move)
        rows.update({("item", item["id"]), ("stock_item", pair.item["id"]),
                     ("stock_warehouse", wh["id"]), ("account", pair.account)})
        if pair.head is not None:
            rows.add(("stock_head", pair.head["id"]))
        if move is not None:
            accounts[pair.account] = s._inventory_account_row(ctx, pair.account, wh["name"])
            moves.append(move)
        observations.append(CountObservation(number, item, pair, books_qty, line.counted, difference, move,
                                             rate_used, line.rate))
    if not moves:
        matches = "; ".join(f"{fmt_qty(row.counted)} {row.item['name']} in {row.pair.wh['name']}" for row in observations)
        raise Refusal(f"The count matches the books: {matches}. Nothing to adjust.", "count_matches")
    counter = ledger.account_by_type(ctx, "stock_adjustment")
    accounts[counter["id"]] = counter
    rows.add(("account", counter["id"]))
    try:
        posting = entry_posting_lines("count", moves, ctx.currency, {"stock_adjustment": counter["id"]})
    except EntryPostingError as exc:
        raise Refusal(f"Those count differences and book costs cannot form a stock adjustment: {exc}.", "count_value") from None
    lines = [ledger.Line(p["account"], debit=Decimal(p["debit"]), credit=Decimal(p["credit"])) for p in posting]
    if lines:
        ledger.check_post(ctx, when, VOUCHER, None, lines)
    mode, external = integration.system_of_record(ctx, "accounting")
    codes = {}
    if mode == "external" and lines:
        ids = list(dict.fromkeys(ln.account_id for ln in lines))
        codes = integration.account_codes(ctx, external, ids)
        rows.update(("account_map", key) for key in ctx.conn.execute(select(m.account_map.c.id).where(
            m.account_map.c.company_id == ctx.company_id, m.account_map.c.external_system == external,
            m.account_map.c.account_id.in_(ids)
        )).scalars())
    return CountPlan(when, dict(period), observations, moves, accounts, lines, sorted(rows), mode, external, codes)


def preview_count(ctx: Ctx, inp: RecordStockCount) -> Preview:
    plan = plan_count(ctx, inp)
    sh = Shown(ctx.currency)
    sh.title("Record stock count")
    sh.line("Date", f"{plan.date.day} {plan.date:%b %Y}")
    for row in plan.observations:
        difference = ("+" if row.difference > 0 else "") + fmt_qty(row.difference)
        value = row.move.value if row.move is not None else ZERO
        text = f"{row.pair.wh['name']}: books {fmt_qty(row.books_qty)}, counted {fmt_qty(row.counted)}, "
        text += f"difference {difference} {row.item['unit']}, value {sh.amount(value)} {ctx.currency}"
        if row.move is None:
            text += "; no adjustment"
        elif row.rate_used is not None:
            text += f" at owner cost {sh.unit_price(row.rate_used)} {ctx.currency} per {row.item['unit']}"
        elif row.difference > 0:
            text += "; average cost on hand"
        else:
            text += "; FIFO cost" if row.pair.item["method"] == "fifo" else "; moving-average cost"
            if value == 0:
                text += f" (less than {s.smallest_unit(ctx.currency)})"
        if row.supplied_rate is not None and row.rate_used is None:
            text += "; the supplied rate is not used for this adjustment"
        sh.line(row.item["name"], text)
    if not plan.lines:
        sh.line("Accounting destination", "No ledger posting")
        sh.book("Stock quantities change at zero book cost; nothing posts to the ledger.")
    else:
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


def count_read_set(ctx: Ctx, inp: RecordStockCount) -> list[tuple[str, str]]:
    return plan_count(ctx, inp).read_set


def commit_count(ctx: Ctx, inp: RecordStockCount, key: str) -> Result:
    s.authorize(ctx, None, NAME)
    plan = plan_count(ctx, inp, lock=True)
    s.check_fresh(ctx, [row.pair for row in plan.observations], plan.read_set)
    voucher_id = new_id()
    if plan.lines:
        ledger.check_post(ctx, plan.date, VOUCHER, voucher_id, plan.lines)
    number = next_number(ctx, "SC", plan.date)
    ctx.conn.execute(t.voucher.insert().values(id=voucher_id, company_id=ctx.company_id,
        kind="count", number=number, posting_date=plan.date, reason=None, created_at=now()))
    for row in plan.observations:
        if row.move is None:
            continue  # retained in approved args/card/readset, never a fake in/out row
        mv = row.move
        ctx.conn.execute(t.voucher_line.insert().values(id=new_id(), company_id=ctx.company_id,
            voucher_id=voucher_id, line_no=row.number, item_id=row.item["id"], warehouse_id=row.pair.wh["id"],
            currency=ctx.currency, qty_moved=s.qty_text(mv.qty), counted_qty=s.qty_text(row.counted),
            books_qty=s.qty_text(row.books_qty), rate=s.qty_text(row.rate_used) if row.rate_used is not None else None,
            direction=mv.direction, value=mv.value))
        s._entry_id(ctx, mv, plan.date, VOUCHER, voucher_id)
    if plan.lines:
        ledger.post(ctx, plan.date, VOUCHER, voucher_id, plan.lines, operation_key=key)
    sh = Shown(ctx.currency)
    adjustments, figures = [], [str(len(plan.moves))]
    for direction, label in (("in", "added"), ("out", "written off")):
        moves = [mv for mv in plan.moves if mv.direction == direction]
        if not moves:
            continue
        amount = sh.amount(sum((mv.value for mv in moves), ZERO), "value", f"{number} {label}")
        adjustments.append(f"{amount} {ctx.currency} {label}")
        figures.append(amount)
    return Result(f"Recorded stock count {number}, dated {plan.date.day} {plan.date:%b %Y}: "
                  f"{len(plan.moves)} stock adjustment{'s' if len(plan.moves) != 1 else ''}, "
                  f"{' and '.join(adjustments)}.",
                  {"voucher_type": VOUCHER, "voucher_id": voucher_id, "number": number,
                   "posting_date": plan.date.isoformat(), "adjustments": len(plan.moves)},
                  figures, sh.amounts)


def find_count(ctx: Ctx, key: str) -> Result | None:
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


CONTRACT = WriteContract(NAME, "stock", "write", "Record physical stock counts and adjust the differences at book cost.",
    RecordStockCount, preview=preview_count, commit=commit_count, find_by_operation_key=find_count,
    read_set=count_read_set, planned="Core", planned_name="recording stock counts", plain="record stock counts",
    synonyms=("stock count", "counted stock", "inventory count", "stock adjustment"), amounts=("value",), moves_money=True)
