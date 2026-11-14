"""Contracts and metrics the core itself provides: get_metric and receivables; and the owner's own books by chat
(set_up_company, add_customer, add_supplier, add_cash_account, add_item)."""
from __future__ import annotations

import calendar
import datetime as dt
import hashlib
import json
import re
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, WithJsonSchema, StringConstraints, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema
from sqlalchemy import func, select

from . import models as m, opening_position, outbox, supplier_payment_reversals as supplier_reversal
from .catalogue import stock_item_creation_on
from .common import Ctx, Refusal, audit, new_id
from .contracts import FIGURES, Amount, Contract, Input, Preview, Result, Shown, WriteContract
from .ledger import account_by_number, cash_account
from .masters import (_near, _norm, _same_name, create_item, create_party, customers, find_customer, find_item,
                      find_supplier, master_page, suppliers)
from .metrics import Metric, MetricResult, MetricRow, render
from .periods import Period, fiscal_year_bounds, fiscal_year_of, resolve
from .manifest import PUBLISHER, Capabilities, Events, Manifest, Source
from .plugins import REGISTRY, Plugin, call_service, enabled_plugins
from .textkey import text_key
from .setup import open_year


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
                         "unit": res.unit, "row_count": res.row_count,
                         "rows": [{"name": r.label, "value": str(r.value)} for r in res.rows[:5]],
                         "from": str(period.start), "to": str(period.end)}, figures, sh.amounts)


# ---------- the owner's own books: setup and masters ----------

SETUP = "set_up_company"
# The owner-facing wording, as the language table in erpclaw_assistant/resolve.py gives it: core cannot
# read the assistant's table, so the keys it says are repeated here, word for word.
TEXT = {"en": {
    "no_records": "There are no {what}s yet.",
    "master_no_match": "No {plural} match \"{name_filter}\".",
    "master_all_one": "There is 1 {what} in all.",
    "master_all": "There are {count} {plural} in all.",
    "master_total": "Total {plural}: {count}.",
    "master_page": "{plural_capitalised}, page {page}:",
    "master_empty_page": "There are no {plural} on this page.",
    "master_name": "{name}",
    "master_item": "{name}: {price} {currency} per {unit}",
    "master_more": "Ask for page {next_page} to see more, or ask for a {what} by name.",
    "master_add": "You can add {article} {what}.",
    "planned_singular": "{name} is not in ERPClaw yet.",
    "planned_plural": "{name} are not in ERPClaw yet.",
    "none_installed_not_done": "I didn't do anything with that.",
    "none_installed_action": "If you want to {action}, say so with the details.",
    "none_installed_multiple": "If you want to do one of those, say so with the details.",
    "setup_name_missing": "What's your business called?",
    "setup_currency_missing": "Which currency do you keep your books in?",
    "setup_currency_planned": ("I can keep books in US dollars only for now; other currencies come with the country "
                               "packs (3.1). Nothing was set up."),
    "setup_fy_start_missing": "When does your financial year start? January, or another month?",
    "setup_date_order_missing": "How do you write dates: 03/04 for 3 April, or for 4 March?",
    "books_not_set_up": "Your books aren't set up yet.",
    "customer_exists": "{name} is already a customer.",
    "customer_near": "Did you mean {name} ({city}), already a customer, or is {said} a new one?",
    "supplier_exists": "{name} is already a supplier.",
    "supplier_near": "Did you mean {name} ({city}), already a supplier, or is {said} a new one?",
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


MASTER_PAGE_SIZE = 25


class ListMasters(Input):
    name_filter: str | None = Field(default=None, min_length=1, max_length=200,
                                    description="Name or part of a name to filter the list, as the owner said it.")
    page: int = Field(default=1, ge=1, le=1000000,
                      description="Page number requested by the owner; the first page is 1.")


def _list_masters(ctx: Ctx, inp: ListMasters, what: str) -> Result:
    plural = {"customer": "customers", "supplier": "suppliers", "item": "items"}[what]
    total, rows = master_page(ctx, what, (inp.page - 1) * MASTER_PAGE_SIZE, MASTER_PAGE_SIZE, inp.name_filter)
    lines = []
    all_total = total
    if total == 0 and inp.name_filter is not None:
        all_total, _ = master_page(ctx, what, 0, 0)
    if total == 0 and all_total:
        lines.append(say("master_no_match", plural=plural, name_filter=inp.name_filter))
        lines.append(say("master_all_one", what=what) if all_total == 1 else
                     say("master_all", count=all_total, plural=plural))
    elif total == 0:
        lines.append(say("no_records", what=what))
    elif not rows:
        lines.append(say("master_empty_page", plural=plural))
    else:
        lines.append(say("master_page", plural_capitalised=plural.capitalize(), page=inp.page))
    lines.append(say("master_total", plural=plural, count=total))
    amounts = []
    for row in rows:
        if what == "item":
            price = format(row["standard_rate"], "f")
            lines.append(say("master_item", name=row["name"], price=price, currency=ctx.currency, unit=row["unit"]))
            amounts.append(Amount("rate", row["name"], price, ctx.currency, kind="unit_price"))
        else:
            lines.append(say("master_name", name=row["name"]))
    if not all_total:
        lines.append(say("master_add", article="an" if what[0] in "aeiou" else "a", what=what))
    elif inp.page * MASTER_PAGE_SIZE < total:
        lines.append(say("master_more", next_page=inp.page + 1, what=what))
    return Result("\n".join(lines), {"count": total, "page": inp.page}, amounts=amounts)


def list_customers(ctx: Ctx, inp: ListMasters) -> Result:
    return _list_masters(ctx, inp, "customer")


def list_suppliers(ctx: Ctx, inp: ListMasters) -> Result:
    return _list_masters(ctx, inp, "supplier")


def list_items(ctx: Ctx, inp: ListMasters) -> Result:
    return _list_masters(ctx, inp, "item")


def resolution_field(kind: str, **how) -> dict:
    """How the assistant resolves a field from the owner's words (kept out of the tool schema)."""
    return {"erpclaw": {"kind": kind, **how}}


# Keep existing core declarations compatible; plugins use the public name.
_mark = resolution_field


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
    account_count = ctx.conn.execute(select(func.count()).select_from(m.account).where(
        m.account.c.company_id == ctx.company_id)).scalar_one()
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
    sh.book(f"Your chart of accounts: {account_count} accounts for a small business (bank, receivables, sales, cost of "
            "goods, and the rest); more are added when you need them.")
    sh.book(NOTHING_POSTED)
    sh.title("Set up your books")
    return Preview(sh.card(), _token(ctx.company_id, name, USD, month, order, *years, "account_count", account_count))


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


class AddSupplier(Input):
    name: Annotated[str, _mark("text")] = Field(
        min_length=1, max_length=200, description="The supplier's name exactly as the owner wrote it.")
    email: Annotated[str | None, _mark("text")] = Field(
        default=None, max_length=200, description="The supplier's email address, only if the owner gave one.")
    city: Annotated[str | None, _mark("text")] = Field(
        default=None, max_length=100, description="The supplier's city, only if the owner gave one.")
    # The owner's answer to the near-name question, never a model argument.
    new_supplier: SkipJsonSchema[bool | None] = None

    @model_validator(mode="before")
    @classmethod
    def _required_name(cls, data):
        if isinstance(data, dict):
            name = data.get("name")
            if isinstance(name, str):
                name = name.strip()
                if len(name) >= 2 and QUOTES.get(name[0]) == name[-1]:
                    name = name[1:-1].strip()
            if name is None or name == "":
                raise Refusal("Which supplier?", "supplier_missing")
        return data

    @field_validator("name")
    @classmethod
    def _unquote(cls, v: str) -> str:
        return _unquoted(v)

    @field_validator("email")
    @classmethod
    def _address(cls, v: str | None) -> str | None:
        return AddCustomer._address(v)

    def takes_no_date(self, ctx: Ctx) -> bool:
        """Adding a supplier posts nothing, so it takes no date."""
        return True


def _supplier_plan(ctx: Ctx, inp: AddSupplier) -> dict | None:
    """Refuse an exact duplicate and ask about a near name before any supplier is added."""
    rows = suppliers(ctx)
    same = [r for r in rows if _same_name(inp.name, r["name"], False)]
    if same:
        raise Refusal(say("supplier_exists", name=same[0]["name"]), "supplier_exists")
    near = _near(rows, inp.name)
    if not near:
        return None
    r = near[0]
    if inp.new_supplier is False:
        raise Refusal(say("supplier_exists", name=r["name"]), "supplier_exists")
    if inp.new_supplier is None:
        where = r.get("city") or r.get("email") or "no city or email on file"
        raise _near_question("supplier", inp.name, say("supplier_near", name=r["name"], city=where, said=inp.name),
                             f"{r['name']} ({where})", "new_supplier")
    return r


def preview_add_supplier(ctx: Ctx, inp: AddSupplier) -> Preview:
    near = _supplier_plan(ctx, inp)
    sh = Shown(ctx.currency)
    sh.line("Name", inp.name)
    sh.line("Email", inp.email or "none given")
    sh.line("City", inp.city or "none given")
    if near is not None:
        sh.warning(f"{near['name']} is already a supplier; this adds {inp.name} as another one.")
    sh.book("Nothing in the books changes.")
    sh.title("Add a supplier")
    return Preview(sh.card(), _token(inp.name, inp.email, inp.city, near and near["id"]))


def read_set_add_supplier(ctx: Ctx, inp: AddSupplier) -> list[tuple[str, str]]:
    return [("company", ctx.company_id)]


def commit_add_supplier(ctx: Ctx, inp: AddSupplier, key: str) -> Result:
    _supplier_plan(ctx, inp)
    pid = create_party(ctx, "supplier", inp.name, inp.email, inp.city)
    audit(ctx, "add_supplier", "party", pid, detail={"name": inp.name, "email": inp.email, "city": inp.city})
    return Result(f"Added {inp.name} as a supplier.", {"name": inp.name})


class AddCashAccount(Input):
    def takes_no_date(self, ctx: Ctx) -> bool:
        """Adding an account posts nothing, so it takes no date."""
        return True


def _cash_account_plan(ctx: Ctx, inp: AddCashAccount) -> tuple[str, dict]:
    try:
        existing = cash_account(ctx)
    except Refusal as r:
        if r.code != "no_cash_account":
            raise
    else:
        raise Refusal(f"{existing['number']} {existing['name']} already exists.", "cash_account_exists")
    used = set(ctx.conn.execute(select(m.account.c.number).where(
        m.account.c.company_id == ctx.company_id)).scalars())
    number = 1110
    while str(number) in used:
        number += 10
    return str(number), account_by_number(ctx, "1000")


def preview_add_cash_account(ctx: Ctx, inp: AddCashAccount) -> Preview:
    number, parent = _cash_account_plan(ctx, inp)
    sh = Shown(ctx.currency)
    sh.line("Account", f"{number} Cash on Hand (cash you hold)")
    sh.line("Parent", "1000 Assets")
    sh.book("Nothing in the books changes; this only adds the place the money will be paid from.")
    sh.title("Add Cash on Hand")
    return Preview(sh.card(), _token(number, parent["id"]))


def read_set_add_cash_account(ctx: Ctx, inp: AddCashAccount) -> list[tuple[str, str]]:
    # The whole company chart supplies both the occupied numbers and the possible cash names, including inserts.
    return [("company", ctx.company_id)] + [("account", aid) for aid in ctx.conn.execute(
        select(m.account.c.id).where(m.account.c.company_id == ctx.company_id)).scalars()]


def commit_add_cash_account(ctx: Ctx, inp: AddCashAccount, key: str) -> Result:
    number, parent = _cash_account_plan(ctx, inp)
    aid = new_id()
    ctx.conn.execute(m.account.insert().values(
        id=aid, company_id=ctx.company_id, number=number, name="Cash on Hand", root_type="asset",
        account_type="bank", parent_id=parent["id"], normal_side="debit", is_group=False, frozen=False))
    detail = {"name": "Cash on Hand", "number": number}
    audit(ctx, "add_cash_account", "account", aid, detail=detail)
    return Result(f"Added {number} Cash on Hand.", detail)


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
    # These choices come from engine questions, never from the model's arguments.
    kind: SkipJsonSchema[Literal["service", "stock"] | None] = None
    valuation_method: SkipJsonSchema[Literal["fifo", "moving_average"] | None] = None
    warehouse: SkipJsonSchema[str | None] = None  # chosen stored name, not a model-supplied id
    inventory_account: SkipJsonSchema[str | None] = None  # chosen account number for a new warehouse
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
    """Keep the existing duplicate/near-name and unit/price rules before planning stock setup."""
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
    if inp.kind is None:
        stock_on = stock_item_creation_on(enabled_plugins(ctx.conn))
        choices = [{"label": "A service I sell", "ref": "service"}]
        if stock_on:
            choices.append({"label": "Goods I keep in stock", "ref": "stock"})
        question = ("Is this a service you sell, or goods you keep in stock?" if stock_on
                    else "Is this a service you sell?")
        raise Refusal(question, "item_kind_missing", field="kind", choices=choices)
    if inp.kind == "service" and any(v is not None for v in
                                      (inp.valuation_method, inp.warehouse, inp.inventory_account)):
        raise Refusal("A valuation method and warehouse apply only to goods kept in stock.",
                      "stock_fields_for_service")
    if not inp.unit:
        raise Refusal(say("item_unit_missing", name=inp.name), "item_unit_missing", field="unit")
    if inp.price is None:
        raise Refusal(say("item_price_missing", name=inp.name), "item_price_missing", field="price")
    return near


def _stock_setup_call(ctx: Ctx, name: str, args: dict) -> dict:
    """Stock owns its tables and authority; core consumes only its declared setup services."""
    try:
        return call_service(ctx, "core", "stock." + name, args)
    except Refusal as rf:
        if rf.code == "plugin_not_enabled":
            raise Refusal("Keeping stock isn't switched on in these books.", rf.code) from None
        if name == "preview_create_warehouse" and rf.code == "account_ambiguous":
            # Stock's refs are account numbers. Bind the consumer field, then
            # resolve its number to a company-scoped core id before the next call.
            data = {**rf.data, "field": "inventory_account"}
            raise Refusal(rf.message, rf.code, rf.options, rf.capability, **data) from None
        raise


def _item_stock_plan(ctx: Ctx, inp: AddItem) -> dict | None:
    if inp.kind != "stock":
        return None
    warehouses = _stock_setup_call(ctx, "list_warehouses", {})["warehouses"]
    if inp.valuation_method is None:
        raise Refusal("How should these goods be valued? FIFO uses the oldest stock's cost first; moving average "
                      "uses the average cost of the stock on hand.", "valuation_method_missing",
                      field="valuation_method", choices=[{"label": "FIFO", "ref": "fifo"},
                                                        {"label": "Moving average", "ref": "moving_average"}])
    chosen = None
    if inp.warehouse is not None:
        chosen = next((row for row in warehouses if _norm(row["name"]) == _norm(inp.warehouse)), None)
        if chosen is None and (warehouses or _norm(inp.warehouse) != _norm("Main Warehouse")):
            choices = [{"label": row["name"], "ref": row["name"]} for row in warehouses] or [
                {"label": "Create Main Warehouse", "ref": "Main Warehouse"}]
            raise Refusal("Which warehouse should this item use?", "warehouse_unknown", field="warehouse",
                          choices=choices)
    elif len(warehouses) == 1:
        chosen = warehouses[0]
    elif len(warehouses) > 1:
        raise Refusal("Which warehouse should this item use?", "warehouse_ambiguous", field="warehouse",
                      choices=[{"label": row["name"], "ref": row["name"]} for row in warehouses])
    create = None
    read_set = []
    if chosen is None:
        args = {"name": "Main Warehouse", "company_default": True}
        if inp.inventory_account is not None:
            account = ctx.conn.execute(select(m.account).where(
                m.account.c.company_id == ctx.company_id,
                m.account.c.number == inp.inventory_account)).mappings().first()
            if account is None:
                # Reuse the producer's current account choices. Even if just one
                # remains, ask it explicitly rather than replacing the stale choice.
                available = _stock_setup_call(ctx, "preview_create_warehouse", args)["account"]
                raise Refusal("Choose an inventory account from these books.", "account_unknown",
                              field="inventory_account", choices=[{
                                  "label": f"{available['number']} {available['name']}",
                                  "ref": available["number"]}])
            args["account"] = account["id"]
        create = _stock_setup_call(ctx, "preview_create_warehouse", args)
        read_set.extend(create["read_set"])
    elif inp.inventory_account is not None:
        raise Refusal("An existing warehouse already has its inventory account.", "warehouse_account_fixed")
    stock_args = {"method": inp.valuation_method}
    if chosen is not None:
        stock_args["default_warehouse"] = chosen["warehouse"]
    checked = _stock_setup_call(ctx, "preview_make_stock_item", stock_args)
    read_set.extend(checked["read_set"])
    return {"warehouse": chosen, "create": create, "method": checked["method"], "read_set": read_set}


def preview_add_item(ctx: Ctx, inp: AddItem) -> Preview:
    near = _item_plan(ctx, inp)
    stock = _item_stock_plan(ctx, inp)
    sh = Shown(ctx.currency)
    sh.line("Item", inp.name)
    sh.line("Kind", "goods kept in stock" if stock else "a service, not kept in stock")
    sh.line("Unit", inp.unit)
    sh.line("Selling price", f"{sh.unit_price(inp.price)} {ctx.currency} per {inp.unit}")
    if stock:
        sh.line("Valuation method", "FIFO" if stock["method"] == "fifo" else "Moving average")
        if stock["create"]:
            new = stock["create"]
            sh.line("Warehouse", f"Create {new['name']} as the company's default warehouse")
            sh.line("Inventory account", f"{new['account']['number']} {new['account']['name']}")
        else:
            sh.line("Warehouse", stock["warehouse"]["name"])
        sh.book("Stock: none yet. Receive or count stock to add some.")
    if near is not None:
        sh.warning(f"{near['name']} is already an item; this adds {inp.name} as another one.")
    sh.book(NOTHING_POSTED)
    sh.title("Add an item")
    return Preview(sh.card(), _token(inp.name, inp.unit, inp.price, near and near["id"], inp.kind,
                                    json.dumps(stock, sort_keys=True)))


def read_set_add_item(ctx: Ctx, inp: AddItem) -> list[tuple[str, str]]:
    stock = _item_stock_plan(ctx, inp)
    return [("company", ctx.company_id)] + ([tuple(row) for row in stock["read_set"]] if stock else [])


def commit_add_item(ctx: Ctx, inp: AddItem, key: str) -> Result:
    _item_plan(ctx, inp)
    stock = _item_stock_plan(ctx, inp)  # approval already recomputed the same plan/token under the company lock
    iid = create_item(ctx, inp.name, inp.price, inp.unit)
    if stock:
        if stock["create"]:
            new = stock["create"]
            warehouse = _stock_setup_call(ctx, "create_warehouse", {
                "name": new["name"], "account": new["account"]["account"],
                "company_default": new["company_default"]})["warehouse"]
        else:
            warehouse = stock["warehouse"]["warehouse"]
        _stock_setup_call(ctx, "make_stock_item", {
            "item": iid, "method": stock["method"], "default_warehouse": warehouse})
    audit(ctx, "add_item", "item", iid, detail={"name": inp.name, "unit": inp.unit, "price": str(inp.price),
                                              "kind": inp.kind})
    suffix = " Stock: none yet. Receive or count stock to add some." if stock else ""
    sh = Shown(ctx.currency)
    price = sh.unit_price(inp.price, "rate", inp.name)
    return Result(f"I added {inp.name} at {price} {ctx.currency} per {inp.unit}." + suffix,
                  {"name": inp.name}, figures=[price], amounts=sh.amounts)


def _supplier_payment_number(ctx: Ctx, said: str) -> str:
    from .payable_tables import supplier_payment

    return _reversal_document(ctx, supplier_payment, said, "payment")["number"]


class ReverseSupplierPaymentInput(Input):
    payment: Annotated[str, _mark("document", what="payment", prefix="SPAY", nouns=("payment",),
                                  resolve=_supplier_payment_number)] = Field(min_length=1, max_length=40,
                         description="The stored supplier payment number as the owner said it, or its trailing digits.")
    date: Annotated[dt.date | None, _mark("date", presence="optional")] = Field(
        default=None, description="The reversal date from the owner's words; defaults to today.")
    reason: Annotated[str | None, StringConstraints(strip_whitespace=False),
                      _mark("text", presence="optional")] = Field(
        default=None, description="The owner's reason, at most 140 characters; stored as audit text.")


def reversal_document(ctx: Ctx, table, said: str, kind: str, *conditions) -> dict:
    """Resolve a company document by its exact number or a unique numeric suffix, never by id."""
    query = select(table).where(table.c.company_id == ctx.company_id, *conditions)
    # commit_payment and commit_goods_received issue SPAY/GR via next_number:
    # an uppercase prefix, integer fiscal year and zero-padded sequence. Only
    # the owner's input needs normalising; compare the stored number unchanged.
    number = said.upper()
    exact = ctx.conn.execute(query.where(table.c.number == number)).mappings().first()
    if exact is not None:
        return dict(exact)
    matches = []
    if number.isascii() and number.isdigit():
        matches = list(ctx.conn.execute(query.where(table.c.number.endswith(f"-{int(number):05d}"))
                                       .order_by(table.c.number)).mappings())
    if len(matches) == 1:
        return dict(matches[0])
    if matches:
        options = [row["number"] for row in matches]
        raise Refusal(f"More than one {kind} ends in {said}: {', '.join(options)}. Which one?",
                      f"{kind}_ambiguous", options=options, field=kind,
                      choices=[{"label": n, "ref": {kind: n}} for n in options])
    raise Refusal(f"There is no such {kind} in these books.", f"{kind}_unknown")


# Existing core callers retain the same resolver; plugins use its public name.
_reversal_document = reversal_document


def _supplier_reversal_request(ctx: Ctx, inp: ReverseSupplierPaymentInput):
    from .payable_tables import supplier_payment

    pay = _reversal_document(ctx, supplier_payment, inp.payment, "payment")
    return supplier_reversal.ReverseSupplierPayment(pay["id"], inp.date or ctx.today, inp.reason)


def preview_supplier_reversal(ctx: Ctx, inp: ReverseSupplierPaymentInput) -> Preview:
    return supplier_reversal.preview_reversal(ctx, _supplier_reversal_request(ctx, inp))


def read_set_supplier_reversal(ctx: Ctx, inp: ReverseSupplierPaymentInput):
    return supplier_reversal.reversal_read_set(ctx, _supplier_reversal_request(ctx, inp))


def commit_supplier_reversal(ctx: Ctx, inp: ReverseSupplierPaymentInput, key: str) -> Result:
    supplier_reversal._approval(ctx, key)
    return supplier_reversal.commit_reversal(ctx, _supplier_reversal_request(ctx, inp), key)


class ResendMessage(Input):
    message: str = Field(min_length=1, max_length=36,
                         description="The stored message reference from the delivery being sent again.")

    def takes_no_date(self, ctx: Ctx) -> bool:
        return True


def _resend_message(ctx: Ctx, inp: ResendMessage) -> dict:
    if outbox.PLANNED:
        from .contracts import planned_text

        raise Refusal(planned_text("sending a message again", outbox.PLANNED), "planned",
                      capability="planned.messaging")
    row = ctx.conn.execute(select(m.outbox).where(
        m.outbox.c.id == inp.message, m.outbox.c.company_id == ctx.company_id)).mappings().first()
    if row is None:
        raise Refusal("There is no such message.", "no_message")
    if row["status"] not in ("unknown", "failed"):
        raise Refusal("Only a message whose sending is unknown, or that failed, can be sent again.", "not_resendable")
    return dict(row)


def preview_resend_message(ctx: Ctx, inp: ResendMessage) -> Preview:
    row = _resend_message(ctx, inp)
    sh = Shown(ctx.currency)
    sh.title("Send this message again")
    sh.line("To", row["recipient"])
    sh.line("Subject", row["subject"])
    sh.line("From address", row["from_address"] or "Not recorded")
    sh.line("From name", row["from_name"] or "Not set")
    sh.line("Reply-To", row["reply_to"] or "Not set")
    sh.line("Message-ID", row["message_id"] or "Not recorded")
    sh.line("Status", outbox.owner_text(row))
    if row["status"] == "unknown":
        sh.warning("Sending again may mean the recipient gets this message twice.")
    sh.book("The same message will be queued again. Nothing is posted to your books.")
    return Preview(sh.card(), _token(json.dumps(row, sort_keys=True, default=str)))


def read_set_resend_message(ctx: Ctx, inp: ResendMessage) -> list[tuple[str, str]]:
    return [("outbox", _resend_message(ctx, inp)["id"])]


def commit_resend_message(ctx: Ctx, inp: ResendMessage, key: str) -> Result:
    from . import approvals, principals

    aid = approvals.committing(ctx)
    card = ctx.conn.execute(select(m.approval).where(
        m.approval.c.id == aid, m.approval.c.company_id == ctx.company_id,
        m.approval.c.contract == "resend_message", m.approval.c.operation_key == key,
        m.approval.c.status == "consumed",
    )).mappings().first() if aid else None
    if card is None or json.loads(card["args_json"]) != inp.model_dump(mode="json"):
        raise Refusal("Sending again needs an approved message card.", "not_approved")
    # The approval path already holds the principals' locks and requires approve/post.
    # Actor text and a helper's grants cannot stand in for the owner's decision.
    owner = principals.owner(ctx.conn)
    if owner["disabled"] or card["principal_id"] != owner["id"] or card["approved_by"] != owner["id"]:
        raise Refusal("Only the owner may send a message again.", "forbidden")
    outbox.send_again(ctx, inp.message)
    # Approval stores this outcome in the same transaction as send_again's update.
    return Result("The message is queued to send again.", {"message": inp.message, "status": "queued"})


def find_resend_message(ctx: Ctx, key: str) -> Result | None:
    outcome, card = m.operation_outcome, m.approval
    raw = ctx.conn.execute(select(outcome.c.result_json).join(card, card.c.id == outcome.c.approval_id).where(
        outcome.c.company_id == ctx.company_id, outcome.c.key == key, outcome.c.status == "committed",
        card.c.contract == "resend_message")).scalar_one_or_none()
    if raw is None:
        return None
    result = json.loads(raw)
    return Result(result["text"], result.get("data", {}))


MANIFEST = Manifest(
    name="core", version="0.0.2", publisher=PUBLISHER, distribution="erpclaw-core", licence="GPL-3.0-only",
    source=Source(repository="workspace", commit="workspace"), engine=">=0.0.2", after_core_revision="0001",
    depends={}, conflicts=[], owned_tables=[],  # core's tables carry the "core" owner mark (models.py)
    contracts={"get_metric": "read", "list_customers": "read", "list_suppliers": "read", "list_items": "read",
               "set_up_company": "write", "add_customer": "write", "add_supplier": "write",
               "add_cash_account": "write", "add_item": "write", "resend_message": "write",
               supplier_reversal.NAME: "write", opening_position.NAME: "write"},
    calls=[], events=Events(emits=[], consumes=[]), slots={},
    voucher_types=["journal", "opening", "year_end_close", "supplier_payment"], capabilities=Capabilities(network=[], files=[]),
    metrics=["receivables"],
    uses=["stock.plan_position_stock?", "stock.write_position_stock?", "stock.list_warehouses?", "stock.preview_create_warehouse?", "stock.create_warehouse?",
          "stock.preview_make_stock_item?", "stock.make_stock_item?"],
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
               WriteContract("add_supplier", "core", "write",
                             "Add a new supplier, with the name, email and city exactly as the owner gave them. The "
                             "owner approves a card before anything is saved.",
                             AddSupplier, preview=preview_add_supplier, commit=commit_add_supplier,
                             find_by_operation_key=_committed, read_set=read_set_add_supplier,
                             synonyms=("add a supplier", "new supplier", "create a supplier"),
                             plain="add suppliers", planned_name="adding a supplier", moves_money=False,
                             no_date_reason={"en": "adding a supplier posts nothing."}),
               WriteContract("add_cash_account", "core", "write",
                             "Add Cash on Hand for cash you hold. The engine chooses its account number; the owner "
                             "approves a card before anything is saved.",
                             AddCashAccount, preview=preview_add_cash_account, commit=commit_add_cash_account,
                             find_by_operation_key=_committed, read_set=read_set_add_cash_account,
                             synonyms=("add a cash account", "new cash account", "create a cash account",
                                       "add cash on hand"),
                             plain="add cash accounts", planned_name="adding a cash account", moves_money=False,
                             no_date_reason={"en": "adding an account posts nothing."}),
               # its selling price is a money figure the books keep, shown on its card
               WriteContract("add_item", "core", "write",
                             "Add a new item, with its name, unit and selling price "
                             "exactly as the owner gave them. The owner approves a card before anything is saved.",
                             AddItem, preview=preview_add_item, commit=commit_add_item,
                             find_by_operation_key=_committed, read_set=read_set_add_item,
                             synonyms=("add an item", "new item", "add a product", "new product", "add a service"),
                             plain="add items", planned_name="adding an item", moves_money=True,
                             amounts=("rate",),
                             no_date_reason={"en": "adding an item posts nothing."}),
               Contract("list_customers", "core", "read", "List customers, or find one by name. Request a page to see more.",
                        ListMasters, run=list_customers, synonyms=("who are my customers", "list customers", "list my customers"),
                        plain="list customers", planned_name="listing customers"),
               Contract("list_suppliers", "core", "read", "List suppliers, or find one by name. Request a page to see more.",
                        ListMasters, run=list_suppliers, synonyms=("list suppliers", "list my suppliers", "who are my suppliers"),
                        plain="list suppliers", planned_name="listing suppliers"),
               Contract("list_items", "core", "read", "List items and their stored prices, or find one by name. Request a page to see more.",
                        ListMasters, run=list_items, amounts=("rate",), synonyms=("list items", "list my items"),
                        plain="list items", planned_name="listing items"),
               WriteContract("resend_message", "core", "write",
                             "Queue the same failed or uncertain message again, only after the owner's approval.",
                             ResendMessage, preview=preview_resend_message, commit=commit_resend_message,
                             find_by_operation_key=find_resend_message, read_set=read_set_resend_message,
                             planned=outbox.PLANNED,
                             synonyms=("send again", "resend a message", "resend email"),
                             plain="send a message again", planned_name="sending a message again", moves_money=False,
                             no_date_reason={"en": "Sending a message again posts nothing to the books."}),
               WriteContract(supplier_reversal.NAME, "core", "write",
                             "Reverse a stored supplier payment by its number, reopening its settled deliveries.",
                             ReverseSupplierPaymentInput, preview=preview_supplier_reversal,
                             commit=commit_supplier_reversal, read_set=read_set_supplier_reversal,
                             find_by_operation_key=supplier_reversal.find_reversal_by_key,
                             planned="later",
                             plain="reverse supplier payments", moves_money=True, amounts=("amount", "reopened")),
               opening_position.CONTRACT],
    metrics=[RECEIVABLES],
)
