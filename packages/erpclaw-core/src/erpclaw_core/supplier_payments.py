"""Supplier-payment planning and an inactive, guarded internal writer.

Only engine-resolved IDs enter here. The writer needs the original live Core
commit context and its exact registered pay_supplier card; no public contract is
registered by this module. Core owns the transaction, savepoint and outcome.
Delivery and opening payables are verified against their actual original postings.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hmac
import json
from datetime import date
from decimal import Decimal, localcontext

from sqlalchemy import select

from . import integration, ledger, models as m, payables
from .common import Ctx, Refusal, audit, new_id, now
from .contracts import Amount, Preview, Result, Shown
from .money import MoneyError, fmt, quantize, to_amount
from .numbering import next_number
from .payable_tables import supplier_payable, supplier_payment, supplier_payment_allocation

PAYMENT = "supplier_payment"
MAX_ITEMS = 200
MAX_HISTORY_PER_ITEM = 64
MAX_HISTORY = 1024
MAX_SOURCE_LINES = 64


@dataclass(frozen=True)
class PaySupplier:
    supplier_id: str
    bank_account_id: str
    amount: object
    posting_date: date


@dataclass(frozen=True)
class PriorAllocation:
    allocation_id: str
    payment_id: str
    amount: Decimal
    payment_status: str


@dataclass(frozen=True)
class Item:
    id: str
    source_id: str
    source_number: str
    posting_date: date
    original_amount: Decimal
    outstanding: Decimal
    posting_id: str
    control_account_id: str
    prior_allocations: tuple[PriorAllocation, ...]
    source_type: str = payables.DELIVERY
    opening_sequence: int = 0


@dataclass(frozen=True)
class Allocation:
    item: Item
    amount: Decimal
    outstanding_after: Decimal


@dataclass(frozen=True)
class PaymentPlan:
    request: PaySupplier
    supplier: dict
    bank: dict
    payable: dict
    amount: Decimal
    total_owed: Decimal
    eligible_owed: Decimal
    owed_after: Decimal
    items: tuple[Item, ...]
    allocations: tuple[Allocation, ...]
    read_set: tuple[tuple[str, str], ...]
    mode: str
    external: str | None
    codes: dict[str, str]

    def posting_lines(self) -> list[ledger.Line]:
        return [ledger.Line(self.payable["id"], debit=self.amount, party_id=self.supplier["id"]),
                ledger.Line(self.bank["id"], credit=self.amount)]


def _id(value: object) -> str:
    if not isinstance(value, str) or not value or value.strip() != value or len(value) > 36:
        raise Refusal("Payment identities must be engine-resolved records of these books.", "payment_input")
    return value


def _amount(ctx: Ctx, value, *, positive: bool = True) -> Decimal:
    try:
        amount = to_amount(value, ctx.currency)
    except MoneyError as exc:
        raise Refusal(f"The payment amounts must be exact amounts in {ctx.currency}: {exc}.", "bad_amount") from None
    if amount < 0 or (positive and amount == 0):
        raise Refusal("The amount paid and each debt must be more than zero.", "bad_amount")
    return amount


def _sum(ctx: Ctx, values) -> Decimal:
    with localcontext() as arithmetic:
        arithmetic.prec = 40
        return _amount(ctx, sum(values, Decimal(0)), positive=False)


def _date(value: object) -> date:
    if type(value) is not date:
        raise Refusal("A payment needs an exact calendar date.", "payment_date")
    return value


def _rows(ctx: Ctx, statement, maximum: int, what: str) -> list[dict]:
    # LIMIT is on SQL, before a driver/Python list can materialize a full relation.
    rows = [dict(row) for row in ctx.conn.execute(statement.limit(maximum + 1)).mappings()]
    if len(rows) > maximum:
        raise Refusal(f"There are more than {maximum} {what}; this bounded payment planner cannot certify "
                      "the complete allocation. Nothing was saved.", "payment_items_limit")
    return rows


def _supplier(ctx: Ctx, key: str) -> dict:
    row = ctx.conn.execute(select(m.party).where(m.party.c.id == key,
                              m.party.c.company_id == ctx.company_id)).mappings().first()
    if row is None or row["id"] != key or row["company_id"] != ctx.company_id or row["kind"] != "supplier":
        raise Refusal("That supplier is not a supplier of these books.", "bad_party")
    return dict(row)


def _bank(ctx: Ctx, key: str) -> dict:
    row = ctx.conn.execute(select(m.account).where(m.account.c.id == key,
                              m.account.c.company_id == ctx.company_id)).mappings().first()
    if (row is None or row["id"] != key or row["company_id"] != ctx.company_id
            or row["account_type"] != "bank" or row["is_group"]):
        raise Refusal("Choose a bank-type leaf account in these books to pay from.", "payment_account")
    return dict(row)


def _payment_membership(ctx: Ctx, payment: dict, supplier_id: str,
                        dependencies: set[tuple[str, str]], verified: dict, allocation_ids: set) -> dict:
    """Certify a whole payment, including allocations to items now fully paid.

    The open-item list alone is not the payment's membership. Every row stays
    in the read-set even when its target is no longer open.
    """
    key = payment["id"]
    if key in verified:
        return verified[key]
    a, t = supplier_payment_allocation, supplier_payable
    rows = _rows(ctx, select(a).where(a.c.company_id == ctx.company_id, a.c.payment_id == key).order_by(a.c.id),
                 min(MAX_ITEMS, MAX_HISTORY - len(allocation_ids)), "allocations of related payments")
    members, targets, amounts = {}, set(), []
    for row in rows:
        if (row["company_id"] != ctx.company_id or row["payment_id"] != key or row["currency"] != ctx.currency
                or row["id"] in members or row["id"] in allocation_ids or row["payable_id"] in targets):
            raise Refusal("A stored payment's allocation membership is inconsistent.", "payment_source")
        value = _amount(ctx, row["amount"])
        target = ctx.conn.execute(select(t).where(t.c.company_id == ctx.company_id,
                                                     t.c.id == row["payable_id"])).mappings().first()
        if (target is None or target["id"] != row["payable_id"] or target["company_id"] != ctx.company_id
                or target["supplier_id"] != supplier_id or target["currency"] != ctx.currency
                or target["source_type"] not in payables.ITEM_SOURCES
                or (target["source_type"] == payables.OPENING and not _opening_link(target))
                or target["status"] not in ("open", "paid", "reversed")
                or (payment["status"] == "posted" and target["status"] == "reversed")
                or _date(target["posting_date"]) > payment["posting_date"]
                or value > _amount(ctx, target["amount"])):
            raise Refusal("A stored payment allocation names an incompatible delivery item.", "payment_source")
        targets.add(row["payable_id"])
        members[row["id"]] = row
        amounts.append(value)
        dependencies.update({("supplier_payment_allocation", row["id"]), ("supplier_payable", target["id"])})
    if _sum(ctx, amounts) != _amount(ctx, payment["amount"]):
        raise Refusal("A stored supplier payment does not equal all of its allocations.", "payment_source")
    allocation_ids.update(members)
    verified[key] = members
    return members


def _opening_link(row: dict) -> bool:
    return bool(row.get("posting_id") and row.get("source_voucher_id")
                and isinstance(row.get("control_line_no"), int) and row["control_line_no"] > 0
                and isinstance(row.get("opening_sequence"), int) and row["opening_sequence"] > 0)


def _source(ctx: Ctx, row: dict, supplier_id: str, payable: dict,
            dependencies: set[tuple[str, str]], remaining_history: int,
            verified_payments: dict, verified_allocations: set) -> Item:
    if (row["company_id"] != ctx.company_id or row["supplier_id"] != supplier_id
            or row["currency"] != ctx.currency or row["status"] != "open"
            or row["source_type"] not in payables.ITEM_SOURCES):
        raise Refusal("A payment can settle only this supplier's supported delivery debts in these books.",
                      "payment_source")
    key, source = _id(row["id"]), _id(row["source_id"])
    when = _date(row["posting_date"])
    original, outstanding = _amount(ctx, row["amount"]), _amount(ctx, row["outstanding"])
    if outstanding > original:
        raise Refusal("A delivery's outstanding amount exceeds its original debt.", "payment_source")
    number = row["source_number"]
    if not isinstance(number, str) or not number or len(number) > 30:
        raise Refusal("A delivery's stored identity is invalid.", "payment_source")
    opening = row["source_type"] == payables.OPENING
    if opening:
        if not _opening_link(row):
            raise Refusal("A supplier starting figure needs its stored posting and payable line.", "payment_source")
        posting = ctx.conn.execute(select(m.posting).where(m.posting.c.company_id == ctx.company_id,
            m.posting.c.id == row.get("posting_id"))).mappings().first()
        if (posting is None or row.get("source_voucher_id") != posting["voucher_id"]
                or posting["memo"] != number or not isinstance(row.get("opening_sequence"), int)
                or row["opening_sequence"] <= 0):
            raise Refusal("A supplier starting figure does not match its stored opening posting.", "payment_source")
    else:
        posting = ledger.posting_for_voucher(ctx, payables.DELIVERY, source)
    if (posting["company_id"] != ctx.company_id or posting["voucher_type"] != row["source_type"]
            or posting["voucher_id"] != (row.get("source_voucher_id") if opening else source) or posting["posting_date"] != when
            or posting["reverses_posting_id"] is not None):
        raise Refusal("A delivery's payable does not match its original posting.", "payment_source")
    if ctx.conn.execute(select(m.posting.c.id).where(m.posting.c.company_id == ctx.company_id,
                     m.posting.c.reverses_posting_id == posting["id"]).limit(1)).first():
        raise Refusal("That delivery has been reversed and cannot be paid.", "payable_reversed")
    lines = _rows(ctx, select(m.ledger_entry).where(m.ledger_entry.c.company_id == ctx.company_id,
                  m.ledger_entry.c.posting_id == posting["id"]).order_by(m.ledger_entry.c.line_no),
                  MAX_SOURCE_LINES, "original delivery posting lines")
    if (len(lines) < 2 or len({line["id"] for line in lines}) != len(lines)
            or any(line["company_id"] != ctx.company_id or line["posting_id"] != posting["id"]
                   or line["currency"] != ctx.currency for line in lines)
            or _sum(ctx, (_amount(ctx, line["debit"], positive=False) for line in lines))
               != _sum(ctx, (_amount(ctx, line["credit"], positive=False) for line in lines))):
        raise Refusal("A delivery's original posting is incomplete or inconsistent.", "payment_source")
    party_lines = [line for line in lines if line["party_id"] is not None]
    if opening:
        party_lines = [line for line in party_lines if line["party_id"] == supplier_id]
    if len(party_lines) != 1:
        raise Refusal("A delivery needs one original supplier payable line.", "payment_source")
    line = party_lines[0]
    if opening and line["line_no"] != row.get("control_line_no"):
        raise Refusal("A supplier starting figure names a different payable control line.", "payment_source")
    if (line["company_id"] != ctx.company_id or line["posting_id"] != posting["id"]
            or line["party_id"] != supplier_id or line["currency"] != ctx.currency
            or _amount(ctx, line["credit"], positive=False) != original
            or _amount(ctx, line["debit"], positive=False) != 0):
        raise Refusal("A delivery's original supplier amount does not match its payable item.", "payment_source")
    if line["account_id"] != payable["id"]:
        raise Refusal("That delivery was credited to a different payable account; this payment cannot "
                      "silently move its debt to today's account.", "payment_source_account")
    dependencies.update({("supplier_payable", key), ("posting", posting["id"])})
    dependencies.update(("ledger_entry", line["id"]) for line in lines)
    a = supplier_payment_allocation
    history = _rows(ctx, select(a).where(a.c.company_id == ctx.company_id, a.c.payable_id == key)
                    .order_by(a.c.id), min(MAX_HISTORY_PER_ITEM, remaining_history), "stored allocations")
    prior, live = [], []
    seen = set()
    for allocation in history:
        if (allocation["company_id"] != ctx.company_id or allocation["payable_id"] != key
                or allocation["currency"] != ctx.currency or allocation["payment_id"] in seen):
            raise Refusal("A delivery's stored allocation membership is inconsistent.", "payment_source")
        seen.add(allocation["payment_id"])
        amount = _amount(ctx, allocation["amount"])
        payment = ctx.conn.execute(select(supplier_payment).where(
            supplier_payment.c.company_id == ctx.company_id,
            supplier_payment.c.id == allocation["payment_id"])).mappings().first()
        if (payment is None or payment["company_id"] != ctx.company_id
                or payment["id"] != allocation["payment_id"] or payment["supplier_id"] != supplier_id
                or payment["currency"] != ctx.currency or payment["status"] not in ("posted", "reversed")
                or _date(payment["posting_date"]) < when):
            raise Refusal("A delivery's allocation has no compatible stored supplier payment.", "payment_source")
        if (amount > original or amount > _amount(ctx, payment["amount"])
                or _amount(ctx, payment["unallocated"], positive=False) != 0):
            raise Refusal("A delivery allocation exceeds its stored payment or original amount.", "payment_source")
        members = _payment_membership(ctx, payment, supplier_id, dependencies,
                                      verified_payments, verified_allocations)
        member = members.get(allocation["id"])
        if member is None or any(member[field] != allocation[field] for field in
                                 ("company_id", "payment_id", "payable_id", "currency", "amount")):
            raise Refusal("An item's allocation differs from its payment's complete membership.", "payment_source")
        if payment["status"] == "posted":
            live.append(amount)
        prior.append(PriorAllocation(allocation["id"], payment["id"], amount, payment["status"]))
        dependencies.update({("supplier_payment_allocation", allocation["id"]),
                             ("supplier_payment", payment["id"])})
    if original - _sum(ctx, live) != outstanding:
        raise Refusal("A delivery's outstanding amount does not match its stored live allocations.", "payment_source")
    return Item(key, source, number, when, original, outstanding, posting["id"], line["account_id"], tuple(prior),
                row["source_type"], row.get("opening_sequence") or 0)


def plan_payment(ctx: Ctx, request: PaySupplier) -> PaymentPlan:
    """Read and validate the complete current delivery source; never write or register a contract.

    A future approved writer must recompute this under core's chain/read-set locks;
    this standalone preview neither acquires commit authority nor guarantees freshness.
    """
    supplier_id, bank_id = _id(request.supplier_id), _id(request.bank_account_id)
    amount, paid_on = _amount(ctx, request.amount), _date(request.posting_date)
    if paid_on > ctx.today:
        raise Refusal("A payment can't be dated in the future.", "future_date")
    supplier = _supplier(ctx, supplier_id)
    t = supplier_payable
    rows = _rows(ctx, select(t).where(t.c.company_id == ctx.company_id, t.c.supplier_id == supplier_id,
                 t.c.status == "open").order_by(t.c.posting_date, t.c.source_number, t.c.opening_sequence, t.c.id),
                 MAX_ITEMS, "open supplier items")
    if not rows:
        raise Refusal(f"You don't owe {supplier['name']} anything in these books.", "nothing_owed")
    payable = payables.payable_account(ctx)
    if payable.get("company_id") != ctx.company_id or payable.get("account_type") != "payable":
        raise Refusal("The payable account must belong to these books.", "payment_account")
    dependencies = {("company", ctx.company_id), ("party", supplier_id), ("account", payable["id"])}
    items, history_count, seen = [], 0, set()
    verified_payments, verified_allocations = {}, set()
    for row in rows:
        if row["id"] in seen:
            raise Refusal("An open item appeared more than once; no allocation can be certified.", "payment_source")
        seen.add(row["id"])
        item = _source(ctx, row, supplier_id, payable, dependencies, MAX_HISTORY - history_count,
                       verified_payments, verified_allocations)
        history_count += len(item.prior_allocations)
        items.append(item)
    items.sort(key=lambda item: (item.posting_date, item.source_number, item.opening_sequence, item.id))
    eligible = [item for item in items if item.posting_date <= paid_on]
    if not eligible:
        raise Refusal(f"{supplier['name']}'s deliveries are all dated after {paid_on.isoformat()}, the payment's "
                      "date, so this payment can't settle them.", "owed_after_payment")
    total = _sum(ctx, (item.outstanding for item in items))
    if amount > total:
        raise Refusal(f"{supplier['name']} is owed {fmt(total, ctx.currency)}; the payment is "
                      f"{fmt(amount, ctx.currency)}. Pay what is owed, or less.", "payment_above_owed")
    upto = _sum(ctx, (item.outstanding for item in eligible))
    if amount > upto:
        raise Refusal(f"{supplier['name']} is owed {fmt(upto, ctx.currency)} for deliveries up to "
                      f"{paid_on.isoformat()}, the payment's date; the payment is {fmt(amount, ctx.currency)}.",
                      "payment_above_eligible")
    left, allocations = amount, []
    for item in eligible:
        if left == 0:
            break
        take = min(left, item.outstanding)
        allocations.append(Allocation(item, take, item.outstanding - take))
        left -= take
    bank = _bank(ctx, bank_id)
    dependencies.add(("account", bank_id))
    period = ledger.open_period(ctx, paid_on)
    dependencies.update({("fiscal_period", period["id"]), ("fiscal_year", period["fiscal_year_id"])})
    mode, external = integration.system_of_record(ctx, "accounting")
    codes = {}
    if mode == "external":
        codes = integration.account_codes(ctx, external, [bank_id, payable["id"]])
        mappings = ctx.conn.execute(select(m.account_map.c.id).where(m.account_map.c.company_id == ctx.company_id,
            m.account_map.c.external_system == external, m.account_map.c.account_id.in_([bank_id, payable["id"]])).limit(3)).scalars()
        mappings = list(mappings)
        if len(mappings) > 2:
            raise Refusal("The payment account mappings are inconsistent.", "payment_source")
        dependencies.update(("account_map", key) for key in mappings)
    plan = PaymentPlan(request, supplier, bank, payable, amount, total, upto, total - amount, tuple(items),
                       tuple(allocations), tuple(sorted(dependencies)), mode, external, codes)
    ledger.check_post(ctx, paid_on, PAYMENT, None, plan.posting_lines())
    return plan


def _preview_plan(ctx: Ctx, plan: PaymentPlan) -> Preview:
    request = plan.request
    sh = Shown(ctx.currency)
    sh.title("Pay supplier")
    sh.line("Supplier", plan.supplier["name"])
    sh.line("Date", request.posting_date.isoformat())
    sh.line("Pay from", f"{plan.bank['number']} {plan.bank['name']}")
    sh.line("Amount", sh.amount(plan.amount))
    for allocation in plan.allocations:
        sh.line(f"Applied to {allocation.item.source_number}",
                f"{sh.amount(allocation.amount)} of {sh.amount(allocation.item.outstanding)} owed; "
                f"{sh.amount(allocation.outstanding_after)} remains")
    sh.line("Total still owed", sh.amount(plan.owed_after))
    sh.line("Accounting destination", "ERPClaw general ledger" if plan.mode == "native" else
            f"{plan.external}: ERPClaw sub-ledger, with the journal staged for this system")
    for line in plan.posting_lines():
        account = plan.payable if line.party_id else plan.bank
        mapped = f" (external account {plan.codes[line.account_id]})" if plan.mode == "external" else ""
        sh.book(f"{account['number']} {account['name']}{mapped}: "
                f"{'debit' if line.debit else 'credit'} {sh.amount(line.debit or line.credit)} {ctx.currency}")
    sh.line("Tax", "A payment carries no tax of its own.")
    # Core hashes the actual Card and read-set; this token is not authority.
    return Preview(sh.card(), "supplier-payment-plan-v1")


def preview_payment(ctx: Ctx, request: PaySupplier) -> Preview:
    return _preview_plan(ctx, plan_payment(ctx, request))


def payment_read_set(ctx: Ctx, request: PaySupplier) -> list[tuple[str, str]]:
    return list(plan_payment(ctx, request).read_set)


def _payment_approval(ctx: Ctx, operation_key: str) -> tuple[dict, object]:
    """Live Core scope is the authority; stored rows only bind its exact effect."""
    from . import approvals
    from .plugins import REGISTRY

    approval_id = approvals.committing(ctx)
    contract = REGISTRY.contracts.get("pay_supplier")
    owner = REGISTRY.plugins.get("core")
    if (approval_id is None or contract is None or owner is None
            or REGISTRY.contract_plugin.get("pay_supplier") != "core"
            or contract.name != "pay_supplier" or contract.plugin != "core"
            or contract.effect != "write" or contract.planned is not None
            or owner.manifest.contracts.get("pay_supplier") != "write"
            or PAYMENT not in owner.manifest.voucher_types):
        raise Refusal("A supplier payment runs only in its registered Core card's active approved commit.",
                      "not_approved")
    card = ctx.conn.execute(select(m.approval).where(m.approval.c.id == approval_id,
                            m.approval.c.company_id == ctx.company_id)).mappings().first()
    if (card is None or card["id"] != approval_id or card["company_id"] != ctx.company_id
            or card["contract"] != "pay_supplier" or card["status"] != "consumed"
            or not isinstance(operation_key, str) or not operation_key
            or card["operation_key"] != operation_key):
        raise Refusal("The active supplier-payment approval does not name this company and operation.", "not_approved")
    if ctx.conn.execute(select(m.operation_outcome.c.key).where(
            m.operation_outcome.c.approval_id == approval_id)).first():
        raise Refusal("This supplier-payment approval already has an outcome.", "not_approved")
    if ctx.conn.execute(select(supplier_payment.c.id).where(
            supplier_payment.c.operation_key == operation_key)).first():
        raise Refusal("This payment operation already has a stored payment; read its outcome instead.",
                      "duplicate_payment")
    return dict(card), contract


def _approved_plan(ctx: Ctx, request: PaySupplier, operation_key: str) -> PaymentPlan:
    from . import approvals
    from .plugins import REGISTRY

    card, contract = _payment_approval(ctx, operation_key)
    plan = plan_payment(ctx, request)
    try:
        stored_card = json.loads(card["card_json"])
        stored_rows = json.loads(card["read_set_json"])
        stored_args = json.loads(card["args_json"])
        # Reuse Core's real card envelope, including company/footer/date source.
        renderer = replace(contract, preview=lambda context, unused: _preview_plan(context, plan))
        actual_card = approvals._preview(ctx, renderer, request, stored_card.get("date_source"))
        actual_rows = [[table, key, approvals._row_digest(ctx.conn, table, key)]
                       for table, key in plan.read_set]
        current = {**card, "contract_version": REGISTRY.plugins["core"].version,
                   "impl_version": approvals.impl_version(contract)}
        actual_digest = approvals.digest(approvals._fields(current, stored_args, actual_rows, actual_card))
        same = (actual_card == stored_card and actual_rows == stored_rows
                and hmac.compare_digest(actual_digest, card["digest"]))
    except (ValueError, TypeError, KeyError, AttributeError):
        raise Refusal("The supplier-payment approval data is incomplete; request a fresh card.", "stale") from None
    if not same:
        raise Refusal("The supplier payment, its accounting destination or its source rows changed; "
                      "request a fresh card. Nothing was saved.", "stale")
    # A callback/transaction loss while reading cannot leave authority cached.
    final, _ = _payment_approval(ctx, operation_key)
    if final != card:
        raise Refusal("The supplier-payment approval changed before its effect.", "stale")
    return plan


def commit_payment(ctx: Ctx, request: PaySupplier, operation_key: str) -> Result:
    """Write one approved plan inside Core's existing transaction/savepoint.

    No begin/commit/rollback, scope issuance, operation-outcome write, registration
    or Trusted escape lives here. Core handles replay using its durable outcome.
    A conditional update failure must propagate so Core rolls back all effects.
    """
    plan = _approved_plan(ctx, request, operation_key)
    payment_id = new_id()
    # Every read/check above precedes the first number-series or business write.
    number = next_number(ctx, "SPAY", request.posting_date)
    zero = quantize(Decimal(0), ctx.currency)
    ctx.conn.execute(supplier_payment.insert().values(
        id=payment_id, company_id=ctx.company_id, number=number, supplier_id=plan.supplier["id"],
        posting_date=request.posting_date, currency=ctx.currency, amount=plan.amount, unallocated=zero,
        account_id=plan.bank["id"], status="posted", operation_key=operation_key, created_at=now()))
    for allocation in plan.allocations:
        item = allocation.item
        ctx.conn.execute(supplier_payment_allocation.insert().values(
            id=new_id(), company_id=ctx.company_id, payment_id=payment_id, payable_id=item.id,
            currency=ctx.currency, amount=allocation.amount))
        changed = ctx.conn.execute(supplier_payable.update().where(
            supplier_payable.c.company_id == ctx.company_id, supplier_payable.c.id == item.id,
            supplier_payable.c.supplier_id == plan.supplier["id"], supplier_payable.c.currency == ctx.currency,
            supplier_payable.c.source_type == item.source_type, supplier_payable.c.source_id == item.source_id,
            supplier_payable.c.status == "open", supplier_payable.c.amount == item.original_amount,
            supplier_payable.c.outstanding == item.outstanding).values(
                outstanding=allocation.outstanding_after,
                status="paid" if allocation.outstanding_after == 0 else "open"))
        if changed.rowcount != 1:
            raise Refusal("A delivery changed while the supplier payment was being saved; nothing was saved.",
                          "conflict")
    posted = ledger.post(ctx, request.posting_date, PAYMENT, payment_id, plan.posting_lines(),
                         operation_key=operation_key)
    audit(ctx, "pay_supplier", PAYMENT, payment_id,
          detail={"number": number, "amount": str(plan.amount), "posting_id": posted.posting_id})
    shown = Shown(ctx.currency)
    amount = shown.amount(plan.amount, "amount", number)
    remaining = shown.amount(plan.owed_after, "outstanding", plan.supplier["name"])
    return Result(f"Paid {plan.supplier['name']} {amount} {ctx.currency}, payment {number}. "
                  f"Still owed: {remaining} {ctx.currency}.",
                  {"id": payment_id, "number": number, "posting_id": posted.posting_id,
                   "supplier_id": plan.supplier["id"], "operation_key": operation_key},
                  [amount, remaining], shown.amounts)


def find_payment_by_key(ctx: Ctx, key: str) -> Result | None:
    """Read Core's exact committed payment result; never reconstruct current debt."""
    outcome, approval = m.operation_outcome, m.approval
    raw = ctx.conn.execute(select(outcome.c.result_json).join(approval, approval.c.id == outcome.c.approval_id).where(
        outcome.c.company_id == ctx.company_id, approval.c.company_id == ctx.company_id,
        outcome.c.key == key, outcome.c.status == "committed", approval.c.contract == "pay_supplier"
    )).scalar_one_or_none()
    if raw is None:
        return None
    stored = json.loads(raw)
    return Result(stored["text"], stored.get("data", {}), stored.get("figures", []),
                  [Amount(**amount) for amount in stored.get("amounts", [])])
