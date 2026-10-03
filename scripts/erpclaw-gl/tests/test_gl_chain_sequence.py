"""GL checksum chain sequenced under the chain head (task m332a).

The chain build (``insert_gl_entries``) and the chain walk
(``check-gl-integrity``) order legs by an explicit per-company
``gl_entry.sequence`` handed out under a ``gl_chain_head`` row, instead of by
write-time ties that never recorded an order on some backends.

Discipline, as elsewhere in this suite: money is compared as exact strings,
reads go through ``erpclaw_lib.query`` builders on connections from
``erpclaw_lib.db.get_connection``. Test fixtures seed companies, accounts,
cost centres, fiscal years and parties with direct SQL; the GL writes under
test go through owner actions (``insert_gl_entries`` and the module actions
that call it). Direct inserts also plant damage no owner action would ever
write (legacy segments, tampered legs, stale-writer rows).
"""
import hashlib
import io
import json
import os
import sys
import uuid
from contextlib import contextmanager

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import pytest

from gl_helpers import (  # noqa: E402
    call_action, get_conn, init_all_tables, is_ok, load_db_query, ns,
)

from erpclaw_lib.db import get_connection, get_dialect  # noqa: E402
from erpclaw_lib.gl_posting import (  # noqa: E402
    insert_gl_entries, reverse_gl_entries,
)
from erpclaw_lib.query import Field, P, Q, Table, fn  # noqa: E402
from erpclaw_lib import seam  # noqa: E402

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


# ── seeding ──

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


def _post(conn, env, voucher_type, voucher_id, date, specs,
          entry_set="primary", **kwargs):
    legs = [_leg(env, key, debit, credit) for key, debit, credit in specs]
    ids = insert_gl_entries(conn, legs, voucher_type=voucher_type,
                            voucher_id=voucher_id, posting_date=date,
                            company_id=env["company_id"],
                            entry_set=entry_set, **kwargs)
    conn.commit()
    return ids


# ── reads ──

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


def _chain_hash(date, account_id, debit, credit, voucher_type, voucher_id, prev):
    return hashlib.sha256("|".join(
        [date, account_id, str(debit), str(credit),
         voucher_type, voucher_id, prev]).encode("utf-8")).hexdigest()


def _assert_pg_test_database():
    """Require the bound database be the one named in ERPCLAW_PG_TEST_URL."""
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


# ── 1. determinism on PostgreSQL ──

class TestDeterminismPostgres:
    @pytest.mark.skipif(not os.environ.get("ERPCLAW_PG_TEST_URL"),
                        reason="needs ERPCLAW_PG_TEST_URL")
    def test_fifty_multi_leg_vouchers_stay_intact_ten_times(self, db_path):
        _pg_only()
        _assert_pg_test_database()
        for round_no in range(10):
            with _fresh_pg_schema() as conn:
                env = _seed_chart(conn)
                cid = env["company_id"]
                for v in range(50):
                    count = 3 + (v % 3)
                    specs = []
                    for i in range(count - 1):
                        specs.append(("cash", "%d.00" % (10 + i), "0"))
                    specs.append(("revenue", "0",
                                  "%d.00" % sum(10 + i for i in range(count - 1))))
                    _post(conn, env, "journal_entry", "JE-DET-%02d" % v,
                          "2026-06-%02d" % (1 + (v % 28)), specs)
                result = _integrity(conn, cid)
                assert result["chain_intact"] is True, (round_no, result)
                assert result["broken_links"] == 0, (round_no, result)
                assert result["sequenced_rows"] == result["total_entries"]
                assert result["legacy_rows"] == 0
                assert result["pre_sequence_rows"] == 0


# ── 2. two calls in one transaction ──

class TestTwoCallsOneTransaction:
    def test_second_call_continues_the_first_call_sequence(self, conn):
        env = _seed_chart(conn)
        cid = env["company_id"]
        # One submit, two postings inside a single transaction: invoice legs,
        # then cost-of-goods legs. A single commit at the end.
        invoice = [_leg(env, "ar", "1000.00", "0"),
                   _leg(env, "revenue", "0", "1000.00")]
        cogs = [_leg(env, "expense", "600.00", "0"),
                _leg(env, "cash", "0", "600.00")]
        insert_gl_entries(conn, invoice, voucher_type="sales_invoice",
                          voucher_id="INV-2CALL", posting_date="2026-03-01",
                          company_id=cid)
        insert_gl_entries(conn, cogs, voucher_type="sales_invoice",
                          voucher_id="COGS-2CALL", posting_date="2026-03-01",
                          company_id=cid, entry_set="cogs")
        conn.commit()

        chained = _ordered_chained(conn, cid)
        assert [r["sequence"] for r in chained] == [1, 2, 3, 4]
        first, second = chained[1], chained[2]
        assert second["voucher_id"] == "COGS-2CALL"
        recomputed = _chain_hash("2026-03-01", second["account_id"],
                                 second["debit"], second["credit"],
                                 "sales_invoice", "COGS-2CALL",
                                 first["gl_checksum"])
        assert second["gl_checksum"] == recomputed
        head = _head(conn, cid)
        assert head["last_sequence"] == 4
        assert head["last_checksum"] == chained[-1]["gl_checksum"]
        result = _integrity(conn, cid)
        assert result["chain_intact"] is True
        assert result["broken_links"] == 0


# ── 3. rollback leaves no gap ──

class TestRollbackLeavesNoGap:
    def test_failed_submit_then_good_posting_is_contiguous(self, conn):
        env = _seed_chart(conn)
        cid = env["company_id"]
        invoice = [_leg(env, "ar", "100.00", "0"),
                   _leg(env, "revenue", "0", "100.00")]
        insert_gl_entries(conn, invoice, voucher_type="sales_invoice",
                          voucher_id="INV-ROLL", posting_date="2026-03-01",
                          company_id=cid)
        bad = [_leg(env, "expense", "50.00", "0"),
               _leg(env, "cash", "0", "40.00")]
        with pytest.raises(ValueError):
            insert_gl_entries(conn, bad, voucher_type="sales_invoice",
                              voucher_id="COGS-ROLL", posting_date="2026-03-01",
                              company_id=cid, entry_set="cogs")
        conn.rollback()

        assert _head(conn, cid) is None
        assert _company_legs(conn, cid) == []

        _post(conn, env, "sales_invoice", "INV-GOOD", "2026-03-02",
              [("ar", "100.00", "0"), ("revenue", "0", "100.00")])
        chained = _ordered_chained(conn, cid)
        assert [r["sequence"] for r in chained] == [1, 2]
        head = _head(conn, cid)
        assert head["last_sequence"] == 2
        assert head["last_checksum"] == chained[-1]["gl_checksum"]
        result = _integrity(conn, cid)
        assert result["chain_intact"] is True
        assert result["broken_links"] == 0


# ── 5. cancellation ──

class TestCancellation:
    def test_reversal_legs_stay_outside_and_chain_continues(self, conn):
        env = _seed_chart(conn)
        cid = env["company_id"]
        _post(conn, env, "journal_entry", "JE-CANCEL", "2026-04-01",
              [("cash", "300.00", "0"), ("revenue", "0", "300.00")])
        before = {r["id"]: (r["gl_checksum"], r["sequence"])
                  for r in _company_legs(conn, cid)}
        reverse_gl_entries(conn, "journal_entry", "JE-CANCEL", "2026-04-02")
        conn.commit()

        legs = _company_legs(conn, cid)
        originals = [r for r in legs if r["voucher_id"] == "JE-CANCEL"
                     and r["gl_checksum"]]
        reversals = [r for r in legs if r["gl_checksum"] is None]
        assert len(originals) == 2
        assert len(reversals) == 2
        for r in originals:
            assert (r["gl_checksum"], r["sequence"]) == before[r["id"]]
        for r in reversals:
            assert r["sequence"] is None

        _post(conn, env, "journal_entry", "JE-AFTER", "2026-04-03",
              [("cash", "50.00", "0"), ("revenue", "0", "50.00")])
        chained = _ordered_chained(conn, cid)
        assert [r["sequence"] for r in chained] == [1, 2, 3, 4]
        assert chained[2]["voucher_id"] == "JE-AFTER"
        assert chained[2]["gl_checksum"] == _chain_hash(
            "2026-04-03", chained[2]["account_id"], chained[2]["debit"],
            chained[2]["credit"], "journal_entry", "JE-AFTER",
            chained[1]["gl_checksum"])
        result = _integrity(conn, cid)
        assert result["chain_intact"] is True
        assert result["broken_links"] == 0


# ── 6. the head is never trusted ──

class TestHeadNeverTrusted:
    def test_overwritten_head_checksum_breaks_exactly_one_link(self, conn):
        env = _seed_chart(conn)
        cid = env["company_id"]
        _post(conn, env, "journal_entry", "JE-H1", "2026-05-01",
              [("cash", "100.00", "0"), ("revenue", "0", "100.00")])
        t = Table("gl_chain_head")
        conn.execute(
            Q.update(t).set(t.last_checksum, P()).where(
                t.company_id == P()).get_sql(),
            ("0" * 64, cid))
        conn.commit()
        _post(conn, env, "journal_entry", "JE-H2", "2026-05-02",
              [("cash", "20.00", "0"), ("revenue", "0", "20.00")])
        result = _integrity(conn, cid)
        assert result["chain_intact"] is False
        assert result["broken_links"] == 1

    def test_raised_head_sequence_reports_the_gap(self, conn):
        env = _seed_chart(conn)
        cid = env["company_id"]
        _post(conn, env, "journal_entry", "JE-S1", "2026-05-01",
              [("cash", "100.00", "0"), ("revenue", "0", "100.00")])
        t = Table("gl_chain_head")
        conn.execute(
            Q.update(t).set(t.last_sequence, P()).where(
                t.company_id == P()).get_sql(),
            (4, cid))
        conn.commit()
        _post(conn, env, "journal_entry", "JE-S2", "2026-05-02",
              [("cash", "20.00", "0"), ("revenue", "0", "20.00")])
        chained = _ordered_chained(conn, cid)
        assert [r["sequence"] for r in chained] == [1, 2, 5, 6]
        result = _integrity(conn, cid)
        assert result["chain_intact"] is False
        assert result["broken_links"] == 2


# ── 7. tamper in the sequenced segment ──

class TestSequencedTamper:
    def _four(self, conn):
        env = _seed_chart(conn)
        _post(conn, env, "journal_entry", "JE-T1", "2026-06-01",
              [("cash", "100.00", "0"), ("revenue", "0", "100.00")])
        _post(conn, env, "journal_entry", "JE-T2", "2026-06-02",
              [("cash", "200.00", "0"), ("revenue", "0", "200.00")])
        return env

    def test_edited_debit_breaks_exactly_one_link(self, conn):
        env = self._four(conn)
        chained = _ordered_chained(conn, env["company_id"])
        victim = chained[1]
        t = Table("gl_entry")
        conn.execute(
            Q.update(t).set(t.debit, P()).where(t.id == P()).get_sql(),
            ("999.00", victim["id"]))
        conn.commit()
        result = _integrity(conn, env["company_id"])
        assert result["chain_intact"] is False
        assert result["broken_links"] == 1

    def test_changed_sequence_breaks_three_links(self, conn):
        env = self._four(conn)
        chained = _ordered_chained(conn, env["company_id"])
        victim = [r for r in chained if r["sequence"] == 2][0]
        t = Table("gl_entry")
        conn.execute(
            Q.update(t).set(t.sequence, P()).where(t.id == P()).get_sql(),
            (5, victim["id"]))
        conn.commit()
        result = _integrity(conn, env["company_id"])
        assert result["chain_intact"] is False
        assert result["broken_links"] == 3

    def test_deleted_middle_row_breaks_two_links(self, conn):
        env = self._four(conn)
        chained = _ordered_chained(conn, env["company_id"])
        victim = [r for r in chained if r["sequence"] == 2][0]
        t = Table("gl_entry")
        conn.execute(
            Q.from_(t).delete().where(t.id == P()).get_sql(), (victim["id"],))
        conn.commit()
        result = _integrity(conn, env["company_id"])
        assert result["chain_intact"] is False
        assert result["broken_links"] == 2

    def test_duplicated_sequence_breaks_three_links(self, conn):
        env = self._four(conn)
        chained = _ordered_chained(conn, env["company_id"])
        victim = [r for r in chained if r["sequence"] == 4][0]
        t = Table("gl_entry")
        conn.execute(
            Q.update(t).set(t.sequence, P()).where(t.id == P()).get_sql(),
            (2, victim["id"]))
        conn.commit()
        result = _integrity(conn, env["company_id"])
        assert result["chain_intact"] is False
        assert result["broken_links"] == 3


# ── 8. stale writer (SQLite) ──

class TestStaleWriter:
    def test_null_sequence_row_after_sequenced_rows_is_broken(self, conn):
        _sqlite_only()
        env = _seed_chart(conn)
        cid = env["company_id"]
        _plant_legacy(conn, env, "JE-LEG", "2026-01-05",
                      [("cash", "700.00", "0"), ("revenue", "0", "700.00")])
        _post(conn, env, "journal_entry", "JE-BASE", "2026-01-10",
              [("cash", "100.00", "0"), ("revenue", "0", "100.00")])
        chained = _ordered_chained(conn, cid)
        legacy_tail = [r for r in chained if r["sequence"] is None][-1]
        tail_hash = legacy_tail["gl_checksum"]
        stale_hash = _chain_hash("2026-02-01", env["cash"], "5.00", "0",
                                 "journal_entry", "JE-STALE", tail_hash)
        row = _stale_row(env, "JE-STALE", "2026-02-01", stale_hash)
        sql, _cols = _insert_row(list(row.keys()))
        conn.execute(sql, tuple(row.values()))
        conn.commit()
        result = _integrity(conn, cid)
        assert result["chain_intact"] is False
        # Two breaks: the stale row itself, and the first sequenced leg,
        # whose walk predecessor is now the stale fork instead of the tail
        # it was chained from.
        assert result["broken_links"] == 2
        assert result["legacy_rows"] == 3
        assert result["sequenced_rows"] == 2


def _plant_legacy(conn, env, voucher_id, date, specs):
    """Plant pre-sequence legs the old way: chained checksums, NULL sequence."""
    prev = "GENESIS"
    stamp = 0
    for key, debit, credit in specs:
        account_id = env[key] if key in env else key
        checksum = _chain_hash(date, account_id, debit, credit,
                               "journal_entry", voucher_id, prev)
        stamp += 1
        row = {
            "id": _uuid(),
            "posting_date": date,
            "account_id": account_id,
            "debit": debit,
            "credit": credit,
            "debit_base": debit,
            "credit_base": credit,
            "currency": "USD",
            "exchange_rate": "1",
            "voucher_type": "journal_entry",
            "voucher_id": voucher_id,
            "entry_set": "primary",
            "cost_center_id": env["cc"] if key in ("revenue", "expense") else None,
            "remarks": "",
            "is_cancelled": 0,
            "gl_checksum": checksum,
            "dimensions_json": "{}",
            "created_at": "2026-01-01 10:00:%02d" % stamp,
        }
        sql, _cols = _insert_row(list(row.keys()))
        conn.execute(sql, tuple(row.values()))
        prev = checksum
    conn.commit()
    return prev


def _insert_row(columns):
    from erpclaw_lib.query import insert_row
    return insert_row("gl_entry", {key: P() for key in columns})


def _stale_row(env, voucher_id, date, checksum):
    return {
        "id": _uuid(),
        "posting_date": date,
        "account_id": env["cash"],
        "debit": "5.00",
        "credit": "0",
        "debit_base": "5.00",
        "credit_base": "0",
        "currency": "USD",
        "exchange_rate": "1",
        "voucher_type": "journal_entry",
        "voucher_id": voucher_id,
        "entry_set": "primary",
        "remarks": "",
        "is_cancelled": 0,
        "gl_checksum": checksum,
        "dimensions_json": "{}",
        "created_at": "2026-02-01 10:00:00",
    }


# ── 10. all-zero posting ──

class TestAllZeroPosting:
    def test_all_zero_consumes_no_sequence_and_leaves_head(self, conn):
        env = _seed_chart(conn)
        cid = env["company_id"]
        ids = insert_gl_entries(
            conn, [{"account_id": env["cash"], "debit": "0", "credit": "0"}],
            voucher_type="journal_entry", voucher_id="JE-ZERO",
            posting_date="2026-07-01", company_id=cid)
        conn.commit()
        assert ids == []
        assert _head(conn, cid) is None
        assert _company_legs(conn, cid) == []

        _post(conn, env, "journal_entry", "JE-REAL", "2026-07-02",
              [("cash", "10.00", "0"), ("revenue", "0", "10.00")])
        chained = _ordered_chained(conn, cid)
        assert [r["sequence"] for r in chained] == [1, 2]
        result = _integrity(conn, cid)
        assert result["chain_intact"] is True
        assert result["broken_links"] == 0


# ── 4. SQLite parity: base-code checksums as literals ──

PARITY_CHECKSUMS = {
    ("LV-1", "acc-par-cash", "500.00", "0.00"): "d4e5906eabdda5f93ffb43da9959af1b41632467280af8f85dd3f63f54e2f23b",
    ("LV-1", "acc-par-rev", "0.00", "500.00"): "69a93b1022fc90229dc1aaa6c77a57ad97a5226993187a1a3712f1f78fb828fb",
    ("LV-2", "acc-par-cash", "200.00", "0.00"): "f54cefb2ae5022710f0ce5acf6943ba727f7664b5152ffaa0579343d10eadbdf",
    ("LV-2", "acc-par-rev", "0.00", "200.00"): "5d9f26d00da6516758c2fd6ce932eb12100972205ae1ff1f7b38571e8483d41c",
    ("INV-P1", "acc-par-ar", "1000.00", "0.00"): "28230640ef53fbc8bb86a2aa8bfac8e93656b7c70a74a6eeaf196500eba4961b",
    ("INV-P1", "acc-par-rev", "0.00", "1000.00"): "7b4aa3566667213a66dd661aab2074749b503118af7ea2a0ed060518553ceba9",
    ("COGS-P1", "acc-par-exp", "600.00", "0.00"): "fc6be35e3e1d94f7b6633af94daa5dbb9dd85223cf2707c9adcf8b11ec7962f8",
    ("COGS-P1", "acc-par-cash", "0.00", "600.00"): "9e67598da7c1718881cfffbe8202d9fc2edd3eb38c1c1973cfd646df3f72205b",
    ("OPEN-1", "acc-par-cash", "1000.00", "0.00"): "f924c5983d893c47bd9230b95c4c438256b067b4136c098f694a56fd2fd51f58",
    ("OPEN-1", "acc-par-eq", "0.00", "1000.00"): "4ae936a14a3fce747eb4524391fe43834b7769861d21a086c28ce76cbc0fb647",
    ("PC-1", "acc-par-cash", "10.00", "0.00"): "6ac702a26c9a11c34cde95097c3db5ed269e3ce219fed727ec238c1fe57d1456",
    ("PC-1", "acc-par-eq", "0.00", "10.00"): "a4d6e0f1ae3a82e85125cee9fa1c3b3bb09661e163c1cf03a78656dc54340649",
}


def _seed_parity_chart(conn):
    cid = "comp-parity-1"
    conn.execute(
        "INSERT INTO company (id, name, abbr, default_currency, country,"
        " fiscal_year_start_month) VALUES (?, 'Parity Co', 'PAR', 'USD',"
        " 'United States', 1)",
        (cid,))
    for aid, name, num, root, typ in [
            ("acc-par-cash", "Cash", "1000", "asset", "cash"),
            ("acc-par-rev", "Revenue", "4000", "income", "revenue"),
            ("acc-par-exp", "Expense", "5000", "expense", "expense"),
            ("acc-par-ar", "Receivable", "1100", "asset", "receivable"),
            ("acc-par-eq", "Equity", "3000", "equity", "equity")]:
        direction = ("debit_normal" if root in ("asset", "expense")
                     else "credit_normal")
        conn.execute(
            "INSERT INTO account (id, name, account_number, root_type,"
            " account_type, balance_direction, company_id, depth, is_group)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)",
            (aid, name, num, root, typ, direction, cid))
    conn.execute(
        "INSERT INTO cost_center (id, name, company_id, is_group)"
        " VALUES ('cc-par-1', 'Main', ?, 0)",
        (cid,))
    conn.execute(
        "INSERT INTO fiscal_year (id, name, start_date, end_date, company_id)"
        " VALUES ('fy-par-1', 'FY26', '2026-01-01', '2026-12-31', ?)",
        (cid,))
    conn.execute(
        "INSERT INTO customer (id, name, company_id)"
        " VALUES ('cust-par-1', 'Parity Customer', ?)",
        (cid,))
    conn.commit()
    return {"company_id": cid, "cc": "cc-par-1", "customer": "cust-par-1",
            "cash": "acc-par-cash", "revenue": "acc-par-rev",
            "expense": "acc-par-exp", "ar": "acc-par-ar",
            "equity": "acc-par-eq"}


def _plant_parity_legacy(conn, env):
    prev = "GENESIS"
    stamp = 0
    for voucher_id, date, account_id, debit, credit in [
            ("LV-1", "2026-01-05", "acc-par-cash", "500.00", "0.00"),
            ("LV-1", "2026-01-05", "acc-par-rev", "0.00", "500.00"),
            ("LV-2", "2026-01-06", "acc-par-cash", "200.00", "0.00"),
            ("LV-2", "2026-01-06", "acc-par-rev", "0.00", "200.00")]:
        checksum = PARITY_CHECKSUMS[(voucher_id, account_id, debit, credit)]
        assert checksum == _chain_hash(date, account_id, debit, credit,
                                       "journal_entry", voucher_id, prev)
        stamp += 1
        row = {
            "id": _uuid(),
            "posting_date": date,
            "account_id": account_id,
            "debit": debit,
            "credit": credit,
            "debit_base": debit,
            "credit_base": credit,
            "currency": "USD",
            "exchange_rate": "1",
            "voucher_type": "journal_entry",
            "voucher_id": voucher_id,
            "entry_set": "primary",
            "cost_center_id": ("cc-par-1" if account_id == "acc-par-rev"
                               else None),
            "remarks": "",
            "is_cancelled": 0,
            "gl_checksum": checksum,
            "dimensions_json": "{}",
            "created_at": "2026-01-01 10:00:%02d" % stamp,
        }
        sql, _cols = _insert_row(list(row.keys()))
        conn.execute(sql, tuple(row.values()))
        prev = checksum
    conn.commit()


class TestSqliteParity:
    def test_base_code_checksums_reproduced_with_legacy_first(self, conn):
        _sqlite_only()
        env = _seed_parity_chart(conn)
        cid = env["company_id"]
        _plant_parity_legacy(conn, env)

        def legs(voucher_type, voucher_id, date, items, **kwargs):
            entries = []
            for account_id, debit, credit in items:
                entry = {"account_id": account_id,
                         "debit": debit, "credit": credit}
                if account_id in ("acc-par-rev", "acc-par-exp"):
                    entry["cost_center_id"] = "cc-par-1"
                if account_id == "acc-par-ar":
                    entry["party_type"] = "customer"
                    entry["party_id"] = "cust-par-1"
                entries.append(entry)
            insert_gl_entries(conn, entries, voucher_type=voucher_type,
                              voucher_id=voucher_id, posting_date=date,
                              company_id=cid, **kwargs)
            conn.commit()

        invoice = [{"account_id": "acc-par-ar", "debit": "1000.00",
                    "credit": "0", "party_type": "customer",
                    "party_id": "cust-par-1"},
                   {"account_id": "acc-par-rev", "debit": "0",
                    "credit": "1000.00", "cost_center_id": "cc-par-1"}]
        cogs = [{"account_id": "acc-par-exp", "debit": "600.00",
                 "credit": "0", "cost_center_id": "cc-par-1"},
                {"account_id": "acc-par-cash", "debit": "0",
                 "credit": "600.00"}]
        insert_gl_entries(conn, invoice, voucher_type="sales_invoice",
                          voucher_id="INV-P1", posting_date="2026-03-01",
                          company_id=cid)
        insert_gl_entries(conn, cogs, voucher_type="sales_invoice",
                          voucher_id="COGS-P1", posting_date="2026-03-01",
                          company_id=cid, entry_set="cogs")
        conn.commit()
        reverse_gl_entries(conn, "sales_invoice", "INV-P1", "2026-03-02")
        conn.commit()
        legs("journal_entry", "OPEN-1", "2026-01-01",
             [("acc-par-cash", "1000.00", "0"),
              ("acc-par-eq", "0", "1000.00")], is_opening=True)
        legs("period_closing", "PC-1", "2026-12-31",
             [("acc-par-cash", "10.00", "0"),
              ("acc-par-eq", "0", "10.00")])

        legs_now = _company_legs(conn, cid)
        keyed = {(r["voucher_id"], r["account_id"], r["debit"], r["credit"]): r
                 for r in legs_now}
        for key, expected in PARITY_CHECKSUMS.items():
            assert key in keyed, key
            assert keyed[key]["gl_checksum"] == expected, key
        sequenced = sorted(
            (r for r in legs_now if r["gl_checksum"]
             and r["sequence"] is not None),
            key=lambda r: r["sequence"])
        assert [r["sequence"] for r in sequenced] == [1, 2, 3, 4, 5, 6, 7, 8]
        assert all(r["sequence"] is None for r in legs_now
                   if r["voucher_id"] in ("LV-1", "LV-2"))
        head = _head(conn, cid)
        assert head["last_sequence"] == 8
        assert head["last_checksum"] == PARITY_CHECKSUMS[
            ("PC-1", "acc-par-eq", "0.00", "10.00")]
        result = _integrity(conn, cid)
        assert result["chain_intact"] is True
        assert result["broken_links"] == 0
        assert result["legacy_rows"] == 4
        assert result["sequenced_rows"] == 8
        assert result["pre_sequence_rows"] == 0


# ── 9. PostgreSQL pre-sequence database ──

class TestPreSequencePostgres:
    @pytest.mark.skipif(not os.environ.get("ERPCLAW_PG_TEST_URL"),
                        reason="needs ERPCLAW_PG_TEST_URL")
    def test_legacy_count_reported_and_new_chain_verifies(self, db_path):
        _pg_only()
        _assert_pg_test_database()
        with _fresh_pg_schema() as conn:
            env = _seed_chart(conn)
            cid = env["company_id"]
            prev = _plant_shuffled_legacy(conn, env, "JE-OLD", 6)
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "mig038_pre",
                os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "..", "erpclaw-setup", "migrations", "038_gl_chain_head.py"))
            mig = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mig)
            report = mig.run_migration(db_path)
            assert report["legacy"] == [(cid, 6)]

            _post(conn, env, "journal_entry", "JE-NEW-1", "2026-08-01",
                  [("cash", "100.00", "0"), ("revenue", "0", "100.00")])
            _post(conn, env, "journal_entry", "JE-NEW-2", "2026-08-02",
                  [("cash", "200.00", "0"), ("revenue", "0", "200.00")])
            result = _integrity(conn, cid)
            assert result["pre_sequence_rows"] == 6
            assert result["legacy_rows"] == 6
            assert result["sequenced_rows"] == 4
            assert result["chain_intact"] is False
            assert result["broken_links"] == 0

        with _fresh_pg_schema() as conn:
            env = _seed_chart(conn)
            cid = env["company_id"]
            _post(conn, env, "journal_entry", "JE-NEW-1", "2026-08-01",
                  [("cash", "100.00", "0"), ("revenue", "0", "100.00")])
            _post(conn, env, "journal_entry", "JE-NEW-2", "2026-08-02",
                  [("cash", "200.00", "0"), ("revenue", "0", "200.00")])
            result = _integrity(conn, cid)
            assert result["chain_intact"] is True
            assert result["broken_links"] == 0
            assert result["pre_sequence_rows"] == 0


def _plant_shuffled_legacy(conn, env, voucher_id, count):
    """Plant legacy legs whose write-time stamps run opposite to insertion.

    The checksums chain in insertion order while the stamps descend, so any
    walk that trusts stamp order recomputes the wrong predecessors. The
    count query only counts, so it is unaffected.
    """
    cid = env["company_id"]
    prev = "GENESIS"
    checksums = []
    for i in range(count):
        debit = "%d.00" % (10 * (i + 1))
        checksum = _chain_hash("2026-01-01", env["cash"], debit, "0",
                               "journal_entry", voucher_id, prev)
        checksums.append(checksum)
        prev = checksum
    for i, checksum in enumerate(checksums):
        row = {
            "id": _uuid(),
            "posting_date": "2026-01-01",
            "account_id": env["cash"],
            "debit": "%d.00" % (10 * (i + 1)),
            "credit": "0",
            "debit_base": "%d.00" % (10 * (i + 1)),
            "credit_base": "0",
            "currency": "USD",
            "exchange_rate": "1",
            "voucher_type": "journal_entry",
            "voucher_id": voucher_id,
            "entry_set": "primary",
            "remarks": "",
            "is_cancelled": 0,
            "gl_checksum": checksum,
            "dimensions_json": "{}",
            "created_at": "2026-01-%02d 10:00:00" % (count - i),
        }
        sql, _cols = _insert_row(list(row.keys()))
        conn.execute(sql, tuple(row.values()))
    conn.commit()
    return prev


# ── 11. migration: idempotent, audited, agreed ──

class TestChainHeadMigration:
    def _load_migration(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "mig038",
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", "..", "erpclaw-setup", "migrations", "038_gl_chain_head.py"))
        mig = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mig)
        return mig

    def test_migration_is_idempotent(self, conn, db_path):
        mig = self._load_migration()
        first = mig.run_migration(db_path)
        second = mig.run_migration(db_path)
        assert first["legacy"] == []
        assert second["legacy"] == []
        assert seam.table_exists("gl_chain_head", db_path)
        assert "idx_gl_entry_sequence" in seam.index_names("gl_entry", db_path)

    def test_fresh_install_and_upgraded_schema_agree(self, conn, db_path):
        env = _seed_chart(conn)
        cid = env["company_id"]
        _post(conn, env, "journal_entry", "JE-MIG", "2026-09-01",
              [("cash", "10.00", "0"), ("revenue", "0", "10.00")])
        fresh_table = seam.describe_table("gl_chain_head", db_path)
        fresh_indexes = sorted(seam.index_names("gl_entry", db_path))

        conn.execute("DROP TABLE gl_chain_head")
        conn.execute("DROP INDEX idx_gl_entry_sequence")
        conn.commit()
        assert not seam.table_exists("gl_chain_head", db_path)

        mig = self._load_migration()
        report = mig.run_migration(db_path)
        assert report["legacy"] == []
        assert seam.describe_table("gl_chain_head", db_path) == fresh_table
        assert sorted(seam.index_names("gl_entry", db_path)) == fresh_indexes
        result = _integrity(conn, cid)
        assert result["chain_intact"] is True

    def test_report_only_provisions_nothing(self, conn, db_path):
        mig = self._load_migration()
        conn.execute("DROP TABLE IF EXISTS gl_chain_head")
        try:
            conn.execute("DROP INDEX IF EXISTS idx_gl_entry_sequence")
        except Exception:
            pass
        conn.commit()
        assert not seam.table_exists("gl_chain_head", db_path)
        report = mig.run_migration(db_path, report_only=True)
        assert report["report_only"] is True
        assert report["provisioned"] is False
        assert report["index_created"] is False
        assert not seam.table_exists("gl_chain_head", db_path)
        assert "idx_gl_entry_sequence" not in seam.index_names(
            "gl_entry", db_path)
