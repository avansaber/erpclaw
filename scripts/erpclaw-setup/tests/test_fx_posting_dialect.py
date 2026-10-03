"""Dialect-aware ORDER BY for nearest exchange-rate lookup."""
from decimal import Decimal
import pytest

from setup_helpers import seed_currency
from erpclaw_lib.fx_posting import get_exchange_rate


class _RecordingCursor:
    def fetchone(self):
        return None


class _RecordingConn:
    """Records every SQL string get_exchange_rate builds; returns no rows."""

    def __init__(self):
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append(sql)
        return _RecordingCursor()


def _seed_rate(conn, frm, to, rate, effective_date, rid):
    conn.execute(
        "INSERT INTO exchange_rate (id, from_currency, to_currency, rate, "
        "effective_date, source) VALUES (?, ?, ?, ?, ?, 'manual')",
        (rid, frm, to, rate, effective_date),
    )
    conn.commit()


def _order_by_abs_lines(statements):
    out = []
    for sql in statements:
        for line in sql.splitlines():
            if "ORDER BY" in line and "ABS(" in line:
                out.append(line.strip())
    return out


def test_nearest_rate_order_by_carries_no_julianday_on_postgresql(monkeypatch):
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    rec = _RecordingConn()
    result = get_exchange_rate(rec, "EUR", "USD", "2026-03-04")
    assert result is None
    assert len(rec.statements) == 4
    joined = "\n".join(rec.statements)
    assert "julianday" not in joined
    lines = _order_by_abs_lines(rec.statements)
    assert len(lines) == 2
    for line in lines:
        assert line == "ORDER BY ABS(EXTRACT(DAY FROM (effective_date::timestamp - ?::timestamp))) ASC"


def test_nearest_rate_order_by_sql_is_unchanged_on_sqlite(monkeypatch):
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
    rec = _RecordingConn()
    result = get_exchange_rate(rec, "EUR", "USD", "2026-03-04")
    assert result is None
    assert len(rec.statements) == 4
    lines = _order_by_abs_lines(rec.statements)
    assert len(lines) == 2
    for line in lines:
        assert line == "ORDER BY ABS(julianday(effective_date) - julianday(?)) ASC"


def test_nearest_rate_selection_unchanged_on_sqlite_later_wins(monkeypatch, conn):
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
    seed_currency(conn, "USD", "US Dollar", "$")
    seed_currency(conn, "EUR", "Euro", "E")
    _seed_rate(conn, "EUR", "USD", "0.920000", "2026-03-01", "r1")
    _seed_rate(conn, "EUR", "USD", "0.910000", "2026-03-05", "r2")
    rate = get_exchange_rate(conn, "EUR", "USD", "2026-03-04")
    assert rate == Decimal("0.910000")
    assert str(rate) == "0.910000"


def test_nearest_rate_selection_unchanged_on_sqlite_earlier_wins(monkeypatch, conn):
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
    seed_currency(conn, "USD", "US Dollar", "$")
    seed_currency(conn, "EUR", "Euro", "E")
    _seed_rate(conn, "EUR", "USD", "0.920000", "2026-03-01", "r1")
    _seed_rate(conn, "EUR", "USD", "0.910000", "2026-03-05", "r2")
    rate = get_exchange_rate(conn, "EUR", "USD", "2026-03-02")
    assert rate == Decimal("0.920000")
    assert str(rate) == "0.920000"


def test_inverse_nearest_rate_unchanged_on_sqlite(monkeypatch, conn):
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
    seed_currency(conn, "USD", "US Dollar", "$")
    seed_currency(conn, "EUR", "Euro", "E")
    _seed_rate(conn, "USD", "EUR", "1.250000", "2026-03-05", "r1")
    rate = get_exchange_rate(conn, "EUR", "USD", "2026-03-04")
    assert rate == Decimal("0.800000")
    assert str(rate) == "0.800000"


def test_outside_window_returns_none(monkeypatch, conn):
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
    seed_currency(conn, "USD", "US Dollar", "$")
    seed_currency(conn, "EUR", "Euro", "E")
    _seed_rate(conn, "EUR", "USD", "0.920000", "2026-01-01", "r1")
    result = get_exchange_rate(conn, "EUR", "USD", "2026-03-04")
    assert result is None


def _between_lines(statements):
    out = []
    for sql in statements:
        for line in sql.splitlines():
            if "BETWEEN" in line:
                out.append(line.strip())
    return out


def test_window_carries_no_sqlite_date_modifier_on_postgresql(monkeypatch):
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    rec = _RecordingConn()
    assert get_exchange_rate(rec, "EUR", "USD", "2026-03-04") is None
    assert len(rec.statements) == 4
    assert "date(?" not in "\n".join(rec.statements)
    assert _between_lines(rec.statements) == [
        "AND effective_date BETWEEN (CAST(? AS date) - CAST(? AS integer))::text"
        " AND (CAST(? AS date) + CAST(? AS integer))::text"] * 2


def test_window_sql_is_unchanged_on_sqlite(monkeypatch):
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
    rec = _RecordingConn()
    assert get_exchange_rate(rec, "EUR", "USD", "2026-03-04") is None
    assert _between_lines(rec.statements) == [
        "AND effective_date BETWEEN date(?, '-' || ? || ' days')"
        " AND date(?, '+' || ? || ' days')"] * 2


def test_window_edges_are_inclusive_at_max_days_on_sqlite(monkeypatch, conn):
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
    seed_currency(conn, "USD", "US Dollar", "$")
    seed_currency(conn, "EUR", "Euro", "E")
    _seed_rate(conn, "EUR", "USD", "0.930000", "2026-02-25", "r1")
    rate = get_exchange_rate(conn, "EUR", "USD", "2026-03-04")
    assert str(rate) == "0.930000"
    assert get_exchange_rate(conn, "EUR", "USD", "2026-03-05") is None
    _seed_rate(conn, "EUR", "USD", "0.940000", "2026-03-11", "r2")
    assert str(get_exchange_rate(conn, "EUR", "USD", "2026-03-18")) == "0.940000"
    assert get_exchange_rate(conn, "EUR", "USD", "2026-03-19") is None


def test_date_add_days_rejects_a_sign_it_cannot_render(monkeypatch):
    from erpclaw_lib.query import date_add_days
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
    with pytest.raises(ValueError):
        date_add_days("?", "?", "*")
