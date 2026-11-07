"""Whole supplier-payment reversal behind a future exact Core approval.

Resolved stored identities only. Allocations and original postings remain
immutable; Core owns the locks, effect savepoint, outcome and retry handling.
This module registers no public contract and issues no commit authority.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
import hmac
import json

from sqlalchemy import select

from . import approvals, integration, ledger, models as m, supplier_payments as payment
from .common import Ctx, Refusal, audit
from .contracts import Amount, Preview, Result, Shown
from .payable_tables import supplier_payable, supplier_payment, supplier_payment_allocation

NAME = "reverse_supplier_payment"
PAYMENT = payment.PAYMENT


@dataclass(frozen=True)
class ReverseSupplierPayment:
    payment_id: str
    reversal_date: date
    reason: str | None = None


@dataclass(frozen=True)
class Release:
    allocation: dict
    item: dict
    amount: Decimal
    before: Decimal
    after: Decimal


@dataclass(frozen=True)
class ReversalPlan:
    request: ReverseSupplierPayment
    payment: dict
    supplier: dict
    original: dict
    mirror: tuple[ledger.Line, ...]
    releases: tuple[Release, ...]
    amount: Decimal
    accounts: dict[str, dict]
    original_period: dict
    reversal_period: dict
    read_set: tuple[tuple[str, str], ...]
    mode: str
    external: str | None
    codes: dict[str, str]


def _bad(message="The payment's stored posting, allocations and delivery balances do not agree."):
    return Refusal(message, "payment_reversal_source")


def _one(ctx, table, key):
    row = ctx.conn.execute(select(table).where(table.c.company_id == ctx.company_id,
                                              table.c.id == key)).mappings().first()
    if row is None or row["id"] != key or row["company_id"] != ctx.company_id:
        raise _bad("A record needed to reverse this payment is missing from these books.")
    return dict(row)


def _header(ctx, row, supplier_id=None):
    if (row["company_id"] != ctx.company_id or row["currency"] != ctx.currency
            or row["status"] not in ("posted", "reversed")
            or (supplier_id is not None and row["supplier_id"] != supplier_id)
            or payment._amount(ctx, row["unallocated"], positive=False) != 0):
        raise _bad()
    payment._id(row["id"])
    payment._date(row["posting_date"])
    payment._amount(ctx, row["amount"])


def _posting(ctx, kind, key, day, dependencies):
    original = ledger.posting_for_voucher(ctx, kind, key)
    if (original["company_id"] != ctx.company_id or original["voucher_type"] != kind
            or original["voucher_id"] != key or original["posting_date"] != day
            or original["reverses_posting_id"] is not None):
        raise _bad()
    rows = payment._rows(ctx, select(m.ledger_entry).where(m.ledger_entry.c.company_id == ctx.company_id,
        m.ledger_entry.c.posting_id == original["id"]).order_by(m.ledger_entry.c.line_no),
        payment.MAX_SOURCE_LINES, "original posting lines")
    if (len(rows) < 2 or len({r["id"] for r in rows}) != len(rows)
            or any(r["company_id"] != ctx.company_id or r["posting_id"] != original["id"]
                   or r["currency"] != ctx.currency for r in rows)
            or payment._sum(ctx, (payment._amount(ctx,r["debit"],positive=False) for r in rows))
               != payment._sum(ctx, (payment._amount(ctx,r["credit"],positive=False) for r in rows))):
        raise _bad()
    dependencies.add(("posting",original["id"]))
    dependencies.update(("ledger_entry",r["id"]) for r in rows)
    return original,rows


def _history(ctx, item, supplier_id, dependencies, verified, allocation_ids, remaining):
    a = supplier_payment_allocation
    rows = payment._rows(ctx, select(a).where(a.c.company_id == ctx.company_id,
        a.c.payable_id == item["id"]).order_by(a.c.id), min(payment.MAX_HISTORY_PER_ITEM,remaining),
        "delivery allocation history")
    seen, live = set(), []
    for row in rows:
        if (row["company_id"] != ctx.company_id or row["payable_id"] != item["id"]
                or row["currency"] != ctx.currency or row["payment_id"] in seen):
            raise _bad()
        seen.add(row["payment_id"])
        prior = _one(ctx,supplier_payment,row["payment_id"])
        _header(ctx,prior,supplier_id)
        if prior["posting_date"] < item["posting_date"]:
            raise _bad()
        members = payment._payment_membership(ctx,prior,supplier_id,dependencies,verified,allocation_ids)
        member = members.get(row["id"])
        if member is None or any(member[k] != row[k] for k in
                ("company_id","payment_id","payable_id","currency","amount")):
            raise _bad()
        if prior["status"] == "posted": live.append(payment._amount(ctx,row["amount"]))
        dependencies.update({("supplier_payment",prior["id"]),("supplier_payment_allocation",row["id"])})
    return payment._sum(ctx,live), len(rows), seen


def plan_reversal(ctx: Ctx, request: ReverseSupplierPayment) -> ReversalPlan:
    key, when = payment._id(request.payment_id), payment._date(request.reversal_date)
    if request.reason is not None and (not isinstance(request.reason,str) or len(request.reason)>140):
        raise Refusal("A reversal reason is text of at most 140 characters.", "payment_reversal_reason")
    if when > ctx.today: raise Refusal("A reversal can't be dated in the future.", "future_date")
    pay = _one(ctx,supplier_payment,key)
    _header(ctx,pay)
    if pay["status"] == "reversed": raise Refusal(f"{pay['number']} is already reversed.", "already_reversed")
    if pay["reverse_operation_key"] is not None: raise _bad()
    if when < pay["posting_date"]: raise Refusal("A reversal cannot be dated before its payment.", "reversal_before_original")
    supplier = payment._supplier(ctx,pay["supplier_id"])
    amount = payment._amount(ctx,pay["amount"])
    dependencies = {("company",ctx.company_id),("party",supplier["id"]),("supplier_payment",key)}
    original,lines = _posting(ctx,PAYMENT,key,pay["posting_date"],dependencies)
    # Bound and certify the append-only original BEFORE shared check_reverse's .all().
    if len(lines)!=2: raise _bad("This payment does not have the supported original payable and bank posting.")
    debits = [r for r in lines if payment._amount(ctx,r["debit"],positive=False)>0]
    credits = [r for r in lines if payment._amount(ctx,r["credit"],positive=False)>0]
    if (len(debits)!=1 or len(credits)!=1 or debits[0] is credits[0]
            or debits[0]["party_id"] != supplier["id"] or credits[0]["party_id"] is not None
            or payment._amount(ctx,debits[0]["debit"])!=amount
            or payment._amount(ctx,credits[0]["credit"])!=amount
            or payment._amount(ctx,debits[0]["credit"],positive=False)!=0
            or payment._amount(ctx,credits[0]["debit"],positive=False)!=0
            or credits[0]["account_id"] != pay["account_id"]):
        raise _bad()
    ap_id = debits[0]["account_id"]
    accounts = {i:_one(ctx,m.account,i) for i in {ap_id,pay["account_id"]}}
    if ap_id == pay["account_id"]: raise _bad()
    dependencies.update(("account",i) for i in accounts)
    mirror = tuple(ledger.check_reverse(ctx,original["id"],when))
    reversal_period = dict(ledger.open_period(ctx,when))
    # The original period may be closed; do not ask open_period to reopen/refuse it.
    periods = payment._rows(ctx,select(m.fiscal_period).where(m.fiscal_period.c.company_id==ctx.company_id,
        m.fiscal_period.c.start_date<=pay["posting_date"],m.fiscal_period.c.end_date>=pay["posting_date"]),
        1,"original fiscal periods")
    if len(periods)!=1: raise _bad("The payment's original fiscal period is missing.")
    original_period=periods[0]
    for period in (original_period,reversal_period):
        dependencies.update({("fiscal_period",period["id"]),("fiscal_year",period["fiscal_year_id"])})
    verified,allocation_ids = {},set()
    members=payment._payment_membership(ctx,pay,supplier["id"],dependencies,verified,allocation_ids)
    releases=[];history_count=0
    for allocation in sorted(members.values(),key=lambda r:r["payable_id"]):
        item=_one(ctx,supplier_payable,allocation["payable_id"])
        value=payment._amount(ctx,allocation["amount"])
        before=payment._amount(ctx,item["outstanding"],positive=False)
        total=payment._amount(ctx,item["amount"])
        if (item["status"] not in ("open","paid") or (item["status"]=="paid") != (before==0)
                or before>total): raise _bad()
        source,source_lines=_posting(ctx,payment.payables.DELIVERY,item["source_id"],item["posting_date"],dependencies)
        if ctx.conn.execute(select(m.posting.c.id).where(m.posting.c.company_id==ctx.company_id,
                m.posting.c.reverses_posting_id==source["id"]).limit(1)).first():
            raise _bad("A delivery settled by this payment has been reversed.")
        party_lines=[r for r in source_lines if r["party_id"] is not None]
        if (len(party_lines)!=1 or party_lines[0]["account_id"]!=ap_id
                or party_lines[0]["party_id"]!=supplier["id"]
                or payment._amount(ctx,party_lines[0]["credit"])!=total
                or payment._amount(ctx,party_lines[0]["debit"],positive=False)!=0): raise _bad()
        live,count,payments_seen=_history(ctx,item,supplier["id"],dependencies,verified,allocation_ids,
                                         payment.MAX_HISTORY-history_count)
        history_count+=count
        if pay["id"] not in payments_seen or total-live!=before: raise _bad()
        after=payment._sum(ctx,(before,value))
        if after>total: raise _bad()
        releases.append(Release(allocation,item,value,before,after))
    mode,external=integration.system_of_record(ctx,"accounting")
    codes={}
    if mode=="external":
        codes=integration.account_codes(ctx,external,list(accounts))
        mappings=payment._rows(ctx,select(m.account_map).where(m.account_map.c.company_id==ctx.company_id,
            m.account_map.c.external_system==external,m.account_map.c.account_id.in_(accounts)),len(accounts),"account mappings")
        dependencies.update(("account_map",row["id"]) for row in mappings)
    return ReversalPlan(request,pay,supplier,original,mirror,tuple(releases),amount,accounts,
        original_period,reversal_period,tuple(sorted(dependencies)),mode,external,codes)


def _preview_plan(ctx: Ctx,plan: ReversalPlan) -> Preview:
    sh=Shown(ctx.currency);sh.title("Reverse a supplier payment")
    sh.line("Payment",plan.payment["number"]);sh.line("Supplier",plan.supplier["name"])
    sh.line("Amount paid",sh.amount(plan.amount));sh.line("Original date",plan.payment["posting_date"].isoformat())
    sh.line("Reversal date",plan.request.reversal_date.isoformat())
    if plan.request.reason: sh.line("Reason",plan.request.reason)
    for release in plan.releases:
        sh.line(release.item["source_number"],f"{sh.amount(release.amount)} owed again: outstanding "
            f"{sh.amount(release.before)} to {sh.amount(release.after)}; {release.item['status']} to open.")
    sh.line("Accounting destination","ERPClaw general ledger" if plan.mode=="native" else
            f"{plan.external}: ERPClaw sub-ledger, with the journal staged for this system")
    for line in plan.mirror:
        account=plan.accounts[line.account_id]
        mapped=f" (external account {plan.codes[line.account_id]})" if plan.mode=="external" else ""
        side,value=("debit",line.debit) if line.debit else ("credit",line.credit)
        party=f" ({plan.supplier['name']})" if line.party_id else ""
        sh.book(f"{account['number']} {account['name']}{party}{mapped}: {side} {sh.amount(value)} {ctx.currency}")
    sh.line("Original entries","The original entries stay; reversing entries are added on the reversal date.")
    if plan.original_period["id"]!=plan.reversal_period["id"]:
        sh.warning(f"{plan.payment['number']} stays in the period beginning {plan.original_period['start_date']}; "
                   f"this reversal is in the period beginning {plan.reversal_period['start_date']}.")
    sh.warning("Use this when the payment was not made or was recorded by mistake: it puts the money back in "
               "the bank account in the books, and the supplier is owed it again.")
    return Preview(sh.card(),"supplier-payment-reversal-v1")


def preview_reversal(ctx,request): return _preview_plan(ctx,plan_reversal(ctx,request))


def reversal_read_set(ctx,request): return list(plan_reversal(ctx,request).read_set)


def _approval(ctx,key):
    from .plugins import REGISTRY
    approval_id=approvals.committing(ctx)
    contract=REGISTRY.contracts.get(NAME);owner=REGISTRY.plugins.get("core")
    if (approval_id is None or contract is None or owner is None or REGISTRY.contract_plugin.get(NAME)!="core"
            or contract.name!=NAME or contract.plugin!="core" or contract.effect!="write" or contract.planned is not None
            or owner.manifest.contracts.get(NAME)!="write" or PAYMENT not in owner.manifest.voucher_types):
        raise Refusal("A supplier payment reverses only in its registered Core card's active approved commit.","not_approved")
    row=ctx.conn.execute(select(m.approval).where(m.approval.c.id==approval_id,
        m.approval.c.company_id==ctx.company_id)).mappings().first()
    if (row is None or row["id"]!=approval_id or row["company_id"]!=ctx.company_id
            or row["contract"]!=NAME or row["status"]!="consumed" or not isinstance(key,str) or not key
            or row["operation_key"]!=key):
        raise Refusal("The active reversal approval does not name this company and operation.","not_approved")
    if ctx.conn.execute(select(m.operation_outcome.c.key).where(m.operation_outcome.c.approval_id==approval_id)).first():
        raise Refusal("This reversal approval already has an outcome.","not_approved")
    if ctx.conn.execute(select(supplier_payment.c.id).where(supplier_payment.c.reverse_operation_key==key)).first():
        raise Refusal("This reversal operation is already stored; read its outcome instead.","already_reversed")
    return dict(row),contract


def _approved_plan(ctx,request,key):
    from .plugins import REGISTRY
    card,contract=_approval(ctx,key)
    plan=plan_reversal(ctx,request)
    try:
        stored=json.loads(card["card_json"]);rows=json.loads(card["read_set_json"]);args=json.loads(card["args_json"])
        renderer=replace(contract,preview=lambda context,unused:_preview_plan(context,plan))
        actual=approvals._preview(ctx,renderer,request,stored.get("date_source"))
        actual_rows=[[t,k,approvals._row_digest(ctx.conn,t,k)] for t,k in plan.read_set]
        current={**card,"contract_version":REGISTRY.plugins["core"].version,"impl_version":approvals.impl_version(contract)}
        match=(actual==stored and actual_rows==rows
               and hmac.compare_digest(approvals.digest(approvals._fields(current,args,actual_rows,actual)),card["digest"]))
    except (ValueError,TypeError,KeyError,AttributeError):
        raise Refusal("The reversal approval is incomplete; request a fresh card.","stale") from None
    if not match: raise Refusal("The payment, its source rows or accounting destination changed; request a fresh card.","stale")
    final,_=_approval(ctx,key)
    if final!=card: raise Refusal("The reversal approval changed before its effect.","stale")
    return plan


def commit_reversal(ctx: Ctx,request: ReverseSupplierPayment,operation_key: str) -> Result:
    plan=_approved_plan(ctx,request,operation_key)
    # Core owns the chain/read-set locks and savepoint. Every failure propagates.
    reversed_posting=ledger.reverse(ctx,plan.original["id"],request.reversal_date)
    pay=plan.payment
    changed=ctx.conn.execute(supplier_payment.update().where(supplier_payment.c.company_id==ctx.company_id,
        supplier_payment.c.id==pay["id"],supplier_payment.c.status=="posted",
        supplier_payment.c.reverse_operation_key.is_(None),supplier_payment.c.operation_key==pay["operation_key"],
        supplier_payment.c.supplier_id==pay["supplier_id"],supplier_payment.c.currency==ctx.currency,
        supplier_payment.c.amount==pay["amount"],supplier_payment.c.account_id==pay["account_id"])
        .values(status="reversed",reverse_operation_key=operation_key))
    if changed.rowcount!=1: raise Refusal("The payment changed during its reversal; nothing was saved.","conflict")
    for release in plan.releases:
        item=release.item
        changed=ctx.conn.execute(supplier_payable.update().where(supplier_payable.c.company_id==ctx.company_id,
            supplier_payable.c.id==item["id"],supplier_payable.c.supplier_id==pay["supplier_id"],
            supplier_payable.c.currency==ctx.currency,supplier_payable.c.source_type==payment.payables.DELIVERY,
            supplier_payable.c.source_id==item["source_id"],supplier_payable.c.amount==item["amount"],
            supplier_payable.c.status==item["status"],supplier_payable.c.outstanding==release.before)
            .values(outstanding=release.after,status="open"))
        if changed.rowcount!=1: raise Refusal("A delivery changed during the reversal; nothing was saved.","conflict")
    audit(ctx,NAME,PAYMENT,pay["id"],detail={"number":pay["number"],"reason":request.reason or "",
        "posting_id":reversed_posting.posting_id,"operation_key":operation_key})
    sh=Shown(ctx.currency);amount=sh.amount(plan.amount,"amount",pay["number"])
    parts,figures=[],[amount]
    for release in plan.releases:
        reopened=sh.amount(release.amount,"reopened",release.item["source_number"])
        figures.append(reopened)
        parts.append(f"{release.item['source_number']} is owed {reopened} again")
    return Result(f"Reversed {pay['number']} ({amount} {ctx.currency}) on {request.reversal_date.isoformat()}: "
        + "; ".join(parts)+". The original entries are kept.",
        {"id":pay["id"],"number":pay["number"],"posting_id":reversed_posting.posting_id,"operation_key":operation_key},
        figures,sh.amounts)


def find_reversal_by_key(ctx: Ctx,key: str) -> Result | None:
    outcome,approval=m.operation_outcome,m.approval
    raw=ctx.conn.execute(select(outcome.c.result_json).join(approval,approval.c.id==outcome.c.approval_id).where(
        outcome.c.company_id==ctx.company_id,approval.c.company_id==ctx.company_id,
        outcome.c.key==key,outcome.c.status=="committed",approval.c.contract==NAME)).scalar_one_or_none()
    if raw is None: return None
    stored=json.loads(raw)
    return Result(stored["text"],stored.get("data",{}),stored.get("figures",[]),
                  [Amount(**amount) for amount in stored.get("amounts",[])])
