"""The stock contract's fixtures (engine tests/contracts/stock/v1/): loading them, building the books each starts
from, and applying its `before` movements as a consumer would (the stock call, then its posting, then the comparison
the call registered), through the stock plugin's explicit Trusted path, never an approval flag. The call under test
goes through call_service: a read service directly, a write service inside a real card's commit (a planted
contract), so its stale backstop and its comparison run as they do for a consumer."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select

from typing import Literal

from erpclaw_core import approvals, ledger
from erpclaw_core import models as m
from erpclaw_core.common import Ctx, company_ctx, new_id
from erpclaw_core.contracts import Contract, Input, Preview, Result, Shown
from erpclaw_core.manifest import Capabilities, Events, Manifest, PUBLISHER, Source
from erpclaw_core.plugins import REGISTRY, Plugin, call_service
from erpclaw_core.setup import create_company
from erpclaw_stock import service as stock
from erpclaw_stock import tables as t
from erpclaw_stock.service import Trusted

FIXTURES = Path(__file__).resolve().parents[1] / "contracts" / "stock" / "v1"
CONSUMER = "stockuser"
SERVICES = ("stock_on_hand", "choose_warehouse", "preview_issue", "issue", "preview_return", "return_in",
            "preview_reverse", "reverse_movements")
EXTRA_STOCK_ACCOUNTS = {"@acct:inv_raw": ("1131", "Raw Materials"), "@acct:inv_fg": ("1133", "Finished Goods")}


class FixtureError(ValueError):
    """A fixture that breaks the format (a float, a number that is not a line ordinal, a missing field)."""


def _no_float(text: str):
    raise FixtureError(f"a JSON number with a fraction or an exponent ({text}); figures are strings")


def _check_numbers(value, key: str = "") -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, int) and key != "line":
        raise FixtureError(f"the JSON number {value} under {key!r}; only line ordinals are numbers")
    if isinstance(value, dict):
        for k, v in value.items():
            _check_numbers(v, k)
    elif isinstance(value, list):
        for v in value:
            _check_numbers(v, key)


REQUIRED = ("contract", "service", "name", "books", "before", "input", "after")


def load_text(text: str) -> dict:
    case = json.loads(text, parse_float=_no_float, parse_constant=_no_float)
    _check_numbers(case)
    missing = [k for k in REQUIRED if k not in case] + ([] if ("output" in case) != ("refusal" in case) else
                                                         ["output or refusal (exactly one)"])
    if missing or case["contract"] != "stock/1":
        raise FixtureError(f"{case.get('name')}: missing {', '.join(missing) or 'contract stock/1'}")
    return case


def load(path: Path) -> dict:
    return load_text(path.read_text())


def all_cases() -> list[Path]:
    return sorted(FIXTURES.glob("*.json"))


PREVIEW_OF = {"issue": "preview_issue", "return_in": "preview_return", "reverse_movements": "preview_reverse"}


class FixtureWrite(Input):
    service: Literal["issue", "return_in", "reverse_movements"]
    input: dict


def _preview_call(ctx: Ctx, inp: FixtureWrite) -> dict:
    args = dict(inp.input)
    if inp.service == "reverse_movements":
        args.pop("reversal")
    return call_service(ctx, CONSUMER, f"stock.{PREVIEW_OF[inp.service]}", args)


def _preview(ctx: Ctx, inp: FixtureWrite) -> Preview:
    _preview_call(ctx, inp)
    sh = Shown(ctx.currency)
    sh.title(f"Stock fixture: {inp.service}")
    return Preview(sh.card(), new_id())


def _read_set(ctx: Ctx, inp: FixtureWrite) -> list[tuple[str, str]]:
    return [("company", ctx.company_id), *[(tbl, key) for tbl, key in _preview_call(ctx, inp)["read_set"]]]


def _commit(ctx: Ctx, inp: FixtureWrite, key: str) -> Result:
    """The write service, then the consumer's obligation: its single posting of the returned lines, or the reversal
    of its original posting. The comparison the service registered runs after this, before the commit ends."""
    real = inp.input
    out = call_service(ctx, CONSUMER, f"stock.{inp.service}", real)
    if inp.service in ("issue", "return_in"):
        lines = out["ledger_lines"]
        if lines:
            ledger.post(ctx, date.fromisoformat(real["posting_date"]), real["voucher_type"], real["voucher"],
                        [ledger.Line(x["account"], debit=Decimal(x["debit"]), credit=Decimal(x["credit"]))
                         for x in lines])
    else:
        original = real["original"]
        posting = ctx.conn.execute(select(m.posting.c.id).where(
            m.posting.c.company_id == ctx.company_id, m.posting.c.voucher_type == original["voucher_type"],
            m.posting.c.voucher_id == original["voucher"])).scalar()
        if posting is not None:
            ledger.reverse(ctx, posting, date.fromisoformat(real["reversal_date"]))
    return Result("Done.", {"out": out})


FIXTURE_WRITE = Contract("stock_fixture_write", CONSUMER, "write", "Run one stock fixture's write (planted).",
                         FixtureWrite, preview=_preview, commit=_commit, find_by_operation_key=lambda ctx, key: None,
                         read_set=_read_set)


def consumer() -> Plugin:
    """A planted consumer that declares every stock service, so the test calls them as a plugin would, and a card
    whose commit runs one fixture's write service."""
    manifest = Manifest(name=CONSUMER, version="1.0.0", publisher=PUBLISHER, distribution=f"erpclaw-{CONSUMER}",
                        licence="GPL-3.0-only", source=Source(repository="workspace", commit="workspace"),
                        engine=">=0.0.1", after_core_revision="0004", depends={"stock": ">=0.0.1"}, conflicts=[],
                        owned_tables=[], contracts={FIXTURE_WRITE.name: "write"}, calls=[],
                        events=Events(emits=[], consumes=[]), slots={}, voucher_types=[],
                        capabilities=Capabilities(network=[], files=[]), metrics=[],
                        uses=[f"stock.{s}" for s in SERVICES])
    return Plugin(manifest=manifest, kind="business-module", contracts=[FIXTURE_WRITE], artifact_digest="0" * 64)


def _summed(lines: list[dict]) -> list[dict]:
    """The stock lines of several calls, summed per account, as the caller's single posting carries them."""
    net: dict[str, Decimal] = {}
    for ln in lines:
        net[ln["account"]] = net.get(ln["account"], Decimal(0)) + Decimal(ln["debit"]) - Decimal(ln["credit"])
    return [{"account": a, "debit": str(v if v > 0 else 0), "credit": str(-v if v < 0 else 0)}
            for a, v in net.items() if v]


class Books:
    """A fixture's starting books: a fresh company, and every symbol mapped to its row's id."""

    def __init__(self, engine, case: dict):
        self.engine, self.case = engine, case
        books = case["books"]
        self.company = create_company(engine, f"Stock fixture {case['name']} {new_id()[:8]}", books["currency"])
        self.ids: dict[str, str] = {}
        with engine.begin() as conn:
            ctx = self.ctx(conn)
            self.ids["@acct:inv"] = ledger.account_by_type(ctx, "stock")["id"]
            self.ids["@acct:cogs"] = ledger.account_by_type(ctx, "cost_of_goods_sold")["id"]
            self.ids["@acct:srnb"] = ledger.account_by_type(ctx, "stock_received_not_billed")["id"]
            parent = conn.execute(select(m.account.c.parent_id).where(m.account.c.id == self.ids["@acct:inv"])).scalar()
            for sym, (number, name) in EXTRA_STOCK_ACCOUNTS.items():
                if any(w["account"] == sym for w in books["warehouses"].values()):
                    self.ids[sym] = new_id()
                    conn.execute(m.account.insert().values(
                        id=self.ids[sym], company_id=self.company, number=number, name=name, root_type="asset",
                        account_type="stock", is_group=False, parent_id=parent, normal_side="debit"))
            for sym, w in books["warehouses"].items():
                self.ids[sym] = stock.create_warehouse(ctx, w["name"], self.ids[w["account"]],
                                                       default=books.get("default_warehouse") == sym)
            for sym, it in books["items"].items():
                item_id = new_id()
                conn.execute(m.item.insert().values(id=item_id, company_id=self.company,
                                                    name=sym.split(":", 1)[1].replace("_", " ").title(),
                                                    unit="unit", standard_rate="1"))
                self.ids[sym] = item_id
                stock.make_stock_item(ctx, item_id, it["method"])
        self.steps: dict[str, dict] = {}
        before = case["before"]
        n = 0
        while n < len(before):  # consecutive issues of one voucher are one document: one transaction, one posting
            group = [before[n]]
            while (before[n]["op"] == "issue" and n + len(group) < len(before)
                   and before[n + len(group)]["op"] == "issue"
                   and before[n + len(group)]["voucher"] == before[n]["voucher"]):
                group.append(before[n + len(group)])
            self.apply(group)
            n += len(group)

    def ctx(self, conn) -> Ctx:
        return company_ctx(conn, company_id=self.company)

    def sym(self, value: str) -> str:
        if value not in self.ids:
            if value.startswith("@v:"):
                self.ids[value] = f"{value[3:]}-{new_id()[:8]}"
            elif value.startswith("@item:"):  # an item the books do not keep in stock
                self.ids[value] = new_id()
            else:
                raise KeyError(value)
        return self.ids[value]


    def post(self, ctx: Ctx, voucher_type: str, voucher: str, when: date, lines: list[dict]) -> None:
        """The consumer's single posting: the stock service's lines (none when every line costs zero)."""
        if lines:
            ledger.post(ctx, when, voucher_type, voucher, [
                ledger.Line(ln["account"], debit=Decimal(ln["debit"]), credit=Decimal(ln["credit"])) for ln in lines])

    def apply(self, group: list[dict]) -> None:
        step = group[0]
        when = date.fromisoformat(step["date"])
        trusted = Trusted(f"the books of stock fixture {self.case['name']}")
        with self.engine.begin() as conn:
            ctx = self.ctx(conn)
            if step["op"] == "issue":
                lines: list[dict] = []
                for s in group:
                    out = stock.issue(ctx, "sales_invoice", self.sym(s["voucher"]), when, [
                        {"item": self.sym(s["item"]), "warehouse": self.sym(s["warehouse"]), "qty": s["qty"]}],
                        trusted=trusted)
                    lines += out["ledger_lines"]
                    self.steps[s["as"]] = {"voucher": self.sym(s["voucher"]), "line": s.get("line", 1)}
                self.post(ctx, "sales_invoice", self.sym(step["voucher"]), when, _summed(lines))
            elif step["op"] == "receive":
                stock.receive(ctx, self.sym(step["voucher"]), self.sym(step["item"]), self.sym(step["warehouse"]),
                              step["qty"], step["rate"], when, trusted=trusted)
            elif step["op"] == "return":
                orig = self.steps[step["against"]]
                out = stock.return_in(ctx, "sales_credit_note", self.sym(step["voucher"]), when, orig["voucher"],
                                      orig["line"], step["qty"], trusted=trusted)
                self.post(ctx, "sales_credit_note", self.sym(step["voucher"]), when, out["ledger_lines"])
            else:
                raise FixtureError(f"unknown step {step['op']}")
            trusted.verify(ctx)

    def resolve(self, value):
        """The value with every symbol it holds replaced by its id (heads and entries looked up now)."""
        if isinstance(value, dict):
            return {k: self.resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.resolve(v) for v in value]
        if isinstance(value, str) and value.startswith("@"):
            return self.lookup(value)
        return value

    def lookup(self, value: str) -> str:
        if value in self.ids or value.startswith(("@v:", "@item:")):
            return self.sym(value)
        kind, _, rest = value.partition(":")
        with self.engine.connect() as conn:
            if kind == "@head":
                item, wh = rest.split("/")
                return conn.execute(select(t.head.c.id).where(
                    t.head.c.company_id == self.company, t.head.c.item_id == self.ids[f"@item:{item}"],
                    t.head.c.warehouse_id == self.ids[f"@wh:{wh}"])).scalar_one()
            if kind == "@entry" and "/" in rest:
                voucher, line = rest.split("/")
                return conn.execute(select(t.entry.c.id).where(
                    t.entry.c.company_id == self.company, t.entry.c.voucher_id == self.sym(f"@v:{voucher}"),
                    t.entry.c.line_no == int(line), t.entry.c.reverses_entry_id.is_(None))).scalar_one()
        return value  # an entry the call itself makes: bound from its output

    def snapshot(self) -> tuple:
        with self.engine.connect() as conn:
            counts = tuple(conn.execute(select(func.count()).select_from(tbl)).scalar() for tbl in t.TABLES)
            heads = tuple(conn.execute(select(t.head.c.id, t.head.c.last_seq, t.head.c.qty, t.head.c.value).where(
                t.head.c.company_id == self.company).order_by(t.head.c.id)).all())
            postings = conn.execute(select(func.count()).select_from(m.posting).where(
                m.posting.c.company_id == self.company)).scalar()
        return counts, heads, postings

    def call(self, service: str, inp: dict, *, write: bool) -> dict:
        """A read service through call_service; a write service inside a real card's commit (propose, then approve),
        so its stale backstop and its comparison run as they do for a consumer. A refusal at the card's preview is
        raised, as the consumer would see it."""
        real = self.resolve(inp)
        if not write:
            with self.engine.begin() as conn:
                return call_service(self.ctx(conn), CONSUMER, f"stock.{service}", real)
        card = approvals.propose(self.engine, REGISTRY.contracts[FIXTURE_WRITE.name], {"service": service,
                                                                                         "input": real},
                                 company_id=self.company)
        out = approvals.approve(self.engine, card.approval_id, card.digest)
        assert out.status == "committed", (out.status, out.text, out.data)
        return out.data["out"]
