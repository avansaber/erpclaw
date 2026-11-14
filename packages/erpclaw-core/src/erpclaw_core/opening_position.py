"""One approved opening posting with bank balances, supplier debts and derived equity."""
from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from pydantic import Field
from sqlalchemy import select

from . import ledger, masters, models as m, payables
from .common import Ctx, Refusal, new_id
from .contracts import Amount, Input, Preview, Result, Shown, WriteContract
from .money import MoneyError, to_amount
from .numbering import next_number

NAME = "record_opening_position"
EQUITY_NAME = "Opening balance (owner's equity)"

# Only commit_opening sets this, after validating its approved input and posting.
# It is execution-local authority for item creation, never request/context data.
_opening_item_commit: ContextVar[Ctx | None] = ContextVar("opening_item_commit", default=None)


class BankLine(Input):
    account: str = Field(min_length=1, max_length=200, description="Bank or cash account named by the owner.")
    amount: str = Field(min_length=1, max_length=80, description="Positive amount as exact decimal text.")


class SupplierLine(Input):
    supplier: str = Field(min_length=1, max_length=200, description="Supplier named by the owner.")
    amount: str = Field(min_length=1, max_length=80, description="Positive debt as exact decimal text.")
    old_reference: str | None = Field(default=None, max_length=200)
    due_date: date | None = None


class StockLine(Input):
    item: str = Field(min_length=1, max_length=200)
    warehouse: str = Field(min_length=1, max_length=140)
    quantity: str
    rate: str


class RecordOpeningPosition(Input):
    stock_lines: list[StockLine] = Field(default_factory=list, max_length=20)
    bank_lines: list[BankLine] = Field(default_factory=list, max_length=5)
    supplier_lines: list[SupplierLine] = Field(default_factory=list, max_length=20)


def _check_once(ctx: Ctx) -> None:
    if ctx.conn.execute(select(m.posting.c.id).where(
        m.posting.c.company_id == ctx.company_id, m.posting.c.voucher_type == ledger.OPENING
    ).limit(1)).first():
        raise Refusal("These books already have an opening position. Use a normal journal for bank or equity corrections.",
                      "opening_already_exists")


def _bank_account(ctx: Ctx, said: str) -> dict:
    if masters._norm(said) == "bank":
        return ledger.account_by_type(ctx, "bank")
    if masters._norm(said) in ("cash", "cash on hand"):
        return ledger.cash_account(ctx)
    rows = [dict(r) for r in ctx.conn.execute(select(m.account).where(
        m.account.c.company_id == ctx.company_id)).mappings()]
    numbered = [a for a in rows if said == a["number"] or masters._norm(said) ==
                masters._norm(f"{a['number']} {a['name']}")]
    return numbered[0] if len(numbered) == 1 else masters._match(rows, said, "account")


@dataclass
class OpeningPlan:
    date: date
    accounts: list[dict]
    lines: list[ledger.Line]
    total: Decimal
    read_set: list[tuple[str, str]]
    suppliers: list[dict]
    owed: Decimal
    equity: Decimal
    stock: dict | None


def _plan(ctx: Ctx, inp: RecordOpeningPosition) -> OpeningPlan:
    if not inp.bank_lines and not inp.supplier_lines and not inp.stock_lines:
        raise Refusal("Tell me at least one starting figure for a bank, cash account or supplier.", "empty_opening")
    year = ctx.conn.execute(select(m.fiscal_year).where(m.fiscal_year.c.company_id == ctx.company_id)
        .order_by(m.fiscal_year.c.start_date, m.fiscal_year.c.id).limit(1)).mappings().first()
    if year is None:
        raise Refusal("These books have no first fiscal year; set up the books first.", "no_fiscal_year")
    when = year["start_date"]
    stock = None
    if inp.stock_lines:
        from .plugins import call_service
        stock = call_service(ctx, "core", "stock.plan_position_stock",
                             {"lines": [row.model_dump() for row in inp.stock_lines]})
    period = ledger.open_period(ctx, when)
    accounts, lines, seen = [], [], set()
    for line in inp.bank_lines:
        try:
            amount = to_amount(line.amount, ctx.currency)
            if amount <= 0:
                raise MoneyError("amount must be positive")
        except MoneyError as exc:
            raise Refusal(f"An amount is not valid: {exc}.", "bad_amount") from None
        account = _bank_account(ctx, line.account)
        if account["id"] in seen:
            raise Refusal(f"{account['name']} appears twice; give one starting figure for it.", "duplicated_part")
        seen.add(account["id"])
        accounts.append(account)
        lines.append(ledger.Line(account["id"], debit=amount))
    for account in accounts:
        if account["account_type"] != "bank":
            raise Refusal(f"{account['name']} is not a bank or cash account.", "account_not_bank")
    if stock:
        for row in stock["lines"]:
            account = ctx.conn.execute(select(m.account).where(m.account.c.company_id == ctx.company_id,
                m.account.c.id == row["account"])).mappings().one()
            accounts.append(dict(account))
            lines.append(ledger.Line(account["id"], debit=Decimal(row["debit"])))
    total = sum((line.debit for line in lines), Decimal(0))
    suppliers = []
    for row in inp.supplier_lines:
        supplier = masters.find_supplier(ctx, row.supplier)
        if supplier["id"] in seen:
            raise Refusal(f"{supplier['name']} appears twice; give one starting figure for it.", "duplicated_part")
        seen.add(supplier["id"])
        amount = payables._amount(ctx, row.amount, "A supplier starting figure")
        payable = payables.payable_account(ctx)
        suppliers.append(supplier)
        accounts.append(payable)
        lines.append(ledger.Line(payable["id"], credit=amount, party_id=supplier["id"]))
    owed = sum((line.credit for line in lines), Decimal(0))
    starting_equity = total - owed
    equity = ledger.account_by_type(ctx, "opening_balance")
    if starting_equity:
        accounts.append(equity)
        lines.append(ledger.Line(equity["id"], credit=max(starting_equity, Decimal(0)),
                                 debit=max(-starting_equity, Decimal(0))))
    # A preview validates the financial shape without granting posting authority.
    ledger.check_post(ctx, when, ledger.OPENING, None, lines, _opening_preview=True)
    rows = {("company", ctx.company_id), ("fiscal_year", year["id"]), ("fiscal_period", period["id"]),
            ("account", equity["id"])}
    rows.update(("account", account["id"]) for account in accounts)
    rows.update(("party", supplier["id"]) for supplier in suppliers)
    if stock:
        rows.update(tuple(row) for row in stock["read_set"])
    # External accounting mappings affect the ledger's destination and must be bound too.
    from .integration import system_of_record

    mode, external = system_of_record(ctx, "accounting")
    if mode == "external":
        rows.update(("account_map", key) for key in ctx.conn.execute(select(m.account_map.c.id).where(
            m.account_map.c.company_id == ctx.company_id, m.account_map.c.external_system == external,
            m.account_map.c.account_id.in_([a["id"] for a in accounts]))).scalars())
    return OpeningPlan(when, accounts, lines, total, sorted(rows), suppliers, owed, starting_equity, stock)


def preview_opening(ctx: Ctx, inp: RecordOpeningPosition) -> Preview:
    # Proposal admission is separate from approval's locked preview comparison.
    # A competing opening is a stored business refusal in commit, not a stale card.
    if not ctx.extra.get("approval_recheck"):
        _check_once(ctx)
    plan = _plan(ctx, inp)
    sh = Shown(ctx.currency)
    sh.title("Record opening position")
    sh.line("Date", f"{plan.date.day} {plan.date:%b %Y} (when these books start)")
    for account, line in zip(plan.accounts[:len(inp.bank_lines)], plan.lines[:len(inp.bank_lines)]):
        sh.line(account["name"], f"{sh.amount(line.debit)} {ctx.currency}")
    if plan.stock:
        for row in plan.stock["details"]:
            sh.line(row["item"], f"{row['quantity']} in {row['warehouse']} at "
                    f"{sh.unit_price(Decimal(row['rate']))} {ctx.currency}: "
                    f"{sh.amount(Decimal(row['value']))} {ctx.currency}")
    for supplier, row in zip(plan.suppliers, inp.supplier_lines):
        value = payables._amount(ctx, row.amount, "A supplier starting figure")
        detail = f"{sh.amount(value)} {ctx.currency}"
        if row.old_reference:
            detail += f"; old reference: {row.old_reference}"
        if row.due_date:
            detail += f"; due {row.due_date.isoformat()}"
        sh.line(supplier["name"], detail)
    sh.line("Total starting value", f"{sh.amount(plan.total)} {ctx.currency}")
    sh.line("Total owed to suppliers", f"{sh.amount(plan.owed)} {ctx.currency}")
    sh.line(EQUITY_NAME, "what the business owns minus what it owes, at the start: "
            f"{sh.amount(plan.equity)} {ctx.currency}")
    for account, line in zip(plan.accounts, plan.lines):
        name = EQUITY_NAME if account["account_type"] == "opening_balance" else account["name"]
        if line.party_id:
            name += " — " + next(s["name"] for s in plan.suppliers if s["id"] == line.party_id)
        side, value = ("credit", line.credit) if line.credit else ("debit", line.debit)
        sh.book(f"{account['number']} {name}: {side} {sh.amount(value)} {ctx.currency}")
    sh.warning("You or your accountant can split this into capital and retained earnings later.")
    if plan.equity < 0:
        sh.warning("The business owes more than it has at the start.")
    sh.footer("The opening position can be posted once. Later bank or equity corrections use a normal journal in an open period. For a wrong supplier amount, pay or reverse against the actual items, or ask your accountant; do not journal the payable account.")
    card = sh.card()
    return Preview(card, hashlib.sha256(json.dumps(card.as_dict(), sort_keys=True).encode()).hexdigest())


def opening_read_set(ctx: Ctx, inp: RecordOpeningPosition) -> list[tuple[str, str]]:
    return _plan(ctx, inp).read_set


def _batch_number(ctx: Ctx) -> str:
    """Called by approval before the chain lock, inside the opening effect savepoint."""
    when = ctx.conn.execute(select(m.fiscal_year.c.start_date).where(
        m.fiscal_year.c.company_id == ctx.company_id).order_by(
        m.fiscal_year.c.start_date, m.fiscal_year.c.id).limit(1)).scalar_one()
    return next_number(ctx, "OP", when)


def commit_opening(ctx: Ctx, inp: RecordOpeningPosition, key: str) -> Result:
    from . import approvals

    approval_id = approvals.committing(ctx)
    row = ctx.conn.execute(select(m.approval).where(m.approval.c.id == approval_id)).mappings().first() if approval_id else None
    if (row is None or row["status"] != "consumed" or row["company_id"] != ctx.company_id
            or row["contract"] != NAME or row["operation_key"] != key
            or RecordOpeningPosition.model_validate_json(row["args_json"]) != inp):
        raise Refusal("An opening position needs its own approved card.", "not_approved")
    ledger.lock_chain(ctx)
    _check_once(ctx)
    plan = _plan(ctx, inp)
    voucher_id = new_id()
    number = ctx.extra.get("opening_batch_number")
    if not number:
        raise Refusal("An opening position needs its approved batch number.", "not_approved")
    posting = ledger.post(ctx, plan.date, ledger.OPENING, voucher_id, plan.lines, memo=number, operation_key=key)
    if plan.stock:
        from .plugins import call_service
        call_service(ctx, "core", "stock.write_position_stock", {
            "lines": [row.model_dump() for row in inp.stock_lines],
            "voucher_id": voucher_id, "number": number})
    token = _opening_item_commit.set(ctx)
    try:
        for sequence, (supplier, row) in enumerate(zip(plan.suppliers, inp.supplier_lines), 1):
            control_no = len(inp.bank_lines) + len(inp.stock_lines) + sequence
            line = payables.open_item(ctx, supplier_id=supplier["id"], source_type=ledger.OPENING,
                source_id=new_id(), source_number=number, posting_date=plan.date, amount=row.amount,
                posting_id=posting.posting_id, control_line_no=control_no, source_voucher_id=voucher_id,
                opening_sequence=sequence, old_reference=row.old_reference, due_date=row.due_date)
            if line != plan.lines[control_no - 1]:
                raise Refusal("The supplier starting figure changed; nothing was saved.", "conflict")
    finally:
        _opening_item_commit.reset(token)
    sh = Shown(ctx.currency)
    amount = sh.amount(plan.total, "value")
    owed = sh.amount(plan.owed, "value")
    equity = sh.amount(plan.equity, "value")
    return Result(f"Recorded the opening position dated {plan.date.day} {plan.date:%b %Y}: "
                  f"total starting value {amount} {ctx.currency}, total owed to suppliers {owed} {ctx.currency}, "
                  f"{EQUITY_NAME.lower()} {equity} {ctx.currency}.",
                  {"voucher_type": ledger.OPENING, "voucher_id": voucher_id, "posting_id": posting.posting_id,
                   "posting_date": plan.date.isoformat(), "number": number}, [amount, owed, equity], sh.amounts)


def find_opening(ctx: Ctx, key: str) -> Result | None:
    raw = ctx.conn.execute(select(m.operation_outcome.c.result_json).join(
        m.approval, m.approval.c.id == m.operation_outcome.c.approval_id).where(
        m.operation_outcome.c.company_id == ctx.company_id, m.operation_outcome.c.key == key,
        m.operation_outcome.c.status == "committed", m.approval.c.contract == NAME)).scalar_one_or_none()
    if raw is None:
        return None
    result = json.loads(raw)
    return Result(result["text"], result.get("data", {}), result.get("figures", []),
                  [Amount(**a) for a in result.get("amounts", [])])


CONTRACT = WriteContract(NAME, "core", "write", "Record starting bank, cash and supplier balances with derived owner's equity.",
    RecordOpeningPosition, preview=preview_opening, commit=commit_opening, read_set=opening_read_set,
    find_by_operation_key=find_opening, moves_money=True, amounts=("value",),
    planned="Core", plain="record an opening position", planned_name="recording an opening position")
