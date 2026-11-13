"""The engine's restore order accepts FIFO roots and preserves every other dependency."""
from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import select

from backup_support import open_target, pg_new  # noqa: F401 (fixture)
from erpclaw_core import backup as bk, backup_reflect as br, models as m
from erpclaw_core.common import company_ctx, new_id
from erpclaw_stock import service as stock
from erpclaw_stock.tables import layer
from test_backup_reflect import books  # noqa: F401 (existing multi-company fixture with a reversal)


@pytest.fixture
def fifo_layers():
    # Both chains oppose primary-key order; an independent root sorts between them.
    return [{"id": "a", "group_id": "c"}, {"id": "b", "group_id": "z"},
            {"id": "c", "group_id": "y"}, {"id": "m", "group_id": "m"},
            {"id": "y", "group_id": "y"}, {"id": "z", "group_id": "z"}]


def test_fifo_self_roots_and_all_distinct_parents_restore_first(fifo_layers):
    before = json.dumps(fifo_layers)
    ordered = bk._restore_order(layer, list(reversed(fifo_layers)))
    assert [r["id"] for r in ordered] == ["y", "c", "a", "z", "b", "m"]
    positions = {r["id"]: i for i, r in enumerate(ordered)}
    for row in ordered:
        if row["group_id"] != row["id"]:
            assert positions[row["group_id"]] < positions[row["id"]]
        assert row is next(r for r in fifo_layers if r["id"] == row["id"])
    assert json.dumps(fifo_layers) == before


@pytest.mark.parametrize("size", [2, 3])
@pytest.mark.parametrize("with_root", [False, True])
def test_distinct_layer_cycles_still_have_the_same_refusal(size, with_root):
    ids = ["b", "c", "d"][:size]
    rows = [{"id": key, "group_id": ids[(i + 1) % size]} for i, key in enumerate(ids)]
    if with_root:
        rows.insert(0, {"id": "a", "group_id": "a"})
    with pytest.raises(bk.BackupRefused) as refusal:
        bk._restore_order(layer, rows)
    assert str(refusal.value) == "stock_layer rows refer to each other in a cycle"


def test_other_self_references_keep_their_existing_refusal():
    with pytest.raises(bk.BackupRefused) as refusal:
        bk._restore_order(m.account, [{"id": "a", "parent_id": "a"}])
    assert str(refusal.value) == "account rows refer to each other in a cycle"


def test_layer_order_without_self_roots_keeps_its_exact_bytes():
    rows = [{"id": "a", "group_id": "z"}, {"id": "m", "group_id": None},
            {"id": "z", "group_id": None}, {"id": "b", "group_id": "a"}]
    actual = b"\n".join(json.dumps(r, separators=(",", ":")).encode()
                        for r in bk._restore_order(layer, rows))
    assert actual == (b'{"id":"z","group_id":null}\n'
                      b'{"id":"a","group_id":"z"}\n'
                      b'{"id":"b","group_id":"a"}\n'
                      b'{"id":"m","group_id":null}')


def test_existing_books_without_self_roots_keep_their_exact_export_order(books, tmp_path):
    """An independent move-before oracle pins the old order on the existing books fixture."""
    destination = tmp_path / "backup"
    bk.backup(books, destination)
    with books.connect() as conn:
        assert conn.execute(select(layer)).first() is None
        for table in bk.books_tables(conn):
            # The old base order is text PK order, except postings use company and numeric sequence.
            rows = [{c.name: bk._encode(r[c.name], c.type) for c in table.columns}
                    for r in conn.execute(select(table)).mappings()]
            rows.sort(key=lambda r: tuple("" if r[c.name] is None else str(r[c.name])
                                         for c in table.primary_key.columns))
            if table.name == "posting":
                rows.sort(key=lambda r: (r["company_id"], int(r["seq"])))
            parents = [fk.parent.name for fk in table.foreign_keys
                       if fk.column.table is table and fk.parent.name != "company_id"]
            # These existing account/reversal chains have at most one parent per row.
            assert not rows or len(parents) <= 1
            if parents:
                column = parents[0]
                assert all(r.get(column) != r["id"] for r in rows)
                # Move the first out-of-order parent just before its child until no inversion remains.
                while True:
                    positions = {r["id"]: i for i, r in enumerate(rows)}
                    inversion = next(((i, positions[r[column]]) for i, r in enumerate(rows)
                                      if r.get(column) in positions and positions[r[column]] > i), None)
                    if inversion is None:
                        break
                    child, parent = inversion
                    rows.insert(child, rows.pop(parent))
            expected = "".join(json.dumps(r, separators=(",", ":"), ensure_ascii=False) + "\n" for r in rows)
            assert (destination / "tables" / f"{table.name}.jsonl").read_bytes() == expected.encode()


@pytest.fixture
def fifo_books(engine, monkeypatch):
    with engine.begin() as conn:
        ctx = company_ctx(conn, company_name="Stark Manufacturing")
        item = new_id()
        conn.execute(m.item.insert().values(id=item, company_id=ctx.company_id, name="FIFO bolt",
                                           unit="unit", standard_rate="1"))
        warehouse = stock.create_warehouse(ctx, "FIFO warehouse")
        stock.make_stock_item(ctx, item, "fifo")
        # The second receipt merges into the first; its layer id sorts before its root.
        ids = iter(["entry-one", "z-root", "head", "entry-two", "a-child"])
        with monkeypatch.context() as patch:
            patch.setattr(stock, "new_id", lambda: next(ids))
            for voucher in ("receipt-one", "receipt-two"):
                trusted = stock.Trusted("backup fixture")
                stock.receive(ctx, voucher, item, warehouse, "3", "2", date(2026, 10, 1), trusted=trusted)
                trusted.verify(ctx)
        assert stock.verify_stock(ctx) == []
    return engine


@pytest.mark.parametrize("exporter", [bk.backup, br.backup], ids=["engine", "reflected"])
def test_m1_backup_and_restore_preserve_fifo_roots_and_children(fifo_books, tmp_path, pg_new, exporter):
    source = tmp_path / "backup"
    exporter(fifo_books, source)
    rows = [json.loads(line) for line in (source / "tables" / "stock_layer.jsonl").read_text().splitlines()]
    assert [(r["id"], r["group_id"]) for r in rows] == [("z-root", "z-root"), ("a-child", "z-root")]
    target = pg_new()
    assert bk.restore(source, target).location == target.name
    restored = open_target(target)
    try:
        assert bk.book_diff(fifo_books, restored) == []
        with restored.connect() as conn:
            assert stock.verify_stock(company_ctx(conn, company_name="Stark Manufacturing")) == []
    finally:
        restored.dispose()
