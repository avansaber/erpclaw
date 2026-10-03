"""Chain-head concurrency and helper proofs (task m332c).

Puts the /tmp proofs from the previous returns into the tree as rerunnable
tests. No product code changes here; every test drives the real
``insert_gl_entries`` / ``take_chain_heads`` / ``is_lock_conflict``.

Conventions shared with the rest of this suite: money is exact text, reads go
through ``erpclaw_lib.query`` builders on connections from
``erpclaw_lib.db.get_connection``. PostgreSQL legs run only on the private
cluster bound through the suite ``db_path`` fixture (skip when
``ERPCLAW_PG_TEST_URL`` is unset); concurrently-run module actions would go
through ``subprocess.Popen`` (used in the payments half of this task) because
the in-process ``call_action`` patches process-wide stdout, while the bare
``insert_gl_entries`` / ``take_chain_heads`` calls below touch no stdout and
can run on plain threads. Every thread join is bounded; every connection is
opened before any barrier or thread start.
"""
import hashlib
import os
import sqlite3
import sys
import threading
import uuid

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import pytest

from gl_helpers import call_action, get_conn, is_ok, load_db_query, ns  # noqa: E402

from erpclaw_lib.db import get_connection, get_dialect, is_lock_conflict  # noqa: E402
from erpclaw_lib.gl_posting import insert_gl_entries, take_chain_heads  # noqa: E402
from erpclaw_lib.query import P, Q, Table, insert_row  # noqa: E402

GL = load_db_query()

_PG_SKIP = pytest.mark.skipif(
    not os.environ.get("ERPCLAW_PG_TEST_URL"),
    reason="needs ERPCLAW_PG_TEST_URL (private PostgreSQL cluster)",
)


def _pg_only():
    if get_dialect() != "postgresql":
        pytest.skip("PostgreSQL-only case: needs the ERPCLAW_PG_TEST_URL lane")


def _is_pg():
    return get_dialect() == "postgresql"


def _lock_timeout_secs():
    raw = os.environ.get("ERPCLAW_PG_LOCK_TIMEOUT", "5s").strip().lower()
    try:
        if raw.endswith("ms"):
            return float(raw[:-2]) / 1000.0
        if raw.endswith("s"):
            return float(raw[:-1])
        return float(raw)
    except ValueError:
        return 5.0


def _uuid():
    return str(uuid.uuid4())


def _seed_chart(conn, company_id=None):
    cid = company_id or _uuid()
    tag = cid.replace("-", "")
    conn.execute(
        "INSERT INTO company (id, name, abbr) VALUES (?, ?, ?)",
        (cid, "Proof Co " + tag[:16], "PC-" + tag[:12]),
    )
    conn.execute(
        "INSERT INTO fiscal_year (id, name, start_date, end_date, company_id)"
        " VALUES (?, ?, '2026-01-01', '2026-12-31', ?)",
        (_uuid(), "FY-" + tag[:12], cid),
    )
    ccid = _uuid()
    conn.execute(
        "INSERT INTO cost_center (id, name, company_id, is_group)"
        " VALUES (?, ?, ?, 0)",
        (ccid, "Main CC " + tag[:12], cid),
    )
    accounts = {}
    for name, root_type in (("cash", "asset"), ("revenue", "income")):
        aid = _uuid()
        direction = ("debit_normal" if root_type in ("asset", "expense")
                     else "credit_normal")
        conn.execute(
            "INSERT INTO account (id, name, account_number, root_type,"
            " balance_direction, company_id, depth, is_group)"
            " VALUES (?, ?, ?, ?, ?, ?, 0, 0)",
            (aid, name + "-" + aid[:4], "N-" + aid[:4],
             root_type, direction, cid),
        )
        accounts[name] = aid
    conn.commit()
    return {"company_id": cid, "cc": ccid, **accounts}


def _legs(env, cash, cash_credit, revenue_debit="0", revenue_credit=None):
    return [
        {"account_id": env["cash"], "debit": cash, "credit": cash_credit},
        {"account_id": env["revenue"], "debit": revenue_debit,
         "credit": revenue_credit if revenue_credit is not None else cash,
         "cost_center_id": env["cc"]},
    ]


def _post_hold(conn, env, voucher_id, amount, date="2026-03-01"):
    insert_gl_entries(
        conn, _legs(env, amount, "0", revenue_credit=amount),
        voucher_type="journal_entry", voucher_id=voucher_id,
        posting_date=date, company_id=env["company_id"])


def _sequences(conn, company_id):
    g = Table("gl_entry").as_("g")
    a = Table("account").as_("a")
    q = (Q.from_(g).join(a).on(g.account_id == a.id)
         .select(g.sequence)
         .where(a.company_id == P())
         .where(g.gl_checksum.isnotnull())
         .where(g.sequence.isnotnull())
         .orderby(g.sequence))
    return [r["sequence"]
            for r in conn.execute(q.get_sql(), (company_id,)).fetchall()]


def _assert_contiguous(conn, company_id, count):
    seqs = _sequences(conn, company_id)
    assert len(seqs) == count, (len(seqs), count)
    assert seqs == list(range(1, count + 1)), seqs


def _assert_intact(conn, company_id):
    result = call_action(GL.check_gl_integrity, conn,
                         ns(company_id=company_id, company_name=None))
    assert is_ok(result), result
    assert result["chain_intact"] is True, result
    assert result["broken_links"] == 0, result
    return result


def _head(conn, company_id):
    t = Table("gl_chain_head")
    q = Q.from_(t).select(t.star).where(t.company_id == P())
    row = conn.execute(q.get_sql(), (company_id,)).fetchone()
    return dict(row) if row else None


def _chain_hash(date, account_id, debit, credit,
                voucher_type, voucher_id, prev):
    return hashlib.sha256("|".join(
        [date, account_id, str(debit), str(credit),
         voucher_type, voucher_id, prev]).encode("utf-8")).hexdigest()


# ── 1. concurrency: a second poster blocks on the held head ──

@_PG_SKIP
def test_concurrent_postings_serialize_on_the_head(db_path):
    """Same-company posters serialize; another company is unaffected.

    A posts to company 1 and holds (uncommitted). A second poster for company
    1 on its own connection is still blocked after ``join(timeout=1.0)``; a
    poster for company 2 finishes within ``join(timeout=2.0)`` while A still
    holds. A commits well inside ``lock_timeout``; the waiter then finishes
    within ``lock_timeout + 2`` with contiguous sequences and an intact
    chain.
    """
    _pg_only()
    conn_a = get_connection()
    conn_same = get_connection()
    conn_other = get_connection()
    try:
        env1 = _seed_chart(conn_a)
        env2 = _seed_chart(conn_a)
        _post_hold(conn_a, env1, "BASE-1", "10.00", "2026-01-01")
        conn_a.commit()
        _post_hold(conn_a, env2, "BASE-2", "10.00", "2026-01-01")
        conn_a.commit()

        _post_hold(conn_a, env1, "A-HOLD", "20.00", "2026-01-02")

        same_result = {}
        other_result = {}

        def post_same():
            try:
                _post_hold(conn_same, env1, "B-WAIT", "30.00", "2026-01-03")
                conn_same.commit()
                same_result["ok"] = True
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                same_result["error"] = repr(exc)
                try:
                    conn_same.rollback()
                except Exception:  # noqa: BLE001, S110 - best effort
                    pass

        def post_other():
            try:
                _post_hold(conn_other, env2, "C-FREE", "40.00", "2026-01-02")
                conn_other.commit()
                other_result["ok"] = True
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                other_result["error"] = repr(exc)
                try:
                    conn_other.rollback()
                except Exception:  # noqa: BLE001, S110 - best effort
                    pass

        waiter = threading.Thread(target=post_same)
        waiter.start()
        waiter.join(timeout=1.0)
        assert waiter.is_alive(), "same-company poster should block on the held head"

        free = threading.Thread(target=post_other)
        free.start()
        free.join(timeout=2.0)
        assert not free.is_alive(), "other-company poster must not block"
        assert other_result.get("ok"), other_result

        conn_a.commit()
        waiter.join(timeout=_lock_timeout_secs() + 2)
        assert not waiter.is_alive(), "waiter must finish once A commits"
        assert same_result.get("ok"), same_result

        _assert_contiguous(conn_a, env1["company_id"], 6)
        _assert_contiguous(conn_a, env2["company_id"], 4)
        _assert_intact(conn_a, env1["company_id"])
        _assert_intact(conn_a, env2["company_id"])
    finally:
        conn_a.close()
        conn_same.close()
        conn_other.close()


# ── 2. racing first postings: exactly one head row ──

@_PG_SKIP
def test_racing_first_postings_share_one_head(db_path):
    """Two threads released by a barrier post a new company's first voucher.

    Exactly one head row, sequences 1..n contiguous, chain intact.
    """
    _pg_only()
    seed_conn = get_connection()
    conn1 = get_connection()
    conn2 = get_connection()
    try:
        env = _seed_chart(seed_conn)
        cid = env["company_id"]
        assert _head(seed_conn, cid) is None
        barrier = threading.Barrier(2)
        errors = {}

        def racer(conn, voucher_id, amount, slot):
            try:
                barrier.wait(timeout=10)
                _post_hold(conn, env, voucher_id, amount, "2026-02-01")
                conn.commit()
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                errors[slot] = repr(exc)
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001, S110 - best effort
                    pass

        first = threading.Thread(target=racer,
                                 args=(conn1, "RACE-A", "100.00", "a"))
        second = threading.Thread(target=racer,
                                  args=(conn2, "RACE-B", "200.00", "b"))
        first.start()
        second.start()
        first.join(timeout=15)
        second.join(timeout=15)
        assert not first.is_alive(), "racer A must finish"
        assert not second.is_alive(), "racer B must finish"
        assert not errors, errors

        t = Table("gl_chain_head")
        q = (Q.from_(t).select(t.star).where(t.company_id == P()))
        heads = conn1.execute(q.get_sql(), (cid,)).fetchall()
        assert len(heads) == 1, "exactly one head row per company"
        _assert_contiguous(seed_conn, cid, 4)
        _assert_intact(seed_conn, cid)
    finally:
        seed_conn.close()
        conn1.close()
        conn2.close()


# ── 5. two companies: heads taken in ascending order ──

@_PG_SKIP
def test_two_companies_take_heads_in_sorted_order(db_path):
    """``take_chain_heads`` holds every head in one global order.

    A posts to ``company-a`` and holds. B takes ``["company-b",
    "company-a"]`` (given unsorted) and is still waiting after
    ``join(timeout=1.0)``. A then posts to ``company-b`` within
    ``join(timeout=2.0)`` and commits; B finishes within ``lock_timeout + 2``.
    """
    _pg_only()
    conn_a = get_connection()
    conn_b = get_connection()
    try:
        env_a = _seed_chart(conn_a, company_id="company-a")
        env_b = _seed_chart(conn_a, company_id="company-b")
        _post_hold(conn_a, env_a, "BASE-A", "10.00", "2026-01-01")
        conn_a.commit()
        _post_hold(conn_a, env_b, "BASE-B", "10.00", "2026-01-01")
        conn_a.commit()

        _post_hold(conn_a, env_a, "A-HOLD", "20.00", "2026-01-02")

        taken = {}

        def take_both():
            try:
                take_chain_heads(conn_b, ["company-b", "company-a"])
                conn_b.commit()
                taken["ok"] = True
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                taken["error"] = repr(exc)
                try:
                    conn_b.rollback()
                except Exception:  # noqa: BLE001, S110 - best effort
                    pass

        taker = threading.Thread(target=take_both)
        taker.start()
        taker.join(timeout=1.0)
        assert taker.is_alive(), "B must wait on company-a's held head"

        second_post = {}

        def post_b_on_a():
            try:
                _post_hold(conn_a, env_b, "A-TO-B", "30.00", "2026-01-03")
                conn_a.commit()
                second_post["ok"] = True
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                second_post["error"] = repr(exc)
                try:
                    conn_a.rollback()
                except Exception:  # noqa: BLE001, S110 - best effort
                    pass

        poster = threading.Thread(target=post_b_on_a)
        poster.start()
        poster.join(timeout=2.0)
        assert not poster.is_alive(), "A's company-b posting must not block"
        assert second_post.get("ok"), second_post

        taker.join(timeout=_lock_timeout_secs() + 2)
        assert not taker.is_alive(), "B must finish once A commits"
        assert taken.get("ok"), taken

        _assert_contiguous(conn_a, "company-a", 4)
        _assert_contiguous(conn_a, "company-b", 4)
        _assert_intact(conn_a, "company-a")
        _assert_intact(conn_a, "company-b")
    finally:
        conn_a.close()
        conn_b.close()


# ── 6. helper order, both backends ──

def test_take_chain_heads_dedupes_and_sorts(db_path):
    """Duplicate and unsorted ids create one head per company."""
    conn = get_conn(db_path)
    try:
        env_b = _seed_chart(conn)
        env_a = _seed_chart(conn)
        first, second = sorted(
            [env_a["company_id"], env_b["company_id"]])
        take_chain_heads(conn, [second, first, second, first])
        conn.commit()
        t = Table("gl_chain_head")
        rows = conn.execute(
            Q.from_(t).select(t.company_id).get_sql()).fetchall()
        assert sorted(r["company_id"] for r in rows) == [first, second]
    finally:
        conn.close()


def test_is_lock_conflict_false_on_value_error(db_path):
    """A plain programming error is never a lock conflict, either backend."""
    assert is_lock_conflict(ValueError("boom")) is False


def test_is_lock_conflict_true_on_locked_database(db_path):
    """A real locked-database error is a lock conflict (SQLite only)."""
    if _is_pg():
        pytest.skip("SQLite-only case: the locked-database error is SQLite's")
    holder = get_conn(db_path)
    try:
        holder.execute("BEGIN IMMEDIATE")
        probe = sqlite3.connect(db_path, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError) as exc_info:
                probe.execute("UPDATE company SET name = name")
            assert is_lock_conflict(exc_info.value) is True
        finally:
            probe.close()
    finally:
        holder.rollback()
        holder.close()


def test_legacy_tail_seeds_the_head_on_first_take(db_path):
    """On SQLite a pre-head legacy chain seeds the head it would have had.

    A company with planted legacy chained rows and no head gets from
    ``take_chain_heads`` a head whose ``last_checksum`` equals the legacy
    tail (the seed a posting would compute), and a posting after it verifies
    intact.
    """
    if _is_pg():
        pytest.skip("SQLite-only case: legacy write order is recoverable here")
    conn = get_conn(db_path)
    try:
        env = _seed_chart(conn)
        cid = env["company_id"]
        tail = _plant_legacy(conn, env, "JE-LEG", "2026-01-05",
                             [("cash", "700.00", "0"),
                              ("revenue", "0", "700.00")])
        assert _head(conn, cid) is None
        take_chain_heads(conn, [cid])
        conn.commit()
        head = _head(conn, cid)
        assert head is not None
        assert head["last_sequence"] == 0
        assert head["last_checksum"] == tail
        _post_hold(conn, env, "JE-NEW", "100.00", "2026-01-10")
        conn.commit()
        _assert_contiguous(conn, cid, 2)
        result = _assert_intact(conn, cid)
        assert result["legacy_rows"] == 2
    finally:
        conn.close()


def _plant_legacy(conn, env, voucher_id, date, specs):
    """Plant pre-sequence legs: chained checksums, NULL sequence."""
    prev = "GENESIS"
    stamp = 0
    for key, debit, credit in specs:
        account_id = env[key]
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
            "cost_center_id": env["cc"] if key == "revenue" else None,
            "remarks": "",
            "is_cancelled": 0,
            "gl_checksum": checksum,
            "dimensions_json": "{}",
            "created_at": "2026-01-01 10:00:%02d" % stamp,
        }
        sql, _ = insert_row("gl_entry", {name: P() for name in row})
        conn.execute(sql, tuple(row[name] for name in row))
        prev = checksum
    conn.commit()
    return prev
