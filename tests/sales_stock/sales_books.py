"""Books for the stock-backed sales tests: a fresh company, its customers, items kept in stock, warehouses on their
inventory accounts, and stock received through the stock plugin's explicit trusted path. Each answer key in
tests/expected_books/ starts from books like these."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select, update

from erpclaw_core import approvals
from erpclaw_core import models as m
from erpclaw_core.common import company_ctx, new_id
from erpclaw_core.plugins import REGISTRY
from erpclaw_core.setup import create_company
from erpclaw_stock import service as stock
from erpclaw_stock import tables as st
from erpclaw_stock.service import Trusted

D = Decimal


class Books:
    """One company's books, with names mapped to ids."""

    def __init__(self, engine, name: str = "Stock Sales Co", returns_role: bool = True):
        self.engine = engine
        self.company = create_company(engine, f"{name} {new_id()[:8]}")
        self.ids: dict[str, str] = {}
        if not returns_role:  # books whose chart has no sales returns account: credits debit sales income
            with engine.begin() as conn:
                conn.execute(update(m.account).where(m.account.c.company_id == self.company,
                                                     m.account.c.account_type == "sales_returns").values(
                    account_type=None))

    def ctx(self, conn):
        return company_ctx(conn, company_id=self.company)

    def customer(self, name: str) -> str:
        with self.engine.begin() as conn:
            self.ids[name] = new_id()
            conn.execute(m.party.insert().values(id=self.ids[name], company_id=self.company, kind="customer",
                                                 name=name))
        return self.ids[name]

    def stock_account(self, number: str, name: str) -> str:
        with self.engine.begin() as conn:
            parent = conn.execute(select(m.account.c.parent_id).where(  # beside the chart's own inventory account
                m.account.c.company_id == self.company, m.account.c.account_type == "stock").order_by(
                m.account.c.number)).scalars().first()
            aid = new_id()
            conn.execute(m.account.insert().values(id=aid, company_id=self.company, number=number, name=name,
                                                   root_type="asset", account_type="stock", is_group=False,
                                                   parent_id=parent, normal_side="debit"))
        return aid

    def warehouse(self, name: str, account: str | None = None, default: bool = False) -> str:
        with self.engine.begin() as conn:
            self.ids[name] = stock.create_warehouse(self.ctx(conn), name, account, default=default)
        return self.ids[name]

    def item(self, name: str, rate: str, method: str = "moving_average", default_warehouse: str | None = None) -> str:
        with self.engine.begin() as conn:
            iid = new_id()
            conn.execute(m.item.insert().values(id=iid, company_id=self.company, name=name, unit="unit",
                                                standard_rate=rate))
            stock.make_stock_item(self.ctx(conn), iid, method, default_warehouse)
        self.ids[name] = iid
        return iid

    def receive(self, item: str, warehouse: str, qty: str, rate: str, when: date) -> None:
        trusted = Trusted("the stock-backed sales tests' starting stock")
        with self.engine.begin() as conn:
            ctx = self.ctx(conn)
            stock.receive(ctx, f"pr-{new_id()[:8]}", self.ids[item], self.ids[warehouse], qty, rate, when,
                          trusted=trusted)
            trusted.verify(ctx)

    def run(self, contract: str, **args) -> approvals.Outcome:
        card = approvals.propose(self.engine, REGISTRY.contracts[contract], args, company_id=self.company)
        return card, approvals.approve(self.engine, card.approval_id, card.digest)

    # ---------------------------------------------------------------- reading the books

    def posting(self, voucher_type: str, voucher_id: str) -> list[tuple[str, str | None, Decimal, Decimal]]:
        """(account number, party name, debit, credit) for a voucher's posting, by account number then side."""
        le, p, a, party = m.ledger_entry, m.posting, m.account, m.party
        with self.engine.connect() as conn:
            rows = conn.execute(select(a.c.number, party.c.name, le.c.debit, le.c.credit)
                                .join(p, p.c.id == le.c.posting_id).join(a, a.c.id == le.c.account_id)
                                .outerjoin(party, party.c.id == le.c.party_id)
                                .where(p.c.company_id == self.company, p.c.voucher_type == voucher_type,
                                       p.c.voucher_id == voucher_id)).all()
        return sorted((r.number, r.name, r.debit, r.credit) for r in rows)

    def head(self, item: str, warehouse: str) -> tuple[Decimal, Decimal]:
        with self.engine.connect() as conn:
            h = stock.head(self.ctx(conn), self.ids[item], self.ids[warehouse])
        return (h["qty"], h["value"]) if h else (D(0), D("0.00"))

    def stock_rows(self, voucher_id: str) -> list[tuple[str, str, Decimal, Decimal]]:
        """(direction, warehouse name, qty, value) of a voucher's stock entries, in sequence."""
        e, w = st.entry, st.warehouse
        with self.engine.connect() as conn:
            rows = conn.execute(select(e.c.direction, w.c.name, e.c.qty, e.c.value).join(w, w.c.id == e.c.warehouse_id)
                                .where(e.c.company_id == self.company, e.c.voucher_id == voucher_id)
                                .order_by(e.c.seq)).all()
        return [tuple(r) for r in rows]

    def verify(self) -> None:
        with self.engine.connect() as conn:
            ctx = self.ctx(conn)
            assert conn.execute(select(st.entry.c.id).where(st.entry.c.company_id == self.company)).first()
            assert stock.verify_stock(ctx) == []
