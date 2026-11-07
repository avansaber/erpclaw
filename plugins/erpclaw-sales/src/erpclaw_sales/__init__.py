"""Sales plugin: invoices, payments, cancellation, and sales measures."""
from __future__ import annotations

import hashlib
import datetime as dt
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, WithJsonSchema, model_validator
from sqlalchemy import select

from erpclaw_core import ledger
from erpclaw_core import models as m
from erpclaw_core.common import Ctx, Refusal
from erpclaw_core.contracts import Contract, Input, Preview, Result, Shown, WriteContract, _inline_refs, planned_text
from erpclaw_core.masters import customer_label, find_customer, find_item
from erpclaw_core.metrics import Metric, MetricResult, MetricRow
from erpclaw_core.money import fmt, fmt_qty, to_decimal
from erpclaw_core.outbox import slot_available
from erpclaw_core.periods import Period
from erpclaw_core.manifest import PUBLISHER, Capabilities, Events, Manifest, Source
from erpclaw_core.plugins import Plugin
from erpclaw_core.textkey import text_key

from . import service as s
from . import stock_sales as ss
from . import tables as t


def _label(ctx: Ctx, customer_id: str) -> str:
    """The card's customer line for a stored invoice's customer (core's rule for telling close names apart)."""
    cust = ctx.conn.execute(select(m.party).where(m.party.c.company_id == ctx.company_id,
                                                  m.party.c.id == customer_id)).mappings().one()
    return customer_label(ctx, dict(cust))

def _no_float(v):
    """Refuse floats and bools before Pydantic could coerce them to Decimal."""
    if isinstance(v, (float, bool)):
        raise ValueError("amounts and quantities must be exact (text or integer), never floating point")
    return v


Number = Annotated[Decimal, BeforeValidator(_no_float), Field(gt=0, max_digits=18, decimal_places=4),
                   WithJsonSchema({"type": "number", "exclusiveMinimum": 0})]


def _token(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


def _money(sh: Shown, v: Decimal, what: str | None = None, of: str | None = None) -> str:
    return f"{sh.amount(v, what, of)} {sh.currency}"


def _mark(kind: str, **how) -> dict:
    """How the assistant resolves a field from the owner's words before validation (kept out of the tool schema)."""
    return {"erpclaw": {"kind": kind, **how}}


def _invoice_number(ctx: Ctx, said: str) -> str:
    return s.find_invoice(ctx, said)["number"]


def _payment_number(ctx: Ctx, said: str) -> str:
    return s.find_payment(ctx, said)["number"]


def _invoice_party(ctx: Ctx, number: str) -> str:
    """The customer of an invoice, by its number: a mention of it is the invoice's."""
    return s.customer_name(ctx, s.find_invoice(ctx, number)["customer_id"])


def _payment_party(ctx: Ctx, number: str) -> str:
    return s.customer_name(ctx, s.find_payment(ctx, number)["customer_id"])


def _payer(ctx: Ctx, args: dict) -> str | None:
    """The customer of the invoice the owner named, when they named no customer."""
    if not args.get("invoice"):
        return None
    inv = s.find_invoice(ctx, args["invoice"])
    return ctx.conn.execute(select(m.party.c.name).where(m.party.c.company_id == ctx.company_id,
                                                         m.party.c.id == inv["customer_id"])).scalar_one()


def _amount_question(ctx: Ctx, args: dict) -> tuple[str, list[str]]:
    """The question names the customer and their open invoices, from the engine."""
    name = args.get("customer")
    row = ctx.conn.execute(select(m.party).where(m.party.c.company_id == ctx.company_id, m.party.c.kind == "customer",
                                                 m.party.c.name == name)).mappings().first() if name else None
    if row is None:
        return "How much was received?", []
    owed = [f"{i['number']} ({fmt(i['outstanding'], ctx.currency)} outstanding)" for i in s.open_invoices(ctx, row["id"])]
    return f"How much did {name} pay?" + (f" Open: {', '.join(owed)}." if owed else ""), []


# Off in this release, refused as planned for the core release: payments, cancelling and
# reversing (they come on behind the resolution step's full contract, in their own task), the invoice list and the
# sales figures, which report without their own specification and review yet, and emailing an invoice.
PLANNED = "Core"
EMAIL_PLANNED = "Core"


# ---------- create_invoice ----------

class InvoiceItem(Input):
    item: Annotated[str, _mark("item")] = Field(description="Item name exactly as the owner said it.", max_length=200)
    quantity: Annotated[Number, _mark("quantity")] = Field(description="How many, exactly as the owner said.")


# create_invoice as the model is offered it (no docstring: it would become the schema's description). The stock
# fields of CreateInvoice are read from the owner's words by the assistant, or given by an API caller; the model
# never supplies them.
class OfferedInvoice(Input):
    customer: Annotated[str, _mark("customer")] = Field(description="Customer name exactly as the owner said it.",
                                                        max_length=200)
    items: Annotated[list[InvoiceItem], _mark("lines")] = Field(min_length=1, max_length=20)
    submit: Annotated[bool, _mark("flag", words="draft", when=False)] = Field(default=True, description="false only if the owner asked for a draft.")
    email: Annotated[bool, _mark("off", off="emailing an invoice")] = Field(default=False, description=(
        "true when the owner says to email, send or mail the invoice to the customer "
        "(for example 'send Wayne an invoice'); false otherwise."))


class StockInvoiceItem(InvoiceItem):
    warehouse: Annotated[str | None, _mark("warehouse", resolve=ss.named_warehouse)] = Field(
        default=None, max_length=140, description="The warehouse a stock item ships from, in the owner's words.")


class CreateInvoice(OfferedInvoice):
    items: Annotated[list[StockInvoiceItem], _mark("lines")] = Field(default_factory=list, max_length=20)
    order: str | None = Field(default=None, max_length=30,
                              description="A sales order whose delivered, uninvoiced lines this invoice bills.")

    @model_validator(mode="after")
    def _lines_or_order(self):
        if bool(self.items) == bool(self.order):
            raise ValueError("an invoice names its items, or the sales order it bills, not both")
        return self


def _offered_invoice(_: dict) -> dict:
    """The tool schema the model sees: create_invoice without the fields it never supplies."""
    return {k: v for k, v in _inline_refs(OfferedInvoice.model_json_schema()).items() if k != "title"}


@dataclass
class InvoicePlan:
    customer: dict
    lines: list[tuple[dict, Decimal]]
    priced: list
    total: Decimal
    order: dict | None
    named: list[dict | None]  # each line's named warehouse (stock_on_hand's row), or none
    stock: ss.StockPlan


def _invoice_warehouse(index: int, read):
    """A stock service's warehouse question belongs to this invoice line, including a default-source question."""
    try:
        return read()
    except Refusal as refusal:
        if refusal.code.startswith("warehouse_"):
            refusal.data["field"] = f"items.{index}.warehouse"
        raise


def _resolve_invoice(ctx: Ctx, inp: CreateInvoice) -> InvoicePlan:
    """Everything the invoice will write, worked out from the books: the card shows it, the read set names its rows
    and the commit writes it again under its locks (so a change refuses or supersedes the card)."""
    cust = find_customer(ctx, inp.customer)
    order = None
    if inp.order:
        order = ss.find_order(ctx, inp.order)
        if order["customer_id"] != cust["id"]:
            raise Refusal(f"{order['number']} is {s.customer_name(ctx, order['customer_id'])}'s order, not "
                          f"{cust['name']}'s.", "customer_ambiguous", options=[s.customer_name(ctx, order["customer_id"])])
        if not inp.submit:
            raise Refusal("An invoice for a sales order is submitted at once; it can't be saved as a draft.",
                          "order_draft")
        cover = ss.plan_order_invoice(ctx, order)
        lines = [(b.item, b.qty) for b in cover.lines]
        ss.guard(ctx, [item for item, _ in lines])  # every sales write that takes a stock item, at preview and commit
        priced, total = s.price_lines(ctx, lines, [b.order_line["rate"] for b in cover.lines])
        return InvoicePlan(cust, lines, priced, total, order, [None] * len(lines), ss.StockPlan())
    lines = [(find_item(ctx, li.item), li.quantity) for li in inp.items]
    ss.guard(ctx, [item for item, _ in lines])
    priced, total = s.price_lines(ctx, lines)
    named = [_invoice_warehouse(k, lambda: ss.named_warehouse(ctx, item, li.warehouse))
             if ss.is_stock(item) and li.warehouse else None
             for k, ((item, _), li) in enumerate(zip(lines, inp.items))]
    stock = ss.StockPlan()
    if inp.submit:
        wanted = [ss.Issued(k, item, to_decimal(qty), _invoice_warehouse(k, lambda: ss.source(
            ctx, item, to_decimal(qty), warehouse_id=named[k]["warehouse"] if named[k] else None)), ss.ZERO)
            for k, (item, qty) in enumerate(lines) if ss.is_stock(item)]
        stock = ss.preview_issue(ctx, "sales_invoice", ctx.today, wanted)
    return InvoicePlan(cust, lines, priced, total, None, named, stock)


def _stock_books(sh: Shown, ctx: Ctx, lines: list) -> None:
    """The stock lines of a posting as the card shows them: each account by name, up or down by its normal side."""
    for ln in lines:
        acc = ctx.conn.execute(select(m.account).where(m.account.c.id == ln.account_id)).mappings().one()
        debit = ln.debit > 0
        sign = "+" if debit == (acc["normal_side"] == "debit") else "-"
        sh.book(f"{acc['name']} ({acc['number']}) {sign}{sh.amount(ln.debit if debit else ln.credit)}")


def _stock_lines(sh: Shown, plan: ss.StockPlan) -> None:
    for i in plan.issued:
        sh.line("From", f"{i.source.name} ({fmt_qty(i.source.held)} on hand)")
        cost = f"Cost of goods sold {_money(sh, i.cost)}"
        sh.line("Cost", f"{cost} ({i.note})" if i.note else cost)


def preview_invoice(ctx: Ctx, inp: CreateInvoice) -> Preview:
    if inp.email and EMAIL_PLANNED:  # the whole request is refused, nothing saved
        raise Refusal(planned_text("emailing an invoice", EMAIL_PLANNED), "planned", capability="planned.messaging")
    plan = _resolve_invoice(ctx, inp)
    cust, priced, total = plan.customer, plan.priced, plan.total
    sh = Shown(ctx.currency)
    sh.line("Customer", customer_label(ctx, cust))
    if plan.order:
        sh.line("Order", f"{plan.order['number']}: what has been delivered and not yet invoiced")
    by_line = {i.key: i for i in plan.stock.issued}
    for k, p in enumerate(priced):
        sh.line("Item", f"{fmt_qty(p.quantity)} x {p.item['name']} @ {sh.unit_price(p.rate)} = {sh.amount(p.amount)}")
        if k in by_line:
            _stock_lines(sh, ss.StockPlan([by_line[k]]))
        elif plan.named[k]:
            sh.line("From", f"{plan.named[k]['name']}, when the draft is submitted")
    sh.line("Total", _money(sh, total))
    sh.line("Date", f"{ctx.today:%d %b %Y}")
    sh.line("Due", f"{ctx.today + timedelta(days=s.PAYMENT_TERMS_DAYS):%d %b %Y}")
    sh.line("Tax", "No tax calculated: no tax template is set. That is not a statement that no tax is owed.")
    email_ok = slot_available(ctx.conn, "deliver_message")
    if inp.email:
        if not cust["email"]:
            sh.warning(f"{cust['name']} has no email address on file, so it can't be emailed.")
        elif not email_ok:
            sh.warning("Email isn't set up, so it won't be sent. You can download it instead.")
        else:
            sh.line("Email to", cust["email"])
    if inp.submit:
        # no card for a posting the books would refuse; the invoice does not exist yet, so no voucher id
        ledger.check_post(ctx, ctx.today, "sales_invoice", None,
                          s.invoice_posting_lines(ctx, cust["id"], total) + plan.stock.lines)
        sh.book(f"Accounts Receivable ({cust['name']}) +{sh.amount(total)}")
        sh.book(f"Sales +{sh.amount(total)}")
        _stock_books(sh, ctx, plan.stock.lines)
        if plan.order:
            sh.book("No cost of goods sold: the deliveries posted it.")
        sh.title("Create and submit a sales invoice")
    else:
        sh.book("Nothing posts to the books until the draft is submitted.")
        sh.title("Save a draft sales invoice")
    token = _token(cust["id"], cust["email"], ctx.today, email_ok, plan.order and plan.order["id"],
                   *[(p.item["id"], p.rate, p.quantity) for p in priced],
                   *[(i.key, i.source.warehouse, i.cost) for i in plan.stock.issued],
                   *[n and n["warehouse"] for n in plan.named])
    return Preview(sh.card(), token)


def commit_invoice(ctx: Ctx, inp: CreateInvoice, key: str) -> Result:
    plan = _resolve_invoice(ctx, inp)
    cust = plan.customer
    send = inp.email and bool(cust["email"]) and slot_available(ctx.conn, "deliver_message")
    r = s.create_invoice(ctx, cust, plan.lines, ctx.today, submit=inp.submit, email=send, operation_key=key,
                         warehouses=[n and n["warehouse"] for n in plan.named], order=plan.order)
    return _invoice_result(ctx, r["number"], cust["name"], r["total"], r["due"], r["submitted"], r["email_to"])


def _invoice_result(ctx, number, customer, total, due, submitted, email_to) -> Result:
    what = "Saved and submitted" if submitted else "Saved draft"
    sh = Shown(ctx.currency)
    shown = _money(sh, total, "total", number)
    text = f"{what} invoice {number} for {customer}: {shown}, due {due:%d %b %Y}."
    if email_to:
        text += f" Email to {email_to} queued."
    return Result(text, {"number": number, "total": str(total)}, [shown], sh.amounts)


def invoice_by_key(ctx: Ctx, key: str) -> Result | None:
    r = ctx.conn.execute(select(t.invoice).where(t.invoice.c.operation_key == key)).mappings().first()
    if r is None:
        return None
    return _invoice_result(ctx, r["number"], s.customer_name(ctx, r["customer_id"]), r["total"], r["due_date"],
                           r["status"] != "draft", None)


# ---------- read sets ----------

def _period(ctx: Ctx, d) -> tuple[str, str]:
    return ("fiscal_period", ledger.find_period(ctx, d)["id"])


def read_set_invoice(ctx: Ctx, inp: CreateInvoice) -> list[tuple[str, str]]:
    plan = _resolve_invoice(ctx, inp)
    order = [("sales_order", plan.order["id"])] if plan.order else []
    return [("company", ctx.company_id), ("party", plan.customer["id"]), _period(ctx, ctx.today), *order,
            *[("item", item["id"]) for item, _ in plan.lines], *plan.stock.read_set]


def _invoice_rows(ctx: Ctx, inv: dict) -> list[tuple[str, str]]:
    """The invoice, its customer (the card shows the name) and its lines."""
    return [("sales_invoice", inv["id"]), ("party", inv["customer_id"]),
            *[("sales_invoice_line", ln["id"]) for ln in s.invoice_lines(ctx, inv["id"])]]


def _submit_stock(ctx: Ctx, inv: dict, lines: list[dict]) -> ss.StockPlan:
    """A draft's stock lines as its submission would issue them now."""
    return ss.preview_issue(ctx, "sales_invoice", inv["posting_date"], ss.draft_issues(ctx, lines))


def read_set_submit(ctx: Ctx, inp) -> list[tuple[str, str]]:
    inv = s.find_invoice(ctx, inp.invoice)
    lines = s.check_draft(ctx, inv)
    return [("company", ctx.company_id), _period(ctx, inv["posting_date"]), *_invoice_rows(ctx, inv),
            *[("item", ln["item_id"]) for ln in lines], *_submit_stock(ctx, inv, lines).read_set]




# ---------- submit_invoice ----------

class SubmitInvoice(Input):
    invoice: Annotated[str, _mark("document", what="invoice", prefix="INV", nouns=("invoice", "inv"),
                                  resolve=_invoice_number, party=_invoice_party)] = Field(description="The draft invoice's number as the owner named it, e.g. INV-2026-00003.",
                         max_length=30)


def preview_submit(ctx: Ctx, inp: SubmitInvoice) -> Preview:
    inv = s.find_invoice(ctx, inp.invoice)
    lines = s.check_draft(ctx, inv)  # a draft of this company, in an open period, its lines adding up
    stock = _submit_stock(ctx, inv, lines)
    ledger.check_post(ctx, inv["posting_date"], "sales_invoice", inv["id"],  # the posting it will make
                      s.invoice_posting_lines(ctx, inv["customer_id"], inv["total"]) + stock.lines)
    name = s.customer_name(ctx, inv["customer_id"])
    items = dict(ctx.conn.execute(select(m.item.c.id, m.item.c.name).where(
        m.item.c.company_id == ctx.company_id, m.item.c.id.in_([ln["item_id"] for ln in lines]))).all())
    sh = Shown(ctx.currency)
    sh.line("Invoice", inv["number"])
    sh.line("Customer", _label(ctx, inv["customer_id"]))
    by_line = {i.key: i for i in stock.issued}
    for ln in lines:
        sh.line("Item", f"{fmt_qty(ln['quantity'])} x {items[ln['item_id']]} @ {sh.unit_price(ln['rate'])} = "
                        f"{sh.amount(ln['amount'])}")
        if ln["id"] in by_line:
            _stock_lines(sh, ss.StockPlan([by_line[ln["id"]]]))
    sh.line("Total", _money(sh, inv["total"]))
    sh.line("Date", f"{inv['posting_date']:%d %b %Y}")
    sh.line("Due", f"{inv['due_date']:%d %b %Y}")
    sh.line("Tax", "No tax calculated: no tax template is set. That is not a statement that no tax is owed.")
    sh.book(f"Accounts Receivable ({name}) +{sh.amount(inv['total'])}")
    sh.book(f"Sales +{sh.amount(inv['total'])}")
    _stock_books(sh, ctx, stock.lines)
    sh.title("Submit a draft sales invoice")
    token = _token(inv["id"], inv["status"], inv["total"], inv["posting_date"], inv["customer_id"],
                   *[(ln["id"], ln["item_id"], ln["quantity"], ln["rate"], ln["amount"]) for ln in lines],
                   *[(i.key, i.source.warehouse, i.cost) for i in stock.issued])
    return Preview(sh.card(), token)


def commit_submit(ctx: Ctx, inp: SubmitInvoice, key: str) -> Result:
    inv = s.find_invoice(ctx, inp.invoice)
    r = s.submit_draft(ctx, inv["id"], key)
    return _submit_result(ctx, r, r["posting"].seq)


def _submit_result(ctx: Ctx, inv: dict, seq: int) -> Result:
    sh = Shown(ctx.currency)
    shown = _money(sh, inv["total"], "total", inv["number"])
    text = (f"Submitted invoice {inv['number']} for {s.customer_name(ctx, inv['customer_id'])}: "
            f"{shown}, due {inv['due_date']:%d %b %Y}.")
    return Result(text, {"number": inv["number"], "total": str(inv["total"]), "posting_seq": seq}, [shown],
                  sh.amounts)


def submit_by_key(ctx: Ctx, key: str) -> Result | None:
    inv = ctx.conn.execute(select(t.invoice).where(t.invoice.c.submit_operation_key == key)).mappings().first()
    if inv is None:
        return None
    return _submit_result(ctx, dict(inv), ledger.posting_for_voucher(ctx, "sales_invoice", inv["id"])["seq"])


# ---------- record_payment ----------

class RecordPayment(Input):
    customer: Annotated[str | None, _mark("customer", presence="derived", derive=_payer, payer=True)] = Field(
        default=None, max_length=200,
        description="Customer name exactly as the owner said it; leave out when the owner named an invoice.")
    amount: Annotated[Number, _mark("figure", ask=_amount_question)] = Field(
        description="Amount received, exactly as the owner said.")
    invoice: Annotated[str | None, _mark("document", presence="optional", what="invoice", prefix="INV",
                                         nouns=("invoice", "inv"), resolve=_invoice_number, party=_invoice_party)] = Field(
        default=None, max_length=30,
                                description="Invoice number only if the owner named one.")
    oldest_first: Annotated[bool, _mark("flag", words="oldest_first", when=True)] = Field(default=False, description="true only if the owner said to apply it oldest first.")
    date: Annotated[dt.date | None, _mark("date", presence="optional")] = Field(
        default=None, description="The date received, only if the owner said one (ISO).")
    account: Annotated[str | None, _mark("account")] = Field(
        default=None, max_length=200, description="The bank account the money went into, only if the owner named one.")
    method: Annotated[str | None, _mark("text", words="methods")] = Field(
        default=None, max_length=60, description="How it was paid if the owner said, e.g. 'bank transfer', 'check'.")
    reference: Annotated[str | None, _mark("text")] = Field(
        default=None, max_length=140, description="A reference the owner gave, e.g. a cheque or transfer number.")


def _payment_plan(ctx: Ctx, inp: RecordPayment) -> s.PaymentPlan:
    invoice = s.find_invoice(ctx, inp.invoice) if inp.invoice else None
    if invoice is not None:
        cust = dict(ctx.conn.execute(select(m.party).where(m.party.c.company_id == ctx.company_id,
                                                           m.party.c.id == invoice["customer_id"])).mappings().one())
        if inp.customer:
            said = find_customer(ctx, inp.customer)
            if said["id"] != cust["id"]:
                raise Refusal(f"{invoice['number']} is {cust['name']}'s invoice, not {said['name']}'s. Is the payment "
                              f"from {cust['name']}?", "customer_ambiguous", options=[cust["name"]])
    elif inp.customer:
        cust = find_customer(ctx, inp.customer)
    else:
        raise Refusal("Which customer paid?", "customer_missing")
    return s.plan_payment(ctx, cust, inp.amount, inp.date or ctx.today, invoice=invoice,
                          oldest_first=inp.oldest_first, account=inp.account, method=inp.method,
                          reference=inp.reference)


def preview_payment(ctx: Ctx, inp: RecordPayment) -> Preview:
    plan = _payment_plan(ctx, inp)
    name, sh = plan.customer["name"], Shown(ctx.currency)
    sh.line("Customer", customer_label(ctx, plan.customer))
    sh.line("Amount received", _money(sh, plan.amount))
    sh.line("Date", f"Received {plan.received:%d %b %Y}")
    sh.line("Account", f"{plan.bank['number']} {plan.bank['name']}")
    sh.line("Method", plan.method or "not stated (will be saved as not recorded)")
    if plan.reference:
        sh.line("Reference", plan.reference)
    for inv, amt in plan.allocations:
        after = "paid" if amt == to_decimal(inv["outstanding"]) else "partly paid"
        sh.line("Applied to", f"{inv['number']} ({sh.amount(amt)} of {sh.amount(inv['outstanding'])} outstanding), "
                              f"now {after}")
    if plan.unallocated > 0:
        sh.line("Credit left", _money(sh, plan.unallocated))
    sh.line("Tax", "A payment carries no tax of its own.")
    sh.book(f"{plan.bank['name']} ({plan.bank['number']} {plan.bank['name']}) +{sh.amount(plan.amount)}")
    sh.book(f"{plan.receivable['name']} ({name}) -{sh.amount(plan.amount)}")
    if plan.unallocated > 0:
        where = (f"more than {name} owes on {plan.allocations[0][0]['number']}" if inp.invoice and plan.allocations
                 else f"more than {name} owes on open invoices" if plan.allocations
                 else f"not applied to any invoice ({name} has none open on that date)")
        sh.warning(f"{_money(sh, plan.amount)} is {where}; {_money(sh, plan.unallocated)} stays as credit in "
                   f"{name}'s favour.")
    for inv in plan.skipped:
        sh.warning(f"{inv['number']} is dated {inv['posting_date']:%d %b %Y}, after this payment, so it was left out.")
    for d in plan.duplicates:
        sh.warning(f"{name} already has a {_money(sh, to_decimal(d['amount']))} payment on "
                   f"{d['posting_date']:%d %b %Y} ({d['number']}). Is this a second payment?")
    sh.title("Record a customer payment")
    token = _token(plan.customer["id"], plan.received, plan.bank["id"], plan.amount,
                   *[(i["id"], i["status"], i["outstanding"]) for i, _ in plan.allocations],
                   *[d["id"] for d in plan.duplicates])
    return Preview(sh.card(), token)


def read_set_payment(ctx: Ctx, inp) -> list[tuple[str, str]]:
    plan = _payment_plan(ctx, inp)
    return [("company", ctx.company_id), ("party", plan.customer["id"]), _period(ctx, plan.received),
            ("account", plan.bank["id"]), ("account", plan.receivable["id"]),
            *[("sales_invoice", inv["id"]) for inv, _ in plan.allocations]]


def _payment_text(ctx: Ctx, pay: dict) -> Result:
    """The result of a payment, rendered from its stored rows: the commit and the read-back say the same."""
    allocs = ctx.conn.execute(select(t.allocation.c.amount, t.allocation.c.invoice_id, t.invoice.c.number,
                                     t.invoice.c.total).join(
        t.invoice, t.invoice.c.id == t.allocation.c.invoice_id).where(
        t.allocation.c.payment_id == pay["id"]).order_by(t.invoice.c.posting_date, t.invoice.c.number)).all()

    def seq_of(payment_id):
        return select(m.posting.c.seq).where(m.posting.c.company_id == ctx.company_id,
                                             m.posting.c.voucher_type == "sales_payment",
                                             m.posting.c.voucher_id == payment_id).scalar_subquery()

    def status_then(a) -> str:
        """The invoice's status just after this payment, from the allocations of payments posted up to it, in the
        ledger's own order (its sequence, taken under the chain head: never tied, never out of order), so a later
        payment never changes this text."""
        upto = ctx.conn.execute(select(t.allocation.c.amount).join(
            t.payment, t.payment.c.id == t.allocation.c.payment_id).join(
            m.posting, (m.posting.c.voucher_type == "sales_payment") & (m.posting.c.voucher_id == t.payment.c.id)
            & (m.posting.c.company_id == t.payment.c.company_id)).where(
            t.allocation.c.invoice_id == a.invoice_id, m.posting.c.seq <= seq_of(pay["id"]))).scalars()
        left = to_decimal(a.total) - sum((to_decimal(x) for x in upto), Decimal(0))
        return "paid" if left == 0 else "partly paid"
    name, amount = s.customer_name(ctx, pay["customer_id"]), to_decimal(pay["amount"])
    sh = Shown(ctx.currency)
    received = _money(sh, amount, "amount", pay["number"])
    text = f"Recorded {pay['number']}: {received} from {name}, received {pay['posting_date']:%d %b %Y}"
    text += f" by {pay['method']}." if pay["method"] else "."
    figures = [received]
    if allocs:
        applied = [_money(sh, to_decimal(a.amount), "applied", a.number) for a in allocs]
        text += " Applied to " + "; ".join(f"{a.number} {x} (now {status_then(a)})" for a, x in zip(allocs, applied)) + "."
        figures += applied
    left = to_decimal(pay["unallocated"])
    if left > 0:
        credit = _money(sh, left, "credit", pay["number"])
        text += f" {credit} stays as credit in {name}'s favour."
        figures.append(credit)
    return Result(text, {"number": pay["number"], "amount": str(amount)}, figures, sh.amounts)


def commit_payment(ctx: Ctx, inp: RecordPayment, key: str) -> Result:
    r = s.record_payment(ctx, _payment_plan(ctx, inp), key)
    pay = ctx.conn.execute(select(t.payment).where(t.payment.c.id == r["id"])).mappings().one()
    return _payment_text(ctx, dict(pay))


def payment_by_key(ctx: Ctx, key: str) -> Result | None:
    r = ctx.conn.execute(select(t.payment).where(t.payment.c.operation_key == key)).mappings().first()
    return None if r is None else _payment_text(ctx, dict(r))


# ---------- cancel_invoice ----------

class CancelInvoice(Input):
    invoice: Annotated[str, _mark("document", what="invoice", prefix="INV", nouns=("invoice", "inv"),
                                  resolve=_invoice_number, party=_invoice_party)] = Field(
        description="The invoice number the owner named, e.g. INV-2026-00003.", max_length=30)
    date: Annotated[dt.date | None, _mark("date", presence="optional")] = Field(
        default=None, description="The reversal date, only if the owner said one (ISO).")

    def takes_no_date(self, ctx: Ctx) -> bool:
        """Cancelling a draft changes no books, so it takes no date."""
        return s.find_invoice(ctx, self.invoice)["status"] == "draft"


def _books(sh: Shown, ctx: Ctx, mirror: list) -> None:
    """Each mirrored line as the card shows it: the account by name, the customer for a receivable line, the sign."""
    for ln in mirror:
        acc = ctx.conn.execute(select(m.account).where(m.account.c.id == ln.account_id)).mappings().one()
        who = s.customer_name(ctx, ln.party_id) if ln.party_id else f"{acc['number']} {acc['name']}"
        sign, amount = ("+", ln.debit) if ln.debit > 0 else ("-", ln.credit)
        sh.book(f"{acc['name']} ({who}) {sign}{sh.amount(amount)}")


def _period_warning(ctx: Ctx, number: str, original: dict, when) -> list[str]:
    if ledger.find_period(ctx, original["posting_date"])["id"] == ledger.find_period(ctx, when)["id"]:
        return []
    return [f"{number} stays in the figures of {original['posting_date']:%B %Y}; the reversal is dated "
            f"{when:%d %b %Y}. To take it out of {original['posting_date']:%B %Y} instead, give a date in that month."]


def _cancel_plan(ctx: Ctx, inp: CancelInvoice) -> s.Reversal:
    return s.plan_cancel(ctx, s.find_invoice(ctx, inp.invoice), inp.date or ctx.today)


def preview_cancel(ctx: Ctx, inp: CancelInvoice) -> Preview:
    plan = _cancel_plan(ctx, inp)
    inv, sh = plan.document, Shown(ctx.currency)
    sh.line("Invoice", inv["number"])
    sh.line("Customer", _label(ctx, inv["customer_id"]))
    sh.line("Total", _money(sh, inv["total"]))
    sh.line("Dated", f"{inv['posting_date']:%d %b %Y}")
    sh.line("Status now", inv["status"])
    if plan.original is None:
        sh.book("Nothing in the books changes; it was a draft.")
    else:
        sh.line("Reversal date", f"{plan.reversal_date:%d %b %Y}")
        _books(sh, ctx, plan.mirror)
        sh.book(f"The original entries stay; reversing entries are added on {plan.reversal_date:%d %b %Y}.")
        for w in _period_warning(ctx, inv["number"], plan.original, plan.reversal_date):
            sh.warning(w)
    sh.title("Cancel a sales invoice")
    return Preview(sh.card(), _token(inv["id"], inv["status"], inv["outstanding"], plan.reversal_date))


def read_set_cancel(ctx: Ctx, inp) -> list[tuple[str, str]]:
    plan = _cancel_plan(ctx, inp)
    period = [] if plan.original is None else [_period(ctx, plan.reversal_date)]  # a draft posts nothing: no date
    return [("company", ctx.company_id), *period, *_invoice_rows(ctx, plan.document)]


def _reversal_posting(ctx: Ctx, voucher_type: str, voucher_id: str) -> dict | None:
    orig = ctx.conn.execute(select(m.posting.c.id).where(
        m.posting.c.company_id == ctx.company_id, m.posting.c.voucher_type == voucher_type,
        m.posting.c.voucher_id == voucher_id)).scalar()
    return None if orig is None else ctx.conn.execute(select(m.posting).where(
        m.posting.c.reverses_posting_id == orig)).mappings().first()


def _cancel_text(ctx: Ctx, inv: dict) -> Result:
    """A cancellation's result from the stored rows: the commit and the read-back say the same."""
    sh = Shown(ctx.currency)
    total = _money(sh, to_decimal(inv["total"]), "total", inv["number"])
    rev = _reversal_posting(ctx, "sales_invoice", inv["id"])
    if rev is None:
        return Result(f"Cancelled draft {inv['number']} ({total}); the books did not change.",
                      {"number": inv["number"]}, [total], sh.amounts)
    return Result(f"Cancelled {inv['number']} ({total}), reversed on {rev['posting_date']:%d %b %Y}; the original "
                  "entries are kept.", {"number": inv["number"]}, [total], sh.amounts)


def commit_cancel(ctx: Ctx, inp: CancelInvoice, key: str) -> Result:
    plan = _cancel_plan(ctx, inp)
    s.cancel_invoice(ctx, plan, key)
    inv = ctx.conn.execute(select(t.invoice).where(t.invoice.c.id == plan.document["id"])).mappings().one()
    return _cancel_text(ctx, dict(inv))


def cancel_by_key(ctx: Ctx, key: str) -> Result | None:
    inv = ctx.conn.execute(select(t.invoice).where(t.invoice.c.cancel_operation_key == key)).mappings().first()
    return None if inv is None else _cancel_text(ctx, dict(inv))


# ---------- reverse_payment ----------

class ReversePayment(Input):
    payment: Annotated[str | None, _mark("document", presence="optional", what="payment", prefix="PAY",
                                         nouns=("payment",), resolve=_payment_number, party=_payment_party)] = Field(
        default=None, max_length=30, description="The payment number, e.g. PAY-2026-00012.")
    customer: Annotated[str | None, _mark("customer", presence="optional", payer=True)] = Field(
        default=None, max_length=200, description="The customer, when the owner named no payment number.")
    date: Annotated[dt.date | None, _mark("date", presence="optional")] = Field(
        default=None, description="The reversal date, only if the owner said one (ISO).")
    reason: str | None = Field(default=None, max_length=140, description="Why, if the owner said.")


def _reverse_plan(ctx: Ctx, inp: ReversePayment) -> s.Reversal:
    if inp.payment:
        pay = s.find_payment(ctx, inp.payment)
        if inp.customer:  # a customer the owner named is checked against the payment's, never ignored
            said = find_customer(ctx, inp.customer)
            if said["id"] != pay["customer_id"]:
                whose = s.customer_name(ctx, pay["customer_id"])
                raise Refusal(f"{pay['number']} is {whose}'s payment, not {said['name']}'s. Is it {whose}'s payment "
                              "you want to reverse?", "customer_ambiguous", options=[whose])
    elif inp.customer:
        cust = find_customer(ctx, inp.customer)
        rows = [dict(r) for r in ctx.conn.execute(select(t.payment).where(
            t.payment.c.company_id == ctx.company_id, t.payment.c.customer_id == cust["id"],
            t.payment.c.status == "submitted").order_by(t.payment.c.posting_date.desc(),
                                                        t.payment.c.number.desc()).limit(10)).mappings()]
        if not rows:
            raise Refusal(f"{cust['name']} has no payments to reverse.", "no_payments")
        def paid(r) -> str:
            nums = ctx.conn.execute(select(t.invoice.c.number).join(
                t.allocation, t.allocation.c.invoice_id == t.invoice.c.id).where(
                t.allocation.c.payment_id == r["id"]).order_by(t.invoice.c.number)).scalars().all()
            return f", paid {', '.join(nums)}" if nums else ", not applied to an invoice"
        options = [s.payment_option(ctx, r, paid(r)) for r in rows]  # the question's words, not a card's
        raise Refusal(f"Which payment from {cust['name']}: {'; '.join(options)}?", "payment_ambiguous", options=options,
                      field="payment", choices=[{"label": o, "ref": {"payment": r["number"]}} for o, r in zip(options, rows)])
    else:
        raise Refusal("Which payment should be reversed?", "payment_missing")
    return s.plan_reverse_payment(ctx, pay, inp.date or ctx.today)


def preview_reverse_payment(ctx: Ctx, inp: ReversePayment) -> Preview:
    plan = _reverse_plan(ctx, inp)
    pay, sh = plan.document, Shown(ctx.currency)
    sh.line("Payment", pay["number"])
    sh.line("Customer", _label(ctx, pay["customer_id"]))
    sh.line("Amount received", _money(sh, to_decimal(pay["amount"])))
    sh.line("Dated", f"{pay['posting_date']:%d %b %Y}")
    sh.line("Reversal date", f"{plan.reversal_date:%d %b %Y}")
    if pay["method"]:
        sh.line("Method", pay["method"])
    if pay["reference"]:
        sh.line("Reference", pay["reference"])
    if inp.reason:
        sh.line("Reason", inp.reason)
    for inv, amt in plan.invoices:
        before = to_decimal(inv["outstanding"])
        sh.line("Owes again", f"{inv['number']} ({sh.amount(amt)} of {sh.amount(before + amt)} outstanding)")
    if to_decimal(pay["unallocated"]) > 0:
        sh.line("Credit removed", _money(sh, to_decimal(pay["unallocated"])))
    _books(sh, ctx, plan.mirror)
    for w in _period_warning(ctx, pay["number"], plan.original, plan.reversal_date):
        sh.warning(w)
    sh.warning("Use this when the money did not arrive or was recorded by mistake: it takes the money out of the "
               "bank account in the books.")
    sh.title("Reverse a customer payment")
    return Preview(sh.card(), _token(pay["id"], pay["status"], plan.reversal_date,
                                     *[(i["id"], i["outstanding"]) for i, _ in plan.invoices]))


def read_set_reverse_payment(ctx: Ctx, inp) -> list[tuple[str, str]]:
    plan = _reverse_plan(ctx, inp)
    pay = plan.document
    return [("company", ctx.company_id), ("party", pay["customer_id"]), _period(ctx, plan.reversal_date),
            ("sales_payment", pay["id"]), *[("sales_invoice", inv["id"]) for inv, _ in plan.invoices]]


def _reverse_text(ctx: Ctx, pay: dict) -> Result:
    sh = Shown(ctx.currency)
    amount = _money(sh, to_decimal(pay["amount"]), "amount", pay["number"])
    rev = _reversal_posting(ctx, "sales_payment", pay["id"])
    allocs = ctx.conn.execute(select(t.allocation.c.amount, t.invoice.c.number).join(
        t.invoice, t.invoice.c.id == t.allocation.c.invoice_id).where(
        t.allocation.c.payment_id == pay["id"]).order_by(t.invoice.c.posting_date, t.invoice.c.number)).all()
    text = f"Reversed {pay['number']} ({amount}) on {rev['posting_date']:%d %b %Y}"
    figures = [amount]
    if allocs:
        owes = [_money(sh, to_decimal(a.amount), "reopened", a.number) for a in allocs]
        text += ": " + "; ".join(f"{a.number} owes {x} again" for a, x in zip(allocs, owes))
        figures += owes
    if to_decimal(pay["unallocated"]) > 0:
        credit = _money(sh, to_decimal(pay["unallocated"]), "credit_removed", pay["number"])
        text += f"; the {credit} credit is removed"
        figures.append(credit)
    return Result(text + ".", {"number": pay["number"]}, figures, sh.amounts)


def commit_reverse_payment(ctx: Ctx, inp: ReversePayment, key: str) -> Result:
    plan = _reverse_plan(ctx, inp)
    s.reverse_payment(ctx, plan, key, inp.reason)
    pay = ctx.conn.execute(select(t.payment).where(t.payment.c.id == plan.document["id"])).mappings().one()
    return _reverse_text(ctx, dict(pay))


def reverse_by_key(ctx: Ctx, key: str) -> Result | None:
    pay = ctx.conn.execute(select(t.payment).where(t.payment.c.cancel_operation_key == key)).mappings().first()
    return None if pay is None else _reverse_text(ctx, dict(pay))


# ---------- create_sales_order ----------

def _order_number(ctx: Ctx, said: str) -> str:
    return ss.find_order(ctx, said)["number"]


def _order_party(ctx: Ctx, number: str) -> str:
    return s.customer_name(ctx, ss.find_order(ctx, number)["customer_id"])


class CreateSalesOrder(Input):
    customer: Annotated[str, _mark("customer")] = Field(description="Customer name exactly as the owner said it.",
                                                        max_length=200)
    items: Annotated[list[InvoiceItem], _mark("lines")] = Field(min_length=1, max_length=20)
    delivery_date: Annotated[dt.date, _mark("date")] = Field(description="When the customer wants it (ISO).")


def _order_plan(ctx: Ctx, inp: CreateSalesOrder):
    cust = find_customer(ctx, inp.customer)
    lines = [(find_item(ctx, li.item), li.quantity) for li in inp.items]
    ss.guard(ctx, [item for item, _ in lines])
    priced, total = s.price_lines(ctx, lines)
    return cust, priced, total, [ss.available(ctx, p.item) if ss.is_stock(p.item) else None for p in priced]


def preview_sales_order(ctx: Ctx, inp: CreateSalesOrder) -> Preview:
    cust, priced, total, avail = _order_plan(ctx, inp)
    sh = Shown(ctx.currency)
    sh.line("Customer", customer_label(ctx, cust))
    for p, a in zip(priced, avail):
        sh.line("Item", f"{fmt_qty(p.quantity)} x {p.item['name']} @ {sh.unit_price(p.rate)} = {sh.amount(p.amount)}")
        if a is not None:  # the shortfall is stated before approval; the order keeps the full quantity
            gap = p.quantity - a
            sh.line("Stock", f"{fmt_qty(a)} available, {fmt_qty(gap)} short" if gap > 0 else f"{fmt_qty(a)} available")
    sh.line("Total", _money(sh, total))
    sh.line("Delivery date", f"{inp.delivery_date:%d %b %Y}")
    sh.book("Nothing posts to the books: an order moves no stock and reserves none.")
    sh.title("Create a sales order")
    return Preview(sh.card(), _token(cust["id"], inp.delivery_date, *[(p.item["id"], p.rate, p.quantity, a)
                                                                      for p, a in zip(priced, avail)]))


def read_set_sales_order(ctx: Ctx, inp: CreateSalesOrder) -> list[tuple[str, str]]:
    cust, priced, _, _ = _order_plan(ctx, inp)
    return [("company", ctx.company_id), ("party", cust["id"]), _period(ctx, ctx.today),
            *[("item", p.item["id"]) for p in priced]]


def _order_result(ctx: Ctx, order: dict) -> Result:
    sh = Shown(ctx.currency)
    shown = _money(sh, to_decimal(order["total"]), "total", order["number"])
    return Result(f"Saved sales order {order['number']} for {s.customer_name(ctx, order['customer_id'])}: {shown}, "
                  f"for delivery by {order['delivery_date']:%d %b %Y}.", {"number": order["number"],
                                                                          "total": str(order["total"])},
                  [shown], sh.amounts)


def commit_sales_order(ctx: Ctx, inp: CreateSalesOrder, key: str) -> Result:
    cust, priced, total, _ = _order_plan(ctx, inp)
    r = ss.create_order(ctx, cust, priced, total, inp.delivery_date, key)
    return _order_result(ctx, dict(ctx.conn.execute(select(t.order).where(t.order.c.id == r["id"])).mappings().one()))


def sales_order_by_key(ctx: Ctx, key: str) -> Result | None:
    r = ctx.conn.execute(select(t.order).where(t.order.c.operation_key == key)).mappings().first()
    return None if r is None else _order_result(ctx, dict(r))


# ---------- deliver_order ----------

class DeliveryItem(Input):
    item: Annotated[str, _mark("item")] = Field(description="Item name exactly as the owner said it.", max_length=200)
    quantity: Annotated[Number, _mark("quantity")] = Field(description="How many, exactly as the owner said.")
    warehouse: Annotated[str | None, _mark("warehouse", resolve=ss.named_warehouse)] = Field(
        default=None, max_length=140, description="The warehouse it ships from, in the owner's words.")


class DeliverOrder(Input):
    order: Annotated[str, _mark("document", what="order", prefix="SO", nouns=("order", "sales order"),
                                resolve=_order_number, party=_order_party)] = Field(
        description="The sales order's number as the owner named it, e.g. SO-2026-00002.", max_length=30)
    items: Annotated[list[DeliveryItem] | None, _mark("lines", presence="optional")] = Field(
        default=None, max_length=20, description="Only if the owner named what to deliver; otherwise the rest.")
    date: Annotated[dt.date | None, _mark("date", presence="optional")] = Field(
        default=None, description="The delivery date, only if the owner said one (ISO).")


def _delivery_plan(ctx: Ctx, inp: DeliverOrder) -> ss.DeliveryPlan:
    order = ss.find_order(ctx, inp.order)
    asked = None if not inp.items else [(find_item(ctx, li.item), to_decimal(li.quantity), li.warehouse)
                                        for li in inp.items]
    return ss.plan_delivery(ctx, order, asked, inp.date or ctx.today)


def preview_delivery(ctx: Ctx, inp: DeliverOrder) -> Preview:
    plan = _delivery_plan(ctx, inp)
    sh = Shown(ctx.currency)
    sh.line("Order", plan.order["number"])
    sh.line("Customer", _label(ctx, plan.order["customer_id"]))
    by_part = {i.key: i for i in plan.stock.issued}
    for n, (oline, item, qty) in enumerate(plan.lines):
        sh.line("Deliver", f"{fmt_qty(qty)} x {item['name']} (of {fmt_qty(to_decimal(oline['quantity']))} ordered)")
        if n in by_part:
            _stock_lines(sh, ss.StockPlan([by_part[n]]))
    sh.line("Date", f"{plan.when:%d %b %Y}")
    if plan.stock.lines:  # no card for a posting the books would refuse
        ledger.check_post(ctx, plan.when, "sales_delivery", None, plan.stock.lines)
    _stock_books(sh, ctx, plan.stock.lines)
    if not plan.stock.lines:
        sh.book("Nothing posts to the books: the goods delivered cost nothing.")
    sh.title("Deliver a sales order")
    return Preview(sh.card(), _token(plan.order["id"], plan.order["status"], plan.when,
                                     *[(ol["id"], q) for ol, _, q in plan.lines],
                                     *[(i.key, i.source.warehouse, i.cost) for i in plan.stock.issued]))


def read_set_delivery(ctx: Ctx, inp: DeliverOrder) -> list[tuple[str, str]]:
    plan = _delivery_plan(ctx, inp)
    return [("company", ctx.company_id), _period(ctx, plan.when), ("sales_order", plan.order["id"]),
            *[("sales_order_line", ol["id"]) for ol, _, _ in plan.lines], *plan.stock.read_set]


def _delivery_result(ctx: Ctx, delivery: dict) -> Result:
    """A delivery's result from its stored rows: its goods at the cost they left at, and the order's status."""
    order = ctx.conn.execute(select(t.order).where(t.order.c.id == delivery["order_id"])).mappings().one()
    cost = sum((to_decimal(c) for c in ctx.conn.execute(select(t.delivery_line.c.cost).where(
        t.delivery_line.c.delivery_id == delivery["id"])).scalars()), Decimal(0))
    sh = Shown(ctx.currency)
    shown = _money(sh, cost, "value", delivery["number"])
    status = order["status"].replace("_", " ")
    return Result(f"Delivered {delivery['number']} against {order['number']} on {delivery['posting_date']:%d %b %Y}: "
                  f"goods at a cost of {shown}; the order is now {status}.",
                  {"number": delivery["number"], "order": order["number"], "status": order["status"]}, [shown],
                  sh.amounts)


def commit_delivery(ctx: Ctx, inp: DeliverOrder, key: str) -> Result:
    r = ss.deliver(ctx, _delivery_plan(ctx, inp), key)
    return _delivery_result(ctx, dict(ctx.conn.execute(select(t.delivery).where(
        t.delivery.c.id == r["id"])).mappings().one()))


def delivery_by_key(ctx: Ctx, key: str) -> Result | None:
    r = ctx.conn.execute(select(t.delivery).where(t.delivery.c.operation_key == key)).mappings().first()
    return None if r is None else _delivery_result(ctx, dict(r))


# ---------- create_credit_note ----------

class CreditItem(Input):
    item: Annotated[str, _mark("item")] = Field(description="Item name exactly as the owner said it.", max_length=200)
    quantity: Annotated[Number, _mark("quantity")] = Field(description="How many come back, as the owner said.")
    line: Annotated[int | None, _mark("invoice_line")] = Field(
        default=None, ge=1, le=99, description="The invoice line's number, only when the owner named one.")


class CreateCreditNote(Input):
    invoice: Annotated[str, _mark("document", what="invoice", prefix="INV", nouns=("invoice", "inv"),
                                  resolve=_invoice_number, party=_invoice_party)] = Field(
        description="The invoice it credits, e.g. INV-2026-00003.", max_length=30)
    items: Annotated[list[CreditItem] | None, _mark("lines", presence="optional")] = Field(
        default=None, max_length=20, description="What comes back, unless the owner credits everything.")
    everything: Annotated[bool, _mark("flag", words="everything", when=True)] = Field(
        default=False, description="true only if the owner credits the whole invoice.")
    date: Annotated[dt.date | None, _mark("date", presence="optional")] = Field(
        default=None, description="The credit note's date, only if the owner said one (ISO).")

    @model_validator(mode="after")
    def _items_or_everything(self):
        if bool(self.items) == self.everything:
            raise ValueError("a credit note names what comes back, or credits everything, not both")
        return self


def _credit_plan(ctx: Ctx, inp: CreateCreditNote) -> ss.CreditPlan:
    inv = s.find_invoice(ctx, inp.invoice)
    asked = None if inp.everything else [(find_item(ctx, li.item), to_decimal(li.quantity), li.line)
                                         for li in inp.items]
    return ss.plan_credit(ctx, inv, asked, inp.date or ctx.today)


def preview_credit_note(ctx: Ctx, inp: CreateCreditNote) -> Preview:
    plan = _credit_plan(ctx, inp)
    inv, sh = plan.invoice, Shown(ctx.currency)
    status = inv["status"].replace("_", " ")
    owed = sh.amount(to_decimal(inv["outstanding"]))  # each figure recorded in the order the line reads it
    paid = f"{sh.amount(plan.paid)} paid" if plan.paid else "nothing paid"
    sh.line("Invoice", f"{inv['number']}: {status}, {owed} outstanding, {paid}")
    sh.line("Customer", _label(ctx, inv["customer_id"]))
    for cl in plan.lines:
        sh.line("Credit", f"line {cl.number}: {fmt_qty(cl.qty)} x {cl.item['name']} @ "
                          f"{sh.unit_price(cl.invoice_line['rate'])} = {sh.amount(cl.amount)}")
        for p in cl.parts:  # where each quantity goes back against, and at what cost: inside the digest
            sh.line("Back into stock", f"{fmt_qty(p.qty)} into {p.warehouse_name} against {p.against} at "
                                       f"{sh.amount(p.cost)}")
    sh.line("Total credit", _money(sh, plan.total))
    sh.line("Outstanding after", _money(sh, plan.outstanding))
    sh.line("Date", f"{plan.when:%d %b %Y}")
    sh.book(f"{plan.returns_account['name']} +{sh.amount(plan.total)}")
    sh.book(f"Accounts Receivable ({plan.customer['name']}) -{sh.amount(plan.total)}")
    _stock_books(sh, ctx, plan.stock_lines)
    sh.book("The invoice is credited, not paid: no payment or refund is recorded.")
    sh.title("Create a credit note")
    return Preview(sh.card(), _token(inv["id"], inv["status"], inv["outstanding"], plan.when, plan.total,
                                     *[(c.invoice_line["id"], c.qty, c.amount,
                                        *[(p.voucher_id, p.stock_line, p.qty, p.cost) for p in c.parts])
                                       for c in plan.lines]))


def read_set_credit_note(ctx: Ctx, inp: CreateCreditNote) -> list[tuple[str, str]]:
    plan = _credit_plan(ctx, inp)
    return [("company", ctx.company_id), _period(ctx, plan.when), *_invoice_rows(ctx, plan.invoice),
            *plan.read_set]


def _credit_result(ctx: Ctx, cn: dict) -> Result:
    inv = ctx.conn.execute(select(t.invoice).where(t.invoice.c.id == cn["invoice_id"])).mappings().one()
    sh = Shown(ctx.currency)
    total = _money(sh, to_decimal(cn["total"]), "total", cn["number"])
    left = _money(sh, to_decimal(inv["outstanding"]), "outstanding", inv["number"])
    return Result(f"Saved credit note {cn['number']} against {inv['number']}: {total}; {inv['number']} now has "
                  f"{left} outstanding.", {"number": cn["number"], "total": str(cn["total"])}, [total, left],
                  sh.amounts)


def commit_credit_note(ctx: Ctx, inp: CreateCreditNote, key: str) -> Result:
    r = ss.credit(ctx, _credit_plan(ctx, inp), key)
    return _credit_result(ctx, dict(ctx.conn.execute(select(t.credit_note).where(
        t.credit_note.c.id == r["id"])).mappings().one()))


def credit_note_by_key(ctx: Ctx, key: str) -> Result | None:
    r = ctx.conn.execute(select(t.credit_note).where(t.credit_note.c.operation_key == key)).mappings().first()
    return None if r is None else _credit_result(ctx, dict(r))


# ---------- list_invoices ----------

class ListInvoices(Input):
    customer: str | None = Field(default=None, max_length=200, description="Customer name, if the owner named one.")
    status: Literal["open", "paid", "draft", "cancelled", "all"] = Field(
        default="open", description="open = submitted and not fully paid.")


STATUS_SETS = {"open": ("submitted", "partly_paid"), "paid": ("paid",), "draft": ("draft",),
               "cancelled": ("cancelled",), "all": ("draft", "submitted", "partly_paid", "paid", "cancelled")}


def list_invoices(ctx: Ctx, inp: ListInvoices) -> Result:
    q = select(t.invoice, m.party.c.name.label("customer")).join(m.party, m.party.c.id == t.invoice.c.customer_id) \
        .where(t.invoice.c.company_id == ctx.company_id, t.invoice.c.status.in_(STATUS_SETS[inp.status])) \
        .order_by(t.invoice.c.posting_date, t.invoice.c.number)
    who = ""
    if inp.customer:
        cust = find_customer(ctx, inp.customer)
        q = q.where(t.invoice.c.customer_id == cust["id"])
        who = f" for {cust['name']}"
    rows = ctx.conn.execute(q.limit(51)).mappings().all()
    label = {"open": "Open invoices", "paid": "Paid invoices", "draft": "Draft invoices",
             "cancelled": "Cancelled invoices", "all": "All invoices"}[inp.status]
    if not rows:
        return Result(f"{label}{who}: none.", {"count": 0})
    shown = rows[:50]
    counted = [r for r in shown if r["status"] not in ("draft", "cancelled")]
    total = sum((r["total"] for r in counted), Decimal("0"))
    outstanding = sum((r["outstanding"] for r in counted), Decimal("0"))
    left_out = len(shown) - len(counted)
    sh, figures = Shown(ctx.currency), []
    if counted:  # a figure the text does not show is neither an amount nor allowed in the model's words
        figures = [_money(sh, total, "total"), _money(sh, outstanding, "outstanding")]
    lines = [f"{label}{who}: {len(shown)}{' (first 50 shown; ask for one customer to narrow it)' if len(rows) > 50 else ''}. "
             + (f"Submitted invoices total {figures[0]}, still to collect {figures[1]}"
                if counted else "None of them is submitted, so nothing is owed on them")
             + (f" ({left_out} draft or cancelled invoice{'s' if left_out != 1 else ''} not counted)." if left_out else ".")]
    for r in shown:
        lines.append(f"  {r['number']}  {r['posting_date']:%d %b %Y}  {r['customer']:<18}  "
                     f"{sh.amount(r['total'], 'invoice_total', r['number']):>12}  "
                     f"outstanding {sh.amount(r['outstanding'], 'invoice_outstanding', r['number']):>10}  {r['status']}")
    return Result("\n".join(lines), {"count": len(shown)}, figures, sh.amounts)


# ---------- metrics ----------

def _submitted_in(ctx: Ctx, period: Period, customer: str | None, city: str | None):
    q = select(t.invoice.c.total, m.party.c.name).join(m.party, m.party.c.id == t.invoice.c.customer_id).where(
        t.invoice.c.company_id == ctx.company_id,
        t.invoice.c.status.in_(("submitted", "partly_paid", "paid")),
        t.invoice.c.posting_date >= period.start, t.invoice.c.posting_date <= period.end)
    if customer:
        q = q.where(t.invoice.c.customer_id == find_customer(ctx, customer)["id"])
    if city:
        q = q.where(m.party.c.city_key == text_key(city))
    return ctx.conn.execute(q).all()


BASIS = "submitted invoices dated in the period, before tax; drafts and cancelled invoices excluded"


def sales_metric(ctx, period, customer, city) -> MetricResult:
    rows = _submitted_in(ctx, period, customer, city)
    if not rows:
        return MetricResult("sales", "Sales", period, BASIS, None)
    return MetricResult("sales", "Sales", period, BASIS, sum((r.total for r in rows), Decimal("0")),
                        row_count=len(rows), method=f"sum of {len(rows)} invoice{'s' if len(rows) != 1 else ''}")


def top_customers_metric(ctx, period, customer, city) -> MetricResult:
    rows = _submitted_in(ctx, period, customer, city)
    if not rows:
        return MetricResult("top_customers", "Top customers by sales", period, BASIS, None)
    by: dict[str, Decimal] = {}
    for r in rows:
        by[r.name] = by.get(r.name, Decimal("0")) + r.total
    ranked = sorted(by.items(), key=lambda kv: (-kv[1], kv[0]))
    return MetricResult("top_customers", "Top customers by sales", period, BASIS,
                        sum(by.values(), Decimal("0")), rows=[MetricRow(n, v) for n, v in ranked[:5]],
                        row_count=len(rows), method="invoice totals per customer, highest first; headline is the total")


def invoice_count_metric(ctx, period, customer, city) -> MetricResult:
    rows = _submitted_in(ctx, period, customer, city)
    if not rows:
        return MetricResult("invoice_count", "Invoices", period, BASIS, None, unit="count")
    return MetricResult("invoice_count", "Invoices", period, BASIS, Decimal(len(rows)), unit="count",
                        row_count=len(rows))


MANIFEST = Manifest(
    name="sales", version="0.0.2", publisher=PUBLISHER, distribution="erpclaw-sales", licence="GPL-3.0-only",
    source=Source(repository="workspace", commit="workspace"), engine=">=0.0.2", after_core_revision="0013",
    depends={}, conflicts=[],
    owned_tables=["sales_invoice", "sales_invoice_line", "sales_payment", "sales_payment_allocation",
                  *[tbl.name for tbl in t.STOCK_TABLES]],
    contracts={"create_invoice": "write", "submit_invoice": "write", "record_payment": "write",
               "cancel_invoice": "write", "reverse_payment": "write", "create_sales_order": "write",
               "deliver_order": "write", "create_credit_note": "write",
               "list_invoices": "read"},
    calls=[], events=Events(emits=[], consumes=[]), slots={},
    voucher_types=["sales_invoice", "sales_payment", "sales_delivery", "sales_credit_note"],
    capabilities=Capabilities(network=[], files=[]),
    metrics=["sales", "top_customers", "invoice_count"],
    # optional: sales loads, and sells services, without the stock plugin (a stock item then gets the planned reply)
    uses=[f"stock.{name}?" for name in ("stock_on_hand", "choose_warehouse", "preview_issue", "issue", "preview_return",
                                         "return_in", "preview_reverse", "reverse_movements")],
)

PLUGIN = Plugin(
    manifest=MANIFEST, kind="business-module",
    contracts=[
        WriteContract("create_invoice", "sales", "write",
                 "Invoice a customer for items. The owner approves a card before anything is saved; "
                 "do not ask for confirmation yourself.", CreateInvoice,
                 preview=preview_invoice, commit=commit_invoice, find_by_operation_key=invoice_by_key,
                 read_set=read_set_invoice, amounts=("total",), schema_hook=_offered_invoice,
                 synonyms=("invoice", "bill", "bill a customer", "charge", "send an invoice", "raise an invoice", "draft invoice"),
                 plain="draft invoices", planned_name="invoicing", moves_money=True),
        WriteContract("submit_invoice", "sales", "write",
                 "Submit a draft invoice by its number, so it posts to the books. The owner approves a card first.",
                 SubmitInvoice, preview=preview_submit, commit=commit_submit, find_by_operation_key=submit_by_key,
                 read_set=read_set_submit, amounts=("total",),
                 synonyms=("submit", "submit invoice", "finalise invoice", "finalize invoice", "post invoice",
                           "issue draft"),
                 plain="submit a draft invoice", planned_name="submitting a draft invoice", moves_money=True),
        WriteContract("record_payment", "sales", "write",
                 "Record money received from a customer. The owner approves a card before anything is saved.",
                 RecordPayment, preview=preview_payment, commit=commit_payment, find_by_operation_key=payment_by_key,
                 read_set=read_set_payment, amounts=("amount", "applied", "credit"),
                 synonyms=("payment", "paid", "received", "receipt", "customer paid", "money in"),
                 plain="record payments", planned_name="recording a payment", planned=PLANNED, moves_money=True),
        WriteContract("cancel_invoice", "sales", "write",
                 "Cancel an invoice by its number. The owner approves a card first.", CancelInvoice,
                 preview=preview_cancel, commit=commit_cancel, find_by_operation_key=cancel_by_key,
                 read_set=read_set_cancel, amounts=("total",),
                 synonyms=("cancel", "void", "delete invoice", "undo invoice"),
                 plain="cancel an invoice", planned_name="cancelling an invoice", planned=PLANNED,
                 moves_money=True, no_date_reason={"en": "cancelling a draft changes no books."}),
        WriteContract("reverse_payment", "sales", "write",
                 "Reverse a customer payment that did not arrive or was recorded by mistake. The owner approves a "
                 "card first.", ReversePayment,
                 preview=preview_reverse_payment, commit=commit_reverse_payment, find_by_operation_key=reverse_by_key,
                 read_set=read_set_reverse_payment, amounts=("amount", "reopened", "credit_removed"),
                 synonyms=("reverse payment", "cancel payment", "undo payment", "bounced", "recorded twice"),
                 plain="reverse a payment", planned_name="reversing a payment", planned=PLANNED, moves_money=True),
        WriteContract("create_sales_order", "sales", "write",
                 "Take a customer's sales order for items, delivered later. The owner approves a card first.",
                 CreateSalesOrder, preview=preview_sales_order, commit=commit_sales_order,
                 find_by_operation_key=sales_order_by_key, read_set=read_set_sales_order, amounts=("total",),
                 synonyms=("sales order", "customer order", "book a sales order"),
                 plain="take sales orders", planned_name="sales orders", planned=PLANNED, moves_money=True),
        WriteContract("deliver_order", "sales", "write",
                 "Deliver a sales order, in full or in part, from stock. The owner approves a card first.",
                 DeliverOrder, preview=preview_delivery, commit=commit_delivery, find_by_operation_key=delivery_by_key,
                 read_set=read_set_delivery, amounts=("value",), synonyms=("deliver an order", "ship an order", "dispatch an order", "delivery note"),
                 plain="deliver sales orders", planned_name="delivering an order", planned=PLANNED, moves_money=True),
        WriteContract("create_credit_note", "sales", "write",
                 "Credit an invoice for goods returned or billed in error. The owner approves a card first.",
                 CreateCreditNote, preview=preview_credit_note, commit=commit_credit_note,
                 find_by_operation_key=credit_note_by_key, read_set=read_set_credit_note,
                 amounts=("total", "outstanding"),
                 synonyms=("credit note", "credit an invoice", "returned goods", "sales return"),
                 plain="issue credit notes", planned_name="credit notes", planned=PLANNED, moves_money=True),
        Contract("list_invoices", "sales", "read",
                 "List invoices with their numbers, totals and what is still to collect.", ListInvoices,
                 run=list_invoices, amounts=("total", "outstanding", "invoice_total", "invoice_outstanding"),
                 synonyms=("invoices", "list invoices", "show invoices", "open invoices", "unpaid"),
                 plain="list invoices", planned_name="the invoice list", planned=PLANNED),
    ],
    metrics=[
        Metric("sales", "Sales", "invoiced sales in a period (submitted invoices, before tax)", sales_metric,
               synonyms=("sales", "revenue", "turnover", "how much did we sell", "income"), planned=PLANNED),
        Metric("top_customers", "Top customers by sales", "customers ranked by invoiced sales in a period",
               top_customers_metric, synonyms=("top customers", "best customers", "biggest customers"), planned=PLANNED),
        Metric("invoice_count", "Invoices", "how many invoices were issued in a period", invoice_count_metric,
               synonyms=("how many invoices", "number of invoices", "invoice count"), planned=PLANNED),
    ],
)
