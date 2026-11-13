"""The sales documents that move stock: an invoice's stock lines, sales orders and their deliveries, credit notes.

Stock is reached only through the stock plugin's services (`call_service`), and only when the stock plugin is
enabled in these books; sales never reads a stock table. A stock item named while it is not enabled gets the planned
stock reply, and nothing is written. The stock plugin computes every cost: an issue's at its valuation, a return's at
the cost the goods left at. The document's single posting carries the lines it returned, and the stock plugin
compares them before the commit ends.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from erpclaw_core import ledger
from erpclaw_core import models as m
from erpclaw_core.common import Ctx, Refusal, audit, new_id, now
from erpclaw_core.masters import _norm
from erpclaw_core.money import fmt_qty, fmt_rate, quantize, to_decimal
from erpclaw_core.numbering import next_number
from erpclaw_core.plugins import call_service, enabled_plugins

from . import tables as t
from .service import AR, SALES, invoice_lines, live_allocations

ZERO = Decimal(0)
PLANNED_STOCK = "Selling from stock isn't available yet."


# ---------------------------------------------------------------- the guard and the stock services

def planned_stock() -> Refusal:
    return Refusal(PLANNED_STOCK, "planned", capability="planned.stock")


def stock_on(ctx: Ctx) -> bool:
    return "stock" in enabled_plugins(ctx.conn)


def is_stock(item: dict) -> bool:
    return item.get("kind") == "stock"


def guard(ctx: Ctx, items) -> None:
    """A stock item takes the stock path only while the stock plugin is enabled here; otherwise the planned reply."""
    if any(is_stock(i) for i in items) and not stock_on(ctx):
        raise planned_stock()


def stock(ctx: Ctx, service: str, **inp) -> dict:
    """One stock service, as the sales plugin; a stock plugin that went away meanwhile is the planned reply."""
    try:
        return call_service(ctx, "sales", f"stock.{service}", inp)
    except Refusal as r:
        if r.code == "plugin_not_enabled":
            raise planned_stock() from None
        raise


def lines_of(out: list[dict]) -> list[ledger.Line]:
    """The ledger lines a stock service returned, as the document's posting carries them."""
    return [ledger.Line(ln["account"], debit=to_decimal(ln["debit"]), credit=to_decimal(ln["credit"])) for ln in out]


def summed(lines: list[ledger.Line]) -> list[ledger.Line]:
    """Lines summed per account, debit or credit by the sign of the net: one line per account, none for a zero."""
    net: dict[str, Decimal] = {}
    for ln in lines:
        net[ln.account_id] = net.get(ln.account_id, ZERO) + ln.debit - ln.credit
    return [ledger.Line(a, debit=v if v > 0 else ZERO, credit=-v if v < 0 else ZERO) for a, v in net.items() if v]


# ---------------------------------------------------------------- where a stock line is taken from

@dataclass
class Source:
    warehouse: str
    name: str
    held: Decimal


def holdings(ctx: Ctx, item: dict) -> list[dict]:
    """Every warehouse of the company with what it holds of the item, in the order they were made."""
    return stock(ctx, "stock_on_hand", item=item["id"])["warehouses"]


def warehouse_choices(whs: list[dict]) -> tuple[list[str], list[dict]]:
    labels = [f"{w['name']} ({fmt_qty(to_decimal(w['qty']))} on hand)" for w in whs]
    return labels, [{"label": lb, "ref": w["name"]} for lb, w in zip(labels, whs)]


def short(item: dict, whs: list[dict], qty: Decimal, needs: str) -> Refusal:
    """Short stock, with what each warehouse holds: nothing moves between warehouses to cover it."""
    held = [w for w in whs if to_decimal(w["qty"]) > 0]
    where = ", ".join(f"{fmt_qty(to_decimal(w['qty']))} on hand in {w['name']}" for w in held) or "none on hand"
    if held and len(held) < len(whs):
        where += ", 0 elsewhere"
    return Refusal(f"{item['name']}: {where}; {needs} needs {fmt_qty(qty)}.", "insufficient_stock")


def named_warehouse(ctx: Ctx, item: dict, said: str) -> dict:
    """The warehouse the owner named, by name among the item's warehouses; another name is the warehouse question."""
    whs = holdings(ctx, item)
    hit = [w for w in whs if _norm(w["name"]) == _norm(said)]
    if hit:
        return hit[0]
    labels, choices = warehouse_choices(whs)
    raise Refusal(f"There is no warehouse called '{said}'. {item['name']} can come from: {'; '.join(labels)}. "
                  "Which one?", "warehouse_unknown", options=labels, field="warehouse", choices=choices)


def source(ctx: Ctx, item: dict, qty: Decimal, *, said: str | None = None, warehouse_id: str | None = None,
           needs: str = "the invoice") -> Source:
    """A stock line's warehouse: the one named (by the owner's words, or as stored on a draft), else the stock
    plugin's choice, which may ask which one; short stock is refused with each warehouse's figure."""
    whs = holdings(ctx, item)
    if warehouse_id is not None:
        w = next((x for x in whs if x["warehouse"] == warehouse_id), None)
        if w is None:
            raise Refusal(f"The warehouse named for {item['name']} no longer exists.", "warehouse_unknown")
    elif said:
        w = named_warehouse(ctx, item, said)
    else:
        out = stock(ctx, "choose_warehouse", item=item["id"], qty=fmt_qty(qty))
        if "chosen" not in out:
            raise short(item, whs, qty, needs)
        w = next(x for x in whs if x["warehouse"] == out["chosen"]["warehouse"])
    if to_decimal(w["qty"]) < qty:
        raise short(item, whs, qty, needs)
    return Source(w["warehouse"], w["name"], to_decimal(w["qty"]))


# ---------------------------------------------------------------- issues

@dataclass
class Issued:
    """One stock line of a document, as previewed or issued: where from, and at what cost."""
    key: object  # the document line it belongs to
    item: dict
    qty: Decimal
    source: Source
    cost: Decimal
    note: str | None = None
    entry_id: str | None = None
    stock_line: int | None = None


@dataclass
class StockPlan:
    issued: list[Issued] = field(default_factory=list)
    lines: list[ledger.Line] = field(default_factory=list)  # the stock lines the posting carries
    read_set: list[tuple[str, str]] = field(default_factory=list)


def _wanted(issued: list[Issued]) -> list[dict]:
    return [{"item": i.item["id"], "warehouse": i.source.warehouse, "qty": fmt_qty(i.qty)} for i in issued]


def preview_issue(ctx: Ctx, voucher_type: str, posting_date: date, wanted: list[Issued]) -> StockPlan:
    if not wanted:
        return StockPlan()
    out = stock(ctx, "preview_issue", voucher_type=voucher_type, voucher="preview",
                posting_date=posting_date.isoformat(), lines=_wanted(wanted))
    for i, c in zip(wanted, out["lines"]):
        i.cost, i.note = to_decimal(c["cost"]), c.get("card_note")
    return StockPlan(wanted, lines_of(out["ledger_lines"]), [tuple(r) for r in out["read_set"]])


def issue(ctx: Ctx, voucher_type: str, voucher_id: str, posting_date: date, wanted: list[Issued]) -> StockPlan:
    """Issue the lines on the voucher, inside the approved commit: a voucher's stock lines are numbered from 1."""
    if not wanted:
        return StockPlan()
    out = stock(ctx, "issue", voucher_type=voucher_type, voucher=voucher_id, posting_date=posting_date.isoformat(),
                lines=_wanted(wanted))
    for n, (i, c, eid) in enumerate(zip(wanted, out["lines"], out["entry_ids"]), 1):
        i.cost, i.note, i.entry_id, i.stock_line = to_decimal(c["cost"]), c.get("card_note"), eid, n
    return StockPlan(wanted, lines_of(out["ledger_lines"]))


# ---------------------------------------------------------------- an invoice's stock lines

def items_by_id(ctx: Ctx, ids) -> dict[str, dict]:
    return {r["id"]: dict(r) for r in ctx.conn.execute(select(m.item).where(
        m.item.c.company_id == ctx.company_id, m.item.c.id.in_(list(ids)))).mappings()}


def covered_lines(ctx: Ctx, line_ids: list[str]) -> set[str]:
    """The invoice lines made from an order's deliveries: their stock left with the delivery."""
    if not line_ids:
        return set()
    return set(ctx.conn.execute(select(t.invoice_delivery.c.invoice_line_id).where(
        t.invoice_delivery.c.company_id == ctx.company_id,
        t.invoice_delivery.c.invoice_line_id.in_(line_ids))).scalars())


def draft_issues(ctx: Ctx, lines: list[dict]) -> list[Issued]:
    """A draft's stock lines at its submission, each from its named warehouse or the one chosen now. A line written
    while its item was a service is refused with the planned reply: it is never submitted without its movement."""
    items = items_by_id(ctx, {ln["item_id"] for ln in lines})
    covered = covered_lines(ctx, [ln["id"] for ln in lines])
    stocked = [ln for ln in lines if is_stock(items[ln["item_id"]]) and ln["id"] not in covered]
    if not stocked:
        return []
    if any(ln["item_kind"] != "stock" for ln in stocked) or not stock_on(ctx):
        raise planned_stock()
    return [Issued(ln["id"], items[ln["item_id"]], to_decimal(ln["quantity"]),
                   source(ctx, items[ln["item_id"]], to_decimal(ln["quantity"]),
                          warehouse_id=ln["named_warehouse_id"]), ZERO) for ln in stocked]


def record_issues(ctx: Ctx, plan: StockPlan) -> None:
    for i in plan.issued:
        ctx.conn.execute(t.invoice_issue.insert().values(
            id=new_id(), company_id=ctx.company_id, invoice_line_id=i.key, currency=ctx.currency,
            warehouse_id=i.source.warehouse, quantity=i.qty, cost=quantize(i.cost, ctx.currency),
            stock_entry_id=i.entry_id, stock_line=i.stock_line))


def preview_invoice_reversal(ctx: Ctx, invoice_id: str, when: date) -> dict:
    """Only this invoice's own issues select the stock branch; deliveries keep their stock."""
    line_ids = [ln["id"] for ln in invoice_lines(ctx, invoice_id)]
    issues = ctx.conn.execute(select(t.invoice_issue).where(
        t.invoice_issue.c.company_id == ctx.company_id,
        t.invoice_issue.c.invoice_line_id.in_(line_ids))).mappings().all()
    deliveries = ctx.conn.execute(select(t.invoice_delivery.c.id, t.delivery_line.c.stock_entry_id).join(
        t.delivery_line, t.delivery_line.c.id == t.invoice_delivery.c.delivery_line_id).where(
        t.invoice_delivery.c.company_id == ctx.company_id,
        t.invoice_delivery.c.invoice_line_id.in_(line_ids))).mappings().all()
    rows = [(t.invoice_issue.name, r["id"]) for r in issues]
    rows += [(t.invoice_delivery.name, r["id"]) for r in deliveries]
    if not issues:
        return {"mirror_entries": [], "read_set": rows,
                "delivery_backed": any(r["stock_entry_id"] for r in deliveries)}
    if not stock_on(ctx):
        raise planned_stock()
    out = stock(ctx, "preview_reverse", original={"voucher_type": "sales_invoice", "voucher": invoice_id},
                reversal_date=when.isoformat())
    return {**out, "read_set": [*rows, *(tuple(r) for r in out["read_set"])],
            "delivery_backed": any(r["stock_entry_id"] for r in deliveries)}


# ---------------------------------------------------------------- sales orders

def find_order(ctx: Ctx, said: str) -> dict:
    s = said.strip().upper()
    rows = [dict(r) for r in ctx.conn.execute(select(t.order).where(t.order.c.company_id == ctx.company_id)).mappings()]
    exact = [r for r in rows if r["number"].upper() == s]
    if exact:
        return exact[0]
    tail = [r for r in rows if s.isdigit() and r["number"].endswith(f"-{int(s):05d}")]
    if len(tail) == 1:
        return tail[0]
    raise Refusal(f"I can't find sales order '{said}'.", "order_unknown")


def order_lines(ctx: Ctx, order_id: str) -> list[dict]:
    return [dict(r) for r in ctx.conn.execute(select(t.order_line).where(
        t.order_line.c.company_id == ctx.company_id, t.order_line.c.order_id == order_id
    ).order_by(t.order_line.c.line_no)).mappings()]


def available(ctx: Ctx, item: dict) -> Decimal:
    return sum((to_decimal(w["qty"]) for w in holdings(ctx, item)), ZERO)


def create_order(ctx: Ctx, customer: dict, priced: list, total: Decimal, delivery_date: date,
                 operation_key: str | None) -> dict:
    """The order for the full quantity asked, whatever is on hand: it posts nothing, moves and reserves nothing."""
    oid = new_id()
    number = next_number(ctx, "SO", ctx.today)
    ctx.conn.execute(t.order.insert().values(
        id=oid, company_id=ctx.company_id, number=number, customer_id=customer["id"], posting_date=ctx.today,
        delivery_date=delivery_date, currency=ctx.currency, total=total, status="submitted",
        operation_key=operation_key, created_at=now()))
    for n, p in enumerate(priced, 1):
        ctx.conn.execute(t.order_line.insert().values(
            id=new_id(), company_id=ctx.company_id, order_id=oid, currency=ctx.currency, line_no=n,
            item_id=p.item["id"], quantity=p.quantity, rate=p.rate, amount=p.amount))
    audit(ctx, "create_sales_order", "sales_order", oid, detail={"number": number, "total": str(total)})
    return {"id": oid, "number": number}


def _delivery_lines(ctx: Ctx, order_line_ids: list[str]) -> list[dict]:
    """The delivery lines of those order lines, oldest delivery first, with their delivery's number and date."""
    if not order_line_ids:
        return []
    d, dl = t.delivery, t.delivery_line
    return [dict(r) for r in ctx.conn.execute(
        select(dl, d.c.number.label("delivery_number"), d.c.posting_date.label("delivery_date")).join(
            d, (d.c.company_id == dl.c.company_id) & (d.c.id == dl.c.delivery_id)).where(
            dl.c.company_id == ctx.company_id, dl.c.order_line_id.in_(order_line_ids)).order_by(
            d.c.posting_date, d.c.number, dl.c.id)).mappings()]


def _invoiced(ctx: Ctx, delivery_line_ids: list[str]) -> dict[str, Decimal]:
    if not delivery_line_ids:
        return {}
    rows = ctx.conn.execute(select(t.invoice_delivery.c.delivery_line_id, t.invoice_delivery.c.quantity).where(
        t.invoice_delivery.c.company_id == ctx.company_id,
        t.invoice_delivery.c.delivery_line_id.in_(delivery_line_ids))).all()
    out: dict[str, Decimal] = {}
    for r in rows:
        out[r.delivery_line_id] = out.get(r.delivery_line_id, ZERO) + to_decimal(r.quantity)
    return out


@dataclass
class OrderState:
    lines: list[dict]
    delivered: dict[str, Decimal]  # per order line
    invoiced: dict[str, Decimal]   # per order line
    deliveries: list[dict]         # every delivery line, oldest first
    invoiced_of: dict[str, Decimal]  # per delivery line


def order_state(ctx: Ctx, order: dict) -> OrderState:
    lines = order_lines(ctx, order["id"])
    dls = _delivery_lines(ctx, [ln["id"] for ln in lines])
    inv = _invoiced(ctx, [d["id"] for d in dls])
    delivered = {ln["id"]: ZERO for ln in lines}
    invoiced = {ln["id"]: ZERO for ln in lines}
    for d in dls:
        delivered[d["order_line_id"]] += to_decimal(d["quantity"])
        invoiced[d["order_line_id"]] += inv.get(d["id"], ZERO)
    return OrderState(lines, delivered, invoiced, dls, inv)


def order_status(state: OrderState) -> str:
    qty = {ln["id"]: to_decimal(ln["quantity"]) for ln in state.lines}
    if all(state.delivered[k] >= q and state.invoiced[k] >= q for k, q in qty.items()):
        return "completed"
    if all(state.delivered[k] >= q for k, q in qty.items()):
        return "delivered"
    return "partly_delivered" if any(v > 0 for v in state.delivered.values()) else "submitted"


def set_order_status(ctx: Ctx, order: dict) -> str:
    """The order's status from its rows, written only when it moves (forward only, by the database's rule)."""
    status = order_status(order_state(ctx, order))
    if status != order["status"]:
        ctx.conn.execute(t.order.update().where(t.order.c.id == order["id"]).values(status=status))
    return status


def lock_order(ctx: Ctx, order_id: str) -> dict:
    row = ctx.conn.execute(ledger.for_update(ctx, select(t.order).where(
        t.order.c.company_id == ctx.company_id, t.order.c.id == order_id))).mappings().first()
    if row is None:
        raise Refusal("That sales order does not exist in this company.", "order_unknown")
    return dict(row)


# ---------------------------------------------------------------- deliveries

@dataclass
class DeliveryPlan:
    order: dict
    when: date
    lines: list[tuple[dict, dict, Decimal]]  # (order line, item, quantity delivered now), one per part asked
    stock: StockPlan  # its issues keyed by the part's place in `lines`: one order line may ship from two warehouses


def plan_delivery(ctx: Ctx, order: dict, asked: list[tuple[dict, Decimal, str | None]] | None,
                  when: date) -> DeliveryPlan:
    """What a delivery ships: each line asked, or by default what is left of every line; never more than is left."""
    if when > ctx.today:
        raise Refusal("A delivery can't be dated in the future.", "future_date")
    state = order_state(ctx, order)
    items = items_by_id(ctx, {ln["item_id"] for ln in state.lines})
    left = {ln["id"]: to_decimal(ln["quantity"]) - state.delivered[ln["id"]] for ln in state.lines}
    picked: list[tuple[dict, dict, Decimal, str | None]] = []
    if asked is None:
        picked = [(ln, items[ln["item_id"]], left[ln["id"]], None) for ln in state.lines if left[ln["id"]] > 0]
    else:
        for item, qty, said in asked:
            on = [ln for ln in state.lines if ln["item_id"] == item["id"]]
            if not on:
                raise Refusal(f"{order['number']} has no {item['name']} on it.", "item_not_on_order")
            ln = next((x for x in on if left[x["id"]] > 0), on[0])
            if qty > left[ln["id"]]:
                raise Refusal(f"{order['number']} has {fmt_qty(left[ln['id']])} of {item['name']} left to deliver, "
                              f"less than {fmt_qty(qty)}.", "over_delivery")
            left[ln["id"]] -= qty
            picked.append((ln, item, qty, said))
    if not picked:
        raise Refusal(f"Everything on {order['number']} has been delivered.", "nothing_to_deliver")
    guard(ctx, [it for _, it, _, _ in picked])
    wanted = [Issued(n, it, q, source(ctx, it, q, said=said, needs="the delivery"), ZERO)
              for n, (ln, it, q, said) in enumerate(picked) if is_stock(it)]
    return DeliveryPlan(order, when, [(ln, it, q) for ln, it, q, _ in picked],
                        preview_issue(ctx, "sales_delivery", when, wanted))


def deliver(ctx: Ctx, plan: DeliveryPlan, operation_key: str | None) -> dict:
    """The delivery, its stock issued and its cost posted (cost of goods sold against inventory, nothing else), and
    the order's status. The number first, then the chain head, then the stock rows: the engine's one lock order."""
    number = next_number(ctx, "DN", plan.when)
    ledger.lock_chain(ctx)
    order = lock_order(ctx, plan.order["id"])
    did = new_id()
    ctx.conn.execute(t.delivery.insert().values(id=did, company_id=ctx.company_id, number=number, order_id=order["id"],
                                                posting_date=plan.when, operation_key=operation_key,
                                                created_at=now()))
    issued = issue(ctx, "sales_delivery", did, plan.when,
                   [Issued(i.key, i.item, i.qty, i.source, ZERO) for i in plan.stock.issued])
    done = {i.key: i for i in issued.issued}
    lines = summed(issued.lines)
    for n, (oline, item, qty) in enumerate(plan.lines):
        i = done.get(n)
        ctx.conn.execute(t.delivery_line.insert().values(
            id=new_id(), company_id=ctx.company_id, delivery_id=did, order_line_id=oline["id"], currency=ctx.currency,
            warehouse_id=i.source.warehouse if i else None, quantity=qty,
            cost=quantize(i.cost if i else ZERO, ctx.currency), stock_entry_id=i.entry_id if i else None,
            stock_line=i.stock_line if i else None))
    if lines:  # a delivery whose every line costs zero makes no posting
        ledger.post(ctx, plan.when, "sales_delivery", did, lines)
    status = set_order_status(ctx, order)
    audit(ctx, "deliver_order", "sales_delivery", did, detail={"number": number, "order": order["number"]})
    return {"id": did, "number": number, "status": status}


# ---------------------------------------------------------------- invoicing an order

@dataclass
class Bill:
    """One invoice line billing an order line's deliveries: the order line, its item, the quantity, the delivery lines
    it covers (oldest delivery first) and the kind they were delivered as."""
    order_line: dict
    item: dict
    qty: Decimal
    covers: list[tuple[dict, Decimal]]
    kind: str  # stock when the deliveries it covers moved stock, service when they moved none


@dataclass
class OrderInvoice:
    order: dict
    lines: list[Bill]


def plan_order_invoice(ctx: Ctx, order: dict) -> OrderInvoice:
    """What has been delivered and not yet invoiced, per order line at the order's rate, each covering its delivery
    lines oldest delivery first. An order line whose uninvoiced deliveries are of both kinds (some delivered while
    its item was a service, moving no stock) is billed as one line per kind, in the order of their first delivery,
    so each line's kind holds for its whole quantity."""
    state = order_state(ctx, order)
    items = items_by_id(ctx, {ln["item_id"] for ln in state.lines})
    out = []
    for ln in state.lines:
        covers = [(d, to_decimal(d["quantity"]) - state.invoiced_of.get(d["id"], ZERO))
                  for d in state.deliveries if d["order_line_id"] == ln["id"]]
        by_kind: dict[str, list[tuple[dict, Decimal]]] = {}
        for d, q in covers:
            if q > 0:
                by_kind.setdefault("stock" if d["stock_entry_id"] else "service", []).append((d, q))
        for kind, part in by_kind.items():
            out.append(Bill(ln, items[ln["item_id"]], sum((q for _, q in part), ZERO), part, kind))
    if not out:
        raise Refusal(f"Nothing delivered on {order['number']} is left to invoice; deliver it first, or invoice "
                      "without the order.", "nothing_to_invoice")
    return OrderInvoice(order, out)


def record_order_cover(ctx: Ctx, plan: OrderInvoice, line_ids: list[str]) -> None:
    for bill, line_id in zip(plan.lines, line_ids):
        for d, q in bill.covers:
            ctx.conn.execute(t.invoice_delivery.insert().values(
                id=new_id(), company_id=ctx.company_id, invoice_line_id=line_id, delivery_line_id=d["id"],
                quantity=q))


# ---------------------------------------------------------------- credit notes

@dataclass
class Part:
    """A quantity coming back against one issue: an invoice's own, or a delivery's."""
    voucher_type: str
    voucher_id: str
    stock_line: int
    warehouse: str
    warehouse_name: str
    against: str  # the document as the card names it: the invoice's or the delivery's number
    qty: Decimal
    cost: Decimal = ZERO
    entry_id: str | None = None


@dataclass
class CreditLine:
    invoice_line: dict
    number: int  # the invoice line's number on the invoice
    item: dict
    qty: Decimal
    amount: Decimal
    parts: list[Part]


@dataclass
class CreditPlan:
    invoice: dict
    customer: dict
    when: date
    lines: list[CreditLine]
    total: Decimal
    paid: Decimal
    outstanding: Decimal  # after the credit
    status: str           # the invoice's, after the credit
    returns_account: dict
    stock_lines: list[ledger.Line]
    read_set: list[tuple[str, str]]

    def posting_lines(self, ctx: Ctx) -> list[ledger.Line]:
        return [ledger.Line(self.returns_account["id"], debit=self.total),
                ledger.Line(ledger.account_by_number(ctx, AR)["id"], credit=self.total,
                            party_id=self.customer["id"]), *self.stock_lines]


def returns_account(ctx: Ctx) -> dict:
    """The sales returns account (its role), or sales income in books whose chart has no such role."""
    row = ctx.conn.execute(select(m.account).where(
        m.account.c.company_id == ctx.company_id, m.account.c.account_type == "sales_returns",
        m.account.c.is_group.is_(False)).order_by(m.account.c.number)).mappings().first()
    return dict(row) if row is not None else ledger.account_by_number(ctx, SALES)


def credited(ctx: Ctx, invoice_line_ids: list[str]) -> dict[str, tuple[Decimal, Decimal]]:
    """Per invoice line: the quantity and amount credit notes have taken back so far."""
    out = {k: (ZERO, ZERO) for k in invoice_line_ids}
    if not invoice_line_ids:
        return out
    for r in ctx.conn.execute(select(t.credit_note_line.c.invoice_line_id, t.credit_note_line.c.quantity,
                                     t.credit_note_line.c.amount).where(
            t.credit_note_line.c.company_id == ctx.company_id,
            t.credit_note_line.c.invoice_line_id.in_(invoice_line_ids))):
        q, a = out[r.invoice_line_id]
        out[r.invoice_line_id] = (q + to_decimal(r.quantity), a + to_decimal(r.amount))
    return out


def _returned(ctx: Ctx, invoice_line_id: str) -> dict[tuple[str, int], Decimal]:
    """What came back so far against each issue, for the credit lines of one invoice line."""
    r, cl = t.credit_note_return, t.credit_note_line
    out: dict[tuple[str, int], Decimal] = {}
    for row in ctx.conn.execute(select(r.c.original_voucher_id, r.c.original_line, r.c.quantity).join(
            cl, (cl.c.company_id == r.c.company_id) & (cl.c.id == r.c.credit_note_line_id)).where(
            r.c.company_id == ctx.company_id, cl.c.invoice_line_id == invoice_line_id)):
        k = (row.original_voucher_id, row.original_line)
        out[k] = out.get(k, ZERO) + to_decimal(row.quantity)
    return out


def _issues_of(ctx: Ctx, inv: dict, line: dict) -> list[Part]:
    """The issues an invoice line's stock left by, latest first, each with what may still come back against it:
    the invoice's own issue, or the deliveries it covers (latest delivery first)."""
    back = _returned(ctx, line["id"])
    own = ctx.conn.execute(select(t.invoice_issue).where(t.invoice_issue.c.company_id == ctx.company_id,
                                                         t.invoice_issue.c.invoice_line_id == line["id"])
                           ).mappings().first()
    names: dict[str, str] = {}
    out = []
    if own is not None:
        rows = [("sales_invoice", inv["id"], own["stock_line"], own["warehouse_id"], inv["number"],
                 to_decimal(own["quantity"]))]
    else:
        d, dl, idl = t.delivery, t.delivery_line, t.invoice_delivery
        rows = [("sales_delivery", r.delivery_id, r.stock_line, r.warehouse_id, r.number, to_decimal(r.quantity))
                for r in ctx.conn.execute(select(dl.c.delivery_id, dl.c.stock_line, dl.c.warehouse_id, d.c.number,
                                                 idl.c.quantity).join(
                    dl, (dl.c.company_id == idl.c.company_id) & (dl.c.id == idl.c.delivery_line_id)).join(
                    d, (d.c.company_id == dl.c.company_id) & (d.c.id == dl.c.delivery_id)).where(
                    idl.c.company_id == ctx.company_id, idl.c.invoice_line_id == line["id"],
                    dl.c.stock_entry_id.is_not(None)).order_by(d.c.posting_date.desc(), d.c.number.desc()))]
    for vtype, vid, sline, wh, against, qty in rows:
        left = qty - back.get((vid, sline), ZERO)
        if left > 0:
            out.append(Part(vtype, vid, sline, wh, names.get(wh, ""), against, left))
    return out


def sold_from_stock(line: dict) -> bool:
    """Whether an invoice line was sold from stock: by the kind it was written with, never the item's kind now (a
    service sold before its item became a stock item moved no stock, and none comes back)."""
    return line.get("item_kind") == "stock"


def invoice_lines_numbered(ctx: Ctx, inv: dict) -> list[tuple[int, dict]]:
    return list(enumerate(invoice_lines(ctx, inv["id"]), 1))


def _line_label(ctx: Ctx, n: int, ln: dict, item: dict, left: Decimal, sh_rate: str, where: str | None) -> str:
    frm = f" from {where}" if where else ""
    return (f"line {n}: {fmt_qty(to_decimal(ln['quantity']))} x {item['name']} @ {sh_rate}{frm}, "
            f"{fmt_qty(left)} still returnable")


def plan_credit(ctx: Ctx, inv: dict, asked: list[tuple[dict, Decimal, int | None]] | None,
                when: date) -> CreditPlan:
    """What a credit note takes back: each line against one invoice line (asked when the item is on several), at
    that line's rate, its amount allocated cumulatively; its stock back against the issues it left by, at their
    cost; never more than the invoice still owes (no refund, no credit balance)."""
    if inv["status"] not in ("submitted", "partly_paid", "paid"):
        what = {"draft": "a draft, so nothing is owed on it", "cancelled": "cancelled",
                "credited": "already credited in full"}.get(inv["status"], inv["status"])
        raise Refusal(f"{inv['number']} is {what}; there is nothing to credit.", "invoice_not_open")
    if when > ctx.today:
        raise Refusal("A credit note can't be dated in the future.", "future_date")
    if when < inv["posting_date"]:
        raise Refusal(f"A credit note dated {when:%d %b %Y} can't credit {inv['number']}, dated "
                      f"{inv['posting_date']:%d %b %Y}.", "credit_before_invoice")
    numbered = invoice_lines_numbered(ctx, inv)
    items = items_by_id(ctx, {ln["item_id"] for _, ln in numbered})
    so_far = credited(ctx, [ln["id"] for _, ln in numbered])
    left = {ln["id"]: to_decimal(ln["quantity"]) - so_far[ln["id"]][0] for _, ln in numbered}
    picked: list[tuple[int, dict, Decimal]] = []
    if asked is None:  # everything: every line in full, each against itself
        picked = [(n, ln, left[ln["id"]]) for n, ln in numbered if left[ln["id"]] > 0]
        if not picked:
            raise Refusal(f"Everything on {inv['number']} has already been credited.", "nothing_to_credit")
    else:
        for item, qty, line_no in asked:
            on = [(n, ln) for n, ln in numbered if ln["item_id"] == item["id"]]
            if not on:
                raise Refusal(f"{inv['number']} has no {item['name']} on it.", "item_not_on_invoice")
            if line_no is not None:
                on = [(n, ln) for n, ln in on if n == line_no]
                if not on:
                    raise Refusal(f"Line {line_no} of {inv['number']} is not {item['name']}.", "invoice_line_unknown")
            if len(on) > 1:
                labels = [_line_label(ctx, n, ln, item, left[ln["id"]], fmt_rate(ln["rate"], ctx.currency),
                                      _line_warehouse(ctx, inv, ln)) for n, ln in on]
                raise Refusal(f"{item['name']} is on {len(on)} lines of {inv['number']}: {'; '.join(labels)}. "
                              "Which line?", "invoice_line_ambiguous", options=labels, field="invoice_line",
                              choices=[{"label": lb, "ref": n} for lb, (n, _) in zip(labels, on)])
            n, ln = on[0]
            if qty > left[ln["id"]]:
                raise Refusal(f"Line {n} of {inv['number']} has {fmt_qty(left[ln['id']])} of {item['name']} still "
                              f"to credit (of {fmt_qty(to_decimal(ln['quantity']))} invoiced), less than "
                              f"{fmt_qty(qty)}.", "over_credit")
            left[ln["id"]] -= qty
            picked.append((n, ln, qty))
    guard(ctx, [items[ln["item_id"]] for _, ln, _ in picked if sold_from_stock(ln)])
    lines, read_set = [], []
    taken: dict[str, Decimal] = {}  # quantity this credit note already takes per invoice line (several asks)
    # per issue: the quantity this credit note takes back against it so far, their cost together, and the stock
    # service's preview of that whole quantity. Returns against one issue cost cumulatively, so a later part costs
    # the cumulative cost less the earlier parts', exactly as return_in will allocate them in order.
    against: dict[tuple[str, int], tuple[Decimal, Decimal, dict]] = {}
    for n, ln, qty in picked:
        q_inv, l_amt = to_decimal(ln["quantity"]), to_decimal(ln["amount"])
        before_q = so_far[ln["id"]][0] + taken.get(ln["id"], ZERO)
        before_a = so_far[ln["id"]][1] + sum((c.amount for c in lines if c.invoice_line["id"] == ln["id"]), ZERO)
        amount = quantize(l_amt * (before_q + qty) / q_inv, ctx.currency) - before_a  # cumulative (round(L x C/Q))
        taken[ln["id"]] = taken.get(ln["id"], ZERO) + qty
        item = items[ln["item_id"]]
        parts: list[Part] = []
        if sold_from_stock(ln):
            need = qty
            for p in _issues_of(ctx, inv, ln):
                key = (p.voucher_id, p.stock_line)
                used, used_cost, _ = against.get(key, (ZERO, ZERO, {}))
                take = min(need, p.qty - used)
                if take <= 0:
                    continue
                p.qty, need = take, need - take
                out = stock(ctx, "preview_return", voucher_type="sales_credit_note", voucher="preview",
                            posting_date=when.isoformat(), original={"voucher": p.voucher_id, "line": p.stock_line},
                            qty=fmt_qty(used + take))
                p.cost = to_decimal(out["cost"]) - used_cost
                against[key] = (used + take, to_decimal(out["cost"]), out)
                read_set += [tuple(r) for r in out["read_set"]]
                parts.append(p)
                if need == 0:
                    break
            if need > 0:
                raise Refusal(f"Only {fmt_qty(qty - need)} of {item['name']} on line {n} can still come back into "
                              "stock.", "return_exceeds_issue")
            names = {w["warehouse"]: w["name"] for w in holdings(ctx, item)}
            for p in parts:
                p.warehouse_name = names.get(p.warehouse, "")
        lines.append(CreditLine(ln, n, item, qty, amount, parts))
    stock_lines = [ln for _, _, out in against.values() for ln in lines_of(out["ledger_lines"])]
    total = sum((c.amount for c in lines), ZERO)
    if total <= 0:
        raise Refusal("That credit comes to nothing at the currency's smallest unit.", "nothing_to_credit")
    owed = to_decimal(inv["outstanding"])
    if total > owed:
        raise Refusal(f"{inv['number']} owes {owed} now; a credit of {total} would leave money owed to the customer, "
                      "and a refund is not available.", "refund_not_available")
    paid = sum((to_decimal(a["amount"]) for a in live_allocations(ctx, inv["id"])), ZERO)
    after = owed - total
    customer = dict(ctx.conn.execute(select(m.party).where(m.party.c.company_id == ctx.company_id,
                                                           m.party.c.id == inv["customer_id"])).mappings().one())
    status = "credited" if after == 0 else inv["status"]
    plan = CreditPlan(inv, customer, when, lines, total, paid, after, status, returns_account(ctx),
                      summed(stock_lines), sorted(set(read_set)))
    ledger.check_post(ctx, when, "sales_credit_note", None, plan.posting_lines(ctx))  # a posting the books refuse
    return plan


def _line_warehouse(ctx: Ctx, inv: dict, ln: dict) -> str | None:
    """Where an invoice line's stock left from, for the line question: the invoice's own issue's warehouse."""
    wh = ctx.conn.execute(select(t.invoice_issue.c.warehouse_id).where(
        t.invoice_issue.c.company_id == ctx.company_id, t.invoice_issue.c.invoice_line_id == ln["id"])).scalar()
    if wh is None:
        return None
    item = items_by_id(ctx, [ln["item_id"]])[ln["item_id"]]
    return next((w["name"] for w in holdings(ctx, item) if w["warehouse"] == wh), None)


def credit(ctx: Ctx, plan: CreditPlan, operation_key: str | None) -> dict:
    """The credit note, its lines and their stock back (one return per issue), its posting, and the invoice's
    outstanding (conditional on the outstanding read): no payment, refund or allocation row. The number first,
    then the chain head, then the stock rows."""
    inv = plan.invoice
    number = next_number(ctx, "CN", plan.when)
    ledger.lock_chain(ctx)
    cid = new_id()
    ctx.conn.execute(t.credit_note.insert().values(
        id=cid, company_id=ctx.company_id, number=number, invoice_id=inv["id"], customer_id=inv["customer_id"],
        posting_date=plan.when, currency=ctx.currency, total=plan.total, operation_key=operation_key,
        created_at=now()))
    stock_lines: list[ledger.Line] = []
    for k, cl in enumerate(plan.lines, 1):
        lid = new_id()
        ctx.conn.execute(t.credit_note_line.insert().values(
            id=lid, company_id=ctx.company_id, credit_note_id=cid, currency=ctx.currency, line_no=k,
            invoice_line_id=cl.invoice_line["id"], item_id=cl.item["id"], quantity=cl.qty,
            rate=cl.invoice_line["rate"], amount=cl.amount))
        for p in cl.parts:
            out = stock(ctx, "return_in", voucher_type="sales_credit_note", voucher=cid,
                        posting_date=plan.when.isoformat(), original={"voucher": p.voucher_id, "line": p.stock_line},
                        qty=fmt_qty(p.qty))
            stock_lines += lines_of(out["ledger_lines"])
            ctx.conn.execute(t.credit_note_return.insert().values(
                id=new_id(), company_id=ctx.company_id, credit_note_line_id=lid, currency=ctx.currency,
                original_voucher_type=p.voucher_type, original_voucher_id=p.voucher_id, original_line=p.stock_line,
                warehouse_id=p.warehouse, quantity=p.qty, cost=to_decimal(out["cost"]), stock_entry_id=out["entry_id"]))
    lines = [*plan.posting_lines(ctx)[:2], *summed(stock_lines)]
    ledger.post(ctx, plan.when, "sales_credit_note", cid, lines)
    res = ctx.conn.execute(t.invoice.update().where(
        t.invoice.c.id == inv["id"], t.invoice.c.outstanding == inv["outstanding"]).values(
        outstanding=plan.outstanding, status=plan.status))
    if res.rowcount != 1:
        raise Refusal("That invoice changed while the credit note was being saved; nothing was saved.", "conflict")
    audit(ctx, "create_credit_note", "sales_credit_note", cid, detail={"number": number, "total": str(plan.total),
                                                                      "invoice": inv["number"]})
    return {"id": cid, "number": number}
