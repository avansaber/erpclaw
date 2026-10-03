"""Journals seam: staged postings flow through the ledger check."""

import json
import os
import re
import statistics
import sys
import time
import uuid

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import journals_helpers as helpers  # noqa: E402  (binds erpclaw_lib to this tree)
from erpclaw_lib import authority_gate  # noqa: E402
from erpclaw_lib import authority_readiness  # noqa: E402
from erpclaw_lib import authority_sink  # noqa: E402
from erpclaw_lib import seam  # noqa: E402
from erpclaw_lib.db import get_connection  # noqa: E402
from erpclaw_lib.query import Field, P, Q, Table, Star  # noqa: E402

DATE = "2026-06-20"

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")


def _mask(value):
    if isinstance(value, dict):
        return {key: _mask(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_mask(item) for item in value]
    if isinstance(value, str):
        out = _UUID_RE.sub("UUID", value)
        out = _TS_RE.sub("TS", out)
        return out
    return value


def _lines(env, *specs):
    return json.dumps([
        {"account_id": account, "debit": debit, "credit": credit,
         "cost_center_id": env["cc"]}
        for account, debit, credit in specs])


def _add(conn, env, lines):
    mod = helpers.load_db_query()
    return helpers.call_action(
        mod.add_journal_entry, conn, helpers.ns(
            company_id=env["company_id"], posting_date=DATE,
            entry_type="journal", remark="seam",
            lines=lines, cwip_asset_id=None))


def _submit(mod, conn, je_id):
    return helpers.call_action(
        mod.submit_journal_entry, conn,
        helpers.ns(journal_entry_id=je_id))


def _cancel(mod, conn, je_id):
    return helpers.call_action(
        mod.cancel_journal_entry, conn,
        helpers.ns(journal_entry_id=je_id))


def _count_rows(conn, table_name):
    table = Table(table_name)
    query = Q.from_(table).select(Star()).get_sql()
    return len(conn.execute(query).fetchall())


def _gl_tuples(conn):
    table = Table("gl_entry")
    query = Q.from_(table).select(
        Field("account_id"), Field("debit"), Field("credit"),
        Field("is_cancelled"), Field("voucher_type"),
        Field("entry_set")).get_sql()
    rows = conn.execute(query).fetchall()
    acc = Table("account")
    amap = {}
    for row in conn.execute(
            Q.from_(acc).select(
                Field("id"), Field("name")).get_sql()).fetchall():
        amap[row["id"]] = row["name"]
    out = []
    for row in rows:
        out.append((amap.get(row["account_id"]), str(row["debit"]),
                    str(row["credit"]), row["is_cancelled"],
                    row["voucher_type"], row["entry_set"]))
    return sorted(out)


def _chain_seq(conn):
    table = Table("gl_chain_head")
    query = Q.from_(table).select(Field("last_sequence")).get_sql()
    rows = conn.execute(query).fetchall()
    return sorted(str(row["last_sequence"]) for row in rows)


def _do_cycle(db_path):
    mod = helpers.load_db_query()
    conn = get_connection(db_path)
    try:
        env = helpers.build_journals_env(conn)
    finally:
        conn.close()
    conn = get_connection(db_path)
    try:
        add = helpers.call_action(
            mod.add_journal_entry, conn, helpers.ns(
                company_id=env["company_id"], posting_date=DATE,
                entry_type="journal", remark="seam",
                lines=_lines(env, (env["expense"], "100.00", "0"),
                             (env["cash"], "0", "100.00")),
                cwip_asset_id=None))
        assert add.get("status") == "ok", add
        je_id = add["journal_entry_id"]
        sub = helpers.call_action(
            mod.submit_journal_entry, conn,
            helpers.ns(journal_entry_id=je_id))
        assert sub.get("status") in ("ok", "submitted"), sub
        can = helpers.call_action(
            mod.cancel_journal_entry, conn,
            helpers.ns(journal_entry_id=je_id))
        assert can.get("status") in ("ok", "cancelled"), can
        responses = [add, sub, can]
    finally:
        conn.close()
    reader = get_connection(db_path)
    try:
        names = seam.table_names(db_path)
        counts = {name: _count_rows(reader, name) for name in names}
        gl_rows = _gl_tuples(reader)
        seq = _chain_seq(reader)
    finally:
        reader.close()
    return responses, counts, gl_rows, seq


@pytest.mark.parametrize("with_install", [True, False])
def test_staged_journal_cycle_unchanged(tmp_path, monkeypatch,
                                        with_install):
    db_a = str(tmp_path / "a.sqlite")
    db_b = str(tmp_path / "b.sqlite")
    helpers.init_all_tables(db_a)
    helpers.init_all_tables(db_b)
    orig = authority_sink.check_statement
    protected = []
    def _spy(wrapper, sql):
        text = sql if isinstance(sql, str) else str(sql)
        targets = {t for t in authority_sink.classify(text)
                   if authority_sink.LEDGER_SINKS[t]
                   in authority_sink.ENFORCED_FAMILIES}
        if targets:
            protected.append(text)
        return orig(wrapper, sql)
    monkeypatch.setattr(authority_sink, "check_statement", _spy)
    if not with_install:
        for target in (db_a, db_b):
            dropper = get_connection(target)
            try:
                dropper.execute("DROP TABLE authority_install")
                dropper.commit()
            finally:
                dropper.close()
    resp_a, counts_a, gl_a, seq_a = _do_cycle(db_a)
    assert len(protected) >= 1
    monkeypatch.setattr(authority_sink, "check_statement",
                        lambda wrapper, sql: None)
    resp_b, counts_b, gl_b, seq_b = _do_cycle(db_b)
    assert len(resp_a) == len(resp_b) == 3
    for one, two in zip(resp_a, resp_b):
        assert one.get("status") == two.get("status")
        assert set(one.keys()) == set(two.keys())
        assert _mask(one) == _mask(two)
    assert counts_a == counts_b
    assert gl_a == gl_b
    assert seq_a == seq_b


def test_malformed_install_blocks_the_posting(tmp_path):
    db_path = str(tmp_path / "m.sqlite")
    helpers.init_all_tables(db_path)
    mod = helpers.load_db_query()
    conn = get_connection(db_path)
    try:
        env = helpers.build_journals_env(conn)
    finally:
        conn.close()
    conn = get_connection(db_path)
    try:
        add = helpers.call_action(
            mod.add_journal_entry, conn, helpers.ns(
                company_id=env["company_id"], posting_date=DATE,
                entry_type="journal", remark="seam",
                lines=_lines(env, (env["expense"], "100.00", "0"),
                             (env["cash"], "0", "100.00")),
                cwip_asset_id=None))
        assert add.get("status") == "ok", add
        je_id = add["journal_entry_id"]
    finally:
        conn.close()
    bad = get_connection(db_path)
    try:
        table = Table("authority_install")
        query = Q.update(table).set(
            Field("install_id"), P()).get_sql()
        bad.execute(query, ("",))
        bad.commit()
    finally:
        bad.close()
    conn = get_connection(db_path)
    try:
        with pytest.raises(authority_sink.LedgerWriteRefused) as excinfo:
            helpers.call_action(
                mod.submit_journal_entry, conn,
                helpers.ns(journal_entry_id=je_id))
        assert excinfo.value.args == ("AUTHORITY_NOT_READY",)
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
    reader = get_connection(db_path)
    try:
        table = Table("journal_entry")
        query = Q.from_(table).select(Field("status")).where(
            Field("id") == P()).get_sql()
        row = reader.execute(query, (je_id,)).fetchone()
        assert row["status"] == "draft"
        assert _count_rows(reader, "gl_entry") == 0
        assert _count_rows(reader, "gl_chain_head") == 0
    finally:
        reader.close()
    direct = get_connection(db_path)
    try:
        from erpclaw_lib.gl_posting import insert_gl_entries
        legs = [
            {"account_id": env["cash"], "debit": "10.00",
             "credit": "0"},
            {"account_id": env["expense"], "debit": "0",
             "credit": "10.00", "cost_center_id": env["cc"]},
        ]
        with pytest.raises(authority_sink.LedgerWriteRefused) as excinfo2:
            insert_gl_entries(
                direct, legs, "journal_entry", str(uuid.uuid4()),
                DATE, env["company_id"])
        assert excinfo2.value.args == ("AUTHORITY_NOT_READY",)
    finally:
        try:
            direct.rollback()
        except Exception:
            pass
        direct.close()


def _make_active(db_path):
    conn = get_connection(db_path)
    try:
        table = Table("authority_install")
        query = Q.update(table).set(
            Field("phase"), P()).get_sql()
        conn.execute(query, ("ACTIVE",))
        conn.commit()
    finally:
        conn.close()


def _direct_legs(env):
    return [
        {"account_id": env["cash"], "debit": "10.00", "credit": "0"},
        {"account_id": env["expense"], "debit": "0",
         "credit": "10.00", "cost_center_id": env["cc"]},
    ]


def _snap_gl(conn):
    table = Table("gl_entry")
    query = Q.from_(table).select(
        Field("account_id"), Field("debit"), Field("credit"),
        Field("is_cancelled"), Field("voucher_type"),
        Field("entry_set"), Field("voucher_id")).get_sql()
    rows = sorted(
        tuple(row) for row in
        (tuple(record) for record in conn.execute(query).fetchall()))
    head = Table("gl_chain_head")
    hquery = Q.from_(head).select(
        Field("company_id"), Field("last_sequence"),
        Field("last_checksum")).get_sql()
    heads = sorted(
        tuple(row) for row in
        (tuple(record) for record in conn.execute(hquery).fetchall()))
    return (rows, heads)


def test_active_refuses_each_gl_helper(tmp_path, monkeypatch):
    """not qualification: patched readiness below proves nothing."""
    from erpclaw_lib.gl_posting import (
        insert_gl_entries, reverse_gl_entries, take_chain_heads)
    db_path = str(tmp_path / "h.sqlite")
    helpers.init_all_tables(db_path)
    setup = get_connection(db_path)
    try:
        env = helpers.build_journals_env(setup)
    finally:
        setup.close()
    company_id = env["company_id"]
    voucher_first = str(uuid.uuid4())
    conn = get_connection(db_path)
    try:
        insert_gl_entries(
            conn, _direct_legs(env), "journal_entry",
            voucher_first, DATE, company_id)
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
    posted = str(uuid.uuid4())
    conn = get_connection(db_path)
    try:
        insert_gl_entries(
            conn, _direct_legs(env), "journal_entry",
            posted, DATE, company_id)
        conn.commit()
    finally:
        conn.close()
    from erpclaw_lib.db import get_connection as _gc
    _make_active(db_path)
    monkeypatch.setattr(authority_readiness, "is_ready", lambda c: True)
    fresh_id = str(uuid.uuid4())
    for label, thunk in [
        ("insert", lambda handle: insert_gl_entries(
            handle, _direct_legs(env), "journal_entry",
            fresh_id, DATE, company_id)),
        ("heads", lambda handle: take_chain_heads(
            handle, [company_id])),
        ("reverse", lambda handle: reverse_gl_entries(
            handle, "journal_entry", posted, DATE)),
    ]:
        before_conn = _gc(db_path)
        try:
            before = _snap_gl(before_conn)
        finally:
            before_conn.close()
        handle = _gc(db_path)
        try:
            with pytest.raises(
                    authority_sink.LedgerWriteRefused) as excinfo:
                thunk(handle)
            assert excinfo.value.args == ("LEDGER_WRITE_UNAUTHORIZED",)
        finally:
            try:
                handle.rollback()
            except Exception:
                pass
            handle.close()
        after_conn = _gc(db_path)
        try:
            assert _snap_gl(after_conn) == before
        finally:
            after_conn.close()
    handle = _gc(db_path)
    try:
        name = "gl_" + "entry"
        with pytest.raises(
                authority_sink.LedgerWriteRefused) as excinfo:
            handle.execute(
                "INSERT INTO " + name + " (id) VALUES (?)",
                ("x-" + fresh_id,))
        assert excinfo.value.args == ("LEDGER_WRITE_UNAUTHORIZED",)
    finally:
        try:
            handle.rollback()
        except Exception:
            pass
        handle.close()
    monkeypatch.setattr(authority_readiness, "is_ready", lambda c: False)
    for thunk in [
        lambda handle: insert_gl_entries(
            handle, _direct_legs(env), "journal_entry",
            str(uuid.uuid4()), DATE, company_id),
        lambda handle: take_chain_heads(handle, [company_id]),
        lambda handle: reverse_gl_entries(
            handle, "journal_entry", posted, DATE),
    ]:
        handle = _gc(db_path)
        try:
            with pytest.raises(
                    authority_sink.LedgerWriteRefused) as excinfo:
                thunk(handle)
            assert excinfo.value.args == ("AUTHORITY_NOT_READY",)
        finally:
            try:
                handle.rollback()
            except Exception:
                pass
            handle.close()


def test_perf_record_forty_line_entry(tmp_path, monkeypatch):
    db_a = str(tmp_path / "p_a.sqlite")
    db_b = str(tmp_path / "p_b.sqlite")
    helpers.init_all_tables(db_a)
    helpers.init_all_tables(db_b)
    orig = authority_sink.check_statement
    protected_counts = []
    def _spy(wrapper, sql):
        text = sql if isinstance(sql, str) else str(sql)
        targets = {t for t in authority_sink.classify(text)
                   if authority_sink.LEDGER_SINKS[t]
                   in authority_sink.ENFORCED_FAMILIES}
        if targets:
            protected_counts.append(text)
        return orig(wrapper, sql)
    monkeypatch.setattr(authority_sink, "check_statement", _spy)
    mod = helpers.load_db_query()
    conn_a = get_connection(db_a)
    try:
        env_a = helpers.build_journals_env(conn_a)
    finally:
        conn_a.close()
    conn_a = get_connection(db_a)
    trace_a = []
    times_a = []
    try:
        raw_a = object.__getattribute__(conn_a, "_conn")
        for _ in range(5):
            legs = []
            for idx in range(20):
                legs.append((env_a["cash"], "1.00", "0"))
                legs.append((env_a["expense"], "0", "1.00"))
            spec = _lines(env_a, *legs)
            add = helpers.call_action(
                mod.add_journal_entry, conn_a, helpers.ns(
                    company_id=env_a["company_id"], posting_date=DATE,
                    entry_type="journal", remark="perf",
                    lines=spec, cwip_asset_id=None))
            assert add.get("status") == "ok", add
            before = len(protected_counts)
            seen = []
            raw_a.set_trace_callback(lambda sql: seen.append(sql))
            start = time.perf_counter()
            sub = helpers.call_action(
                mod.submit_journal_entry, conn_a,
                helpers.ns(journal_entry_id=add["journal_entry_id"]))
            elapsed = time.perf_counter() - start
            raw_a.set_trace_callback(None)
            assert sub.get("status") in ("ok", "submitted"), sub
            trace_a.append(len(seen))
            times_a.append(elapsed)
            protected_counts.append(("MARK", len(seen), before))
    finally:
        conn_a.close()
    per_submit_protected = []
    running = []
    for entry in protected_counts:
        if isinstance(entry, tuple):
            per_submit_protected.append(len(running))
            running = []
        else:
            running.append(entry)
    monkeypatch.setattr(authority_sink, "check_statement",
                        lambda wrapper, sql: None)
    conn_b = get_connection(db_b)
    try:
        env_b = helpers.build_journals_env(conn_b)
    finally:
        conn_b.close()
    conn_b = get_connection(db_b)
    trace_b = []
    times_b = []
    try:
        raw_b = object.__getattribute__(conn_b, "_conn")
        for _ in range(5):
            legs = []
            for idx in range(20):
                legs.append((env_b["cash"], "1.00", "0"))
                legs.append((env_b["expense"], "0", "1.00"))
            spec = _lines(env_b, *legs)
            add = helpers.call_action(
                mod.add_journal_entry, conn_b, helpers.ns(
                    company_id=env_b["company_id"], posting_date=DATE,
                    entry_type="journal", remark="perf",
                    lines=spec, cwip_asset_id=None))
            assert add.get("status") == "ok", add
            seen = []
            raw_b.set_trace_callback(lambda sql: seen.append(sql))
            start = time.perf_counter()
            sub = helpers.call_action(
                mod.submit_journal_entry, conn_b,
                helpers.ns(journal_entry_id=add["journal_entry_id"]))
            elapsed = time.perf_counter() - start
            raw_b.set_trace_callback(None)
            assert sub.get("status") in ("ok", "submitted"), sub
            trace_b.append(len(seen))
            times_b.append(elapsed)
    finally:
        conn_b.close()
    assert len(trace_a) == len(trace_b) == 5
    assert len(per_submit_protected) == 5
    for idx in range(5):
        assert trace_a[idx] - trace_b[idx] == 3 * per_submit_protected[idx]
    med_a = statistics.median(times_a)
    med_b = statistics.median(times_b)
    ratio = (med_a / med_b) if med_b else float("inf")
    print("perf medians: seam=%.6f no-op=%.6f ratio=%.4f" % (
        med_a, med_b, ratio))
    print("statement diff per submit: %s protected per submit: %s" % (
        [a - b for a, b in zip(trace_a, trace_b)],
        per_submit_protected))
