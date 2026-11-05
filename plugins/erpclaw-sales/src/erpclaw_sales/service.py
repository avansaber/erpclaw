"""Sales business rules. Called by the contracts and by the demo seed.

Money posts only through the core ledger service. Invoice rows are documents
(their status and outstanding change); ledger rows never change.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select

from erpclaw_core import ledger, outbox
from erpclaw_core import models as m
from erpclaw_core.common import Ctx, Refusal, audit, new_id, now
from erpclaw_core.money import fmt, fmt_qty, fmt_rate, quantize, to_decimal
from erpclaw_core.numbering import next_number

from . import tables as t

PAYMENT_TERMS_DAYS = 30
AR, SALES, BANK = "1200", "4100", "1100"


@dataclass
class PricedLine:
    item: dict
    quantity: Decimal
    rate: Decimal
    amount: Decimal


def price_lines(ctx: Ctx, lines: list[tuple[dict, Decimal]],
                rates: list[Decimal] | None = None) -> tuple[list[PricedLine], Decimal]:
    """Each line at the item's rate, or at the rates given (an order's)."""
    out = []
    for n, (item, qty) in enumerate(lines):
        q = to_decimal(qty)
        if q <= 0:
            raise Refusal(f"The quantity for {item['name']} must be more than zero.", "bad_quantity")
        rate = to_decimal(rates[n] if rates is not None else item["standard_rate"])
        out.append(PricedLine(item, q, rate, quantize(q * rate, ctx.currency)))
    return out, sum((x.amount for x in out), Decimal("0"))


def invoice_email(customer: dict, number: str, priced: list[PricedLine], total: Decimal, due: date,
                  ctx: Ctx) -> tuple[str, str]:
    subject = f"Invoice {number} from {ctx.company_name}"
    body_lines = [f"Hello {customer['name']},", "", f"Please find invoice {number} below.", ""]
    for p in priced:
        body_lines.append(f"  {fmt_qty(p.quantity)} x {p.item['name']} @ {fmt_rate(p.rate, ctx.currency)} = "
                          f"{fmt(p.amount, ctx.currency)}")
    body_lines += ["", f"Total: {fmt(total, ctx.currency)} {ctx.currency}", f"Due: {due:%d %b %Y}", "",
                   f"Thank you, {ctx.company_name}"]
    return subject, "\n".join(body_lines)


def _zero(ctx: Ctx) -> Decimal:
    return quantize(Decimal(0), ctx.currency)  # zero at the currency's scale: 0.00, 0 for JPY


def create_invoice(ctx: Ctx, customer: dict, lines: list[tuple[dict, Decimal]], posting_date: date,
                   submit: bool = True, email: bool = False, operation_key: str | None = None,
                   warehouses: list[str | None] | None = None, order: dict | None = None) -> dict:
    """Save the invoice as a draft with its lines, then, unless a draft was asked for, submit it in the
    same transaction (the database admits lines only into a draft). A stock line keeps the id of the warehouse the
    owner named, if any (`warehouses`: resolved ids, never the owner's words). From an order, the lines are what its deliveries hold uninvoiced, worked out under the order's
    lock (the number, the chain head, then the order: the engine's one lock order), each covering its deliveries."""
    from . import stock_sales as ss

    number = next_number(ctx, "INV", posting_date)
    cover = None
    if order is not None:
        ledger.lock_chain(ctx)
        cover = ss.plan_order_invoice(ctx, ss.lock_order(ctx, order["id"]))
        lines = [(b.item, b.qty) for b in cover.lines]
        priced, total = price_lines(ctx, lines, [b.order_line["rate"] for b in cover.lines])
    else:
        priced, total = price_lines(ctx, lines)
    inv_id = new_id()
    due = posting_date + timedelta(days=PAYMENT_TERMS_DAYS)
    ctx.conn.execute(t.invoice.insert().values(
        id=inv_id, company_id=ctx.company_id, number=number, customer_id=customer["id"],
        posting_date=posting_date, due_date=due, currency=ctx.currency, total=total, outstanding=_zero(ctx),
        status="draft", operation_key=operation_key, created_at=now()))
    # a line's kind is what it was sold as: the item's kind now, or, for a line billing an order's deliveries, the
    # kind they were delivered as (an item delivered as a service and made a stock item since moved no stock)
    kinds = [p.item["kind"] for p in priced] if cover is None else [b.kind for b in cover.lines]
    line_ids = []
    for n, p in enumerate(priced):
        line_ids.append(new_id())
        ctx.conn.execute(t.invoice_line.insert().values(
            id=line_ids[-1], company_id=ctx.company_id, invoice_id=inv_id, currency=ctx.currency,
            item_id=p.item["id"], quantity=p.quantity, rate=p.rate, amount=p.amount, line_no=n + 1,
            named_warehouse_id=(warehouses or [None] * len(priced))[n], item_kind=kinds[n]))
    if cover is not None:
        ss.record_order_cover(ctx, cover, line_ids)
    if submit:
        submit_draft(ctx, inv_id, None, audited=False)
    if cover is not None:
        ss.set_order_status(ctx, cover.order)
    queued_to = None
    if email:
        subject, body = invoice_email(customer, number, priced, total, due, ctx)
        outbox.enqueue(ctx, "deliver_message", customer["email"], subject, body, operation_key)
        queued_to = customer["email"]
    audit(ctx, "submit_invoice" if submit else "save_draft_invoice", "sales_invoice", inv_id,
          detail={"number": number, "total": str(total)})
    return {"id": inv_id, "number": number, "total": total, "due": due, "submitted": submit,
            "email_to": queued_to}


def invoice_lines(ctx: Ctx, inv_id: str) -> list[dict]:
    """An invoice's lines in their order on it (older lines, which have no number, by id)."""
    return [dict(r) for r in ctx.conn.execute(select(t.invoice_line).where(
        t.invoice_line.c.company_id == ctx.company_id, t.invoice_line.c.invoice_id == inv_id
    ).order_by(t.invoice_line.c.line_no.asc().nulls_last(), t.invoice_line.c.id)).mappings()]


def check_draft(ctx: Ctx, inv: dict) -> list[dict]:
    """The refusals before a draft is submitted; returns its lines."""
    if inv["company_id"] != ctx.company_id:
        raise Refusal("That invoice belongs to another company.", "invoice_unknown")
    if inv["status"] != "draft":
        raise Refusal(f"{inv['number']} is not a draft (it is {inv['status']}), so it can't be submitted.",
                      "not_a_draft")
    ledger.open_period(ctx, inv["posting_date"])
    lines = invoice_lines(ctx, inv["id"])
    if not lines or sum((ln["amount"] for ln in lines), Decimal(0)) != inv["total"]:
        raise Refusal(f"{inv['number']}'s lines do not add up to its total, so it can't be submitted.",
                      "lines_differ")
    return lines


def invoice_posting_lines(ctx: Ctx, customer_id: str, total) -> list:
    """The invoice's posting: receivable (the customer as party) against sales, the total each side. The
    submit, its preview and the create-and-submit preview use these same lines."""
    return [ledger.Line(ledger.account_by_number(ctx, AR)["id"], debit=total, party_id=customer_id),
            ledger.Line(ledger.account_by_number(ctx, SALES)["id"], credit=total)]


def submit_draft(ctx: Ctx, inv_id: str, operation_key: str | None, *, audited: bool = True) -> dict:
    """Submit a draft, in the caller's transaction and under the chain-head lock: lock and re-read the draft
    and its lines, issue its stock lines (from the warehouse named, or the one chosen now, recorded in
    sales_invoice_issue), post it with their cost, then set it submitted with its outstanding, refused if the row
    changed."""
    from . import stock_sales as ss

    ledger.lock_chain(ctx)  # the ledger's lock order: the chain head first
    inv = ctx.conn.execute(ledger.for_update(ctx, select(t.invoice).where(
        t.invoice.c.company_id == ctx.company_id, t.invoice.c.id == inv_id))).mappings().first()
    if inv is None:
        raise Refusal("That invoice does not exist in this company.", "invoice_unknown")
    inv = dict(inv)
    lines = check_draft(ctx, inv)
    stock = ss.issue(ctx, "sales_invoice", inv["id"], inv["posting_date"], ss.draft_issues(ctx, lines))
    posted = ledger.post(ctx, inv["posting_date"], "sales_invoice", inv["id"],
                         invoice_posting_lines(ctx, inv["customer_id"], inv["total"]) + stock.lines)
    ss.record_issues(ctx, stock)
    res = ctx.conn.execute(t.invoice.update().where(
        t.invoice.c.id == inv["id"], t.invoice.c.status == "draft", t.invoice.c.total == inv["total"]).values(
        status="submitted", outstanding=inv["total"], submit_operation_key=operation_key))
    if res.rowcount != 1:
        raise Refusal("That invoice changed while it was being submitted; try again.", "conflict")
    if audited:
        audit(ctx, "submit_invoice", "sales_invoice", inv["id"], detail={"number": inv["number"],
                                                                          "total": str(inv["total"])})
    return {**inv, "status": "submitted", "outstanding": inv["total"], "posting": posted}


def open_invoices(ctx: Ctx, customer_id: str) -> list[dict]:
    return [dict(r) for r in ctx.conn.execute(select(t.invoice).where(
        t.invoice.c.company_id == ctx.company_id, t.invoice.c.customer_id == customer_id,
        t.invoice.c.status.in_(("submitted", "partly_paid")),
    ).order_by(t.invoice.c.posting_date, t.invoice.c.number)).mappings()]


DUPLICATE_DAYS = 7  # a payment of the same amount this close is flagged as a possible duplicate


@dataclass
class PaymentPlan:
    """Everything a payment will write, worked out once from the books: the card shows it, the read set names its
    rows, and the commit writes exactly it (recomputed under the commit's locks, so a change refuses or supersedes)."""
    customer: dict
    amount: Decimal
    received: date
    bank: dict
    receivable: dict
    allocations: list[tuple[dict, Decimal]]  # (open invoice as read, amount applied)
    unallocated: Decimal
    skipped: list[dict]                      # open invoices dated after the payment, left out
    duplicates: list[dict]                   # the customer's payments that look like this one
    method: str | None
    reference: str | None

    def posting_lines(self) -> list:
        return [ledger.Line(self.bank["id"], debit=self.amount),
                ledger.Line(self.receivable["id"], credit=self.amount, party_id=self.customer["id"])]


def _norm_ref(text: str | None) -> str:
    return " ".join((text or "").lower().split())


def payment_amount(ctx: Ctx, value) -> Decimal:
    """More than zero, exact at the currency's scale (JPY none, USD two, KWD three places)."""
    from erpclaw_core.money import MoneyError, to_amount
    try:
        amount = to_amount(value, ctx.currency)
    except MoneyError as e:
        raise Refusal(f"The amount received must be a plain amount in {ctx.currency}: {e}.", "bad_amount") from None
    if amount <= 0:
        raise Refusal("The amount received must be more than zero.", "bad_amount")
    return amount


def bank_account(ctx: Ctx, said: str | None) -> dict:
    """The account the money went into: a named account first, whatever the number of bank accounts; otherwise the
    one bank account; several and none named is a question; none at all is refused."""
    rows = [dict(r) for r in ctx.conn.execute(select(m.account).where(
        m.account.c.company_id == ctx.company_id)).mappings()]
    eligible = sorted((a for a in rows if a["account_type"] == "bank" and not a["is_group"] and not a["frozen"]),
                      key=lambda a: a["number"])
    label = [f"{a['number']} {a['name']}" for a in eligible]
    if said:
        from erpclaw_core.masters import _norm
        hit = [a for a in rows if a["number"] == said.strip() or _norm(a["name"]) == _norm(said)
               or _norm(f"{a['number']} {a['name']}") == _norm(said)]
        if len(hit) == 1:
            a = hit[0]
            if a["is_group"]:
                raise Refusal(f"{a['number']} {a['name']} is a group account.", "account_not_bank")
            if a["account_type"] != "bank":
                raise Refusal(f"{a['number']} {a['name']} is not a bank account.", "account_not_bank")
            if a["frozen"]:
                raise Refusal(f"{a['number']} {a['name']} is frozen.", "account_frozen")
            return a
        raise Refusal(f"I can't find a bank account called '{said}'. "
                      + (f"The bank accounts are: {', '.join(label)}." if label else ""),
                      "account_unknown", options=label, field="account",
                      choices=[{"label": lb, "ref": {"account": a["number"]}} for lb, a in zip(label, eligible)])
    if not eligible:
        raise Refusal(f"{ctx.company_name} has no bank account to record it in.", "no_bank_account")
    if len(eligible) > 1:
        raise Refusal(f"Which account did the money go into: {', '.join(label)}?", "account_ambiguous", options=label,
                      field="account",
                      choices=[{"label": lb, "ref": {"account": a["number"]}} for lb, a in zip(label, eligible)])
    return eligible[0]


def receivable_account(ctx: Ctx) -> dict:
    rows = [dict(r) for r in ctx.conn.execute(select(m.account).where(
        m.account.c.company_id == ctx.company_id, m.account.c.account_type == "receivable",
        m.account.c.is_group.is_(False))).mappings()]
    if len(rows) != 1:
        raise Refusal("The chart of accounts needs exactly one receivable account to record a customer payment; "
                      f"it has {len(rows)}.", "receivable_account")
    return rows[0]


def _not_open(inv: dict) -> str:
    return {"paid": f"{inv['number']} is already paid.", "cancelled": f"{inv['number']} is cancelled.",
            "draft": f"{inv['number']} is a draft, so nothing is owed on it yet."}.get(
        inv["status"], f"{inv['number']} is not open.")


def plan_payment(ctx: Ctx, customer: dict, amount, received: date, *, invoice: dict | None = None,
                 oldest_first: bool = False, account: str | None = None, method: str | None = None,
                 reference: str | None = None) -> PaymentPlan:
    """Which invoices the money goes to, and every refusal and question before a card is made."""
    amount = payment_amount(ctx, amount)
    if received > ctx.today:
        raise Refusal("A payment can't be dated in the future.", "future_date")
    bank, receivable = bank_account(ctx, account), receivable_account(ctx)
    opens = open_invoices(ctx, customer["id"])
    if invoice is not None:
        if invoice["status"] not in ("submitted", "partly_paid"):
            raise Refusal(_not_open(invoice), "invoice_not_open")
        if invoice["posting_date"] > received:
            raise Refusal(f"A payment received on {received:%d %b %Y} can't settle {invoice['number']}, dated "
                          f"{invoice['posting_date']:%d %b %Y}.", "payment_before_invoice")
        targets, skipped = [invoice], []
    else:
        targets = [i for i in opens if i["posting_date"] <= received]
        skipped = [i for i in opens if i["posting_date"] > received]
        if len(targets) > 1 and not oldest_first:
            options = [f"{i['number']} ({fmt(i['outstanding'], ctx.currency)} outstanding)" for i in targets]
            raise Refusal(f"{customer['name']} has {len(targets)} open invoices: {'; '.join(options)}. Which should it "
                          "go to, or oldest first?", "invoice_ambiguous", options=[*options, "oldest first"],
                          field="invoice", choices=[  # what each answer settles (the reference never leaves the engine)
                              *({"label": o, "ref": {"invoice": i["number"]}} for o, i in zip(options, targets)),
                              {"label": "oldest first", "ref": {"oldest_first": True}}])
    left, allocations = amount, []
    for inv in targets:
        if left <= 0:
            break
        take = min(left, to_decimal(inv["outstanding"]))
        allocations.append((inv, take))
        left -= take
    window = (received - timedelta(days=DUPLICATE_DAYS), received + timedelta(days=DUPLICATE_DAYS))
    dupes = [dict(r) for r in ctx.conn.execute(select(t.payment).where(
        t.payment.c.company_id == ctx.company_id, t.payment.c.customer_id == customer["id"],
        t.payment.c.status == "submitted").order_by(t.payment.c.posting_date, t.payment.c.number)).mappings()
        if (to_decimal(r["amount"]) == amount and window[0] <= r["posting_date"] <= window[1])
        or (_norm_ref(reference) and _norm_ref(r["reference"]) == _norm_ref(reference))]
    plan = PaymentPlan(customer, amount, received, bank, receivable, allocations, left, skipped, dupes,
                       method, reference)
    ledger.check_post(ctx, received, "sales_payment", None, plan.posting_lines())  # a posting the books would refuse
    return plan


def record_payment(ctx: Ctx, plan: PaymentPlan, operation_key: str | None = None) -> dict:
    """Write the plan: the payment, its allocations with each invoice's outstanding and status (conditional on the
    outstanding read), the posting, the audit row. The caller holds the chain head (a card commit, or the seed)."""
    pay_id = new_id()
    number = next_number(ctx, "PAY", plan.received)
    ctx.conn.execute(t.payment.insert().values(
        id=pay_id, company_id=ctx.company_id, number=number, customer_id=plan.customer["id"],
        posting_date=plan.received, currency=ctx.currency, amount=plan.amount, unallocated=plan.unallocated,
        method=plan.method, reference=plan.reference, status="submitted", operation_key=operation_key,
        created_at=now()))
    for inv, amt in plan.allocations:
        ctx.conn.execute(t.allocation.insert().values(id=new_id(), company_id=ctx.company_id, payment_id=pay_id,
                                                      invoice_id=inv["id"], currency=ctx.currency, amount=amt))
        new_out = to_decimal(inv["outstanding"]) - amt
        res = ctx.conn.execute(t.invoice.update().where(
            t.invoice.c.id == inv["id"], t.invoice.c.outstanding == inv["outstanding"]).values(
            outstanding=new_out, status="paid" if new_out == 0 else "partly_paid"))
        if res.rowcount != 1:
            raise Refusal("That invoice changed while the payment was being saved; nothing was saved.", "conflict")
    ledger.post(ctx, plan.received, "sales_payment", pay_id, plan.posting_lines())
    audit(ctx, "record_payment", "sales_payment", pay_id, detail={"number": number, "amount": str(plan.amount)})
    return {"id": pay_id, "number": number}


def find_invoice(ctx: Ctx, said: str) -> dict:
    s = said.strip().upper()
    rows = [dict(r) for r in ctx.conn.execute(select(t.invoice).where(
        t.invoice.c.company_id == ctx.company_id)).mappings()]
    exact = [r for r in rows if r["number"].upper() == s]
    if exact:
        return exact[0]
    tail = [r for r in rows if s.isdigit() and r["number"].endswith(f"-{int(s):05d}")]
    if len(tail) == 1:
        return tail[0]
    raise Refusal(f"I can't find invoice '{said}'. Ask me to list invoices to see their numbers.",
                  "invoice_unknown")


def live_allocations(ctx: Ctx, invoice_id: str) -> list[dict]:
    """The allocations to an invoice of payments still standing (a reversed payment's allocations are history)."""
    return [dict(r) for r in ctx.conn.execute(
        select(t.allocation.c.amount, t.payment.c.number, t.payment.c.id.label("payment_id")).join(
            t.payment, (t.payment.c.company_id == t.allocation.c.company_id) & (t.payment.c.id == t.allocation.c.payment_id))
        .where(t.allocation.c.company_id == ctx.company_id, t.allocation.c.invoice_id == invoice_id,
               t.payment.c.status == "submitted").order_by(t.payment.c.number)).mappings()]


@dataclass
class Reversal:
    """A cancellation or a payment reversal, checked before its card: what the books' reversal will post (the
    original posting's lines mirrored) and what changes on the documents."""
    document: dict
    reversal_date: date
    original: dict | None            # the posting reversed; none for a draft invoice
    mirror: list                     # its lines mirrored, as the posting service will write them
    invoices: list[tuple[dict, Decimal]] = None  # a payment's invoices, each with the amount given back


def _check_date(ctx: Ctx, when: date) -> None:
    if when > ctx.today:
        raise Refusal("A reversal can't be dated in the future.", "future_date")


def plan_cancel(ctx: Ctx, inv: dict, when: date) -> Reversal:
    if inv["status"] == "cancelled":
        raise Refusal(f"{inv['number']} is already cancelled.", "already_cancelled")
    live = live_allocations(ctx, inv["id"])
    if live:
        names = ", ".join(f"{a['number']} ({fmt(a['amount'], ctx.currency)})" for a in live)
        first = live[0]["number"]
        raise Refusal(f"{inv['number']} has payments applied: {names}. Reverse those first (\"reverse {first}\"), "
                      "then cancel the invoice.", "has_payments")
    if inv["status"] == "draft":
        return Reversal(inv, when, None, [])
    _check_date(ctx, when)
    original = ledger.posting_for_voucher(ctx, "sales_invoice", inv["id"])
    return Reversal(inv, when, original, ledger.check_reverse(ctx, original["id"], when))


def cancel_invoice(ctx: Ctx, plan: Reversal, operation_key: str | None = None) -> dict:
    """The reversal first, so the database can check it (sales_0002), then the invoice cancelled. The caller holds
    the chain head."""
    inv = plan.document
    if plan.original is not None:
        ledger.reverse(ctx, plan.original["id"], plan.reversal_date)
    res = ctx.conn.execute(t.invoice.update().where(t.invoice.c.id == inv["id"],
                                                    t.invoice.c.status == inv["status"]).values(
        status="cancelled", outstanding=_zero(ctx), cancel_operation_key=operation_key))
    if res.rowcount != 1:
        raise Refusal("That invoice changed while it was being cancelled; nothing was saved.", "conflict")
    audit(ctx, "cancel_invoice", "sales_invoice", inv["id"], detail={"number": inv["number"]})
    return {"number": inv["number"]}


def find_payment(ctx: Ctx, said: str) -> dict:
    s_ = said.strip().upper()
    rows = [dict(r) for r in ctx.conn.execute(select(t.payment).where(t.payment.c.company_id == ctx.company_id)).mappings()]
    exact = [r for r in rows if r["number"].upper() == s_]
    if exact:
        return exact[0]
    tail = [r for r in rows if s_.isdigit() and r["number"].endswith(f"-{int(s_):05d}")]
    if len(tail) == 1:
        return tail[0]
    if len(tail) > 1:
        raise Refusal(f"More than one payment ends in {said}: {', '.join(r['number'] for r in tail)}. Which one?",
                      "payment_ambiguous", options=[r["number"] for r in tail], field="payment",
                      choices=[{"label": r["number"], "ref": {"payment": r["number"]}} for r in tail])
    raise Refusal(f"I can't find payment '{said}'.", "payment_unknown")


def payment_option(ctx: Ctx, pay: dict, paid: str) -> str:
    """A payment as a question names it: its number, amount and date, and what it paid."""
    return f"{pay['number']} ({fmt(pay['amount'], ctx.currency)}, {pay['posting_date']:%d %b %Y}{paid})"


def plan_reverse_payment(ctx: Ctx, pay: dict, when: date) -> Reversal:
    if pay["status"] == "cancelled":
        raise Refusal(f"{pay['number']} is already reversed.", "already_reversed")
    _check_date(ctx, when)
    original = ledger.posting_for_voucher(ctx, "sales_payment", pay["id"])
    mirror = ledger.check_reverse(ctx, original["id"], when)
    rows = ctx.conn.execute(select(t.allocation.c.amount, t.invoice).join(
        t.invoice, (t.invoice.c.company_id == t.allocation.c.company_id) & (t.invoice.c.id == t.allocation.c.invoice_id))
        .where(t.allocation.c.payment_id == pay["id"]).order_by(t.invoice.c.posting_date, t.invoice.c.number)).mappings()
    invoices = [({k: v for k, v in r.items() if k != "amount"}, to_decimal(r["amount"])) for r in rows]
    return Reversal(pay, when, original, mirror, invoices)


def new_status(total: Decimal, outstanding: Decimal, paid: bool | None = None) -> str:
    """An open invoice's status from what it owes; `paid` says whether a payment still stands on it, since a
    credit note also lowers what it owes."""
    if outstanding == 0:
        return "paid"
    if paid is None:
        paid = outstanding != total
    return "partly_paid" if paid else "submitted"


def reverse_payment(ctx: Ctx, plan: Reversal, operation_key: str | None = None, reason: str | None = None) -> dict:
    """The reversal posting first, then the payment marked reversed, then each invoice it paid owes its share
    again (conditional on the outstanding read). The caller holds the chain head."""
    pay = plan.document
    ledger.reverse(ctx, plan.original["id"], plan.reversal_date)
    res = ctx.conn.execute(t.payment.update().where(t.payment.c.id == pay["id"], t.payment.c.status == "submitted")
                           .values(status="cancelled", cancel_operation_key=operation_key))
    if res.rowcount != 1:
        raise Refusal("That payment changed while it was being reversed; nothing was saved.", "conflict")
    for inv, amt in plan.invoices:
        out = to_decimal(inv["outstanding"]) + amt
        res = ctx.conn.execute(t.invoice.update().where(
            t.invoice.c.id == inv["id"], t.invoice.c.outstanding == inv["outstanding"]).values(
            outstanding=out, status=new_status(to_decimal(inv["total"]), out,
                                               paid=bool(live_allocations(ctx, inv["id"])))))
        if res.rowcount != 1:
            raise Refusal("That invoice changed while the payment was being reversed; nothing was saved.", "conflict")
    audit(ctx, "reverse_payment", "sales_payment", pay["id"], detail={"number": pay["number"], "reason": reason or ""})
    return {"number": pay["number"]}


def customer_name(ctx: Ctx, customer_id: str) -> str:
    return ctx.conn.execute(select(m.party.c.name).where(m.party.c.id == customer_id)).scalar_one()
