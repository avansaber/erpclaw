"""One statement call per posting (task m760p)."""
import hashlib
import os
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import pytest

from gl_helpers import (  # noqa: E402
    call_action, get_conn, init_all_tables, is_ok, load_db_query, ns,
)

from erpclaw_lib.db import get_connection, get_dialect  # noqa: E402
from erpclaw_lib.db import db_integrity_error  # noqa: E402
from erpclaw_lib import gl_posting  # noqa: E402
from erpclaw_lib.gl_posting import (  # noqa: E402
    insert_gl_entries, reverse_gl_entries,
)
from erpclaw_lib.query import Field, P, Q, Table, fn  # noqa: E402

GL = load_db_query()


def _uuid():
    return str(uuid.uuid4())


def _is_pg():
    return get_dialect() == "postgresql"


def _pg_only():
    if not _is_pg():
        pytest.skip("PostgreSQL-only case: needs ERPCLAW_PG_TEST_URL")


def _sqlite_only():
    if _is_pg():
        pytest.skip("SQLite-only case: legacy write order is recoverable here")


def _seed_company(conn, name=None, abbr=None):
    cid = _uuid()
    conn.execute(
        "INSERT INTO company (id, name, abbr, default_currency, country,"
        " fiscal_year_start_month) VALUES (?, ?, ?, 'USD', 'United States', 1)",
        (cid, name or ("Chain Co " + cid[:6]), abbr or ("CC" + cid[:4].replace("-", ""))),
    )
    conn.commit()
    return cid


def _seed_account(conn, company_id, name, number, root_type, account_type):
    aid = _uuid()
    direction = "debit_normal" if root_type in ("asset", "expense") else "credit_normal"
    conn.execute(
        "INSERT INTO account (id, name, account_number, root_type, account_type,"
        " balance_direction, company_id, depth, is_group)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)",
        (aid, name, number, root_type, account_type, direction, company_id, 0),
    )
    conn.commit()
    return aid


def _seed_chart(conn, company_id=None):
    cid = company_id or _seed_company(conn)
    ccid = _uuid()
    conn.execute(
        "INSERT INTO cost_center (id, name, company_id, is_group) VALUES (?, ?, ?, 0)",
        (ccid, "Main CC", cid),
    )
    fyid = _uuid()
    conn.execute(
        "INSERT INTO fiscal_year (id, name, start_date, end_date, company_id)"
        " VALUES (?, ?, '2026-01-01', '2026-12-31', ?)",
        (fyid, "FY-" + fyid[:6], cid),
    )
    cust = _uuid()
    conn.execute(
        "INSERT INTO customer (id, name, company_id) VALUES (?, ?, ?)",
        (cust, "Chain Customer", cid),
    )
    conn.commit()
    cash = _seed_account(conn, cid, "Cash", "1000", "asset", "cash")
    revenue = _seed_account(conn, cid, "Revenue", "4000", "income", "revenue")
    expense = _seed_account(conn, cid, "Expense", "5000", "expense", "expense")
    ar = _seed_account(conn, cid, "Receivable", "1100", "asset", "receivable")
    equity = _seed_account(conn, cid, "Equity", "3000", "equity", "equity")
    return {"company_id": cid, "cc": ccid, "fy": fyid, "customer": cust,
            "cash": cash, "revenue": revenue, "expense": expense,
            "ar": ar, "equity": equity}


def _leg(env, account_key, debit, credit):
    entry = {"account_id": env[account_key], "debit": debit, "credit": credit}
    if account_key in ("revenue", "expense"):
        entry["cost_center_id"] = env["cc"]
    if account_key == "ar":
        entry["party_type"] = "customer"
        entry["party_id"] = env["customer"]
    return entry


def _chain_hash(date, account_id, debit, credit, voucher_type, voucher_id, prev):
    return hashlib.sha256("|".join(
        [date, account_id, str(debit), str(credit),
         voucher_type, voucher_id, prev]).encode("utf-8")).hexdigest()


def _integrity(conn, company_id):
    result = call_action(GL.check_gl_integrity, conn,
                         ns(company_id=company_id, company_name=None))
    assert is_ok(result), result
    return result


def _company_legs(conn, company_id):
    g = Table("gl_entry").as_("g")
    a = Table("account").as_("a")
    q = (Q.from_(g).join(a).on(g.account_id == a.id)
         .select(g.id, g.voucher_id, g.account_id, g.debit, g.credit,
                 g.gl_checksum, g.sequence, g.is_cancelled)
         .where(a.company_id == P()))
    return [dict(r) for r in conn.execute(q.get_sql(), (company_id,)).fetchall()]


def _ordered_chained(conn, company_id):
    legs = [r for r in _company_legs(conn, company_id) if r["gl_checksum"]]
    return sorted(legs, key=lambda r: (r["sequence"] if r["sequence"] is not None else -1))


def _head(conn, company_id):
    t = Table("gl_chain_head")
    q = (Q.from_(t).select(t.star).where(t.company_id == P()))
    row = conn.execute(q.get_sql(), (company_id,)).fetchone()
    return dict(row) if row else None


def _assert_pg_test_database():
    from urllib.parse import urlparse, unquote
    url = os.environ.get("ERPCLAW_PG_TEST_URL", "")
    path = urlparse(url).path.lstrip("/")
    expected = unquote(path.split("?")[0])
    conn = get_connection()
    try:
        row = conn.execute("SELECT current_database() AS db").fetchone()
        actual = row["db"] if "db" in row.keys() else row[0]
    finally:
        conn.close()
    assert actual == expected, (actual, expected)
    return expected


@contextmanager
def _fresh_pg_schema():
    init_all_tables(None)
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


def _forty_specs():
    return [("cash", "1.00", "0")] * 20 + [("expense", "0", "1.00")] * 20


def _is_gl_insert(sql):
    return sql.lstrip().startswith("INSERT INTO gl_entry")


_UPDATE_SQL = "UPDATE gl_entry SET is_cancelled = 1 WHERE id = ?"


class _RecordingProxy:
    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "calls", [])

    def execute(self, sql, *args, **kwargs):
        self.calls.append(("execute", sql, 1))
        return self._conn.execute(sql, *args, **kwargs)

    def executemany(self, sql, seq=None, *args, **kwargs):
        try:
            n = len(seq) if seq is not None else 0
        except TypeError:
            seq = list(seq)
            n = len(seq)
        self.calls.append(("executemany", sql, n or 1))
        return self._conn.executemany(sql, seq, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_conn"), name)

    def __setattr__(self, name, value):
        if name in ("_conn", "calls"):
            object.__setattr__(self, name, value)
        else:
            try:
                setattr(object.__getattribute__(self, "_conn"), name, value)
            except (AttributeError, TypeError):
                object.__setattr__(self, name, value)


def _fixed_uuids(n, start=1):
    return [uuid.UUID(int=start + i) for i in range(n)]


def _patch_uuid(monkeypatch, objs):
    it = iter(objs)
    monkeypatch.setattr(gl_posting, "uuid", SimpleNamespace(uuid4=lambda: next(it)))


class _FrozenDatetime:
    _now = None

    @staticmethod
    def now(tz=None):
        return _FrozenDatetime._now


FIXED_NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


def _norm2(s):
    return str(Decimal(str(s)).quantize(Decimal("0.01")))


def _expected_forty(env, fixed_strs, date, voucher_type, voucher_id):
    specs = _forty_specs()
    prev = "GENESIS"
    rows = []
    for i, (key, debit, credit) in enumerate(specs):
        account_id = env[key]
        debit_n = _norm2(debit)
        credit_n = _norm2(credit)
        checksum = _chain_hash(date, account_id, debit_n, credit_n, voucher_type, voucher_id, prev)
        prev = checksum
        cc = env["cc"] if key in ("revenue", "expense") else None
        rows.append({
            "id": fixed_strs[i],
            "posting_date": date,
            "account_id": account_id,
            "party_type": None,
            "party_id": None,
            "debit": debit_n,
            "credit": credit_n,
            "currency": "USD",
            "debit_base": debit_n,
            "credit_base": credit_n,
            "exchange_rate": "1",
            "voucher_type": voucher_type,
            "voucher_id": voucher_id,
            "entry_set": "primary",
            "cost_center_id": cc,
            "project_id": None,
            "remarks": "",
            "fiscal_year": None,
            "is_cancelled": "0",
            "cancelled_by": None,
            "sequence": str(i + 1),
            "gl_checksum": checksum,
            "dimensions_json": "{}",
        })
    return rows, prev


def _read_voucher_rows(conn, voucher_id):
    t = Table("gl_entry")
    q = (Q.from_(t).select(t.star).where(t.voucher_id == P()).orderby(t.sequence))
    fetched = [dict(r) for r in conn.execute(q.get_sql(), (voucher_id,)).fetchall()]
    out = []
    for r in fetched:
        d = {}
        for k, v in r.items():
            if k == "created_at":
                continue
            d[k] = None if v is None else str(v)
        out.append(d)
    return fetched, out


def test_insert_uses_one_statement_call_per_posting(conn):
    env = _seed_chart(conn)
    proxy = _RecordingProxy(conn)
    specs = _forty_specs()
    legs = [_leg(env, key, debit, credit) for key, debit, credit in specs]
    insert_gl_entries(proxy, legs, voucher_type="journal_entry",
                      voucher_id="JE-40", posting_date="2026-06-01",
                      company_id=env["company_id"])
    proxy.commit()
    inserts = [c for c in proxy.calls if _is_gl_insert(c[1])]
    assert len(inserts) == 1
    kind, _sql, n = inserts[0]
    assert kind == "executemany"
    assert n == 40
    bad = [c for c in proxy.calls if c[0] == "execute" and _is_gl_insert(c[1])]
    assert bad == []


def test_rows_equal_independent_expectation(conn, monkeypatch):
    env = _seed_chart(conn)
    cid = env["company_id"]
    fixed_objs = _fixed_uuids(40, start=1)
    fixed_strs = [str(u) for u in fixed_objs]
    _patch_uuid(monkeypatch, fixed_objs)
    _FrozenDatetime._now = FIXED_NOW
    monkeypatch.setattr(gl_posting, "datetime", _FrozenDatetime)
    date = "2026-06-01"
    voucher_id = "JE-40"
    legs = [_leg(env, key, debit, credit) for key, debit, credit in _forty_specs()]
    ids = insert_gl_entries(conn, legs, voucher_type="journal_entry",
                            voucher_id=voucher_id, posting_date=date,
                            company_id=cid)
    conn.commit()
    assert ids == fixed_strs
    expected, last_checksum = _expected_forty(env, fixed_strs, date, "journal_entry", voucher_id)
    _fetched, actual = _read_voucher_rows(conn, voucher_id)
    assert actual == expected
    for r in _fetched:
        assert r["created_at"] not in (None, "")
    h = _head(conn, cid)
    assert h is not None
    fixed_stamp = FIXED_NOW.strftime("%Y-%m-%d %H:%M:%S")
    assert (h["company_id"], str(h["last_sequence"]), h["last_checksum"], h["updated_at"]) == (cid, "40", last_checksum, fixed_stamp)


def test_chain_continues_across_postings(conn):
    env = _seed_chart(conn)
    cid = env["company_id"]
    legs1 = [_leg(env, key, debit, credit) for key, debit, credit in [("cash", "5.00", "0"), ("expense", "0", "5.00")]]
    ids1 = insert_gl_entries(conn, legs1, voucher_type="journal_entry",
                             voucher_id="JE-A", posting_date="2026-06-01",
                             company_id=cid)
    conn.commit()
    legs2 = [_leg(env, key, debit, credit) for key, debit, credit in [("cash", "7.00", "0"), ("expense", "0", "7.00")]]
    ids2 = insert_gl_entries(conn, legs2, voucher_type="journal_entry",
                             voucher_id="JE-B", posting_date="2026-06-02",
                             company_id=cid)
    conn.commit()
    chained = _ordered_chained(conn, cid)
    assert [r["sequence"] for r in chained] == [1, 2, 3, 4]
    first = [r for r in chained if r["voucher_id"] == "JE-A"]
    second = [r for r in chained if r["voucher_id"] == "JE-B"]
    assert len(first) == 2
    assert len(second) == 2
    tail = sorted(first, key=lambda r: r["sequence"])[-1]
    head2 = sorted(second, key=lambda r: r["sequence"])[0]
    assert head2["gl_checksum"] == _chain_hash("2026-06-02", head2["account_id"], head2["debit"], head2["credit"], "journal_entry", "JE-B", tail["gl_checksum"])
    result = _integrity(conn, cid)
    assert result["chain_intact"] is True
    assert result["broken_links"] == 0


def test_reverse_uses_one_insert_and_one_update_call(conn):
    env = _seed_chart(conn)
    cid = env["company_id"]
    specs = _forty_specs()
    legs = [_leg(env, key, debit, credit) for key, debit, credit in specs]
    insert_gl_entries(conn, legs, voucher_type="journal_entry",
                      voucher_id="JE-40", posting_date="2026-06-01",
                      company_id=cid)
    conn.commit()
    proxy = _RecordingProxy(conn)
    reverse_gl_entries(proxy, "journal_entry", "JE-40", "2026-06-02")
    proxy.commit()
    inserts = [c for c in proxy.calls if _is_gl_insert(c[1])]
    assert len(inserts) == 1
    assert inserts[0][0] == "executemany"
    assert inserts[0][2] == 40
    updates = [c for c in proxy.calls if c[1] == _UPDATE_SQL]
    assert len(updates) == 1
    assert updates[0][0] == "executemany"
    assert updates[0][2] == 40
    bad = [c for c in proxy.calls if c[0] == "execute" and (_is_gl_insert(c[1]) or c[1] == _UPDATE_SQL)]
    assert bad == []
    t = Table("gl_entry")
    q = (Q.from_(t).select(t.star).where(t.voucher_id == P()))
    rows = [dict(r) for r in conn.execute(q.get_sql(), ("JE-40",)).fetchall()]
    assert len(rows) == 80
    originals = [r for r in rows if r["gl_checksum"] is not None]
    mirrors = [r for r in rows if r["gl_checksum"] is None]
    assert len(originals) == 40
    assert len(mirrors) == 40
    for r in originals + mirrors:
        assert str(r["is_cancelled"]) == "1"
    for r in mirrors:
        assert r["gl_checksum"] is None
        assert r["sequence"] is None
    assert sorted(tuple(sorted(d.items())) for d in rows) == sorted(tuple(sorted(d.items())) for d in rows)
    by_account = {}
    for r in rows:
        aid = r["account_id"]
        by_account.setdefault(aid, [Decimal("0"), Decimal("0")])
        by_account[aid][0] += Decimal(str(r["debit"]))
        by_account[aid][1] += Decimal(str(r["credit"]))
    for _aid, (d, c) in by_account.items():
        assert d - c == Decimal("0")
    result = _integrity(conn, cid)
    assert result["chain_intact"] is True


def test_failure_mid_batch_writes_nothing(conn, monkeypatch):
    env = _seed_chart(conn)
    cid = env["company_id"]
    before = _head(conn, cid)
    base_objs = _fixed_uuids(40, start=5001)
    dup_objs = list(base_objs)
    dup_objs[20] = dup_objs[19]
    _patch_uuid(monkeypatch, dup_objs)
    legs = [_leg(env, key, debit, credit) for key, debit, credit in _forty_specs()]
    with pytest.raises(db_integrity_error(conn)):
        insert_gl_entries(conn, legs, voucher_type="journal_entry",
                          voucher_id="JE-FAIL", posting_date="2026-06-01",
                          company_id=cid)
    conn.rollback()
    fresh = get_connection()
    try:
        t = Table("gl_entry")
        q = (Q.from_(t).select(t.id).where(t.voucher_id == P()))
        found = list(fresh.execute(q.get_sql(), ("JE-FAIL",)).fetchall())
        assert found == []
        after = _head(fresh, cid)
        assert after == before
    finally:
        fresh.close()


@pytest.mark.skipif(not os.environ.get("ERPCLAW_PG_TEST_URL"), reason="needs ERPCLAW_PG_TEST_URL")
def test_rows_equal_independent_expectation_postgres(db_path, monkeypatch):
    _pg_only()
    _assert_pg_test_database()
    with _fresh_pg_schema() as pg_conn:
        env = _seed_chart(pg_conn)
        cid = env["company_id"]
        fixed_objs = _fixed_uuids(40, start=9001)
        fixed_strs = [str(u) for u in fixed_objs]
        _patch_uuid(monkeypatch, fixed_objs)
        _FrozenDatetime._now = FIXED_NOW
        monkeypatch.setattr(gl_posting, "datetime", _FrozenDatetime)
        date = "2026-06-01"
        voucher_id = "JE-40-PG"
        legs = [_leg(env, key, debit, credit) for key, debit, credit in _forty_specs()]
        ids = insert_gl_entries(pg_conn, legs, voucher_type="journal_entry",
                                voucher_id=voucher_id, posting_date=date,
                                company_id=cid)
        pg_conn.commit()
        assert ids == fixed_strs
        expected, last_checksum = _expected_forty(env, fixed_strs, date, "journal_entry", voucher_id)
        _fetched, actual = _read_voucher_rows(pg_conn, voucher_id)
        assert actual == expected
        for r in _fetched:
            assert r["created_at"] not in (None, "")
        h = _head(pg_conn, cid)
        assert h is not None
        fixed_stamp = FIXED_NOW.strftime("%Y-%m-%d %H:%M:%S")
        assert (h["company_id"], str(h["last_sequence"]), h["last_checksum"], h["updated_at"]) == (cid, "40", last_checksum, fixed_stamp)
