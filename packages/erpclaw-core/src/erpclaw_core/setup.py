"""Company setup: currencies, chart, fiscal periods and installed plugins."""
from __future__ import annotations

import calendar
import uuid
from datetime import date

from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Connection, Engine

from . import models as m
from .administration import ensure_owner
from .common import Refusal, new_id, now
from .db import OWNER_MARK, SETUP_LOCK_KEY, books_identity, create_all, owner
from .migrate import upgrade
from .periods import fiscal_year_bounds
from .plugins import MESSAGE_SLOTS, REGISTRY, install_into, load_plugins

CHART = [  # number, name, root, type, is_group, parent; the normal side is the root's unless SIDES says otherwise
    ("1000", "Assets", "asset", None, True, None),
    ("1100", "Bank", "asset", "bank", False, "1000"),
    ("1200", "Accounts Receivable", "asset", "receivable", False, "1000"),
    ("1300", "Inventory", "asset", "stock", False, "1000"),
    ("2000", "Liabilities", "liability", None, True, None),
    ("2300", "Stock Received Not Billed", "liability", "stock_received_not_billed", False, "2000"),
    ("2310", "Asset Received Not Billed", "liability", None, False, "2000"),
    ("3000", "Equity", "equity", None, True, None),
    ("3100", "Retained Earnings", "equity", "retained_earnings", False, "3000"),
    ("4000", "Income", "income", None, True, None),
    ("4100", "Sales", "income", "income", False, "4000"),
    ("4130", "Sales Returns", "income", "sales_returns", False, "4000"),
    ("5000", "Cost of Goods Sold", "expense", None, True, None),
    ("5100", "Cost of Goods Sold", "expense", "cost_of_goods_sold", False, "5000"),
    ("6000", "Expenses", "expense", None, True, None),
    ("6810", "Realized Exchange Gain/Loss", "expense", "exchange_gain_loss_realised", False, "6000"),
    ("6820", "Unrealized Exchange Gain/Loss", "expense", "exchange_gain_loss_unrealised", False, "6000"),
    ("6900", "Rounding", "expense", "rounding", False, "6000"),
]
SIDES = {"4130": "debit"}  # sales returns: income, debit-normal


ALREADY = "These books already exist."


def create_company(engine: Engine, name: str, currency: str = "USD", fy_start_month: int = 1,
                   years: range = range(2025, 2028), date_order: str | None = None) -> str:
    """A new company. `date_order` is how the owner writes figure dates ("MDY" or "DMY"), unset by default: it is
    the owner's choice and never follows from the currency."""
    if date_order is not None and date_order not in m.DATE_ORDERS:
        raise ValueError(f"date order {date_order!r} is not one of {', '.join(m.DATE_ORDERS)}")
    create_all(engine)
    load_plugins()
    with engine.begin() as conn:
        if conn.execute(select(m.currency.c.code).where(m.currency.c.code == currency)).first() is None:
            raise ValueError(f"currency {currency} is not supported")
        cid = new_id()
        conn.execute(m.company.insert().values(id=cid, name=name, functional_currency=currency,
                                               fiscal_year_start_month=fy_start_month, date_order=date_order,
                                               status="ready", created_at=now()))
        _chart(conn, cid)
        for fy in years:
            open_year(conn, cid, fy, fy_start_month)
        ensure_owner(conn, cid)  # the owner principal holds every permission on each company
        _install_plugins(conn)
    return cid


def create_own_books(engine: Engine, runtime_role: str | None = None) -> str:
    """The trusted bootstrap of the owner's own books, in one transaction: the schema and the first-party plugins, as
    `create_company` installs them, and one pending company with the base chart, its chain head and the owner's
    grants. It makes no business choice: no name, currency, fiscal year, date order, party or item; those are the
    owner's, on the setup card. Refused, changing nothing, when the books already hold a company; books that hold the
    schema and no company (made by an earlier step that stopped) are finished. With `runtime_role`, its grants are
    applied in the same transaction."""
    load_plugins()
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": SETUP_LOCK_KEY})  # one bootstrap at a time
        if inspect(conn).has_table(m.company.name) and conn.execute(select(m.company.c.id).limit(1)).first():
            raise Refusal(ALREADY, "books_exist")
        _schema(conn)
        cid = new_id()
        conn.execute(m.company.insert().values(id=cid, name=None, functional_currency=None,
                                               fiscal_year_start_month=None, date_order=None, status="pending",
                                               created_at=now()))
        _chart(conn, cid)
        ensure_owner(conn, cid)
        _install_plugins(conn)
        if runtime_role:
            from .grants import apply_grants

            apply_grants(conn, runtime_role)
    return cid


def _schema(conn: Connection) -> None:
    """What `db.create_all` makes, on the caller's transaction: core's tables by migration, the engine's ownership
    mark and the books' identity, each only if absent."""
    upgrade(conn)
    if not conn.execute(select(owner.c.mark)).first():
        conn.execute(owner.insert().values(mark=OWNER_MARK))
    if not conn.execute(select(books_identity.c.books_id)).first():  # written once, at the books' first init
        conn.execute(books_identity.insert().values(id=1, books_id=str(uuid.uuid4())))


def _chart(conn: Connection, cid: str) -> None:
    """The chain head (it exists before the first posting) and the base chart, which names
    no currency and no year."""
    conn.execute(m.posting_chain_head.insert().values(company_id=cid, last_seq=0, last_hash="0" * 64))
    ids: dict[str, str] = {}
    for number, aname, root, atype, grp, parent in CHART:
        ids[number] = new_id()
        side = {"normal_side": SIDES[number]} if number in SIDES else {}
        conn.execute(m.account.insert().values(id=ids[number], company_id=cid, number=number, name=aname,
                                               root_type=root, account_type=atype, is_group=grp,
                                               parent_id=ids.get(parent) if parent else None, **side))


def open_year(conn: Connection, cid: str, fy: int, fy_start_month: int) -> None:
    """Fiscal year `fy`, open, with twelve calendar-month periods, contiguous from its start."""
    start, end = fiscal_year_bounds(fy, fy_start_month)
    fyid = new_id()
    conn.execute(m.fiscal_year.insert().values(id=fyid, company_id=cid, label=fy, start_date=start,
                                               end_date=end, status="open"))
    for i in range(12):
        mi = (start.month - 1 + i) % 12 + 1
        y = start.year + (start.month - 1 + i) // 12
        conn.execute(m.fiscal_period.insert().values(
            id=new_id(), company_id=cid, fiscal_year_id=fyid, period_no=i + 1, start_date=date(y, mi, 1),
            end_date=date(y, mi, calendar.monthrange(y, mi)[1]), status="open"))


def _install_plugins(conn: Connection) -> None:
    """The first-party plugins these books do not have yet; never a mail connector that sends for real."""
    installed = set(conn.execute(select(m.plugin_install.c.name)).scalars())  # per books, not per company
    # one message connector per set of books: a set that has one keeps it, whichever the owner chose
    connector = any(MESSAGE_SLOTS & set(REGISTRY.plugins[n].slots) for n in installed if n in REGISTRY.plugins)
    for name in REGISTRY.order:  # dependencies first; core is not an installable plugin
        p = REGISTRY.plugins[name]
        # a mail connector that sends for real is the owner's choice, installed by them; the folder connector is not
        sends = "send_message" in p.slots
        if name == "core" or name in installed or sends or (connector and MESSAGE_SLOTS & set(p.slots)):
            continue
        install_into(conn, p)  # its branch, then its record
        connector = connector or bool(MESSAGE_SLOTS & set(p.slots))
