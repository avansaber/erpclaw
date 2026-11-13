"""Filtered master lists report matches and the true size of the books."""
from decimal import Decimal

import pytest
from datetime import datetime, timezone

from erpclaw_core import models
from erpclaw_core.common import Ctx, company_ctx, new_id
from erpclaw_core.db import read_only
from erpclaw_core.masters import create_item, create_party
from erpclaw_core.plugins import REGISTRY


@pytest.fixture
def empty_fictional_books(engine):
    with engine.begin() as conn:
        own = company_ctx(conn)
        company_id = new_id()
        conn.execute(models.company.insert().values(id=company_id, name="Fictional Books",
                     functional_currency=own.currency, fiscal_year_start_month=1,
                     created_at=datetime.now(timezone.utc), status="ready"))
        return company_id, "Fictional Books", own.currency, own.today


@pytest.fixture
def books(engine, empty_fictional_books):
    with engine.begin() as conn:
        ctx = Ctx(conn, *empty_fictional_books)
        for name in ("Circuit Board", "Copper Wire", "Motor Housing Unit"):
            create_item(ctx, name, Decimal("10"))
        for name in ("Hooli", "Pied Piper"):
            create_party(ctx, "customer", name)
        create_party(ctx, "supplier", "Circuit Box")
        return empty_fictional_books


def listing(engine, books, kind, name_filter=None, page=1):
    contract = REGISTRY.contracts[f"list_{kind}s"]
    with read_only(engine) as conn:
        return contract.run(Ctx(conn, *books), contract.input_model(name_filter=name_filter, page=page))


@pytest.mark.parametrize("name_filter", ["circuit boards", "Circuit board", "circuit-board", "circuit-boards"])
def test_item_normalisation_and_plural(engine, books, name_filter):
    result = listing(engine, books, "item", name_filter)
    assert result.data["count"] == 1
    assert "Circuit Board" in result.text
    assert "Copper Wire" not in result.text


@pytest.mark.parametrize("kind,name_filter,total", [
    ("item", "boards", 3), ("item", "---", 3), ("customer", "missing", 2), ("supplier", "missing", 1),
])
def test_unmatched_filter_reports_real_total(engine, books, kind, name_filter, total):
    result = listing(engine, books, kind, name_filter)
    assert result.data["count"] == 0
    assert f'No {kind}s match "{name_filter}".' in result.text
    all_text = f"There is 1 {kind} in all." if total == 1 else f"There are {total} {kind}s in all."
    assert all_text in result.text
    assert "yet" not in result.text and "You can add" not in result.text


def test_customer_and_supplier_normalisation_and_plural(engine, books):
    assert "Pied Piper" in listing(engine, books, "customer", "pied-piper").text
    assert "Circuit Box" in listing(engine, books, "supplier", "circuit boxes").text


@pytest.mark.parametrize("kind,article", [("item", "an"), ("customer", "a"), ("supplier", "a")])
@pytest.mark.parametrize("name_filter", [None, "missing"])
def test_truly_empty_books(engine, empty_fictional_books, kind, article, name_filter):
    result = listing(engine, empty_fictional_books, kind, name_filter)
    assert f"There are no {kind}s yet." in result.text
    assert f"You can add {article} {kind}." in result.text
    assert "match" not in result.text


def test_empty_page_is_not_a_no_match(engine, books):
    result = listing(engine, books, "item", "circuit boards", page=2)
    assert result.data["count"] == 1
    assert "There are no items on this page." in result.text
