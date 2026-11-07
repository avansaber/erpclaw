"""A bounded, current supplier-item balance with four distinct meanings.

Internal resolved identities only: no name lookup, public registration or write.
Stored payable items are the reporting source, not a posting-graph proof.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext
from typing import Literal

from sqlalchemy import inspect, select

from . import models as m
from .common import Ctx, Refusal
from .contracts import Result, Shown
from .money import MINOR_UNITS, MoneyError, canonical, to_amount
from .payable_tables import supplier_payable

MAX_ITEMS = 200
State = Literal["figure", "verified_zero", "no_activity", "not_available"]


@dataclass(frozen=True)
class Supplier:
    id: str
    name: str


@dataclass(frozen=True)
class OwedItem:
    id: str
    number: str
    posting_date: date
    amount: Decimal
    outstanding: Decimal


@dataclass(frozen=True)
class OwedClaim:
    state: State
    company_id: str
    currency: str
    supplier: Supplier | None
    value: Decimal | None
    items: tuple[OwedItem, ...] = ()
    source_count: int = 0
    unavailable_reason: Literal["source_absent", "supplier_unknown"] | None = None


def _id(value):
    return isinstance(value,str) and bool(value) and value.strip()==value and len(value)<=36


def _bad():
    return Refusal("The stored supplier items are inconsistent, so their balance cannot be stated.", "owed_source")


def owed(ctx: Ctx, supplier_id: str | None) -> OwedClaim:
    """Read the complete bounded item set; absence is never silently a zero."""
    if not inspect(ctx.conn).has_table(supplier_payable.name):
        return OwedClaim("not_available",ctx.company_id,ctx.currency,None,None,unavailable_reason="source_absent")
    supplier=None
    if _id(supplier_id):
        row=ctx.conn.execute(select(m.party).where(m.party.c.id==supplier_id,
            m.party.c.company_id==ctx.company_id)).mappings().first()
        if (row is not None and row["id"]==supplier_id and row["company_id"]==ctx.company_id
                and row["kind"]=="supplier"):
            if not isinstance(row["name"],str) or not row["name"]: raise _bad()
            supplier=Supplier(row["id"],row["name"])
    if supplier is None:
        return OwedClaim("not_available",ctx.company_id,ctx.currency,None,None,unavailable_reason="supplier_unknown")
    t=supplier_payable
    rows=list(ctx.conn.execute(select(t).where(t.c.company_id==ctx.company_id,t.c.supplier_id==supplier.id)
        .order_by(t.c.posting_date,t.c.source_number,t.c.id).limit(MAX_ITEMS+1)).mappings())
    if len(rows)>MAX_ITEMS:
        raise Refusal(f"There are more than {MAX_ITEMS} supplier items. This bounded read cannot state the complete "
                      "balance; no partial total was returned.","owed_items_limit")
    if not rows: return OwedClaim("no_activity",ctx.company_id,ctx.currency,supplier,None)
    if ctx.currency not in MINOR_UNITS: raise _bad()
    seen,opens=set(),[]
    # Each persisted row follows the amount contract, but the sum is not a new
    # posted amount: up to 200 valid rows may legitimately exceed that ceiling.
    with localcontext() as arithmetic:
        arithmetic.prec=40
        for row in rows:
            if (not _id(row["id"]) or row["id"] in seen or row["company_id"]!=ctx.company_id
                    or row["supplier_id"]!=supplier.id or row["currency"]!=ctx.currency
                    or row["status"] not in ("open","paid","reversed")
                    or type(row["posting_date"]) is not date or not isinstance(row["source_number"],str)
                    or not row["source_number"] or len(row["source_number"])>30):
                raise _bad()
            seen.add(row["id"])
            try:
                amount=to_amount(row["amount"],ctx.currency)
                outstanding=to_amount(row["outstanding"],ctx.currency)
            except MoneyError:
                raise _bad() from None
            if amount<=0 or outstanding>amount or (row["status"]=="open")!=(outstanding>0): raise _bad()
            if row["status"]=="open":
                opens.append(OwedItem(row["id"],row["source_number"],row["posting_date"],amount,outstanding))
        zero=Decimal(0).quantize(Decimal(1).scaleb(-MINOR_UNITS[ctx.currency]))
        total=sum((item.outstanding for item in opens),zero)
    return OwedClaim("figure" if opens else "verified_zero",ctx.company_id,ctx.currency,supplier,total,
                     tuple(opens),len(rows))


def render_owed(ctx: Ctx, claim: OwedClaim) -> Result:
    """Render the typed source read using the existing exact-money result API."""
    if claim.company_id!=ctx.company_id or claim.currency!=ctx.currency: raise _bad()
    with localcontext() as arithmetic:
        arithmetic.prec=40
        data={"state":claim.state,"value":None if claim.value is None else canonical(claim.value,ctx.currency),
              "currency":ctx.currency,"supplier_id":None if claim.supplier is None else claim.supplier.id,
              "source_count":claim.source_count}
        if claim.state=="not_available":
            why=("these books have no supplier payables yet" if claim.unavailable_reason=="source_absent"
                 else "that supplier is not known in these books")
            return Result(f"I can't state what is owed: {why}.",data)
        name=claim.supplier.name
        if claim.state=="no_activity":
            return Result(f"Nothing has been owed to {name} in these books.",data)
        sh=Shown(ctx.currency)
        total=sh.amount(claim.value,"outstanding",name)
        if claim.state=="verified_zero":
            return Result(f"You owe {name} {total} {ctx.currency}: every recorded payable item is paid or reversed.",
                          data,[total],sh.amounts)
        parts,figures=[],[total]
        for item in claim.items:
            outstanding=sh.amount(item.outstanding,"outstanding",item.number)
            original=sh.amount(item.amount,"outstanding",item.number)
            parts.append(f"{item.number} {outstanding} of {original}")
            figures.extend([outstanding,original])
        return Result(f"You owe {name} {total} {ctx.currency}: {'; '.join(parts)}.",data,figures,sh.amounts)
