"""A planted consumer of the stock contract, for the commit-path tests (test_stock_commits.py, test_stock_locks.py): a card that sells stock (receivable, sales,
and the stock service's cost and inventory lines in one posting), and one that cancels such a sale by reversal. Its
`tamper` switch makes the posting differ from what the stock service returned, in each way the pre-commit comparison
must catch."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from erpclaw_core import ledger
from erpclaw_core.common import Ctx, Refusal, new_id
from erpclaw_core.contracts import Contract, Input, Preview, Result, Shown
from erpclaw_core.manifest import Capabilities, Events, Manifest, PUBLISHER, Source
from erpclaw_core.plugins import Plugin, call_service

NAME = "stocksale"
USES = ("stock_on_hand", "choose_warehouse", "preview_issue", "issue", "preview_return", "return_in",
        "preview_reverse", "reverse_movements")
Tamper = Literal["none", "drop_cost", "off_by_cent", "extra_inventory", "post_stock_twice"]


class Sale(Input):
    customer_id: str
    item_id: str
    warehouse_id: str
    qty: str
    price: str
    voucher: str
    posting_date: date
    tamper: Tamper = "none"
    declare_heads: bool = True  # False: a consumer whose read set leaves out the stock it read


class Cancel(Input):
    voucher: str
    reversal_date: date
    tamper: Tamper = "none"


def _preview_issue(ctx: Ctx, inp: Sale) -> dict:
    return call_service(ctx, NAME, "stock.preview_issue", {
        "voucher_type": "planted_sale", "voucher": inp.voucher, "posting_date": inp.posting_date.isoformat(),
        "lines": [{"item": inp.item_id, "warehouse": inp.warehouse_id, "qty": inp.qty}]})


def cost_words(line: dict, currency: str) -> str:
    """A cost on a card, with the stock service's note when it is zero: never shown as nothing."""
    return f"{line['cost']} {currency}" + (f" ({line['card_note']})" if "card_note" in line else "")


def preview_sale(ctx: Ctx, inp: Sale) -> Preview:
    out = _preview_issue(ctx, inp)
    sh = Shown(ctx.currency)
    sh.title("Sell stock")
    sh.line("Quantity", inp.qty)
    sh.line("Cost of goods sold", cost_words(out["lines"][0], ctx.currency))
    return Preview(sh.card(), new_id())


def read_set_sale(ctx: Ctx, inp: Sale) -> list[tuple[str, str]]:
    heads = [(tbl, key) for tbl, key in _preview_issue(ctx, inp)["read_set"]] if inp.declare_heads else []
    return [("company", ctx.company_id), *heads]


def _lines(lines: list[dict]) -> list[ledger.Line]:
    return [ledger.Line(ln["account"], debit=Decimal(ln["debit"]), credit=Decimal(ln["credit"])) for ln in lines]


def commit_sale(ctx: Ctx, inp: Sale, key: str) -> Result:
    out = call_service(ctx, NAME, "stock.issue", {
        "voucher_type": "planted_sale", "voucher": inp.voucher, "posting_date": inp.posting_date.isoformat(),
        "lines": [{"item": inp.item_id, "warehouse": inp.warehouse_id, "qty": inp.qty}]})
    receivable = ledger.account_by_type(ctx, "receivable")["id"]
    sales = ledger.account_by_type(ctx, "income")["id"]
    price = Decimal(inp.price)  # zero: a delivery, which owes nothing and posts only its stock lines
    lines = [ledger.Line(receivable, debit=price, party_id=inp.customer_id), ledger.Line(sales, credit=price)] \
        if price else []
    stock_lines = _lines(out["ledger_lines"])
    if inp.tamper == "drop_cost":
        stock_lines = [ln for ln in stock_lines if ln.debit == 0]  # the cost line gone; rebalanced on sales
        lines[1] = ledger.Line(sales, credit=price - sum((ln.credit for ln in stock_lines), Decimal(0)))
    elif inp.tamper == "off_by_cent":
        cogs, inv = stock_lines
        stock_lines = [ledger.Line(cogs.account_id, debit=cogs.debit + Decimal("0.01")),
                       ledger.Line(inv.account_id, credit=inv.credit + Decimal("0.01"))]
    elif inp.tamper == "extra_inventory":
        inv = stock_lines[-1]
        stock_lines = stock_lines + [ledger.Line(inv.account_id, debit=Decimal("1.00")),
                                     ledger.Line(sales, credit=Decimal("1.00"))]
    if lines + stock_lines:  # a voucher with no lines at all makes no posting
        ledger.post(ctx, inp.posting_date, "planted_sale", inp.voucher, lines + stock_lines, operation_key=key)
    return Result("Sold.", {"voucher": inp.voucher})


def preview_cancel(ctx: Ctx, inp: Cancel) -> Preview:
    call_service(ctx, NAME, "stock.preview_reverse", {
        "original": {"voucher_type": "planted_sale", "voucher": inp.voucher},
        "reversal_date": inp.reversal_date.isoformat()})
    sh = Shown(ctx.currency)
    sh.title("Cancel the sale")
    return Preview(sh.card(), new_id())


def read_set_cancel(ctx: Ctx, inp: Cancel) -> list[tuple[str, str]]:
    out = call_service(ctx, NAME, "stock.preview_reverse", {
        "original": {"voucher_type": "planted_sale", "voucher": inp.voucher},
        "reversal_date": inp.reversal_date.isoformat()})
    return [("company", ctx.company_id), *[(tbl, key) for tbl, key in out["read_set"]]]


def commit_cancel(ctx: Ctx, inp: Cancel, key: str) -> Result:
    call_service(ctx, NAME, "stock.reverse_movements", {
        "original": {"voucher_type": "planted_sale", "voucher": inp.voucher},
        "reversal_date": inp.reversal_date.isoformat(),
        "reversal": {"voucher_type": "planted_sale_reversal", "voucher": f"{inp.voucher}-rev"}})
    posting = ledger.posting_for_voucher(ctx, "planted_sale", inp.voucher)
    ledger.reverse(ctx, posting["id"], inp.reversal_date)
    if inp.tamper == "post_stock_twice":  # the stock lines posted again under the reversal's own voucher
        lines = [ln for ln in _lines_of(ctx, posting["id"]) if ln.party_id is None]
        stock = [ledger.Line(ln.account_id, debit=ln.credit, credit=ln.debit) for ln in lines
                 if ln.account_id in _stock_accounts(ctx)]
        if stock:
            ledger.post(ctx, inp.reversal_date, "planted_sale_reversal", f"{inp.voucher}-rev", stock)
    return Result("Cancelled.", {"voucher": inp.voucher})


def _lines_of(ctx: Ctx, posting_id: str) -> list[ledger.Line]:
    from sqlalchemy import select

    le = ledger.m.ledger_entry
    return [ledger.Line(r["account_id"], debit=r["debit"], credit=r["credit"], party_id=r["party_id"])
            for r in ctx.conn.execute(select(le).where(le.c.posting_id == posting_id)).mappings()]


def _stock_accounts(ctx: Ctx) -> set[str]:
    from erpclaw_stock.service import stock_role_accounts

    return stock_role_accounts(ctx)


def _by_key(ctx: Ctx, key: str) -> Result | None:
    return None


CONTRACTS = [
    Contract("stock_sale", NAME, "write", "Sell stock (a planted consumer).", Sale, preview=preview_sale,
             commit=commit_sale, find_by_operation_key=_by_key, read_set=read_set_sale),
    Contract("stock_sale_cancel", NAME, "write", "Cancel a planted stock sale.", Cancel, preview=preview_cancel,
             commit=commit_cancel, find_by_operation_key=_by_key, read_set=read_set_cancel),
]


def plugin(declares: Literal["plain", "optional", "none"] = "plain") -> Plugin:
    """The planted consumer, declaring the stock services as plain uses (with a dependency on stock), as optional
    uses ("stock.<service>?", no dependency), or not at all."""
    uses = [] if declares == "none" else [f"stock.{s}{'?' if declares == 'optional' else ''}" for s in USES]
    manifest = Manifest(name=NAME, version="1.0.0", publisher=PUBLISHER, distribution=f"erpclaw-{NAME}",
                        licence="GPL-3.0-only", source=Source(repository="workspace", commit="workspace"),
                        engine=">=0.0.1", after_core_revision="0004",
                        depends={"stock": ">=0.0.1"} if declares == "plain" else {}, conflicts=[],
                        owned_tables=[], contracts={c.name: c.effect for c in CONTRACTS}, calls=[],
                        events=Events(emits=[], consumes=[]), slots={},
                        voucher_types=["planted_sale", "planted_sale_reversal"],
                        capabilities=Capabilities(network=[], files=[]), metrics=[],
                        uses=uses)
    return Plugin(manifest=manifest, kind="business-module", contracts=list(CONTRACTS), artifact_digest="0" * 64)


__all__ = ["plugin", "Refusal"]
