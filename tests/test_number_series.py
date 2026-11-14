"""Document kinds own independent, gapless number series."""
import ast
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

import pytest

from sqlalchemy import select

from erpclaw_core import approvals, masters, models as m
from erpclaw_core.common import company_ctx
from erpclaw_core.plugins import REGISTRY
from erpclaw_core.setup import create_company
from erpclaw_stock import service as stock


def test_opening_stock_and_sales_orders_have_independent_gapless_sequences(engine):
    company = create_company(engine, "Number Series Co", years=range(2026, 2027))
    items = ("Copper Wire", "Circuit Board")
    with engine.begin() as conn:
        ctx = company_ctx(conn, company_id=company)
        stock.create_warehouse(ctx, "Main Warehouse", default=True)
        masters.create_party(ctx, "customer", "Acme Corporation")
        for name in items:
            item = masters.create_item(ctx, name, Decimal("10"))
            stock.make_stock_item(ctx, item, "fifo")

    def run(contract, **args):
        card = approvals.propose(engine, REGISTRY.contracts[contract], args, company_id=company)
        result = approvals.approve(engine, card.approval_id, card.digest)
        assert result.status == "committed", result.text
        return result.data["number"]

    openings, orders = [], []
    for item in items:
        openings.append(run("record_opening_stock", lines=[{
            "item": item, "warehouse": "Main Warehouse", "quantity": "5", "rate": "2.50",
        }]))
        orders.append(run("create_sales_order", customer="Acme Corporation",
                          items=[{"item": item, "quantity": "1"}], delivery_date="2026-11-30"))

    assert openings == ["OS-2026-00001", "OS-2026-00002"]
    assert orders == ["SO-2026-00001", "SO-2026-00002"]
    with engine.connect() as conn:
        rows = conn.execute(select(m.number_series.c.series, m.number_series.c.fiscal_year,
                                   m.number_series.c.next_value).where(
            m.number_series.c.company_id == company)).all()
    assert sorted(rows) == [("OS", 2026, 3), ("SO", 2026, 3)]


def _number_series_callers(source, path):
    callers = defaultdict(list)
    for node in ast.walk(ast.parse(source, filename=str(path))):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if not (isinstance(function, ast.Name) and function.id == "next_number"
                or isinstance(function, ast.Attribute) and function.attr == "next_number"):
            continue
        location = f"{path}:{node.lineno}"
        assert (len(node.args) >= 2 and not isinstance(node.args[0], ast.Starred)
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
                and node.args[1].value.isascii() and node.args[1].value.isalpha()
                and node.args[1].value.isupper()), f"Unsupported next_number prefix at {location}"
        callers[node.args[1].value].append(location)
    return callers


def test_variable_prefix_is_not_silently_skipped():
    source = '''next_number(ctx, "SO", today)
series = "SO"
next_number(other_context, series, today)
'''
    with pytest.raises(AssertionError, match=r"Unsupported next_number prefix at sample.py:3"):
        _number_series_callers(source, "sample.py")


def test_every_next_number_caller_has_a_distinct_prefix():
    root = Path(__file__).resolve().parents[1]
    callers = defaultdict(list)
    for directory in ("packages", "plugins"):
        for path in sorted((root / directory).glob("*/src/**/*.py")):
            for prefix, sites in _number_series_callers(path.read_text(), path.relative_to(root)).items():
                callers[prefix].extend(sites)

    assert callers, "No next_number callers found"
    collisions = {prefix: sites for prefix, sites in callers.items() if len(sites) > 1}
    assert not collisions, f"Number series prefixes shared by multiple callers: {collisions}"
