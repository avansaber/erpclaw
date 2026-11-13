"""Currency inventory at the stock caller boundary, in yen and Kuwaiti dinars.

The manifest inventory is exact: adding an operation requires classifying its
outputs here. Decimal valuation primitives and database rows are internal, not
wire outputs. The inactive goods-received backend is exercised through a test-only
contract and real approvals; its reversal uses the registered public contract.
Unit prices retain their documented stored precision (up to four places);
posted amounts, including zero, use the books' minor unit.
"""
from dataclasses import asdict
from datetime import date
from decimal import Decimal
import os
import re

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError

from erpclaw_core import approval_scope, approvals, ledger, models as m
from erpclaw_core.common import new_id
from erpclaw_core.contracts import Input, WriteContract, check_card_amounts
from erpclaw_core.plugins import REGISTRY
from erpclaw_stock import MANIFEST, PLUGIN
from erpclaw_stock import cancellation, count_stock, goods_received, opening_stock
from erpclaw_stock import reverse_goods_received, service as stock, write_off
from erpclaw_stock.entry_posting import entry_posting_lines
from service_plugins import install
from stock_fixtures import Books, consumer
from stock_setup_fixtures import SERVICES as SETUP_SERVICES, all_cases as setup_cases
from stock_setup_producer import SetupBooks, consumer as setup_consumer


DAY = date(2026, 10, 20)
SCALES = {"JPY": 0, "KWD": 3}
# Paths name every monetary output, not quantities, account numbers or IDs.
SERVICE_MONEY = {
    "stock_on_hand": ("warehouses[].value",),
    "choose_warehouse": (),
    "preview_issue": ("lines[].cost", "ledger_lines[].debit", "ledger_lines[].credit"),
    "issue": ("lines[].cost", "ledger_lines[].debit", "ledger_lines[].credit"),
    "preview_return": ("cost", "ledger_lines[].debit", "ledger_lines[].credit"),
    "return_in": ("cost", "ledger_lines[].debit", "ledger_lines[].credit"),
    "preview_reverse": ("mirror_entries[].value", "mirror_entries[].value_after", "contribution[].net"),
    "reverse_movements": ("contribution[].net",),
    "list_warehouses": (),
    "preview_create_warehouse": (),
    "create_warehouse": (),
    "preview_make_stock_item": (),
    "make_stock_item": (),
}
CONTRACTS = {
    "record_opening_stock": opening_stock,
    "write_off_stock": write_off,
    "record_stock_count": count_stock,
    "cancel_stock_movement": cancellation,
    # The contract declares value; its Result carries Shown's currency-bearing amount.
    "reverse_goods_received": reverse_goods_received,
}
DELIVERIES = {
    "record_goods_received": goods_received,
}


def test_public_output_inventory_matches_manifest():
    assert {s.name for s in PLUGIN.services} == set(SERVICE_MONEY)
    assert {s.name: s.effect for s in MANIFEST.services} == {s.name: s.effect for s in PLUGIN.services}
    assert {c.name for c in PLUGIN.contracts} == set(CONTRACTS)
    assert MANIFEST.contracts == {c.name: c.effect for c in PLUGIN.contracts}
    assert SETUP_SERVICES == {name for name, fields in SERVICE_MONEY.items() if not fields} - {"choose_warehouse"}
    assert not set(DELIVERIES) & set(MANIFEST.contracts)
    assert {c.amounts for c in PLUGIN.contracts} == {("value",)}


@pytest.fixture
def pg_engine(request):
    """Skip only a genuinely unreachable PostgreSQL, before the destructive fixture."""
    probe = create_engine(os.environ["ERPCLAW_PG_TEST_URL"])
    try:
        with probe.connect():
            pass
    except OperationalError:
        pytest.skip("Disposable PostgreSQL is unavailable in this jail")
    finally:
        probe.dispose()
    return request.getfixturevalue("engine")


@pytest.fixture(params=["JPY", "KWD"])
def currency(request):
    return request.param


@pytest.fixture(params=["moving_average", "fifo"])
def method(request):
    return request.param


def books_case(currency, method):
    return {
        "name": "stock currency outputs", "books": {
            "currency": currency,
            "warehouses": {"@wh:main": {"name": "Main Store", "account": "@acct:inv"}},
            "items": {"@item:wire": {"method": method}},
            "default_warehouse": "@wh:main",
        }, "before": [],
    }


def seed(books, *, qty="12.5", rate="4.25"):
    books.apply([{"op": "receive", "voucher": "@v:receipt", "item": "@item:wire",
                  "warehouse": "@wh:main", "qty": qty, "rate": rate, "date": DAY.isoformat()}])


def money_errors(row, field, currency, path):
    """A string plus sibling currency or a currency-bearing value object is legal."""
    value = row[field]
    carried = value.get("currency") if isinstance(value, dict) else row.get("currency")
    value = value.get("value") if isinstance(value, dict) else value
    errors = []
    if carried != currency:
        errors.append(f"{path}: currency {carried!r}, expected books currency {currency}")
    pattern = r"-?\d+" + (rf"\.\d{{{SCALES[currency]}}}" if SCALES[currency] else "")
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        errors.append(f"{path}: {value!r} is not a decimal string at scale {SCALES[currency]}")
    return errors


def field_rows(out, path):
    prefix, dot, tail = path.partition(".")
    if not dot:
        yield out, prefix
    else:
        assert prefix.endswith("[]"), path
        rows = out[prefix[:-2]]
        assert rows, f"{path} was vacuously empty"
        for row in rows:
            yield from field_rows(row, tail)


def assert_money_fields(out, paths, currency):
    errors = [error for path in paths for row, field in field_rows(out, path)
              for error in money_errors(row, field, currency, path)]
    assert not errors, "\n".join(errors)


@pytest.mark.parametrize("operation", [name for name, fields in SERVICE_MONEY.items() if fields])
def test_service_money_outputs(pg_engine, monkeypatch, currency, method, operation):
    install(monkeypatch, pg_engine, consumer())
    books = Books(pg_engine, books_case(currency, method))
    seed(books)
    issue = {"voucher_type": "sales_invoice", "voucher": "@v:sale", "posting_date": DAY.isoformat(),
             "lines": [{"item": "@item:wire", "warehouse": "@wh:main", "qty": "2.5"}]}
    if operation in {"preview_return", "return_in", "preview_reverse", "reverse_movements"}:
        books.call("issue", issue, write=True)
    if operation == "stock_on_hand":
        inp = {"item": "@item:wire"}
    elif operation in {"preview_issue", "issue"}:
        inp = issue
    elif operation in {"preview_return", "return_in"}:
        inp = {"voucher_type": "sales_credit_note", "voucher": "@v:return", "posting_date": DAY.isoformat(),
               "original": {"voucher": "@v:sale", "line": 1}, "qty": "2.5"}
    else:
        inp = {"original": {"voucher_type": "sales_invoice", "voucher": "@v:sale"},
               "reversal_date": DAY.isoformat()}
        if operation == "reverse_movements":
            inp["reversal"] = {"voucher_type": "sales_invoice:reversal", "voucher": "@v:reversal"}
    out = books.call(operation, inp, write=operation in {"issue", "return_in", "reverse_movements"})
    assert_money_fields(out, SERVICE_MONEY[operation], currency)
    expected = {"stock_on_hand": {"JPY": "53", "KWD": "53.125"}}.get(
        operation, {"JPY": "11", "KWD": "10.625"})[currency]
    path = SERVICE_MONEY[operation][0]
    values = [row[field] for row, field in field_rows(out, path)]
    values = [v["value"] if isinstance(v, dict) else v for v in values]
    assert any(Decimal(v).copy_abs() == Decimal(expected) for v in values)


@pytest.mark.parametrize("operation", ["receive", "ledger_lines"])
def test_receipt_and_posting_outputs(pg_engine, currency, method, operation):
    books = Books(pg_engine, books_case(currency, method))
    trusted = stock.Trusted("currency output receipt")
    with pg_engine.begin() as conn:
        ctx = books.ctx(conn)
        if operation == "receive":
            out = stock.receive(ctx, books.sym("@v:receipt"), books.sym("@item:wire"), books.sym("@wh:main"),
                                "12.5", "4.25", DAY, trusted=trusted)
            trusted.verify(ctx)
            fields = ("value", "ledger_lines[].debit", "ledger_lines[].credit")
        else:
            move = stock._receipt_move(ctx, books.sym("@item:wire"), books.sym("@wh:main"), "12.5", "4.25", DAY,
                                       lock=False)
            out = {"ledger_lines": stock.ledger_lines(ctx, [move], srnb=True)}
            fields = ("ledger_lines[].debit", "ledger_lines[].credit")
        assert_money_fields(out, fields, currency)


def test_zero_cost_service_outputs(pg_engine, monkeypatch, currency, method):
    install(monkeypatch, pg_engine, consumer())
    books = Books(pg_engine, books_case(currency, method))
    empty = books.call("stock_on_hand", {"item": "@item:wire"}, write=False)
    assert_money_fields(empty, SERVICE_MONEY["stock_on_hand"], currency)
    seed(books, qty="1", rate="1" if currency == "JPY" else "0.001")
    inp = {"voucher_type": "sales_invoice", "voucher": "@v:zero", "posting_date": DAY.isoformat(),
           "lines": [{"item": "@item:wire", "warehouse": "@wh:main", "qty": "0.125"}]}
    for op in ("preview_issue", "issue"):
        out = books.call(op, inp, write=op == "issue")
        assert_money_fields(out, ("lines[].cost",), currency)
        assert out["ledger_lines"] == []
        assert out["lines"][0]["card_note"] == f"less than {'1' if currency == 'JPY' else '0.001'} {currency}"


@pytest.mark.parametrize("kind,direction", [("opening", "in"), ("count", "in"), ("count", "out"), ("write_off", "out")])
def test_entry_posting_money_outputs(currency, kind, direction):
    value = Decimal("53" if currency == "JPY" else "53.125")
    pair = stock.Pair({"item_id": "item"}, {"id": "warehouse"}, "inventory", None, Decimal(10), value)
    move = stock.Move(pair, direction, Decimal(2), value, 1)
    role = {"opening": "opening_balance", "count": "stock_adjustment", "write_off": "stock_write_off"}[kind]
    out = {"lines": entry_posting_lines(kind, [move], currency, {role: "counter"})}
    assert_money_fields(out, ("lines[].debit", "lines[].credit"), currency)
    assert sum(Decimal(row["debit"]) for row in out["lines"]) == value


def assert_card(card, currency):
    check_card_amounts(card)
    assert card["currency"] == currency
    assert card["amounts"], "The scenario must actually show money"
    errors = []
    for amount in card["amounts"]:
        assert amount["currency"] == currency
        assert amount["kind"] in {"amount", "unit_price"}
        if amount["kind"] == "amount":
            errors += money_errors(amount, "value", currency, f"card.{amount['label']}")
        else:
            assert Decimal(amount["value"]) == Decimal("4.25")
    assert not errors, "\n".join(errors)


def run_contract(books, name, inp, currency, expected):
    contract = REGISTRY.contracts[name]
    proposed = approvals.propose(books.engine, contract, inp, company_id=books.company)
    assert proposed.status == "card", proposed
    assert_card(proposed.card, currency)
    out = approvals.approve(books.engine, proposed.approval_id, proposed.digest)
    assert out.status == "committed", (out.status, out.text, out.data)
    assert len(out.amounts) == len(expected)
    assert_money_fields({"amounts": out.amounts}, ("amounts[].value",), currency)
    assert [a["value"] for a in out.amounts] == expected
    assert all(a.get("kind", "amount") == "amount" and a["what"] == "value" for a in out.amounts)
    assert all(f"{a['value']} {currency}" in out.text for a in out.amounts)
    with books.engine.begin() as conn:
        replay = contract.find_by_operation_key(books.ctx(conn), proposed.operation_key)
    assert replay is not None
    assert [asdict(a) for a in replay.amounts] == [{**a, "kind": a.get("kind", "amount")} for a in out.amounts]
    assert replay.text == out.text and replay.figures == out.figures
    return out


def test_voucher_previews_results_and_replays(pg_engine, currency, method):
    books = Books(pg_engine, books_case(currency, method))
    line = {"item": "Wire", "warehouse": "Main Store", "quantity": "12.5", "rate": "4.25"}
    total = "53" if currency == "JPY" else "53.125"
    run_contract(books, "record_opening_stock", {"lines": [line]}, currency, [total])
    cost = "11" if currency == "JPY" else "10.625"
    off = run_contract(books, "write_off_stock", {"item": "Wire", "warehouse": "Main Store", "quantity": "2.5",
                                               "date": DAY.isoformat()}, currency, [cost])
    run_contract(books, "cancel_stock_movement", {"document": off.data["number"], "date": DAY.isoformat()}, currency, [cost])
    # A gain and a loss on different items exercise both count result amounts.
    with pg_engine.begin() as conn:
        ctx = books.ctx(conn)
        other = new_id()
        conn.execute(m.item.insert().values(id=other, company_id=books.company, name="Bolt", unit="unit", standard_rate="1"))
        stock.make_stock_item(ctx, other, method)
    run_contract(books, "record_stock_count", {"date": DAY.isoformat(), "lines": [
        {"item": "Wire", "warehouse": "Main Store", "counted": "10"},
        {"item": "Bolt", "warehouse": "Main Store", "counted": "2.5", "rate": "4.25"},
    ]}, currency, [cost, cost])
    with pg_engine.connect() as conn:
        assert stock.verify_stock(books.ctx(conn)) == []


class DeliveryInput(Input):
    supplier_id: str
    item_id: str
    warehouse_id: str
    settlement: str
    bank_account_id: str | None = None


def delivery_request(inp):
    return goods_received.GoodsReceived(inp.supplier_id,
        (goods_received.ReceiptLine(inp.item_id, "12.5", "4.25", inp.warehouse_id),),
        DAY, inp.settlement, inp.bank_account_id)


def install_delivery_contracts(monkeypatch):
    """Test-only registration; all backend authorization and ledger code stays real."""
    for name, module, model, request, suffix in (
        (goods_received.NAME, goods_received, DeliveryInput, delivery_request, "goods_received"),
    ):
        preview = getattr(module, "preview_" + suffix)
        commit = getattr(module, "commit_" + suffix)
        read_set = getattr(module, suffix + "_read_set")
        find = getattr(module, "find_" + suffix)
        contract = WriteContract(name, "stock", "write", "Exercise inactive delivery output formatting.", model,
            preview=lambda ctx, inp, fn=preview, req=request: fn(ctx, req(inp)),
            commit=lambda ctx, inp, key, fn=commit, req=request: fn(ctx, req(inp), key),
            read_set=lambda ctx, inp, fn=read_set, req=request: fn(ctx, req(inp)),
            find_by_operation_key=find, amounts=("value",), moves_money=True)
        # Registry.fill normally installs these frames while loading a plugin.
        # This test-only contract is added after load; issue the same real frame.
        contract.commit = approval_scope._wrap_contract(contract.commit, "stock", name, "commit")
        monkeypatch.setitem(REGISTRY.contracts, name, contract)
        monkeypatch.setitem(REGISTRY.contract_plugin, name, "stock")


@pytest.mark.parametrize("settlement", ["paid_now", "still_owed"])
def test_delivery_previews_results_and_replays(pg_engine, monkeypatch, currency, method, settlement):
    books = Books(pg_engine, books_case(currency, method))
    install_delivery_contracts(monkeypatch)
    with pg_engine.begin() as conn:
        ctx = books.ctx(conn)
        supplier = new_id()
        conn.execute(m.party.insert().values(id=supplier, company_id=books.company, kind="supplier", name="Supplier"))
        bank = ledger.account_by_number(ctx, "1100")["id"]
    total = "53" if currency == "JPY" else "53.125"
    out = run_contract(books, goods_received.NAME, {
        "supplier_id": supplier, "item_id": books.sym("@item:wire"), "warehouse_id": books.sym("@wh:main"),
        "settlement": settlement, "bank_account_id": bank if settlement == "paid_now" else None,
    }, currency, [total])
    run_contract(books, reverse_goods_received.NAME, {"delivery": out.data["number"], "date": DAY.isoformat()},
                 currency, [total])
    with pg_engine.connect() as conn:
        assert stock.verify_stock(books.ctx(conn)) == []


def test_setup_outputs_have_no_money(pg_engine, monkeypatch, currency):
    install(monkeypatch, pg_engine, setup_consumer())
    cases = [case for case in setup_cases() if case["books"]["currency"] == currency and "output" in case]
    assert cases
    # Cover all five setup operations in each currency using the existing cases.
    for operation in sorted(SETUP_SERVICES):
        case = next((case for case in cases if case["service"] == operation), None)
        if case is None:
            case = next(case for case in setup_cases() if case["service"] == operation and "output" in case)
            case = {**case, "books": {**case["books"], "currency": currency}}
        books = SetupBooks(pg_engine, case)
        out = books.call_setup()
        assert out == books.resolve(case["output"])
        assert SERVICE_MONEY[operation] == ()

