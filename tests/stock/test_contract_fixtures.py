"""The producer side of the stock contract, stock/1: every fixture in tests/contracts/stock/v1/ against the stock
plugin, called through call_service as a consumer plugin calls it. A write service is followed by the consumer's
obligation (its posting, or the reversal of its original posting) and the comparison it registered; then the books
must be as the fixture's `after` says, and verify_stock must agree."""
from __future__ import annotations

import re

import pytest
from sqlalchemy import select

from erpclaw_core.common import Refusal
from erpclaw_stock import service as stock
from erpclaw_stock import tables as t

from stock_fixtures import Books, FixtureError, all_cases, consumer, load, load_text
from service_plugins import install  # tests/ is on the path

WRITES = {"issue", "return_in", "reverse_movements"}
FIELDS = ("item", "warehouse", "direction", "qty", "value", "qty_after", "value_after")


@pytest.fixture
def stock_consumer(engine, monkeypatch):
    install(monkeypatch, engine, consumer())  # registered and enabled in the books: its card's commit calls stock


def _entries(books: Books, ids: list[str]) -> list[dict]:
    with books.engine.connect() as conn:
        rows = {r["id"]: dict(r) for r in conn.execute(select(t.entry).where(t.entry.c.id.in_(ids))).mappings()}
    return [{"id": i, "item": rows[i]["item_id"], "warehouse": rows[i]["warehouse_id"],
             "direction": rows[i]["direction"], "qty": stock.qty_text(rows[i]["qty"]),
             "value": stock.money_text(rows[i]["value"], rows[i]["currency"]),
             "qty_after": stock.qty_text(rows[i]["qty_after"]),
             "value_after": stock.money_text(rows[i]["value_after"], rows[i]["currency"])} for i in ids]


def _bind_new_entries(books: Books, case: dict, out: dict) -> None:
    made = out.get("entry_ids") or ([out["entry_id"]] if "entry_id" in out else [])
    for n, real in enumerate(made, 1):
        books.ids[f"@entry:{case['name']}#{n}"] = real


def _check_refusal(books: Books, expected: dict, r: Refusal) -> None:
    assert r.code == expected["code"], (r.code, r.message)
    for k, v in expected.items():
        if k == "code":
            continue
        if k == "options":  # the standard question: labels and refs (names, never ids); its id is set at run time
            assert [{"label": c["label"], "ref": c["ref"]} for c in r.data["choices"]] == v
            assert r.options == [o["label"] for o in v]
        else:
            assert r.data.get(k) == books.resolve(v), (k, r.data)


def _verify(books: Books) -> None:
    with books.engine.connect() as conn:
        assert stock.verify_stock(books.ctx(conn)) == []


@pytest.mark.parametrize("path", all_cases(), ids=lambda p: p.stem)
def test_every_fixture_against_the_producer(engine, stock_consumer, path):
    case = load(path)
    if case["service"] == "receive_stock":
        pytest.skip("receive_stock, the user-facing contract, is not in this release")
    books = Books(engine, case)
    before = books.snapshot()
    write = case["service"] in WRITES
    if "refusal" in case:
        with pytest.raises(Refusal) as caught:
            books.call(case["service"], case["input"], write=write)
        _check_refusal(books, case["refusal"], caught.value)
        assert books.snapshot() == before  # nothing written
    else:
        out = books.call(case["service"], case["input"], write=write)
        _bind_new_entries(books, case, out)
        currency = case["books"]["currency"]
        if "cost" in case["output"]:
            case["output"]["currency"] = currency
        for field in ("lines", "ledger_lines", "warehouses", "mirror_entries", "contribution"):
            for row in case["output"].get(field, []):
                row["currency"] = currency
        assert out == books.resolve(case["output"])
        after = case["after"]
        if after.get("unchanged"):
            assert books.snapshot() == before
        if "entries" in after:
            expected = books.resolve(after["entries"])
            assert _entries(books, [e["id"] for e in expected]) == [{k: e[k] for k in ("id", *FIELDS)}
                                                                     for e in expected]
        if "head" in after:
            [entry] = after["entries"][-1:]
            with engine.connect() as conn:
                h = conn.execute(select(t.head).where(t.head.c.company_id == books.company,
                                                      t.head.c.item_id == books.resolve(entry["item"]),
                                                      t.head.c.warehouse_id == books.resolve(entry["warehouse"]))
                                 ).mappings().one()
            assert {"qty": stock.qty_text(h["qty"]), "value": stock.money_text(h["value"], h["currency"])} \
                == after["head"]
    _verify(books)


def test_the_loader_refuses_a_float_a_stray_number_or_a_missing_field():
    good = (all_cases()[0]).read_text()
    case = load_text(good)
    assert case["contract"] == "stock/1"
    first_qty = re.compile(r'"qty": "[0-9.]+"')
    assert first_qty.search(good)
    for number in ("100.0", "100", "1e2"):
        with pytest.raises(FixtureError):
            load_text(first_qty.sub(f'"qty": {number}', good, count=1))
    without = load_text(good)
    del without["input"]
    import json
    with pytest.raises(FixtureError):
        load_text(json.dumps(without))


def test_every_fixture_is_used():
    services = {load(p)["service"] for p in all_cases()}
    assert services == {"stock_on_hand", "choose_warehouse", "preview_issue", "issue", "preview_return", "return_in",
                        "preview_reverse", "reverse_movements", "receive_stock"}
