"""The stock service: warehouses and stock items, every movement's rules, its valuation, its rows and the ledger lines
its owner posts. Other plugins reach it only through the stock contract's services (`__init__.py`); this module is
their implementation and the setup functions a company's stock starts from.

Every movement checks, in order, stopping at the first and writing nothing: the item is a stock item of the company
and the warehouse the company's; the quantity is an exact decimal, positive, of at most four places; the date is in an
open period and not before the pair's last movement; an issue never takes more than is on hand; a return comes back
against its issue, at most what is left to return; the method is one of the two; each account named has its role.
The stock plugin never posts for another plugin's document: it returns the lines, and the document's owner posts them
in its single posting, which the pre-commit comparison checks before the commit ends.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select

from erpclaw_core import ledger, masters
from erpclaw_core import models as m
from erpclaw_core.common import Ctx, Refusal, new_id, now
from erpclaw_core.money import MINOR_UNITS, canonical, fmt_qty, quantize

from . import tables as t
from .setup_names import warehouse_name
from .valuation import Group, fifo_take, moving_average_cost, receipt_value, return_cost

ZERO = Decimal(0)
QTY_PLACES = 4
POINTS_TO_CREDIT_NOTE = "a credit note (return_in)"


# ---------------------------------------------------------------- figures

def qty_text(qty: Decimal) -> str:
    return fmt_qty(qty)


def money_text(value: Decimal, currency: str) -> str:
    return canonical(quantize(value, currency), currency)


def smallest_unit(currency: str) -> str:
    """The currency's smallest unit in words: "0.01 USD", "1 JPY", "0.001 KWD"."""
    places = MINOR_UNITS.get(currency, 2)
    return f"{Decimal(1).scaleb(-places):f} {currency}"


QTY_DIGITS = 14  # whole digits a quantity may have: with its four places, well inside any Decimal context
QTY_TEXT = re.compile(r"^(0|[1-9][0-9]*)(?:\.([0-9]+))?$")


def parse_qty(value: object, field: str = "qty") -> Decimal:
    """An exact decimal written plainly (digits, an optional point and decimals), positive, of at most fourteen whole
    digits and four places; trailing zeros after the point are allowed. It is read from its text alone, so no
    Decimal context can round it. Anything else is refused with the field and the value: floats (never exact),
    exponents, separators, signs, NaN and infinity included."""
    shown = value if isinstance(value, str) else repr(value)

    def refuse(why: str) -> Refusal:
        return Refusal(f"The {field} {shown} {why}.", "bad_quantity", field=field, value=shown)

    if isinstance(value, bool) or isinstance(value, float):
        raise refuse("is not an exact quantity")
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise refuse("is not a number")
        text = format(value, "f")
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise refuse("is not an exact quantity")
    match = QTY_TEXT.match(text)
    if match is None:
        raise refuse("is not a plain positive number")
    whole, places = match.group(1), (match.group(2) or "").rstrip("0")
    if len(whole) > QTY_DIGITS:
        raise refuse(f"has more than {QTY_DIGITS} whole digits")
    if len(places) > QTY_PLACES:
        raise refuse(f"has more than {QTY_PLACES} decimal places")
    qty = Decimal(f"{whole}.{places}" if places else whole)
    if qty <= 0:
        raise refuse("must be more than zero")
    return qty


# ---------------------------------------------------------------- masters

def warehouse(ctx: Ctx, warehouse_id: str) -> dict:
    row = ctx.conn.execute(select(t.warehouse).where(t.warehouse.c.company_id == ctx.company_id,
                                                     t.warehouse.c.id == warehouse_id)).mappings().first()
    if row is None:
        raise Refusal(f"{ctx.company_name} has no such warehouse.", "not_company_warehouse", warehouse=warehouse_id)
    return dict(row)


def warehouses(ctx: Ctx) -> list[dict]:
    """The company's warehouses in the order they were made."""
    return [dict(r) for r in ctx.conn.execute(select(t.warehouse).where(
        t.warehouse.c.company_id == ctx.company_id).order_by(t.warehouse.c.position)).mappings()]


def stock_item(ctx: Ctx, item_id: str) -> dict:
    """The item as a stock item of this company, its method checked."""
    row = ctx.conn.execute(select(t.stock_item).where(t.stock_item.c.company_id == ctx.company_id,
                                                      t.stock_item.c.item_id == item_id)).mappings().first()
    if row is None:
        raise Refusal(f"That item is not kept in stock by {ctx.company_name}.", "not_stock_item", item=item_id)
    if row["method"] not in t.METHODS:  # the table's check holds; a restored or edited row is refused, never defaulted
        raise Refusal(f"The item's valuation method '{row['method']}' is not one of {', '.join(t.METHODS)}.",
                      "bad_valuation_method", item=item_id)
    return dict(row)


def _inventory_account_row(ctx: Ctx, account_id: str, name: str) -> dict:
    row = ctx.conn.execute(select(m.account).where(m.account.c.company_id == ctx.company_id,
                                                   m.account.c.id == account_id)).mappings().first()
    if row is None or row["account_type"] != "stock" or row["is_group"]:
        raise Refusal(f"The warehouse {name}'s account is not an inventory account of {ctx.company_name}.",
                      "not_inventory_account", account=account_id)
    if row["frozen"]:
        raise Refusal(f"{row['number']} {row['name']} is frozen.", "frozen_account")
    return dict(row)


def _check_inventory_account(ctx: Ctx, account_id: str, warehouse_name: str, warehouse_id: str | None) -> str:
    row = ctx.conn.execute(select(m.account).where(m.account.c.company_id == ctx.company_id,
                                                   m.account.c.id == account_id)).mappings().first()
    if row is None or row["account_type"] != "stock" or row["is_group"]:
        raise Refusal(f"The warehouse {warehouse_name}'s account is not an inventory account of {ctx.company_name}.",
                      "not_inventory_account", warehouse=warehouse_id)
    return row["id"]


def inventory_account(ctx: Ctx, wh: dict) -> str:
    """The warehouse's inventory account, checked to hold the stock role in this company."""
    return _check_inventory_account(ctx, wh["account_id"], wh["name"], wh["id"])


def role_account(ctx: Ctx, role: str) -> str:
    return ledger.account_by_type(ctx, role)["id"]


def _warehouse_setup(ctx: Ctx, name: str, account_id: str | None, default: bool) -> tuple[str, dict]:
    """The preview and internal write share the ordered setup rules, before any insert."""
    name = warehouse_name(name)
    existing = warehouses(ctx)
    same = next((w for w in existing if masters.normalise_name(w["name"]) == masters.normalise_name(name)), None)
    if same:
        raise Refusal(f"{ctx.company_name} already has a warehouse called {same['name']}.",
                      "warehouse_exists", warehouse=same["id"], name=same["name"])
    if account_id is None:
        rows = ctx.conn.execute(select(m.account).where(
            m.account.c.company_id == ctx.company_id, m.account.c.account_type == "stock",
            m.account.c.is_group.is_(False), m.account.c.frozen.is_(False)).order_by(
            m.account.c.number)).mappings().all()
        if len(rows) > 1:
            options = [f"{row['number']} {row['name']}" for row in rows]
            raise Refusal(f"Which inventory account should {name} use: {'; '.join(options)}?", "account_ambiguous",
                          options=options, field="account",
                          choices=[{"label": label, "ref": row["number"]} for label, row in zip(options, rows)])
        account = ledger.account_by_type(ctx, "stock")
    else:
        account = _inventory_account_row(ctx, account_id, name)
    previous = next((w for w in existing if w["is_default"]), None)
    if default and previous:
        raise Refusal(f"{ctx.company_name}'s default warehouse is already {previous['name']}.",
                      "default_warehouse_exists", warehouse=previous["id"])
    return name, account


def preview_create_warehouse(ctx: Ctx, name: str, account_id: str | None = None, *, default: bool = False) -> dict:
    name, account = _warehouse_setup(ctx, name, account_id, default)
    return {"name": name, "account": {"account": account["id"], "number": account["number"], "name": account["name"]},
            "company_default": default, "read_set": [["account", account["id"]]]}


def create_warehouse(ctx: Ctx, name: str, account_id: str | None = None, *, default: bool = False) -> str:
    """Internal setup keeps its signature; service writes require the exact account shown on the card."""
    name, account = _warehouse_setup(ctx, name, account_id, default)
    position = (ctx.conn.execute(select(func.max(t.warehouse.c.position)).where(
        t.warehouse.c.company_id == ctx.company_id)).scalar() or 0) + 1
    wid = new_id()
    ctx.conn.execute(t.warehouse.insert().values(id=wid, company_id=ctx.company_id, name=name, account_id=account["id"],
                                                 is_default=default, position=position, created_at=now()))
    return wid


def company_item(ctx: Ctx, item_id: str) -> dict:
    row = ctx.conn.execute(select(m.item).where(m.item.c.company_id == ctx.company_id,
                                               m.item.c.id == item_id)).mappings().first()
    if row is None:
        raise Refusal(f"{ctx.company_name} has no such item.", "not_company_item", item=item_id)
    return dict(row)


def _stock_item_setup(ctx: Ctx, item_id: str | None, method: str, default_warehouse: str | None) -> tuple:
    if method not in t.METHODS:
        raise Refusal(f"The valuation method '{method}' is not one of {', '.join(t.METHODS)}.", "bad_valuation_method",
                      item=item_id, method=method)
    item = company_item(ctx, item_id) if item_id is not None else None
    if item is not None:
        existing = ctx.conn.execute(select(t.stock_item).where(
            t.stock_item.c.company_id == ctx.company_id, t.stock_item.c.item_id == item_id)).mappings().first()
        if existing is not None:
            label = "FIFO" if existing["method"] == "fifo" else "moving average"
            message = f"{item['name']} is already kept in stock, valued by {label}"
            if method != existing["method"]:
                raise Refusal(message + "; a stock item's valuation method never changes.", "valuation_method_fixed",
                              item=item_id, method=existing["method"])
            extra = {"default_warehouse": existing["default_warehouse_id"]} if existing["default_warehouse_id"] else {}
            raise Refusal(message + ".", "already_stock_item", item=item_id, method=existing["method"], **extra)
    default = warehouse(ctx, default_warehouse) if default_warehouse is not None else None
    return item, default


def preview_make_stock_item(ctx: Ctx, item_id: str | None, method: str,
                            default_warehouse: str | None = None) -> dict:
    item, default = _stock_item_setup(ctx, item_id, method, default_warehouse)
    result = {"method": method, "read_set": [["item", item["id"]]] if item is not None else []}
    if default is not None:
        result["default_warehouse"] = {"warehouse": default["id"], "name": default["name"]}
    return result


def make_stock_item(ctx: Ctx, item_id: str, method: str, default_warehouse: str | None = None) -> str:
    item, _ = _stock_item_setup(ctx, item_id, method, default_warehouse)
    sid = new_id()
    ctx.conn.execute(t.stock_item.insert().values(id=sid, company_id=ctx.company_id, item_id=item_id, method=method,
                                                  default_warehouse_id=default_warehouse, created_at=now()))
    if item["kind"] == "service":
        masters.mark_stock_item(ctx, item_id)
    return sid


# ---------------------------------------------------------------- heads and FIFO groups

def head(ctx: Ctx, item_id: str, warehouse_id: str, *, lock: bool = False) -> dict | None:
    q = select(t.head).where(t.head.c.company_id == ctx.company_id, t.head.c.item_id == item_id,
                             t.head.c.warehouse_id == warehouse_id)
    row = ctx.conn.execute(ledger.for_update(ctx, q) if lock else q).mappings().first()
    return None if row is None else dict(row)


def on_hand(ctx: Ctx, item_id: str, warehouse_id: str) -> tuple[Decimal, Decimal]:
    h = head(ctx, item_id, warehouse_id)
    return (ZERO, ZERO) if h is None else (h["qty"], h["value"])


def fifo_groups(ctx: Ctx, item_id: str, warehouse_id: str) -> list[Group]:
    """The pair's FIFO groups, oldest first, each with what it still holds: its layers less its takes plus its
    give-backs. Nothing is stored twice."""
    layers = ctx.conn.execute(select(t.layer, t.entry.c.seq).join(t.entry, t.entry.c.id == t.layer.c.entry_id).where(
        t.layer.c.company_id == ctx.company_id, t.layer.c.item_id == item_id,
        t.layer.c.warehouse_id == warehouse_id).order_by(t.entry.c.seq)).mappings().all()
    held: dict[str, list[Decimal]] = {}
    for r in layers:
        held.setdefault(r["group_id"], [ZERO, ZERO])
        held[r["group_id"]][0] += r["qty"]
        held[r["group_id"]][1] += r["value"]
    if held:
        for r in ctx.conn.execute(select(t.take).where(t.take.c.company_id == ctx.company_id,
                                                       t.take.c.group_id.in_(list(held)))).mappings():
            sign = -1 if r["kind"] == "take" else 1
            held[r["group_id"]][0] += sign * r["qty"]
            held[r["group_id"]][1] += sign * r["value"]
    return [Group(gid, qty, value) for gid, (qty, value) in held.items()]


# ---------------------------------------------------------------- the rules

def check_date(ctx: Ctx, posting_date: date, h: dict | None) -> None:
    ledger.open_period(ctx, posting_date)
    if h is not None and posting_date < h["last_date"]:
        raise Refusal(f"Stock of that item last moved in that warehouse on {h['last_date'].isoformat()}; a movement "
                      f"cannot be dated before it.", "backdated", last_movement=h["last_date"].isoformat())


def _short(ctx: Ctx, item_id: str, warehouse_id: str, qty: Decimal, have: Decimal) -> Refusal:
    elsewhere = [f"{w['name']}: {qty_text(on_hand(ctx, item_id, w['id'])[0])}" for w in warehouses(ctx)
                 if w["id"] != warehouse_id and on_hand(ctx, item_id, w["id"])[0] > 0]
    where = warehouse(ctx, warehouse_id)["name"]
    more = f" Elsewhere: {'; '.join(elsewhere)}." if elsewhere else " No other warehouse holds it."
    return Refusal(f"{where} holds {qty_text(have)} of that item, less than {qty_text(qty)}.{more}",
                   "insufficient_stock", warehouse=warehouse_id, on_hand=qty_text(have))


@dataclass
class Pair:
    """One (item, warehouse) as a voucher moves it: the head before, and what the voucher's movements so far left."""
    item: dict
    wh: dict
    account: str
    head: dict | None
    qty: Decimal
    value: Decimal
    groups: list[Group] = field(default_factory=list)


def _pair(ctx: Ctx, pairs: dict, item_id: str, warehouse_id: str, posting_date: date, *, lock: bool,
          dated: bool = True) -> Pair:
    key = (item_id, warehouse_id)
    if key not in pairs:
        item, wh = stock_item(ctx, item_id), warehouse(ctx, warehouse_id)  # validate the item, its kind and the warehouse
        h = head(ctx, item_id, warehouse_id, lock=lock)
        pairs[key] = Pair(item, wh, inventory_account(ctx, wh), h, *((h["qty"], h["value"]) if h else (ZERO, ZERO)),
                          fifo_groups(ctx, item_id, warehouse_id) if item["method"] == "fifo" else [])
    p = pairs[key]
    if dated:
        check_date(ctx, posting_date, p.head)  # validate the period and the pair's movement date
    return p


@dataclass
class Move:
    """One stock row a movement will write, with what it does to its pair's FIFO groups."""
    pair: Pair
    direction: str
    qty: Decimal
    value: Decimal
    line_no: int
    takes: list[tuple[str, Decimal, Decimal]] = field(default_factory=list)  # (group, qty, value) taken
    give_backs: list[tuple[str, Decimal, Decimal]] = field(default_factory=list)
    layer: bool = False  # an incoming FIFO entry: a new layer, its own group (or merged into `merge_into`)
    merge_into: str | None = None
    take_layer: str | None = None  # a reversed FIFO receipt or return: the layer it subtracts
    against: str | None = None
    reverses: str | None = None
    qty_after: Decimal = ZERO
    value_after: Decimal = ZERO
    original: dict | None = None  # reversal contribution comes from this entry, never its voucher type


def _apply(p: Pair, mv: Move) -> None:
    """Move the pair's running state by one movement, so the next line of the same voucher sees it."""
    sign = 1 if mv.direction == "in" else -1
    p.qty += sign * mv.qty
    p.value += sign * mv.value
    mv.qty_after, mv.value_after = p.qty, p.value
    if p.item["method"] != "fifo":
        return
    out = {g: (q, v) for g, q, v in mv.takes}
    back = {g: (q, v) for g, q, v in mv.give_backs}
    p.groups = [Group(g.id, g.qty - out.get(g.id, (ZERO, ZERO))[0] + back.get(g.id, (ZERO, ZERO))[0],
                      g.value - out.get(g.id, (ZERO, ZERO))[1] + back.get(g.id, (ZERO, ZERO))[1]) for g in p.groups]
    if mv.layer and mv.merge_into is None:
        p.groups.append(Group(f"new:{len(p.groups)}", mv.qty, mv.value))
    elif mv.layer:
        p.groups = [Group(g.id, g.qty + mv.qty, g.value + mv.value) if g.id == mv.merge_into else g for g in p.groups]


def _issue_moves(ctx: Ctx, lines: list[dict], posting_date: date, *, lock: bool, first_line: int = 1) -> list[Move]:
    pairs: dict = {}
    moves = []
    for n, line in enumerate(lines, first_line):
        qty = parse_qty(line.get("qty"))  # validate the positive quantity
        p = _pair(ctx, pairs, line["item"], line["warehouse"], posting_date, lock=lock)
        if qty > p.qty:  # never negative stock
            raise _short(ctx, line["item"], line["warehouse"], qty, p.qty)
        if p.item["method"] == "fifo":
            takes = fifo_take(p.groups, qty, ctx.currency)
            mv = Move(p, "out", qty, sum((v for _, _, v in takes), ZERO), n, takes=takes)
        else:
            mv = Move(p, "out", qty, moving_average_cost(p.qty, p.value, qty, ctx.currency), n)
        _apply(p, mv)
        moves.append(mv)
    return moves


def _merge_target(p: Pair, qty: Decimal, value: Decimal) -> str | None:
    """A FIFO receipt joins the last open group when its rate equals that group's (compared exactly)."""
    open_groups = [g for g in p.groups if g.qty > 0]
    if not open_groups:
        return None
    last = open_groups[-1]
    return last.id if value * last.qty == last.value * qty and not last.id.startswith("new:") else None


# ---------------------------------------------------------------- ledger lines and the pre-commit comparison

def ledger_lines(ctx: Ctx, moves: list[Move], *, srnb: bool = False) -> list[dict]:
    """One line per account: cost of goods sold first, then the inventory accounts, then (a receipt) stock received
    not billed; a zero amount gives no line. Amounts at the company currency's scale."""
    cogs = inv = None
    net: dict[str, Decimal] = {}
    for mv in moves:
        if mv.value == 0:
            continue
        sign = 1 if mv.direction == "in" else -1
        net[mv.pair.account] = net.get(mv.pair.account, ZERO) + sign * mv.value
        if srnb:
            inv = inv or role_account(ctx, "stock_received_not_billed")
            net[inv] = net.get(inv, ZERO) - sign * mv.value
        else:
            cogs = cogs or role_account(ctx, "cost_of_goods_sold")
            net[cogs] = net.get(cogs, ZERO) - sign * mv.value
    order = ([cogs] if cogs else []) + [a for a in net if a not in (cogs, inv)] + ([inv] if inv else [])
    out = []
    for a in order:
        if net.get(a, ZERO) != 0:
            amount = abs(net[a])
            out.append({"account": a, "debit": money_text(amount if net[a] > 0 else ZERO, ctx.currency),
                        "credit": money_text(amount if net[a] < 0 else ZERO, ctx.currency)})
    return out


def lines_net(lines: list[dict]) -> dict[str, Decimal]:
    net: dict[str, Decimal] = {}
    for ln in lines:
        net[ln["account"]] = net.get(ln["account"], ZERO) + Decimal(ln["debit"]) - Decimal(ln["credit"])
    return net


def stock_role_accounts(ctx: Ctx) -> set[str]:
    """The accounts the comparison owns: every warehouse's inventory account and every cost-of-goods-sold account."""
    accounts = {w["account_id"] for w in warehouses(ctx)}
    accounts |= set(ctx.conn.execute(select(m.account.c.id).where(
        m.account.c.company_id == ctx.company_id, m.account.c.account_type == "cost_of_goods_sold")).scalars())
    return accounts


def _posting_net(ctx: Ctx, posting_id: str | None) -> dict[str, Decimal]:
    if posting_id is None:
        return {}
    net: dict[str, Decimal] = {}
    le = m.ledger_entry
    for r in ctx.conn.execute(select(le.c.account_id, le.c.debit, le.c.credit).where(le.c.posting_id == posting_id)):
        net[r.account_id] = net.get(r.account_id, ZERO) + r.debit - r.credit
    return net


def _posting_id(ctx: Ctx, voucher_type: str, voucher_id: str) -> str | None:
    p = m.posting
    return ctx.conn.execute(select(p.c.id).where(p.c.company_id == ctx.company_id, p.c.voucher_type == voucher_type,
                                                 p.c.voucher_id == voucher_id, p.c.reverses_posting_id.is_(None))).scalar()


def _reversal_posting_id(ctx: Ctx, voucher_type: str, voucher_id: str) -> str | None:
    original = _posting_id(ctx, voucher_type, voucher_id)
    if original is None:
        return None
    return ctx.conn.execute(select(m.posting.c.id).where(m.posting.c.reverses_posting_id == original)).scalar()


def contribute(ctx: Ctx, target: tuple[str, ...], net: dict[str, Decimal], trusted: Trusted | None = None) -> None:
    """Add a write service's stock contribution to its voucher's, and register the voucher's comparison: before the
    commit ends, the posting's net on every stock-role account and every account contributed must equal it. `target`
    is ("voucher", type, id) for an issue or a return; ("reversal", type, id, reversal type, reversal id) for the
    reversal of that voucher, whose postings are the original's reversal (`ledger.reverse`) and any posting the
    caller made under the reversal's own voucher, so stock lines posted again there are caught."""
    totals = ctx.extra.setdefault("stock_contribution", {}).setdefault(target, {})
    for a, v in net.items():
        totals[a] = totals.get(a, ZERO) + v

    def compare(ctx: Ctx) -> None:
        kind, vtype, vid = target[:3]
        if kind == "voucher":
            postings = [_posting_id(ctx, vtype, vid)]
        else:
            postings = [_reversal_posting_id(ctx, vtype, vid), _posting_id(ctx, target[3], target[4])]
        posted: dict[str, Decimal] = {}
        for pid in postings:
            for a, v in _posting_net(ctx, pid).items():
                posted[a] = posted.get(a, ZERO) + v
        expected = ctx.extra["stock_contribution"][target]
        for a in stock_role_accounts(ctx) | set(expected):
            if posted.get(a, ZERO) != expected.get(a, ZERO):
                raise Refusal("The document's posting does not carry the stock lines the stock service computed for "
                              "it, so nothing was saved.", "stock_posting_mismatch", account=a,
                              posted=money_text(posted.get(a, ZERO), ctx.currency),
                              expected=money_text(expected.get(a, ZERO), ctx.currency))

    key = "stock:" + ":".join(target)
    if trusted is not None:
        trusted.checks.setdefault(key, compare)
    else:
        ctx.register_check(key, compare)


# ---------------------------------------------------------------- who may write, and the stale backstop

class Trusted:
    """The explicit authority of an internal caller that writes stock outside any card (the stock tests' books, a
    system job): never the approval flag, so it cannot be mistaken for an approved commit. The stock services, the
    only way in from another plugin, the assistant or the API, never pass one. The comparisons its writes would have
    registered are collected here; the caller runs `verify` before it commits."""

    def __init__(self, why: str):
        if not why:
            raise ValueError("a trusted write states why it is trusted")
        self.why = why
        self.checks: dict[str, object] = {}

    def verify(self, ctx: Ctx) -> None:
        for check in list(self.checks.values()):
            check(ctx)


def authorize(ctx: Ctx, trusted: Trusted | None, write: str) -> None:
    """Require core's current approval or the explicit, non-approval Trusted path."""
    from erpclaw_core import approval_scope, approvals
    from erpclaw_core.plugins import REGISTRY, declares_service

    if trusted is not None:
        if ctx.extra.get("commit") or approval_scope._related_issued(ctx):
            raise ValueError("a trusted write never runs under an approval's commit flag")
        ledger.lock_chain(ctx)  # preserve chain-first order for the explicit bootstrap path
        return
    approval_id = approvals.committing(ctx)
    card = ctx.conn.execute(select(m.approval.c.contract).where(
        m.approval.c.id == approval_id, m.approval.c.company_id == ctx.company_id
    )).first() if approval_id else None
    plugin = REGISTRY.contract_plugin.get(card.contract) if card is not None else None
    declared = plugin is not None and (plugin == "stock" or declares_service(plugin, f"stock.{write}"))
    if card is None or not declared:
        raise Refusal("A stock write runs only inside the commit of an approved card that declares it.",
                      "not_approved")


def check_fresh(ctx: Ctx, pairs: list[Pair], extra_rows: list[tuple[str, str]] = (),
                trusted: Trusted | None = None) -> None:
    """A write service runs inside an approved card's commit, after the card's read set was locked and compared.
    Backstop: every head it moves, and the entries it reads, must be in that card's read set with the digest stored
    at approval, unless this commit already moved them. A card whose read set left them out, or a row that moved, is
    refused as stale. A trusted internal write has no card, so nothing to compare."""
    from erpclaw_core.approvals import _row_digest, committing

    if trusted is not None:  # authorized by the caller's own explicit path (authorize ran first)
        return
    approval_id = committing(ctx)
    if approval_id is None:
        raise Refusal("A stock write runs only inside the commit of an approved card that declares it.",
                      "not_approved")
    approval = ctx.conn.execute(select(m.approval.c.read_set_json).where(
        m.approval.c.id == approval_id, m.approval.c.company_id == ctx.company_id)).scalar_one()
    stored = {(tbl, key): dig for tbl, key, dig in json.loads(approval)}
    moved = ctx.extra.setdefault("stock_heads_moved", set())
    rows = [("stock_head", p.head["id"]) for p in pairs if p.head is not None] + list(extra_rows)
    for tbl, key in rows:
        if (tbl, key) in moved:
            continue
        if stored.get((tbl, key)) != _row_digest(ctx.conn, tbl, key):
            raise Refusal("The stock this card read has changed since it was shown, so nothing was saved; ask again "
                          "for a fresh card.", "stale", table=tbl)


# ---------------------------------------------------------------- writing

def _entry_id(ctx: Ctx, mv: Move, posting_date: date, voucher_type: str, voucher_id: str) -> str:
    p = mv.pair
    seq = (p.head["last_seq"] if p.head else 0) + 1
    eid = new_id()
    ctx.conn.execute(t.entry.insert().values(
        id=eid, company_id=ctx.company_id, item_id=p.item["item_id"], warehouse_id=p.wh["id"], currency=ctx.currency,
        posting_date=posting_date, seq=seq, direction=mv.direction, qty=qty_text(mv.qty),
        value=quantize(mv.value, ctx.currency), qty_after=qty_text(mv.qty_after),
        value_after=quantize(mv.value_after, ctx.currency), voucher_type=voucher_type, voucher_id=voucher_id,
        line_no=mv.line_no, against_entry_id=mv.against, reverses_entry_id=mv.reverses, created_at=now()))
    _fifo_rows(ctx, mv, eid)
    _move_head(ctx, p, seq, mv, posting_date)
    return eid


def _fifo_rows(ctx: Ctx, mv: Move, eid: str) -> None:
    if mv.pair.item["method"] != "fifo":
        return
    rows = [(g, q, v, "take", None) for g, q, v in mv.takes] + [(g, q, v, "give_back", None) for g, q, v in mv.give_backs]
    if mv.take_layer is not None:
        layer = ctx.conn.execute(select(t.layer).where(t.layer.c.id == mv.take_layer)).mappings().one()
        rows.append((layer["group_id"], layer["qty"], layer["value"], "take", layer["id"]))
    for g, q, v, kind, layer_id in rows:
        ctx.conn.execute(t.take.insert().values(id=new_id(), company_id=ctx.company_id, entry_id=eid, group_id=g,
                                                layer_id=layer_id, currency=ctx.currency, qty=qty_text(q),
                                                value=quantize(v, ctx.currency), kind=kind))
    if mv.layer:
        lid = new_id()
        group = mv.merge_into or lid
        ctx.conn.execute(t.layer.insert().values(id=lid, company_id=ctx.company_id, entry_id=eid,
                                                 item_id=mv.pair.item["item_id"], warehouse_id=mv.pair.wh["id"],
                                                 currency=ctx.currency, qty=qty_text(mv.qty),
                                                 value=quantize(mv.value, ctx.currency), group_id=group))
        # the running state's placeholder for this layer's group becomes its id, for a later line of the voucher
        mv.pair.groups = [Group(lid, g.qty, g.value) if g.id.startswith("new:") else g for g in mv.pair.groups]


def _move_head(ctx: Ctx, p: Pair, seq: int, mv: Move, posting_date: date) -> None:
    qty, value = qty_text(mv.qty_after), quantize(mv.value_after, ctx.currency)
    if p.head is None:  # a pair's first movement: under the chain head's lock, so no insert race
        hid = new_id()
        ctx.conn.execute(t.head.insert().values(id=hid, company_id=ctx.company_id, item_id=p.item["item_id"],
                                                warehouse_id=p.wh["id"], currency=ctx.currency, last_seq=seq,
                                                qty=qty, value=value, last_date=posting_date))
    else:  # the four columns the runtime role may update, named so the grants test reads them
        hid = p.head["id"]
        ctx.conn.execute(t.head.update().where(t.head.c.id == hid).values(last_seq=seq, qty=qty, value=value,
                                                                          last_date=posting_date))
    p.head = head(ctx, p.item["item_id"], p.wh["id"])
    ctx.extra.setdefault("stock_heads_moved", set()).add(("stock_head", hid))


# ---------------------------------------------------------------- issues

def _cost_line(ctx: Ctx, mv: Move) -> dict:
    """An issue line's cost; a zero cost carries its note for the card: less than the currency's smallest unit."""
    line = {"cost": money_text(mv.value, ctx.currency)}
    if mv.value == 0:
        line["card_note"] = f"less than {smallest_unit(ctx.currency)}"
    return line


def preview_issue(ctx: Ctx, posting_date: date, lines: list[dict]) -> dict:
    moves = _issue_moves(ctx, lines, posting_date, lock=False)
    pairs = {id(mv.pair): mv.pair for mv in moves}.values()
    return {"lines": [_cost_line(ctx, mv) for mv in moves],
            "ledger_lines": ledger_lines(ctx, moves),
            "read_set": sorted({("stock_head", p.head["id"]) for p in pairs})}


def next_line(ctx: Ctx, voucher_type: str, voucher_id: str) -> int:
    """A voucher's lines are numbered across calls: several calls for one document continue its numbering."""
    e = t.entry
    last = ctx.conn.execute(select(func.max(e.c.line_no)).where(
        e.c.company_id == ctx.company_id, e.c.voucher_type == voucher_type, e.c.voucher_id == voucher_id,
        e.c.reverses_entry_id.is_(None))).scalar()
    return (last or 0) + 1


def issue(ctx: Ctx, voucher_type: str, voucher_id: str, posting_date: date, lines: list[dict], *,
          trusted: Trusted | None = None) -> dict:
    authorize(ctx, trusted, "issue")
    moves = _issue_moves(ctx, lines, posting_date, lock=True, first_line=next_line(ctx, voucher_type, voucher_id))
    check_fresh(ctx, list({id(mv.pair): mv.pair for mv in moves}.values()), trusted=trusted)
    out = ledger_lines(ctx, moves)
    ids = [_entry_id(ctx, mv, posting_date, voucher_type, voucher_id) for mv in moves]
    contribute(ctx, ("voucher", voucher_type, voucher_id), lines_net(out), trusted)
    return {"lines": [_cost_line(ctx, mv) for mv in moves], "ledger_lines": out, "entry_ids": ids}


# ---------------------------------------------------------------- returns

def original_issue(ctx: Ctx, voucher_id: str, line_no: int) -> dict:
    e = t.entry
    row = ctx.conn.execute(select(e).where(e.c.company_id == ctx.company_id, e.c.voucher_id == voucher_id,
                                           e.c.line_no == line_no, e.c.direction == "out",
                                           e.c.reverses_entry_id.is_(None))).mappings().first()
    if row is None:
        raise Refusal("There is no stock issue for that document line to take goods back against.",
                      "no_original_issue", voucher=voucher_id, line=line_no)
    return dict(row)


def returned_against(ctx: Ctx, issue_id: str) -> tuple[Decimal, Decimal]:
    """What has come back against an issue so far: its returns, less any return since reversed."""
    e = t.entry
    returns = ctx.conn.execute(select(e.c.id, e.c.qty, e.c.value).where(e.c.against_entry_id == issue_id)).all()
    reversed_ = set(ctx.conn.execute(select(e.c.reverses_entry_id).where(
        e.c.reverses_entry_id.in_([r.id for r in returns]))).scalars()) if returns else set()
    live = [r for r in returns if r.id not in reversed_]
    return sum((r.qty for r in live), ZERO), sum((r.value for r in live), ZERO)


def _return_move(ctx: Ctx, original: dict, posting_date: date, qty_value: object, *, lock: bool) -> Move:
    qty = parse_qty(qty_value)  # validate the positive quantity
    if original["reverses_entry_id"] is not None or ctx.conn.execute(select(t.entry.c.id).where(
            t.entry.c.reverses_entry_id == original["id"])).first():
        raise Refusal("That issue has been reversed; nothing can come back against it.", "issue_reversed")
    p = _pair(ctx, {}, original["item_id"], original["warehouse_id"], posting_date, lock=lock)
    back_qty, back_value = returned_against(ctx, original["id"])
    if back_qty + qty > original["qty"]:  # never return more than the original issue
        raise Refusal(f"Only {qty_text(original['qty'] - back_qty)} of the {qty_text(original['qty'])} issued can "
                      f"still come back.", "return_exceeds_issue", issued=qty_text(original["qty"]),
                      returned=qty_text(back_qty))
    cost = return_cost(original["qty"], original["value"], back_qty, back_value, qty, ctx.currency)
    mv = Move(p, "in", qty, cost, 1, against=original["id"], layer=p.item["method"] == "fifo")
    _apply(p, mv)
    return mv


def preview_return(ctx: Ctx, posting_date: date, voucher_id: str, line_no: int, qty: object) -> dict:
    original = original_issue(ctx, voucher_id, line_no)
    mv = _return_move(ctx, original, posting_date, qty, lock=False)
    return {"cost": money_text(mv.value, ctx.currency), "ledger_lines": ledger_lines(ctx, [mv]),
            "read_set": sorted({("stock_head", mv.pair.head["id"]), ("stock_ledger_entry", original["id"])})}


def return_in(ctx: Ctx, voucher_type: str, voucher_id: str, posting_date: date, original_voucher: str,
              line_no: int, qty: object, *, trusted: Trusted | None = None) -> dict:
    authorize(ctx, trusted, "return_in")
    original = original_issue(ctx, original_voucher, line_no)
    mv = _return_move(ctx, original, posting_date, qty, lock=True)
    check_fresh(ctx, [mv.pair], [("stock_ledger_entry", original["id"])], trusted=trusted)
    out = ledger_lines(ctx, [mv])
    eid = _entry_id(ctx, mv, posting_date, voucher_type, voucher_id)
    contribute(ctx, ("voucher", voucher_type, voucher_id), lines_net(out), trusted)
    return {"cost": money_text(mv.value, ctx.currency), "ledger_lines": out, "entry_id": eid}


# ---------------------------------------------------------------- receipts (the stock plugin's own voucher)

def _receipt_move(ctx: Ctx, item_id: str, warehouse_id: str, qty_value: object, rate_value: object,
                  posting_date: date, *, lock: bool) -> Move:
    qty, rate = parse_qty(qty_value), parse_qty(rate_value, "rate")  # validate the rate like a quantity
    p = _pair(ctx, {}, item_id, warehouse_id, posting_date, lock=lock)
    value = receipt_value(qty, rate, ctx.currency)
    if value == 0:
        raise Refusal(f"That receipt is worth less than {smallest_unit(ctx.currency)} at that rate.",
                      "receipt_below_minor_unit", unit=smallest_unit(ctx.currency))
    mv = Move(p, "in", qty, value, 1, layer=p.item["method"] == "fifo")
    if mv.layer:
        mv.merge_into = _merge_target(p, qty, value)
    _apply(p, mv)
    return mv


def receive(ctx: Ctx, voucher_id: str, item_id: str, warehouse_id: str, qty: object, rate: object,
            posting_date: date, *, trusted: Trusted | None = None) -> dict:
    """Stock received into a warehouse at a rate (voucher type `stock_receipt`): its row, and its posting, inventory
    debit and stock received not billed credit, checked whole before anything is written. Only the stock plugin's
    own card, or a trusted caller, receives stock."""
    authorize(ctx, trusted, "receive")
    mv = _receipt_move(ctx, item_id, warehouse_id, qty, rate, posting_date, lock=True)
    check_fresh(ctx, [mv.pair], trusted=trusted)
    out = ledger_lines(ctx, [mv], srnb=True)
    lines = [ledger.Line(ln["account"], debit=Decimal(ln["debit"]), credit=Decimal(ln["credit"])) for ln in out]
    ledger.check_post(ctx, posting_date, "stock_receipt", voucher_id, lines)  # validate the whole posting
    eid = _entry_id(ctx, mv, posting_date, "stock_receipt", voucher_id)
    ledger.post(ctx, posting_date, "stock_receipt", voucher_id, lines)
    return {"value": money_text(mv.value, ctx.currency), "ledger_lines": out, "entry_id": eid}


# ---------------------------------------------------------------- reversal

def _voucher_entries(ctx: Ctx, voucher_type: str, voucher_id: str) -> list[dict]:
    e = t.entry
    rows = ctx.conn.execute(select(e).where(e.c.company_id == ctx.company_id, e.c.voucher_type == voucher_type,
                                            e.c.voucher_id == voucher_id, e.c.reverses_entry_id.is_(None))).mappings()
    return [dict(r) for r in rows]


def _reverse_moves(ctx: Ctx, voucher_type: str, voucher_id: str, reversal_date: date, *, lock: bool) -> list[Move]:
    entries = _voucher_entries(ctx, voucher_type, voucher_id)
    if not entries:
        raise Refusal("That document moved no stock.", "nothing_to_reverse")
    e = t.entry
    if ctx.conn.execute(select(e.c.id).where(e.c.reverses_entry_id.in_([x["id"] for x in entries]))).first():
        raise Refusal("That document's stock has already been reversed.", "already_reversed")
    pairs: dict = {}
    moves = []
    by_pair: dict[tuple[str, str], list[dict]] = {}
    for x in entries:
        by_pair.setdefault((x["item_id"], x["warehouse_id"]), []).append(x)
    for (item_id, wh_id), rows in by_pair.items():
        p = _pair(ctx, pairs, item_id, wh_id, reversal_date, lock=lock, dated=False)  # validate the item, its kind and the warehouse
        seqs = sorted(r["seq"] for r in rows)
        # only while its movements are the latest: the pair's last contiguous run, nothing from another voucher after
        if seqs[-1] != p.head["last_seq"] or seqs != list(range(seqs[0], seqs[-1] + 1)):
            outgoing = all(r["direction"] == "out" for r in rows)
            raise Refusal("Stock has moved since this document; issue a credit note to take the goods back."
                          if outgoing else "Stock has moved since this document, so it cannot be reversed.",
                          "not_latest", item=item_id, warehouse=wh_id,
                          **({"points_to": POINTS_TO_CREDIT_NOTE} if outgoing else {}))
        check_date(ctx, reversal_date, p.head)  # an open period, not before the pair's last movement
        for r in sorted(rows, key=lambda r: r["seq"], reverse=True):  # descending sequence per pair
            mv = Move(p, "in" if r["direction"] == "out" else "out", r["qty"], r["value"], r["line_no"],
                      reverses=r["id"], original=r)
            if p.item["method"] == "fifo":
                if r["direction"] == "out":  # a reversed issue gives each take back to its group
                    mv.give_backs = [(x["group_id"], x["qty"], x["value"]) for x in ctx.conn.execute(
                        select(t.take).where(t.take.c.entry_id == r["id"], t.take.c.kind == "take")).mappings()]
                else:  # a reversed receipt or return takes its own layer out of its group
                    layer = ctx.conn.execute(select(t.layer).where(t.layer.c.entry_id == r["id"])).mappings().one()
                    mv.take_layer = layer["id"]
                    mv.takes = [(layer["group_id"], layer["qty"], layer["value"])]
            _apply(p, mv)
            if mv.take_layer is not None:
                mv.takes = []  # written once, as the layer's take (_fifo_rows)
            moves.append(mv)
    return moves


def _contribution(ctx: Ctx, moves: list[Move]) -> list[dict]:
    """Negate each original stock contribution: receipts have inventory only; issues/returns also have COGS."""
    net: dict[str, Decimal] = {}
    cogs = None
    for move in moves:
        original = move.original
        if original is None:
            raise ValueError("a reversal contribution requires the original entry")
        if not move.value:
            continue
        value = move.value if move.direction == "in" else -move.value
        account = move.pair.account
        net[account] = net.get(account, ZERO) + value
        if original["direction"] == "out" or original["against_entry_id"] is not None:
            cogs = cogs or role_account(ctx, "cost_of_goods_sold")
            net[cogs] = net.get(cogs, ZERO) - value
    order = ([cogs] if cogs else []) + [account for account in net if account != cogs]
    return [{"account": account, "net": money_text(net[account], ctx.currency)}
            for account in order if net[account] != 0]


def stock_inventory(ctx: Ctx) -> set[str]:
    return {w["account_id"] for w in warehouses(ctx)}


def _mirror_entry(mv: Move, ctx: Ctx) -> dict:
    return {"item": mv.pair.item["item_id"], "warehouse": mv.pair.wh["id"], "direction": mv.direction,
            "qty": qty_text(mv.qty), "value": money_text(mv.value, ctx.currency), "qty_after": qty_text(mv.qty_after),
            "value_after": money_text(mv.value_after, ctx.currency)}


def _require_delivery_reversal(ctx: Ctx, voucher_type: str, voucher_id: str) -> None:
    """All GR settlement variants use their owner, so no generic path can orphan a payable."""
    if voucher_type != "stock_goods_received":
        return
    header = ctx.conn.execute(select(t.goods_received).where(t.goods_received.c.company_id == ctx.company_id,
        t.goods_received.c.voucher_id == voucher_id)).mappings().first()
    text = "Goods received must be reversed as a delivery."
    if header is not None:
        text = f"{header['number']} must be reversed as a delivery."
        if header["settlement"] == "still_owed":
            supplier = ctx.conn.execute(select(m.party.c.name).where(m.party.c.company_id == ctx.company_id,
                m.party.c.id == header["supplier_id"])).scalar_one_or_none()
            if supplier is not None:
                text = f"{header['number']} is still owed to {supplier}; reverse it as a delivery."
    raise Refusal(text, "goods_received_reversal_required", points_to="reverse_goods_received")


def preview_reverse(ctx: Ctx, voucher_type: str, voucher_id: str, reversal_date: date) -> dict:
    _require_delivery_reversal(ctx, voucher_type, voucher_id)
    moves = _reverse_moves(ctx, voucher_type, voucher_id, reversal_date, lock=False)
    pairs = {id(mv.pair): mv.pair for mv in moves}.values()
    return {"mirror_entries": [_mirror_entry(mv, ctx) for mv in moves],
            "contribution": _contribution(ctx, moves),
            "read_set": sorted({("stock_head", p.head["id"]) for p in pairs})}


def reverse_movements(ctx: Ctx, voucher_type: str, voucher_id: str, reversal_date: date, reversal_type: str,
                      reversal_id: str, *, trusted: Trusted | None = None) -> dict:
    authorize(ctx, trusted, "reverse_movements")
    _require_delivery_reversal(ctx, voucher_type, voucher_id)
    moves = _reverse_moves(ctx, voucher_type, voucher_id, reversal_date, lock=True)
    check_fresh(ctx, list({id(mv.pair): mv.pair for mv in moves}.values()), trusted=trusted)
    contribution = _contribution(ctx, moves)
    ids = [_entry_id(ctx, mv, reversal_date, reversal_type, reversal_id) for mv in moves]
    contribute(ctx, ("reversal", voucher_type, voucher_id, reversal_type, reversal_id),
               {c["account"]: Decimal(c["net"]) for c in contribution}, trusted)
    return {"contribution": contribution, "entry_ids": ids}


# ---------------------------------------------------------------- reads

def stock_on_hand(ctx: Ctx, item_id: str, warehouse_id: str | None = None) -> dict:
    stock_item(ctx, item_id)
    whs = [warehouse(ctx, warehouse_id)] if warehouse_id else warehouses(ctx)
    out = []
    for w in whs:
        qty, value = on_hand(ctx, item_id, w["id"])
        out.append({"warehouse": w["id"], "name": w["name"], "qty": qty_text(qty),
                    "value": money_text(value, ctx.currency)})
    return {"warehouses": out}


def choose_warehouse(ctx: Ctx, item_id: str, qty_value: object, warehouse_id: str | None = None) -> dict:
    """The warehouse an issue line takes from when the owner named none: the item's default if it holds enough,
    else the company's default if it does, else the one that does; several: the engine asks; none: short."""
    item = stock_item(ctx, item_id)
    qty = parse_qty(qty_value)
    whs = warehouses(ctx)
    held = {w["id"]: on_hand(ctx, item_id, w["id"])[0] for w in whs}
    if warehouse_id is not None:
        warehouse(ctx, warehouse_id)
        candidates = [warehouse_id]
    else:
        default = next((w["id"] for w in whs if w["is_default"]), None)
        candidates = [x for x in (item["default_warehouse_id"], default) if x is not None]
    for w in candidates:
        if held[w] >= qty:
            return {"chosen": {"warehouse": w, "qty": qty_text(held[w])}}
    enough = [w for w in whs if held[w["id"]] >= qty] if warehouse_id is None else []
    if len(enough) == 1:
        return {"chosen": {"warehouse": enough[0]["id"], "qty": qty_text(held[enough[0]["id"]])}}
    if len(enough) > 1:
        options = [f"{w['name']} ({qty_text(held[w['id']])} on hand)" for w in enough]
        raise Refusal(f"More than one warehouse holds enough: {'; '.join(options)}. Which one?",
                      "warehouse_ambiguous", options=options, field="warehouse",
                      choices=[{"label": o, "ref": w["name"]} for o, w in zip(options, enough)])
    return {"short": [{"warehouse": w["id"], "qty": qty_text(held[w["id"]])} for w in whs]}


# ---------------------------------------------------------------- verification

def verify_stock(ctx: Ctx) -> list[str]:
    """Problems, none when the stock books agree: each inventory account against the combined value of every
    warehouse mapped to it; each head against its entries; every entry with value against its voucher's posting
    (a zero-value entry needs none). An empty company has nothing to agree, which is not vacuous: the callers assert
    there is stock first."""
    problems = []
    e = t.entry
    by_account: dict[str, Decimal] = {}
    for w in warehouses(ctx):
        for h in ctx.conn.execute(select(t.head).where(t.head.c.company_id == ctx.company_id,
                                                       t.head.c.warehouse_id == w["id"])).mappings():
            rows = ctx.conn.execute(select(e).where(e.c.company_id == ctx.company_id, e.c.item_id == h["item_id"],
                                                    e.c.warehouse_id == w["id"]).order_by(e.c.seq)).mappings().all()
            qty = sum(((r["qty"] if r["direction"] == "in" else -r["qty"]) for r in rows), ZERO)
            value = sum(((r["value"] if r["direction"] == "in" else -r["value"]) for r in rows), ZERO)
            if [r["seq"] for r in rows] != list(range(1, len(rows) + 1)) or (qty, value) != (h["qty"], h["value"]) \
                    or (rows and rows[-1]["seq"] != h["last_seq"]):
                problems.append(f"head {h['id']} does not match its entries")
            by_account[w["account_id"]] = by_account.get(w["account_id"], ZERO) + h["value"]
        by_account.setdefault(w["account_id"], ZERO)
    for account, value in by_account.items():
        balance = ledger.balance(ctx, account, date.max)
        if balance != value:
            problems.append(f"inventory account {account} holds {balance}, its warehouses {value}")
    for r in ctx.conn.execute(select(e).where(e.c.company_id == ctx.company_id)).mappings():
        if r["value"] == 0:
            continue
        if r["reverses_entry_id"] is None:
            ok = _posting_id(ctx, r["voucher_type"], r["voucher_id"]) is not None
        else:
            orig = ctx.conn.execute(select(e).where(e.c.id == r["reverses_entry_id"])).mappings().one()
            ok = _reversal_posting_id(ctx, orig["voucher_type"], orig["voucher_id"]) is not None
        if not ok:
            problems.append(f"stock entry {r['id']} has value and no posting")
    for item_id, kind in ctx.conn.execute(select(t.stock_item.c.item_id, m.item.c.kind).join(
            m.item, (m.item.c.company_id == t.stock_item.c.company_id) & (m.item.c.id == t.stock_item.c.item_id))
            .where(t.stock_item.c.company_id == ctx.company_id)):
        if kind != "stock":
            problems.append(f"stock item {item_id} is a {kind} in the core item list")
    return problems
