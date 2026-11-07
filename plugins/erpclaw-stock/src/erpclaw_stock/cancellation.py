"""Cancel stock-owned vouchers by mirroring their immutable effects.

The original posting, never today's counter-account role, owns its reversal.
No numbering, replacement voucher, schema extension or generic COGS inference.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal

from pydantic import Field
from sqlalchemy import select

from erpclaw_core import integration, ledger, models as m
from erpclaw_core.common import Ctx, Refusal
from erpclaw_core.contracts import Amount, Input, Preview, Result, Shown, WriteContract
from erpclaw_core.money import fmt_qty

from . import service as s, tables as t

NAME = "cancel_stock_movement"
KINDS = {"opening": "stock_opening", "count": "stock_count", "write_off": "stock_write_off"}
LABELS = {"opening": "opening stock", "count": "stock count", "write_off": "stock write-off"}
ZERO = Decimal(0)


class CancelStockMovement(Input):
    document: str = Field(min_length=1, max_length=40)
    date: dt.date  # the owner's reversal date, never inferred from the original


@dataclass
class CancellationPlan:
    voucher: dict
    voucher_type: str
    date: dt.date
    moves: list[s.Move]
    posting: dict | None
    lines: list[ledger.Line]
    items: dict[str, dict]
    accounts: dict[str, dict]
    read_set: list[tuple[str, str]]
    mode: str
    external: str | None
    codes: dict[str, str]


def plan_cancellation(ctx: Ctx, inp: CancelStockMovement, *, lock: bool = False) -> CancellationPlan:
    voucher = ctx.conn.execute(select(t.voucher).where(
        t.voucher.c.company_id == ctx.company_id, t.voucher.c.number == inp.document
    )).mappings().one_or_none()
    if voucher is None:
        raise Refusal(f"{ctx.company_name} has no stock document numbered {inp.document}.", "no_stock_document")
    voucher = dict(voucher)
    kind = voucher["kind"]
    if kind == "goods_received":
        raise Refusal("Reverse that delivery with reverse goods received, so its settlement is reversed with it.",
                      "goods_received_reversal_required")
    if kind not in KINDS:
        raise Refusal("Only an opening stock, stock count or stock write-off is cancelled here.", "stock_cancel_kind")
    if inp.date < voucher["posting_date"]:
        raise Refusal("A reversal cannot be dated before the document it reverses.", "reversal_before_original")
    period = ledger.open_period(ctx, inp.date)
    vtype = KINDS[kind]
    try:
        moves = s._reverse_moves(ctx, vtype, voucher["id"], inp.date, lock=lock)
    except Refusal as exc:
        if exc.code == "not_latest":
            raise Refusal(f"Stock has moved since this {LABELS[kind]}; make a new count to correct it.",
                          "not_latest", **{k: v for k, v in exc.data.items() if k != "points_to"}) from None
        raise
    posting = ctx.conn.execute(select(m.posting).where(
        m.posting.c.company_id == ctx.company_id, m.posting.c.voucher_type == vtype,
        m.posting.c.voucher_id == voucher["id"], m.posting.c.reverses_posting_id.is_(None)
    )).mappings().one_or_none()
    posting = dict(posting) if posting is not None else None
    if posting is None and any(mv.value != 0 for mv in moves):
        # Offsetting count gains/losses can net to zero and still require four
        # gross original lines. Only individually zero-valued moves need none.
        raise Refusal("That stock document moved value but its original posting is missing; "
                      "the books need checking before it can be cancelled.", "stock_posting_missing")
    lines = ledger.check_reverse(ctx, posting["id"], inp.date) if posting is not None else []
    rows = {("company", ctx.company_id), ("stock_voucher", voucher["id"]),
            ("fiscal_year", period["fiscal_year_id"]), ("fiscal_period", period["id"])}
    items, accounts = {}, {}
    for mv in moves:
        pair = mv.pair
        iid = pair.item["item_id"]
        if iid not in items:
            item = ctx.conn.execute(select(m.item).where(m.item.c.company_id == ctx.company_id,
                                                         m.item.c.id == iid)).mappings().one()
            items[iid] = dict(item)
        rows.update({("item", iid), ("stock_item", pair.item["id"]), ("stock_warehouse", pair.wh["id"]),
                     ("account", pair.account), ("stock_head", pair.head["id"]), (t.entry.name, mv.reverses)})
    if posting is not None:
        rows.add(("posting", posting["id"]))
        rows.update(("ledger_entry", eid) for eid in ctx.conn.execute(select(m.ledger_entry.c.id).where(
            m.ledger_entry.c.company_id == ctx.company_id, m.ledger_entry.c.posting_id == posting["id"]
        )).scalars())
        for line in lines:
            aid = line.account_id
            if aid not in accounts:
                account = ctx.conn.execute(select(m.account).where(m.account.c.company_id == ctx.company_id,
                                                                    m.account.c.id == aid)).mappings().one()
                accounts[aid] = dict(account)
            rows.add(("account", aid))
            if line.party_id is not None:
                rows.add(("party", line.party_id))
    mode, external = integration.system_of_record(ctx, "accounting")
    codes = {}
    if mode == "external" and lines:
        ids = list(dict.fromkeys(line.account_id for line in lines))
        codes = integration.account_codes(ctx, external, ids)
        rows.update(("account_map", key) for key in ctx.conn.execute(select(m.account_map.c.id).where(
            m.account_map.c.company_id == ctx.company_id, m.account_map.c.external_system == external,
            m.account_map.c.account_id.in_(ids)
        )).scalars())
    return CancellationPlan(voucher, vtype, inp.date, moves, posting, lines, items, accounts,
                            sorted(rows), mode, external, codes)


def preview_cancellation(ctx: Ctx, inp: CancelStockMovement) -> Preview:
    plan = plan_cancellation(ctx, inp)
    sh = Shown(ctx.currency)
    sh.title(f"Cancel {LABELS[plan.voucher['kind']]} {plan.voucher['number']}")
    original_date = plan.voucher["posting_date"]
    sh.line("Original date", f"{original_date.day} {original_date:%b %Y}")
    sh.line("Reversal date", f"{plan.date.day} {plan.date:%b %Y}")
    for mv in plan.moves:
        item = plan.items[mv.pair.item["item_id"]]
        action = "restore" if mv.direction == "in" else "remove"
        sh.line(item["name"], f"{mv.pair.wh['name']}: {action} {fmt_qty(mv.qty)} {item['unit']}, "
                f"value {sh.amount(mv.value)} {ctx.currency}; afterward {fmt_qty(mv.qty_after)} {item['unit']}, "
                f"value {sh.amount(mv.value_after)} {ctx.currency}")
    if not plan.lines:
        sh.line("Accounting destination", "No ledger posting")
        sh.book("The original stock movements had zero book cost; no ledger posting is reversed.")
    else:
        sh.line("Accounting destination", "ERPClaw general ledger" if plan.mode == "native" else
                f"{plan.external}: ERPClaw sub-ledger, with the journal staged for this system")
        for line in plan.lines:
            account = plan.accounts[line.account_id]
            side, value = ("debit", line.debit) if line.debit else ("credit", line.credit)
            mapped = f" (external account {plan.codes[line.account_id]})" if plan.mode == "external" else ""
            sh.book(f"{account['number']} {account['name']}{mapped}: {side} {sh.amount(value)} {ctx.currency}")
    sh.line("Original document", "Kept unchanged; linked reversal entries record this cancellation.")
    card = sh.card()
    token = hashlib.sha256(json.dumps({"card": card.as_dict(), "rows": plan.read_set}, sort_keys=True).encode()).hexdigest()
    return Preview(card, token)


def cancellation_read_set(ctx: Ctx, inp: CancelStockMovement) -> list[tuple[str, str]]:
    return plan_cancellation(ctx, inp).read_set


def commit_cancellation(ctx: Ctx, inp: CancelStockMovement, key: str) -> Result:
    s.authorize(ctx, None, NAME)
    plan = plan_cancellation(ctx, inp, lock=True)
    pairs = list({id(mv.pair): mv.pair for mv in plan.moves}.values())
    s.check_fresh(ctx, pairs, plan.read_set)
    reversal_type = plan.voucher_type + ":reversal"
    vid = plan.voucher["id"]
    # Additional transaction check uses the exact mirror's account nets, not
    # generic stock/1's COGS policy. Gross rows remain ledger.reverse's own list.
    expected = {}
    for line in plan.lines:
        expected[line.account_id] = expected.get(line.account_id, ZERO) + line.debit - line.credit
    s.contribute(ctx, ("reversal", plan.voucher_type, vid, reversal_type, vid), expected)
    for mv in plan.moves:
        s._entry_id(ctx, mv, plan.date, reversal_type, vid)
    if plan.posting is not None:
        ledger.reverse(ctx, plan.posting["id"], plan.date)
    sh = Shown(ctx.currency)
    amount = sh.amount(sum((mv.value for mv in plan.moves), ZERO), "value", plan.voucher["number"])
    return Result(f"Cancelled {plan.voucher['number']} with reversal entries dated {plan.date.day} {plan.date:%b %Y}; "
                  f"{amount} {ctx.currency} of stock value is reversed.",
                  {"voucher_type": reversal_type, "voucher_id": vid, "number": plan.voucher["number"],
                   "posting_date": plan.date.isoformat()}, [amount], sh.amounts)


def find_cancellation(ctx: Ctx, key: str) -> Result | None:
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


CONTRACT = WriteContract(NAME, "stock", "write", "Cancel an opening stock, stock count or stock write-off at its original value.",
    CancelStockMovement, preview=preview_cancellation, commit=commit_cancellation, find_by_operation_key=find_cancellation,
    read_set=cancellation_read_set, planned="Core", planned_name="cancelling stock movements", plain="cancel stock movement",
    synonyms=("cancel stock count", "cancel opening stock", "cancel stock write-off"), amounts=("value",), moves_money=True)
