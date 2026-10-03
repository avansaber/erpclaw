import uuid

from erpclaw_lib.cwip_posting import (record_cwip_accumulation,
                                      reverse_cwip_accumulations)
from setup_helpers import seed_company


ACCUM_UPDATE_SQLITE = (
    'UPDATE "asset" SET "gross_value"=?,"current_book_value"=?,'
    '"updated_at"=datetime(\'now\') WHERE "id"=?')
ACCUM_UPDATE_POSTGRESQL = (
    'UPDATE "asset" SET "gross_value"=?,"current_book_value"=?,'
    '"updated_at"=NOW()::text WHERE "id"=?')


class _RecordingCursor:
    def fetchone(self):
        return {"gross_value": "100.00", "current_book_value": "100.00"}

    def fetchall(self):
        return [{"id": "ACC1", "asset_id": "A1", "accumulated_amount": "250.50"}]


class _RecordingConn:
    """Records every SQL string cwip_posting builds; returns canned rows."""

    def __init__(self):
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append(sql)
        return _RecordingCursor()


def _asset_updates(conn):
    return [s for s in conn.statements
            if "asset" in s and s.lstrip().upper().startswith("UPDATE")]


def _drive(rec, rev):
    record_cwip_accumulation(
        rec, {"id": "A1", "gross_value": "100.00", "current_book_value": "100.00"},
        "250.50", source_voucher_type="purchase_invoice", source_voucher_id="PI1",
        gl_entry_id="GL1", accumulated_at="2026-03-04")
    reverse_cwip_accumulations(rev, "purchase_invoice", "PI1")


def _seed_under_construction_asset(conn, company_id):
    cat_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO asset_category (id, name, depreciation_method, "
        "useful_life_years, company_id) "
        "VALUES (?, 'Plant', 'straight_line', 10, ?)", (cat_id, company_id))
    asset_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO asset (id, asset_name, asset_category_id, gross_value, "
        "salvage_value, current_book_value, accumulated_depreciation, status, "
        "company_id) VALUES (?, 'Warehouse', ?, '100.00', '0.00', '100.00', "
        "'0.00', 'under_construction', ?)", (asset_id, cat_id, company_id))
    conn.commit()
    return asset_id


def test_asset_update_carries_no_sqlite_datetime_on_postgresql(monkeypatch):
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    rec = _RecordingConn()
    rev = _RecordingConn()
    _drive(rec, rev)
    assert all("datetime('now')" not in s for s in rec.statements)
    assert all("datetime('now')" not in s for s in rev.statements)


def test_asset_update_sql_is_exact_per_dialect(monkeypatch):
    for dialect, expected in (("sqlite", ACCUM_UPDATE_SQLITE),
                              ("postgresql", ACCUM_UPDATE_POSTGRESQL)):
        monkeypatch.setenv("ERPCLAW_DB_DIALECT", dialect)
        rec = _RecordingConn()
        rev = _RecordingConn()
        _drive(rec, rev)
        assert _asset_updates(rec) == [expected]
        assert _asset_updates(rev) == [expected]


def test_sqlite_accumulate_then_reverse_roundtrip(monkeypatch, conn):
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
    company_id = seed_company(conn)
    asset_id = _seed_under_construction_asset(conn, company_id)
    asset = dict(conn.execute("SELECT * FROM asset WHERE id = ?", (asset_id,)).fetchone())
    record_cwip_accumulation(conn, asset, "250.50", source_voucher_type="purchase_invoice", source_voucher_id="PI1", gl_entry_id=None, accumulated_at="2026-03-04")
    conn.commit()
    row = dict(conn.execute("SELECT * FROM asset WHERE id = ?", (asset_id,)).fetchone())
    assert row["gross_value"] == "350.50"
    assert row["current_book_value"] == "350.50"
    assert row["updated_at"] is not None
    assert reverse_cwip_accumulations(conn, "purchase_invoice", "PI1") == 1
    conn.commit()
    row = dict(conn.execute("SELECT * FROM asset WHERE id = ?", (asset_id,)).fetchone())
    assert row["gross_value"] == "100.00"
    assert row["current_book_value"] == "100.00"
