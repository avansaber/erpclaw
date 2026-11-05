"""The stock plugin: warehouses, stock items, the stock ledger and its valuation (moving average and FIFO).

Its interface is the stock contract, version stock/1: services other plugins call inside one of their own
transactions (plugin contract v1). It never posts for another plugin's document: `issue`, `return_in` and
`reverse_movements` return the stock lines and the contribution the document's owner posts, and the comparison they
register runs before the owner's commit ends. The services are never offered to the model. The fixtures in the
engine's `tests/contracts/stock/v1/` are the contract's cases, for the producer and every consumer.
"""
from __future__ import annotations

from datetime import date

from pydantic import model_serializer

from erpclaw_core.contracts import Service, ServiceModel
from erpclaw_core.manifest import PUBLISHER, Capabilities, Events, Manifest, ServiceDecl, Source
from erpclaw_core.plugins import Plugin

from . import service
from .tables import TABLES

# ---------------------------------------------------------------- the contract's models (stock/1)
# Every figure crosses as a canonical decimal string; item and warehouse are ids (the consumer resolves names).
# A quantity is text on the way in, so a bad one is refused by the service with its field and value (bad_quantity),
# never by the model's own validation.


class Omitting(ServiceModel):
    """An output whose optional fields are left out when empty, never sent as null: a choice gives `chosen` or
    `short`, and a line's `card_note` is there only when its cost is zero."""

    @model_serializer(mode="wrap")
    def _omit_empty(self, handler):
        return {k: v for k, v in handler(self).items() if v is not None}


class ItemIn(ServiceModel):
    item: str
    warehouse: str | None = None


class WarehouseOnHand(ServiceModel):
    warehouse: str
    name: str
    qty: str
    value: str


class OnHandOut(ServiceModel):
    warehouses: list[WarehouseOnHand]


class ChooseIn(ServiceModel):
    item: str
    qty: str
    warehouse: str | None = None


class Held(ServiceModel):
    warehouse: str
    qty: str


class ChooseOut(Omitting):
    chosen: Held | None = None
    short: list[Held] | None = None


class IssueLine(ServiceModel):
    item: str
    warehouse: str
    qty: str


class IssueIn(ServiceModel):
    voucher_type: str
    voucher: str
    posting_date: date
    lines: list[IssueLine]


class Cost(Omitting):
    cost: str
    card_note: str | None = None  # a zero cost: "less than 0.01 USD", the currency's smallest unit, for the card


class LedgerLine(ServiceModel):
    account: str
    debit: str
    credit: str


class PreviewIssueOut(ServiceModel):
    lines: list[Cost]
    ledger_lines: list[LedgerLine]
    read_set: list[list[str]]


class IssueOut(ServiceModel):
    lines: list[Cost]
    ledger_lines: list[LedgerLine]
    entry_ids: list[str]


class OriginalLine(ServiceModel):
    voucher: str
    line: int


class ReturnIn(ServiceModel):
    voucher_type: str
    voucher: str
    posting_date: date
    original: OriginalLine
    qty: str


class PreviewReturnOut(ServiceModel):
    cost: str
    ledger_lines: list[LedgerLine]
    read_set: list[list[str]]


class ReturnOut(ServiceModel):
    cost: str
    ledger_lines: list[LedgerLine]
    entry_id: str


class Voucher(ServiceModel):
    voucher_type: str
    voucher: str


class PreviewReverseIn(ServiceModel):
    original: Voucher
    reversal_date: date


class ReverseIn(ServiceModel):
    original: Voucher
    reversal_date: date
    reversal: Voucher


class Mirror(ServiceModel):
    item: str
    warehouse: str
    direction: str
    qty: str
    value: str
    qty_after: str
    value_after: str


class Net(ServiceModel):
    account: str
    net: str


class PreviewReverseOut(ServiceModel):
    mirror_entries: list[Mirror]
    contribution: list[Net]
    read_set: list[list[str]]


class ReverseOut(ServiceModel):
    contribution: list[Net]
    entry_ids: list[str]


# ---------------------------------------------------------------- additive setup models (stock/2); stock/1 above stays frozen

class NoInput(ServiceModel):
    pass


class WarehouseRow(ServiceModel):
    warehouse: str
    name: str
    account: str
    company_default: bool


class WarehousesOut(ServiceModel):
    warehouses: list[WarehouseRow]


class PreviewWarehouseIn(ServiceModel):
    name: str
    account: str | None = None
    company_default: bool = False


class AccountRef(ServiceModel):
    account: str
    number: str
    name: str


class PreviewWarehouseOut(ServiceModel):
    name: str
    account: AccountRef
    company_default: bool
    read_set: list[list[str]]


class WarehouseIn(ServiceModel):
    name: str
    account: str
    company_default: bool = False


class WarehouseOut(ServiceModel):
    warehouse: str
    name: str
    account: str
    company_default: bool


class PreviewStockItemIn(ServiceModel):
    item: str | None = None
    method: str
    default_warehouse: str | None = None


class Named(ServiceModel):
    warehouse: str
    name: str


class PreviewStockItemOut(Omitting):
    method: str
    default_warehouse: Named | None = None
    read_set: list[list[str]]


class StockItemIn(ServiceModel):
    item: str
    method: str
    default_warehouse: str | None = None


class StockItemOut(Omitting):
    item: str
    method: str
    default_warehouse: str | None = None
    kind: str


# ---------------------------------------------------------------- the services

def _rows(read_set) -> list[list[str]]:
    return [list(r) for r in read_set]


def stock_on_hand(ctx, inp: ItemIn) -> dict:
    return service.stock_on_hand(ctx, inp.item, inp.warehouse)


def choose_warehouse(ctx, inp: ChooseIn) -> dict:
    return {k: v for k, v in service.choose_warehouse(ctx, inp.item, inp.qty, inp.warehouse).items()}


def preview_issue(ctx, inp: IssueIn) -> dict:
    out = service.preview_issue(ctx, inp.posting_date, [ln.model_dump() for ln in inp.lines])
    return {**out, "read_set": _rows(out["read_set"])}


def issue(ctx, inp: IssueIn) -> dict:
    return service.issue(ctx, inp.voucher_type, inp.voucher, inp.posting_date, [ln.model_dump() for ln in inp.lines])


def preview_return(ctx, inp: ReturnIn) -> dict:
    out = service.preview_return(ctx, inp.posting_date, inp.original.voucher, inp.original.line, inp.qty)
    return {**out, "read_set": _rows(out["read_set"])}


def return_in(ctx, inp: ReturnIn) -> dict:
    return service.return_in(ctx, inp.voucher_type, inp.voucher, inp.posting_date, inp.original.voucher,
                             inp.original.line, inp.qty)


def preview_reverse(ctx, inp: PreviewReverseIn) -> dict:
    out = service.preview_reverse(ctx, inp.original.voucher_type, inp.original.voucher, inp.reversal_date)
    return {**out, "read_set": _rows(out["read_set"])}


def reverse_movements(ctx, inp: ReverseIn) -> dict:
    return service.reverse_movements(ctx, inp.original.voucher_type, inp.original.voucher, inp.reversal_date,
                                     inp.reversal.voucher_type, inp.reversal.voucher)


def _warehouse_row(row: dict) -> dict:
    return {"warehouse": row["id"], "name": row["name"], "account": row["account_id"],
            "company_default": row["is_default"]}


def list_warehouses(ctx, inp: NoInput) -> dict:
    return {"warehouses": [_warehouse_row(row) for row in service.warehouses(ctx)]}


def preview_create_warehouse(ctx, inp: PreviewWarehouseIn) -> dict:
    return service.preview_create_warehouse(ctx, inp.name, inp.account, default=inp.company_default)


def create_warehouse(ctx, inp: WarehouseIn) -> dict:
    service.authorize(ctx, None, "create_warehouse")
    warehouse = service.create_warehouse(ctx, inp.name, inp.account, default=inp.company_default)
    return _warehouse_row(service.warehouse(ctx, warehouse))


def preview_make_stock_item(ctx, inp: PreviewStockItemIn) -> dict:
    return service.preview_make_stock_item(ctx, inp.item, inp.method, inp.default_warehouse)


def make_stock_item(ctx, inp: StockItemIn) -> dict:
    service.authorize(ctx, None, "make_stock_item")
    service.make_stock_item(ctx, inp.item, inp.method, inp.default_warehouse)
    result = {"item": inp.item, "method": inp.method, "kind": service.company_item(ctx, inp.item)["kind"]}
    if inp.default_warehouse is not None:
        result["default_warehouse"] = inp.default_warehouse
    return result


SERVICES = [
    Service("stock_on_hand", "read", ItemIn, OnHandOut, stock_on_hand),
    Service("choose_warehouse", "read", ChooseIn, ChooseOut, choose_warehouse),
    Service("preview_issue", "read", IssueIn, PreviewIssueOut, preview_issue),
    Service("issue", "write", IssueIn, IssueOut, issue),
    Service("preview_return", "read", ReturnIn, PreviewReturnOut, preview_return),
    Service("return_in", "write", ReturnIn, ReturnOut, return_in),
    Service("preview_reverse", "read", PreviewReverseIn, PreviewReverseOut, preview_reverse),
    Service("reverse_movements", "write", ReverseIn, ReverseOut, reverse_movements),
    Service("list_warehouses", "read", NoInput, WarehousesOut, list_warehouses),
    Service("preview_create_warehouse", "read", PreviewWarehouseIn, PreviewWarehouseOut, preview_create_warehouse),
    Service("create_warehouse", "write", WarehouseIn, WarehouseOut, create_warehouse),
    Service("preview_make_stock_item", "read", PreviewStockItemIn, PreviewStockItemOut, preview_make_stock_item),
    Service("make_stock_item", "write", StockItemIn, StockItemOut, make_stock_item),
]

MANIFEST = Manifest(
    name="stock", version="0.0.1", publisher=PUBLISHER, distribution="erpclaw-stock", licence="GPL-3.0-only",
    source=Source(repository="workspace", commit="workspace"), engine=">=0.0.1", after_core_revision="0013",
    depends={}, conflicts=[], owned_tables=[tbl.name for tbl in TABLES], contracts={}, calls=[],
    events=Events(emits=[], consumes=[]), slots={}, voucher_types=["stock_receipt"],
    capabilities=Capabilities(network=[], files=[]), metrics=[],
    services=[ServiceDecl(name=s.name, effect=s.effect) for s in SERVICES],
)

PLUGIN = Plugin(manifest=MANIFEST, kind="business-module", contracts=[], metrics=[], services=SERVICES)
