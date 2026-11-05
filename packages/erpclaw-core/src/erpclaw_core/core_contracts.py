"""Contracts and metrics the core itself provides: get_metric and receivables; and the owner's own books by chat
(set_up_company, add_customer, add_item)."""
from __future__ import annotations

import calendar
import hashlib
import json
import re
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, WithJsonSchema, field_validator
from pydantic.json_schema import SkipJsonSchema
from sqlalchemy import select

from . import models as m
from .common import Ctx, Refusal, audit
from .contracts import FIGURES, Contract, Input, Preview, Result, Shown, WriteContract
from .masters import _near, _norm, _same_name, create_item, create_party, customers, find_customer
from .metrics import Metric, MetricResult, MetricRow, render
from .periods import Period, fiscal_year_bounds, fiscal_year_of, resolve
from .manifest import PUBLISHER, Capabilities, Events, Manifest, Source
from .plugins import REGISTRY, Plugin, enabled_plugins
from .textkey import text_key
from .setup import CHART, open_year


def receivables(ctx: Ctx, period: Period, customer: str | None, city: str | None) -> MetricResult:
    le, p, a, po = m.ledger_entry, m.party, m.account, m.posting
    q = (select(p.c.name, p.c.city, le.c.debit, le.c.credit)
         .join(a, a.c.id == le.c.account_id).join(p, p.c.id == le.c.party_id).join(po, po.c.id == le.c.posting_id)
         .where(le.c.company_id == ctx.company_id, a.c.account_type == "receivable",
                po.c.posting_date <= period.end))
    if customer:
        q = q.where(p.c.id == find_customer(ctx, customer)["id"])
    if city:
        q = q.where(p.c.city_key == text_key(city))
    rows = ctx.conn.execute(q).all()
    basis = "submitted invoices less payments received, from the receivables ledger; drafts are not owed"
    if not rows:
        return MetricResult("receivables", "Owed to us", period, basis, None, as_of=True)
    by: dict[str, Decimal] = {}
    for r in rows:
        by[r.name] = by.get(r.name, Decimal("0")) + r.debit - r.credit
    out = [MetricRow(k, v, "(credit in their favour)" if v < 0 else "")
           for k, v in sorted(by.items(), key=lambda kv: -kv[1]) if v != 0]
    total = sum(by.values(), Decimal("0"))
    return MetricResult("receivables", "Owed to us", period, basis, total, rows=out, row_count=len(rows),
                        method="net balance per customer; credits shown, not hidden", as_of=True)


# Reports and figures are off in this release, refused as planned for the core release; they come back on
# through their own specification and review.
REPORTS = "Core"


RECEIVABLES = Metric("receivables", "Owed to us",
                     "Net amount customers owe: submitted invoices minus payments, per customer.",
                     receivables, default_period="today", as_of=True,
                     synonyms=("owed", "owes", "who owes", "receivables", "debtors", "outstanding", "money owed"),
                     planned=REPORTS)


class GetMetric(Input):
    metric: str = Field(description="Which measure. One of the listed names.")
    period: str | None = Field(default=None, description=(
        "The period exactly as the owner said it, e.g. 'last month', 'FY 2025', 'last 6 months', "
        "'Q3 2026', 'October 2026'. Leave empty if the owner gave none."))
    customer: str | None = Field(default=None, description="Customer name as the owner said it, if any.")
    city: str | None = Field(default=None, description="Customer city, if the owner limited it to one.")


def _metric_hook(schema: dict) -> dict:
    names = sorted(n for n, mt in REGISTRY.metrics.items() if not mt.planned)  # a planned metric is not offered
    schema["properties"]["metric"]["enum"] = names + ["other"]
    schema["properties"]["metric"]["description"] = "Which measure: " + "; ".join(
        f"{n} = {REGISTRY.metrics[n].definition}" for n in names) + "; other = anything else the owner asked for."
    return schema


def get_metric(ctx: Ctx, inp: GetMetric) -> Result:
    mt = REGISTRY.metrics.get(inp.metric)
    if mt is not None and REGISTRY.metric_plugin[inp.metric] not in enabled_plugins(ctx.conn):
        mt = None  # its plugin is not enabled in these books
    if mt is None:
        known = ", ".join(sorted(x.title.lower() for x in REGISTRY.metrics.values()))
        return Result(f"ERPClaw doesn't track that measure yet, so I won't guess a number. "
                      f"Measures I can give you now: {known}.", {"known": False})
    co = ctx.conn.execute(select(m.company.c.fiscal_year_start_month).where(
        m.company.c.id == ctx.company_id)).scalar_one()
    period = resolve(inp.period or mt.default_period, ctx.today, co)
    asked_end = None
    if mt.as_of and period.end > ctx.today:
        # A balance can only be known as of today: query and label use today, and the answer says so.
        asked_end, period = period.end, Period(period.start, ctx.today, period.phrase)
    try:
        res = mt.run(ctx, period, inp.customer, inp.city)
    except Refusal:
        raise
    scope = []
    if inp.customer:
        scope.append(inp.customer)
    if inp.city:
        scope.append(f"customers in {inp.city}")
    res.asked_end = asked_end
    sh = Shown(ctx.currency)
    text, figures = render(res, sh)
    if scope:
        text = text.replace(res.title, f"{res.title} ({', '.join(scope)})", 1)
    return Result(text, {"metric": res.metric, "value": None if res.value is None else str(res.value),
                         "from": str(period.start), "to": str(period.end)}, figures, sh.amounts)


# ---------- the owner's own books: the company's setup, its customers and its items ----------

SETUP = "set_up_company"
# The owner-facing wording, as the language table in erpclaw_assistant/resolve.py gives it: core cannot
# read the assistant's table, so the keys it says are repeated here, word for word.
TEXT = {"en": {
    "setup_name_missing": "What's your business called?",
    "setup_currency_missing": "Which currency do you keep your books in?",
    "setup_currency_planned": ("I can keep books in US dollars only for now; other currencies come with the country "
                               "packs (3.1). Nothing was set up."),
    "setup_fy_start_missing": "When does your financial year start? January, or another month?",
    "setup_date_order_missing": "How do you write dates: 03/04 for 3 April, or for 4 March?",
    "books_not_set_up": "Your books aren't set up yet.",
    "customer_exists": "{name} is already a customer.",
    "customer_near": "Did you mean {name} ({city}), already a customer, or is {said} a new one?",
    "item_unit_missing": "What unit do you sell {name} in: each, hour, meter, or another?",
    "item_price_missing": "At what price do you usually sell {name}?",
    "item_exists": "{name} is already an item.",
    "item_near": "Did you mean {name}, already an item, or is {said} a new one?",
}}
LANGUAGE = "en"
SET_UP_ALREADY = "These books are already set up."
NOTHING_POSTED = "Nothing is posted."
READY = ("Your books are ready. You can add your first customer, like 'add a customer called Acme Corp, "
         "accounts@acme.example'. If your business already has money in the bank or customers who owe you, opening "
         "balances come next; for now, new transactions only.")
USD = "USD"
# what the owner may call the one currency v3.0 keeps books in, normalised (masters._norm)
USD_WORDS = frozenset({"usd", "us", "us dollar", "us dollars", "united states dollar", "united states dollars"})
DATE_ORDER_CHOICES = (("03/04 is 3 April (day first)", "DMY"), ("03/04 is 4 March (month first)", "MDY"))
DATE_ORDER_LINE = {"DMY": "Day first: 03/04 is 3 April", "MDY": "Month first: 03/04 is 4 March"}
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
QUOTES = {'"': '"', "'": "'", "\u201c": "\u201d", "\u2018": "\u2019"}


def say(key: str, **kw) -> str:
    return TEXT[LANGUAGE][key].format(**kw)


def _mark(kind: str, **how) -> dict:
    """How the assistant resolves a field from the owner's words (kept out of the tool schema)."""
    return {"erpclaw": {"kind": kind, **how}}


def _unquoted(v: str | None) -> str | None:
    """A name the owner wrote inside quotation marks is the name within them."""
    if v and len(v) >= 2 and QUOTES.get(v[0]) == v[-1]:
        v = v[1:-1].strip()
    if v is not None and not v.strip():
        raise ValueError("a name must not be blank")
    return v


def _no_float(v):
    if isinstance(v, (float, bool)):
        raise ValueError("a price must be exact (text or integer), never floating point")
    return v


Price = Annotated[Decimal, BeforeValidator(_no_float), Field(gt=0, max_digits=18, decimal_places=4),
                  WithJsonSchema({"type": "number", "exclusiveMinimum": 0})]


def _token(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


def _committed(ctx: Ctx, key: str) -> Result | None:
    """A committed setup or master by its operation key, from its outcome row (its rows carry no key)."""
    o = ctx.conn.execute(select(m.operation_outcome).where(m.operation_outcome.c.key == key,
                                                           m.operation_outcome.c.company_id == ctx.company_id,
                                                           m.operation_outcome.c.status == "committed")).mappings().first()
    if o is None:
        return None
    r = json.loads(o["result_json"])
    return Result(r.get("text") or "", r.get("data") or {})


class SetUpCompany(Input):
    name: Annotated[str | None, _mark("text")] = Field(
        default=None, min_length=1, max_length=120, description="The business's name exactly as the owner wrote it.")
    currency: Annotated[str | None, _mark("text")] = Field(
        default=None, max_length=40, description="The currency exactly as the owner said it, e.g. 'US dollars'.")
    # chosen by the owner from the engine's own choices, never from the model (the step binds the answer)
    fiscal_year_start_month: SkipJsonSchema[int | None] = Field(default=None, ge=1, le=12)
    date_order: SkipJsonSchema[Literal["DMY", "MDY"] | None] = None

    @field_validator("name")
    @classmethod
    def _unquote(cls, v: str | None) -> str | None:
        return _unquoted(v)

    def takes_no_date(self, ctx: Ctx) -> bool:
        """Setting up the books posts nothing, so it takes no date."""
        return True


def _setup_plan(ctx: Ctx, inp: SetUpCompany) -> tuple[str, int, str, list[int]]:
    """The four choices, each the owner's, asked in turn when missing; and the fiscal years to open: the one holding
    today and the next."""
    status = ctx.conn.execute(select(m.company.c.status).where(m.company.c.id == ctx.company_id)).scalar_one()
    if status != "pending":
        raise Refusal(SET_UP_ALREADY, "books_ready")
    if not inp.name:
        raise Refusal(say("setup_name_missing"), "setup_name_missing", field="name")
    if not inp.currency:
        raise Refusal(say("setup_currency_missing"), "setup_currency_missing", field="currency")
    if _norm(inp.currency) not in USD_WORDS:  # a planned answer: the country packs bring the others
        raise Refusal(say("setup_currency_planned"), "planned", capability="planned.country_packs")
    if inp.fiscal_year_start_month is None:
        raise Refusal(say("setup_fy_start_missing"), "setup_fy_start_missing", field="fiscal_year_start_month",
                      choices=[{"label": calendar.month_name[k], "ref": k} for k in range(1, 13)])
    if inp.date_order is None:
        raise Refusal(say("setup_date_order_missing"), "setup_date_order_missing", field="date_order",
                      choices=[{"label": label, "ref": ref} for label, ref in DATE_ORDER_CHOICES])
    first = fiscal_year_of(ctx.today, inp.fiscal_year_start_month)
    return inp.name, inp.fiscal_year_start_month, inp.date_order, [first, first + 1]


def _year_text(start_month: int) -> str:
    """"1 April to 31 March": the year's first and last day, by month (February's last day changes with the year)."""
    last = start_month - 1 or 12
    end = "the end of February" if last == 2 else f"{calendar.monthrange(2001, last)[1]} {calendar.month_name[last]}"
    return f"1 {calendar.month_name[start_month]} to {end}"


def preview_set_up(ctx: Ctx, inp: SetUpCompany) -> Preview:
    name, month, order, years = _setup_plan(ctx, inp)
    # the card's Company line and currency are the books being set up (approvals._preview reads them from ctx)
    ctx.company_name, ctx.currency = name, USD
    opened = " and ".join(f"FY {fy} ({s.day} {s:%b %Y} to {e.day} {e:%b %Y})"
                          for fy, (s, e) in ((fy, fiscal_year_bounds(fy, month)) for fy in years))
    sh = Shown(USD)
    sh.line("Currency", "US dollars (USD). This can't be changed after your first posting.")
    sh.line("Financial year", _year_text(month))
    sh.line("Years opened", f"{opened}, twelve monthly periods each. Earlier years open only when you add opening "
                            "balances.")
    sh.line("Dates", DATE_ORDER_LINE[order])
    sh.book(f"Your chart of accounts: {len(CHART)} accounts for a small business (bank, receivables, sales, cost of "
            "goods, and the rest); more are added when you need them.")
    sh.book(NOTHING_POSTED)
    sh.title("Set up your books")
    return Preview(sh.card(), _token(ctx.company_id, name, USD, month, order, *years))


def read_set_set_up(ctx: Ctx, inp: SetUpCompany) -> list[tuple[str, str]]:
    return [("company", ctx.company_id)]


def commit_set_up(ctx: Ctx, inp: SetUpCompany, key: str) -> Result:
    """A compare-and-set on `pending`: one setup commits, a second finds the books ready and changes nothing."""
    name, month, order, years = _setup_plan(ctx, inp)
    c = m.company
    done = ctx.conn.execute(c.update().where(c.c.id == ctx.company_id, c.c.status == "pending").values(
        name=name, functional_currency=USD, fiscal_year_start_month=month, date_order=order, status="ready"))
    if done.rowcount != 1:
        raise Refusal(SET_UP_ALREADY, "books_ready")
    for fy in years:
        open_year(ctx.conn, ctx.company_id, fy, month)
    audit(ctx, SETUP, "company", ctx.company_id, detail={"name": name, "currency": USD, "fiscal_year_start_month": month,
                                                          "date_order": order, "fiscal_years": years})
    return Result(READY, {"name": name})


def _near_question(what: str, said: str, text: str, stored: str, field: str) -> Refusal:
    """A near name: the owner says whether it is the stored record or a new one; nothing is written before the
    answer, which binds `field`."""
    return Refusal(text, f"{what}_near_ambiguous", field=field, choices=[
        {"label": f"{stored}, already {'an' if what == 'item' else 'a'} {what}", "ref": False},
        {"label": f"{said}, a new {what}", "ref": True}])


class AddCustomer(Input):
    name: Annotated[str, _mark("text")] = Field(
        min_length=1, max_length=200, description="The customer's name exactly as the owner wrote it.")
    email: Annotated[str | None, _mark("text")] = Field(
        default=None, max_length=200, description="The customer's email address, only if the owner gave one.")
    city: Annotated[str | None, _mark("text")] = Field(
        default=None, max_length=100, description="The customer's city, only if the owner gave one.")
    # the owner's answer when a near name was asked about: True a new customer, False the stored one
    new_customer: SkipJsonSchema[bool | None] = None

    @field_validator("name")
    @classmethod
    def _unquote(cls, v: str | None) -> str | None:
        return _unquoted(v)

    @field_validator("email")
    @classmethod
    def _address(cls, v: str | None) -> str | None:
        if v is not None and not EMAIL.match(v):
            raise ValueError("an email address is like name@example.com")
        return v

    def takes_no_date(self, ctx: Ctx) -> bool:
        """Adding a customer posts nothing, so it takes no date."""
        return True


def _customer_plan(ctx: Ctx, inp: AddCustomer) -> dict | None:
    """An exact duplicate (the same name once normalised) refused; a near one asked about. Returns the near customer
    the owner said is another one, if any."""
    rows = customers(ctx)
    same = [r for r in rows if _same_name(inp.name, r["name"], False)]
    if same:
        raise Refusal(say("customer_exists", name=same[0]["name"]), "customer_exists")
    near = _near(rows, inp.name)
    if not near:
        return None
    r = near[0]
    if inp.new_customer is False:
        raise Refusal(say("customer_exists", name=r["name"]), "customer_exists")
    if inp.new_customer is None:
        where = r.get("city") or r.get("email") or "no city or email on file"
        raise _near_question("customer", inp.name, say("customer_near", name=r["name"], city=where, said=inp.name),
                             f"{r['name']} ({where})", "new_customer")
    return r


def preview_add_customer(ctx: Ctx, inp: AddCustomer) -> Preview:
    near = _customer_plan(ctx, inp)
    sh = Shown(ctx.currency)
    sh.line("Name", inp.name)
    sh.line("Email", inp.email or "none given")
    sh.line("City", inp.city or "none given")
    if near is not None:
        sh.warning(f"{near['name']} is already a customer; this adds {inp.name} as another one.")
    sh.book(NOTHING_POSTED)
    sh.title("Add a customer")
    return Preview(sh.card(), _token(inp.name, inp.email, inp.city, near and near["id"]))


def read_set_add_customer(ctx: Ctx, inp: AddCustomer) -> list[tuple[str, str]]:
    return [("company", ctx.company_id)]


def commit_add_customer(ctx: Ctx, inp: AddCustomer, key: str) -> Result:
    _customer_plan(ctx, inp)
    pid = create_party(ctx, "customer", inp.name, inp.email, inp.city)
    audit(ctx, "add_customer", "party", pid, detail={"name": inp.name, "email": inp.email, "city": inp.city})
    return Result(f"Added {inp.name} as a customer.", {"name": inp.name})


def _price_question(ctx: Ctx, args: dict) -> tuple[str, list[str]]:
    return say("item_price_missing", name=args.get("name") or "it"), []


class AddItem(Input):
    name: Annotated[str, _mark("text")] = Field(
        min_length=1, max_length=200, description="The item's name exactly as the owner wrote it.")
    unit: Annotated[str | None, _mark("text")] = Field(
        default=None, min_length=1, max_length=20,
        description="The unit it is sold in (hour, each, meter), only as the owner said it.")
    price: Annotated[Price | None, _mark("figure", ask=_price_question)] = Field(
        default=None, description="The selling price, only as the owner wrote it in digits.")
    # the owner's answer when a near name was asked about: True a new item, False the stored one
    new_item: SkipJsonSchema[bool | None] = None

    @field_validator("name")
    @classmethod
    def _unquote(cls, v: str | None) -> str | None:
        return _unquoted(v)

    def takes_no_date(self, ctx: Ctx) -> bool:
        """Adding an item posts nothing, so it takes no date."""
        return True


def _item_plan(ctx: Ctx, inp: AddItem) -> dict | None:
    """A service item (stock items, a later build): a duplicate refused, a near name asked about, then its unit and its
    price asked when missing, never defaulted. Returns the near item the owner said is another one, if any."""
    rows = [dict(r) for r in ctx.conn.execute(select(m.item).where(m.item.c.company_id == ctx.company_id)).mappings()]
    same = [r for r in rows if _same_name(inp.name, r["name"], True)]
    if same:
        raise Refusal(say("item_exists", name=same[0]["name"]), "item_exists")
    near = (_near(rows, inp.name, plural=True) or [None])[0]
    if near is not None and inp.new_item is False:
        raise Refusal(say("item_exists", name=near["name"]), "item_exists")
    if near is not None and inp.new_item is None:
        raise _near_question("item", inp.name, say("item_near", name=near["name"], said=inp.name), near["name"],
                             "new_item")
    if not inp.unit:
        raise Refusal(say("item_unit_missing", name=inp.name), "item_unit_missing", field="unit")
    if inp.price is None:
        raise Refusal(say("item_price_missing", name=inp.name), "item_price_missing", field="price")
    return near


def preview_add_item(ctx: Ctx, inp: AddItem) -> Preview:
    near = _item_plan(ctx, inp)
    sh = Shown(ctx.currency)
    sh.line("Item", inp.name)
    sh.line("Kind", "a service, not kept in stock")
    sh.line("Unit", inp.unit)
    sh.line("Selling price", f"{sh.unit_price(inp.price)} {ctx.currency} per {inp.unit}")
    if near is not None:
        sh.warning(f"{near['name']} is already an item; this adds {inp.name} as another one.")
    sh.book(NOTHING_POSTED)
    sh.title("Add an item")
    return Preview(sh.card(), _token(inp.name, inp.unit, inp.price, near and near["id"]))


def read_set_add_item(ctx: Ctx, inp: AddItem) -> list[tuple[str, str]]:
    return [("company", ctx.company_id)]


def commit_add_item(ctx: Ctx, inp: AddItem, key: str) -> Result:
    _item_plan(ctx, inp)
    iid = create_item(ctx, inp.name, inp.price, inp.unit)
    audit(ctx, "add_item", "item", iid, detail={"name": inp.name, "unit": inp.unit, "price": str(inp.price)})
    return Result(f"Added {inp.name} as an item, sold by the {inp.unit}.", {"name": inp.name})


MANIFEST = Manifest(
    name="core", version="0.0.1", publisher=PUBLISHER, distribution="erpclaw-core", licence="GPL-3.0-only",
    source=Source(repository="workspace", commit="workspace"), engine=">=0.0.1", after_core_revision="0001",
    depends={}, conflicts=[], owned_tables=[],  # core's tables carry the "core" owner mark (models.py)
    contracts={"get_metric": "read", "set_up_company": "write", "add_customer": "write", "add_item": "write"},
    calls=[], events=Events(emits=[], consumes=[]), slots={},
    voucher_types=["journal", "opening", "year_end_close"], capabilities=Capabilities(network=[], files=[]),
    metrics=["receivables"],
)

PLUGIN = Plugin(
    manifest=MANIFEST, kind="core",
    contracts=[Contract("get_metric", "core", "read",
                        "Get a business figure (money owed, sales, top customers, invoice counts) for a period. "
                        "Use this for every figure; never calculate figures yourself.",
                        GetMetric, run=get_metric, schema_hook=_metric_hook, amounts=("value", "row"), planned=REPORTS,
                        plain="give figures and reports", planned_name=FIGURES),
               WriteContract(SETUP, "core", "write",
                             "Set up the owner's new books: the business name and the currency, in the owner's own "
                             "words. The engine asks the rest. The owner approves a card before anything is saved.",
                             SetUpCompany, preview=preview_set_up, commit=commit_set_up,
                             find_by_operation_key=_committed, read_set=read_set_set_up,
                             synonyms=("set up my books", "set up the books", "set up my company", "company setup"),
                             plain="set up your books", planned_name="setting up your books", moves_money=False,
                             no_date_reason={"en": "setting up your books posts nothing."}),
               WriteContract("add_customer", "core", "write",
                             "Add a new customer, with the name, email and city exactly as the owner gave them. The "
                             "owner approves a card before anything is saved.",
                             AddCustomer, preview=preview_add_customer, commit=commit_add_customer,
                             find_by_operation_key=_committed, read_set=read_set_add_customer,
                             synonyms=("add a customer", "new customer", "create a customer"),
                             plain="add customers", planned_name="adding a customer", moves_money=False,
                             no_date_reason={"en": "adding a customer posts nothing."}),
               # its selling price is a money figure the books keep, shown on its card
               WriteContract("add_item", "core", "write",
                             "Add a new item the owner sells as a service, with its name, unit and selling price "
                             "exactly as the owner gave them. The owner approves a card before anything is saved.",
                             AddItem, preview=preview_add_item, commit=commit_add_item,
                             find_by_operation_key=_committed, read_set=read_set_add_item,
                             synonyms=("add an item", "new item", "add a product", "new product", "add a service"),
                             plain="add items", planned_name="adding an item", moves_money=True,
                             no_date_reason={"en": "adding an item posts nothing."})],
    metrics=[RECEIVABLES],
)
