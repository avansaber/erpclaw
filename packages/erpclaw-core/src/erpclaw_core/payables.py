"""Payable items, behind engine-owned approved commits.

This internal slice opens and closes delivery items and supplies the ledger's
role-based source refusal. It registers no supplier, payment or owed-read contract,
resolves no owner/model name or identity, and changes no schema. Callers hold the
company chain and (for delivery reversal) stock heads before this item's row, and
own the transaction, posting and approved read-set/recompute checks. The ledger's
frozen 0015 database source guard remains a separate backstop.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from . import ledger, models as m
from .common import Ctx, Refusal, audit, new_id, now
from .money import MoneyError, quantize, to_amount, to_decimal
from .payable_tables import supplier_payable

DELIVERY = "stock_goods_received"
PAYMENT = "supplier_payment"
REVERSAL = ":reversal"
PAYABLE_SOURCES = (DELIVERY, DELIVERY + REVERSAL, PAYMENT, PAYMENT + REVERSAL)
ITEM_SOURCES = (DELIVERY,)
WRITERS = ("core", "stock")
SOURCE_TEXT = "Only a delivery still owed or a supplier payment can change what a supplier is owed in these books."


def check_payable_source(ctx: Ctx, voucher_type: str, lines: list) -> None:
    """The payable-source rule, by role: a line naming a party on an account of role `payable` is refused unless the
    voucher type is a payable source. The posting service calls it on its cleaned lines, before any write."""
    if voucher_type in PAYABLE_SOURCES:
        return
    ids = {ln.account_id for ln in lines if ln.party_id is not None}
    if not ids:
        return
    if ctx.conn.execute(select(m.account.c.id).where(m.account.c.company_id == ctx.company_id, m.account.c.id.in_(ids),
                                       m.account.c.account_type == "payable")
                        .limit(1)).first():
        raise Refusal(SOURCE_TEXT, "payable_source_not_allowed")

def _authorize(ctx: Ctx) -> None:
    """Require this company's in-flight core/stock approval, not a completed outcome.

    Core has already locked the company chain before calling its contract commit.
    The supplied context is engine-owned; this helper is no independent request or
    supplier-identity authority, and offers no internal bypass.
    """
    from .plugins import REGISTRY

    from .approvals import committing
    approval_id = committing(ctx)
    card = ctx.conn.execute(select(m.approval.c.contract, m.approval.c.company_id).where(
        m.approval.c.id == approval_id, m.approval.c.company_id == ctx.company_id
    )).first() if approval_id else None
    plugin = REGISTRY.contract_plugin.get(card.contract) if card is not None else None
    if card is None or card.company_id != ctx.company_id or plugin not in WRITERS:
        raise Refusal("What a supplier is owed changes only inside the commit of an approved card that may change it.",
                      "not_approved")

def payable_account(ctx: Ctx) -> dict:
    return ledger.account_by_type(ctx, "payable")

def _amount(ctx: Ctx, value, what: str) -> Decimal:
    try:
        amount = to_amount(value, ctx.currency)
    except MoneyError as e:
        raise Refusal(f"{what} must be a plain amount in {ctx.currency}: {e}.", "bad_amount") from None
    if amount <= 0:
        raise Refusal(f"{what} must be more than zero.", "bad_amount")
    return amount

def _supplier(ctx: Ctx, supplier_id: str) -> dict:
    row = ctx.conn.execute(select(m.party).where(m.party.c.company_id == ctx.company_id,
                                                 m.party.c.id == supplier_id)).mappings().first()
    if row is None or row["kind"] != "supplier":
        raise Refusal("A payable must name a supplier of this company.", "bad_party")
    return dict(row)

def open_item(ctx: Ctx, *, supplier_id: str, source_type: str, source_id: str, source_number: str,
              posting_date: date, amount) -> ledger.Line:
    """Open the supplier payable item for one still-owed posting, in the caller's transaction, and return its line
    (Accounts Payable credit, the supplier as party) for that posting. Refused outside an approved commit of core or
    the stock plugin. The caller owns the transaction and its posting."""
    _authorize(ctx)
    if source_type not in ITEM_SOURCES:
        raise Refusal(SOURCE_TEXT, "payable_source_not_allowed")
    value = _amount(ctx, amount, "A payable")
    _supplier(ctx, supplier_id)
    ap = payable_account(ctx)
    ctx.conn.execute(supplier_payable.insert().values(
        id=new_id(), company_id=ctx.company_id, supplier_id=supplier_id, source_type=source_type, source_id=source_id,
        source_number=source_number, posting_date=posting_date, currency=ctx.currency, amount=value,
        outstanding=value, status="open", created_at=now()))
    return ledger.Line(ap["id"], credit=value, party_id=supplier_id)

def _items(ctx: Ctx, supplier_id: str, *, status: tuple[str, ...] = ("open",)) -> list[dict]:
    """A supplier's items with one of `status` (all three: every item), oldest first: by date, then number. A read;
    a card's commit locks the ones it settles through its read set."""
    t = supplier_payable
    return [dict(r) for r in ctx.conn.execute(select(t).where(
        t.c.company_id == ctx.company_id, t.c.supplier_id == supplier_id, t.c.status.in_(status)).order_by(
        t.c.posting_date, t.c.source_number, t.c.id)).mappings()]

def _item_by_source(ctx: Ctx, source_type: str, source_id: str, lock: bool = False) -> dict:
    if source_type not in ITEM_SOURCES:
        raise Refusal(SOURCE_TEXT, "payable_source_not_allowed")
    t = supplier_payable
    q = select(t).where(t.c.company_id == ctx.company_id, t.c.source_type == source_type, t.c.source_id == source_id)
    row = ctx.conn.execute(ledger.for_update(ctx, q) if lock else q).mappings().first()
    if row is None:
        raise Refusal("Nothing is owed to a supplier for that document.", "no_payable")
    return dict(row)

def check_close_item(ctx: Ctx, source_type: str, source_id: str, *, lock: bool = False) -> dict:
    """Every refusal `close_item` raises, without writing (a preview): the item untouched, outstanding equal to its
    amount. Returns the item."""
    item = _item_by_source(ctx, source_type, source_id, lock=lock)
    if item["status"] == "reversed":
        raise Refusal(f"{item['source_number']} is already reversed.", "payable_reversed")
    if item["status"] != "open" or to_decimal(item["outstanding"]) != to_decimal(item["amount"]):
        raise Refusal(f"{item['source_number']} has a payment against it; reverse the payment first.",
                      "payable_touched")
    return item

def close_item(ctx: Ctx, source_type: str, source_id: str) -> dict:
    """Close an untouched item (status reversed, outstanding 0.00) in the caller's transaction, for the reversal of
    its delivery. The item row is locked here, after the caller's stock heads, and checked
    again under the lock; a touched item is refused. Refused outside an approved commit of core or the stock plugin,
    with no independent authority bypass."""
    _authorize(ctx)
    item = check_close_item(ctx, source_type, source_id, lock=True)
    zero = quantize(Decimal(0), ctx.currency)
    res = ctx.conn.execute(supplier_payable.update().where(
        supplier_payable.c.company_id == ctx.company_id, supplier_payable.c.id == item["id"],
        supplier_payable.c.status == "open",
        supplier_payable.c.outstanding == item["outstanding"]).values(status="reversed", outstanding=zero))
    if res.rowcount != 1:
        raise Refusal("That delivery's payable changed while it was being reversed; nothing was saved.", "conflict")
    audit(ctx, "close_payable", "supplier_payable", item["id"], detail={"number": item["source_number"]})
    return {**item, "status": "reversed", "outstanding": zero}

def open_items(ctx: Ctx, supplier_id: str, on_or_before: date | None = None) -> list[dict]:
    rows = _items(ctx, supplier_id)
    return [{**r, "number": r["source_number"]} for r in rows
            if on_or_before is None or r["posting_date"] <= on_or_before]
