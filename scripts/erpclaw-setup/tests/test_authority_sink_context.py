"""Authorized ledger writes pass the seam; refused ones poison the handle."""

import logging
import os
import sys
import uuid

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import setup_helpers  # noqa: E402  (binds erpclaw_lib to this tree)
import authority_fixtures as fx  # noqa: E402
from setup_helpers import init_all_tables  # noqa: E402
from erpclaw_lib import authority_gate  # noqa: E402
from erpclaw_lib import authority_readiness  # noqa: E402
from erpclaw_lib import authority_sink  # noqa: E402
from erpclaw_lib import seam  # noqa: E402
from erpclaw_lib.db import get_connection  # noqa: E402
from erpclaw_lib.query import Field, P, Q, Table  # noqa: E402

STAMP = "2026-01-02 03:04:05"

_PG_URL = os.environ.get("ERPCLAW_PG_TEST_URL")

AUTH_COLS = ["id", "install_id", "principal_id", "action",
             "binding_digest", "delegation_id", "issued_at",
             "expires_at", "revoked_at", "consumed_at", "consumed_txn"]
RESULT_COLS = ["authorization_id", "consumed_txn", "result_kind",
               "result_id", "result_status", "recorded_at"]
HEAD_COLS = ["company_id", "last_sequence", "last_checksum", "updated_at"]
COMPANY_COLS = ["id", "name"]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    yield
    seam.dispose_engines()


@pytest.fixture
def seeded(db_path):
    company_id = fx.seed_company(db_path)
    return fx.seed_authority(db_path, company_id)


def _gate():
    return authority_gate


def _issuance():
    from erpclaw_lib import authorization_issuance
    return authorization_issuance


def _setup(monkeypatch, fixed):
    gate = _gate()
    monkeypatch.setitem(
        gate.ENVELOPE_ACTIONS, "add-uom", fx.make_declaration())
    if fixed is not None:
        from erpclaw_lib import authority_clock
        monkeypatch.setattr(
            authority_clock, "now_ms", lambda: fixed)
    return gate


def _issue(target, info, key="key-1", amount="60.00", name="Crate",
           action="add-uom"):
    mod = _issuance()
    conn = get_connection(target)
    try:
        return mod.issue_envelope(
            conn, principal_id=fx.SERVICE,
            delegation_id=fx.DELEGATION, action=action,
            argv=fx.standard_argv(
                info["company_id"], amount=amount, name=name),
            reason_code="ops-need", reason_text="need units",
            idempotency_key=key)
    finally:
        conn.close()


def _pg_reset_schema():
    from urllib.parse import urlparse
    from erpclaw_lib.db import _resolve_pg_url, get_connection as _gc
    test_url = os.environ.get("ERPCLAW_PG_TEST_URL")
    if not test_url:
        raise RuntimeError("pg target unset")
    expected_db = urlparse(test_url).path.strip("/")
    if not expected_db:
        raise RuntimeError("pg target names no database")
    db_url = _resolve_pg_url(None)
    conn = _gc()
    try:
        resolved_db = conn.execute(
            "SELECT current_database()").fetchone()[0]
        if resolved_db != expected_db:
            raise RuntimeError("pg database mismatch")
        test_parts = urlparse(test_url)
        db_parts = urlparse(db_url)
        if (test_parts.hostname != db_parts.hostname
                or test_parts.port != db_parts.port):
            raise RuntimeError("pg host/port mismatch")
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()
    finally:
        conn.close()


def _pg_identity(url):
    from urllib.parse import urlparse
    from erpclaw_lib.db import get_connection as _gc
    conn = _gc()
    try:
        want_dir = os.environ.get("AUTHORITY_PGDATA_CHECK")
        data_dir = None
        if want_dir:
            data_dir = conn.execute(
                "SELECT current_setting('data_directory')").fetchone()[0]
            listen = conn.execute(
                "SELECT current_setting('listen_addresses')").fetchone()[0]
            assert data_dir == want_dir
            assert listen == ""
        server_addr = conn.execute(
            "SELECT inet_server_addr()").fetchone()[0]
        current_db = conn.execute(
            "SELECT current_database()").fetchone()[0]
    finally:
        conn.close()
    assert server_addr is None
    assert current_db == urlparse(url).path.strip("/")
    return data_dir


def _head_insert_sql():
    table = Table("gl_chain_head")
    return Q.into(table).columns(
        "company_id", "last_sequence", "last_checksum",
        "updated_at").insert(P(), P(), P(), P()).get_sql()


def _company_insert_sql():
    table = Table("company")
    return Q.into(table).columns(
        "id", "name", "abbr", "default_currency", "country",
        "fiscal_year_start_month").insert(
        P(), P(), P(), P(), P(), P()).get_sql()


def _company_data(name="Probe Co", abbr="PB"):
    cid = str(uuid.uuid4())
    return {
        "id": cid,
        "name": "%s %s" % (name, cid[:6]),
        "abbr": "%s%s" % (abbr, cid[:4]),
        "default_currency": "USD",
        "country": "United States",
        "fiscal_year_start_month": 1,
    }


def _prepare(db_path, seeded, monkeypatch, key="key-1"):
    gate = _setup(monkeypatch, seeded["now"])
    out = _issue(db_path, seeded, key=key)
    fx.make_active(db_path)
    monkeypatch.setattr(authority_readiness, "is_ready", lambda c: True)
    return gate, out["authorization_id"]


def _consume(conn, auth_id, txn_id, now):
    fx._update_where(
        conn, "operation_authorization",
        {"consumed_at": now, "consumed_txn": txn_id},
        {"id": auth_id})


def _close(conn):
    try:
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()


def test_bound_consumed_context_passes(db_path, seeded, monkeypatch):
    """not qualification: patched readiness below proves nothing."""
    gate, auth_id = _prepare(db_path, seeded, monkeypatch)
    conn = get_connection(db_path)
    try:
        gate.open_transaction(conn)
        txn_id = uuid.uuid4().hex
        _consume(conn, auth_id, txn_id, seeded["now"])
        txn = authority_sink.AuthorityTxn(
            auth_id, "add-uom", txn_id, seeded["install_id"],
            "verified")
        token = authority_sink.bind(conn, txn)
        conn.execute(
            _head_insert_sql(),
            (seeded["company_id"], 0, "GENESIS", STAMP))
        assert authority_sink.current(conn) == txn
        authority_sink.unbind(conn, token)
        assert authority_sink.current(conn) is None
        with pytest.raises(
                authority_sink.LedgerWriteRefused) as excinfo:
            conn.execute(
                _head_insert_sql(),
                (seeded["company_id"], 0, "GENESIS", STAMP))
        assert excinfo.value.args == ("LEDGER_WRITE_UNAUTHORIZED",)
        assert fx.read_rows(
            db_path, "gl_chain_head", HEAD_COLS) == []
        stored = next(
            row for row in fx.read_rows(
                db_path, "operation_authorization", AUTH_COLS)
            if row["id"] == auth_id)
        assert stored["consumed_at"] is None
        assert next(
            (row for row in fx.read_rows(
                db_path, "operation_authorization_result",
                RESULT_COLS)
             if row["authorization_id"] == auth_id), None) is None
    finally:
        _close(conn)


def test_forged_or_stale_context_refuses(
        db_path, seeded, monkeypatch, caplog):
    """not qualification: patched readiness below proves nothing."""
    gate, auth_id = _prepare(db_path, seeded, monkeypatch)
    company_id = seeded["company_id"]
    install_id = seeded["install_id"]
    now = seeded["now"]

    def _attempt(label, bind_txn, consume_txn, check_log=False):
        conn = get_connection(db_path)
        try:
            gate.open_transaction(conn)
            if consume_txn is not None:
                _consume(conn, auth_id, consume_txn, now)
            if label == "result":
                gate.record_result(
                    conn, authorization_id=auth_id,
                    consumed_txn=consume_txn,
                    result_kind="uom",
                    result_id=str(uuid.uuid4()),
                    result_status="created", recorded_at=now)
            token_txn = authority_sink.AuthorityTxn(*bind_txn)
            if check_log:
                base = len(caplog.records)
                with caplog.at_level(
                        logging.WARNING,
                        logger="erpclaw.authority"):
                    with pytest.raises(
                            authority_sink.LedgerWriteRefused) as excinfo:
                        authority_sink.bind(conn, token_txn)
                        conn.execute(
                            _head_insert_sql(),
                            (company_id, 0, "GENESIS", STAMP))
                assert excinfo.value.args == (
                    "LEDGER_WRITE_UNAUTHORIZED",)
                hits = [record for record in caplog.records[base:]
                        if record.name == "erpclaw.authority"
                        and record.levelno == logging.WARNING]
                assert len(hits) == 1
                assert (hits[0].getMessage()
                        == "ledger write refused: code="
                        "LEDGER_WRITE_UNAUTHORIZED table=gl_chain_head"
                        " action=add-uom")
            else:
                authority_sink.bind(conn, token_txn)
                with pytest.raises(
                        authority_sink.LedgerWriteRefused) as excinfo:
                    conn.execute(
                        _head_insert_sql(),
                        (company_id, 0, "GENESIS", STAMP))
                assert excinfo.value.args == (
                    "LEDGER_WRITE_UNAUTHORIZED",)
            assert fx.read_rows(
                db_path, "gl_chain_head", HEAD_COLS) == []
            stored = next(
                row for row in fx.read_rows(
                    db_path, "operation_authorization", AUTH_COLS)
                if row["id"] == auth_id)
            assert stored["consumed_at"] is None
            assert next(
                (row for row in fx.read_rows(
                    db_path, "operation_authorization_result",
                    RESULT_COLS)
                 if row["authorization_id"] == auth_id), None) is None
        finally:
            _close(conn)

    real_txn = uuid.uuid4().hex
    other_txn = uuid.uuid4().hex
    unknown_id = uuid.uuid4().hex
    _attempt("mismatch", (auth_id, "add-uom", other_txn, install_id,
                          "verified"), real_txn, check_log=True)
    _attempt("unconsumed", (auth_id, "add-uom", other_txn, install_id,
                            "verified"), None)
    _attempt("install", (auth_id, "add-uom", real_txn, "other-install",
                         "verified"), real_txn)
    _attempt("action", (auth_id, "add-item", real_txn, install_id,
                        "verified"), real_txn)
    _attempt("unknown", (unknown_id, "add-uom", other_txn, install_id,
                         "verified"), None)
    conn = get_connection(db_path)
    try:
        gate.open_transaction(conn)
        _consume(conn, auth_id, real_txn, now)
        bound = authority_sink.AuthorityTxn(
            auth_id, "add-uom", real_txn, install_id, "verified")
        authority_sink.bind(conn, bound)
        gate.record_result(
            conn, authorization_id=auth_id, consumed_txn=real_txn,
            result_kind="uom", result_id=str(uuid.uuid4()),
            result_status="created", recorded_at=now)
        with pytest.raises(
                authority_sink.LedgerWriteRefused) as excinfo:
            conn.execute(
                _head_insert_sql(),
                (company_id, 0, "GENESIS", STAMP))
        assert excinfo.value.args == ("LEDGER_WRITE_UNAUTHORIZED",)
        assert fx.read_rows(
            db_path, "gl_chain_head", HEAD_COLS) == []
        stored = next(
            row for row in fx.read_rows(
                db_path, "operation_authorization", AUTH_COLS)
            if row["id"] == auth_id)
        assert stored["consumed_at"] is None
        assert next(
            (row for row in fx.read_rows(
                db_path, "operation_authorization_result",
                RESULT_COLS)
             if row["authorization_id"] == auth_id), None) is None
    finally:
        _close(conn)


def test_bind_rules(db_path, seeded, monkeypatch):
    """not qualification: patched readiness below proves nothing."""
    from erpclaw_lib.authorization_consumption import INPUT_INVALID
    gate, auth_id = _prepare(db_path, seeded, monkeypatch)
    txn_id = uuid.uuid4().hex
    txn = authority_sink.AuthorityTxn(
        auth_id, "add-uom", txn_id, seeded["install_id"], "verified")
    idle = get_connection(db_path)
    try:
        with pytest.raises(ValueError) as excinfo:
            authority_sink.bind(idle, txn)
        assert excinfo.value.args == (INPUT_INVALID,)
    finally:
        idle.close()
    conn = get_connection(db_path)
    try:
        gate.open_transaction(conn)
        _consume(conn, auth_id, txn_id, seeded["now"])
        proxy = gate._DeferredHandle(conn)
        with pytest.raises(ValueError) as excinfo:
            authority_sink.bind(proxy, txn)
        assert excinfo.value.args == (INPUT_INVALID,)
        token = authority_sink.bind(conn, txn)
        other = authority_sink.AuthorityTxn(
            auth_id, "add-uom", uuid.uuid4().hex,
            seeded["install_id"], "verified")
        with pytest.raises(gate.AuthorityRefusal) as excinfo:
            authority_sink.bind(conn, other)
        assert excinfo.value.args == ("AUTHORIZATION_REFUSED",)
        assert authority_sink.current(conn) == txn
        with pytest.raises(ValueError) as excinfo:
            authority_sink.unbind(conn, "wrong-token")
        assert excinfo.value.args == (INPUT_INVALID,)
        assert authority_sink.current(conn) == txn
        authority_sink.unbind(conn, token)
        assert authority_sink.current(conn) is None
    finally:
        _close(conn)


def test_swallowed_refusal_cannot_commit(db_path, seeded, monkeypatch):
    """not qualification: patched readiness below proves nothing."""
    _prepare(db_path, seeded, monkeypatch)
    before = sorted(
        row["id"] for row in fx.read_rows(
            db_path, "company", COMPANY_COLS))
    conn = get_connection(db_path)
    try:
        with pytest.raises(
                authority_sink.LedgerWriteRefused) as excinfo:
            conn.execute(
                _head_insert_sql(),
                (seeded["company_id"], 0, "GENESIS", STAMP))
        assert excinfo.value.args == ("LEDGER_WRITE_UNAUTHORIZED",)
        with pytest.raises(
                authority_sink.LedgerWriteRefused) as excinfo:
            conn.commit()
        assert excinfo.value.args == ("LEDGER_WRITE_UNAUTHORIZED",)
        first = _company_data(name="First Co", abbr="FC")
        with pytest.raises(
                authority_sink.LedgerWriteRefused) as excinfo:
            fx._insert_row(conn, "company", first)
        assert excinfo.value.args == ("LEDGER_WRITE_UNAUTHORIZED",)
        second = _company_data(name="Second Co", abbr="SC")
        with pytest.raises(
                authority_sink.LedgerWriteRefused) as excinfo:
            conn.executemany(_company_insert_sql(), [(
                second["id"], second["name"], second["abbr"],
                second["default_currency"], second["country"],
                second["fiscal_year_start_month"])])
        assert excinfo.value.args == ("LEDGER_WRITE_UNAUTHORIZED",)
        conn.rollback()
        third = _company_data(name="Third Co", abbr="TC")
        with pytest.raises(
                authority_sink.LedgerWriteRefused) as excinfo:
            fx._insert_row(conn, "company", third)
        assert excinfo.value.args == ("LEDGER_WRITE_UNAUTHORIZED",)
        with pytest.raises(
                authority_sink.LedgerWriteRefused) as excinfo:
            conn.commit()
        assert excinfo.value.args == ("LEDGER_WRITE_UNAUTHORIZED",)
        live = get_connection(db_path)
        try:
            row = _company_data(name="Live Co", abbr="LC")
            fx._insert_row(live, "company", row)
            live.commit()
        finally:
            live.close()
        assert fx.read_rows(
            db_path, "gl_chain_head", HEAD_COLS) == []
        names = sorted(
            row["id"] for row in fx.read_rows(
                db_path, "company", COMPANY_COLS))
        assert first["id"] not in names
        assert second["id"] not in names
        assert third["id"] not in names
        assert row["id"] in names
        assert len(names) == len(before) + 1
    finally:
        _close(conn)


def test_handler_rollback_then_commit(
        db_path, seeded, monkeypatch, tmp_path):
    """not qualification: patched readiness below proves nothing."""
    _prepare(db_path, seeded, monkeypatch)
    before = sorted(
        row["id"] for row in fx.read_rows(
            db_path, "company", COMPANY_COLS))

    def _run(target, company_id):
        conn = get_connection(target)
        try:
            fx._insert_row(
                conn, "company",
                _company_data(name="Handler One", abbr="H1"))
            try:
                conn.execute(
                    _head_insert_sql(),
                    (company_id, 0, "GENESIS", STAMP))
            except authority_sink.LedgerWriteRefused:
                pass
            conn.rollback()
            fx._insert_row(
                conn, "company",
                _company_data(name="Handler Two", abbr="H2"))
            conn.commit()
        finally:
            conn.close()

    with pytest.raises(
            authority_sink.LedgerWriteRefused) as excinfo:
        _run(db_path, seeded["company_id"])
    assert excinfo.value.args == ("LEDGER_WRITE_UNAUTHORIZED",)
    assert sorted(
        row["id"] for row in fx.read_rows(
            db_path, "company", COMPANY_COLS)) == before
    assert fx.read_rows(db_path, "gl_chain_head", HEAD_COLS) == []
    fresh = str(tmp_path / "staged-malformed.sqlite")
    init_all_tables(fresh)
    staged_company = fx.seed_company(fresh)
    staged_before = sorted(
        row["id"] for row in fx.read_rows(
            fresh, "company", COMPANY_COLS))
    bad = get_connection(fresh)
    try:
        table = Table("authority_install")
        query = Q.update(table).set(
            Field("install_id"), P()).get_sql()
        bad.execute(query, ("",))
        bad.commit()
    finally:
        bad.close()
    with pytest.raises(
            authority_sink.LedgerWriteRefused) as excinfo:
        _run(fresh, staged_company)
    assert excinfo.value.args == ("AUTHORITY_NOT_READY",)
    assert sorted(
        row["id"] for row in fx.read_rows(
            fresh, "company", COMPANY_COLS)) == staged_before
    assert fx.read_rows(fresh, "gl_chain_head", HEAD_COLS) == []


def test_context_manager_exit_refuses(db_path, seeded, monkeypatch):
    """not qualification: patched readiness below proves nothing."""
    _prepare(db_path, seeded, monkeypatch)
    before = sorted(
        row["id"] for row in fx.read_rows(
            db_path, "company", COMPANY_COLS))
    held = _company_data(name="Held Co", abbr="HC")
    conn = get_connection(db_path)
    try:
        with pytest.raises(
                authority_sink.LedgerWriteRefused) as excinfo:
            with conn:
                fx._insert_row(conn, "company", held)
                try:
                    conn.execute(
                        _head_insert_sql(),
                        (seeded["company_id"], 0, "GENESIS", STAMP))
                except authority_sink.LedgerWriteRefused:
                    pass
        assert excinfo.value.args == ("LEDGER_WRITE_UNAUTHORIZED",)
    finally:
        conn.close()
    assert fx.read_rows(db_path, "gl_chain_head", HEAD_COLS) == []
    assert sorted(
        row["id"] for row in fx.read_rows(
            db_path, "company", COMPANY_COLS)) == before
    ghost = _company_data(name="Ghost Co", abbr="GC")
    conn2 = get_connection(db_path)
    try:
        with pytest.raises(KeyError):
            with conn2:
                fx._insert_row(conn2, "company", ghost)
                try:
                    conn2.execute(
                        _head_insert_sql(),
                        (seeded["company_id"], 0, "GENESIS", STAMP))
                except authority_sink.LedgerWriteRefused:
                    pass
                raise KeyError("boom")
    finally:
        conn2.close()
    assert fx.read_rows(db_path, "gl_chain_head", HEAD_COLS) == []
    assert sorted(
        row["id"] for row in fx.read_rows(
            db_path, "company", COMPANY_COLS)) == before


def test_clear_and_close(db_path, seeded, monkeypatch):
    """not qualification: patched readiness below proves nothing."""
    gate, auth_id = _prepare(db_path, seeded, monkeypatch)
    conn = get_connection(db_path)
    try:
        gate.open_transaction(conn)
        txn_id = uuid.uuid4().hex
        _consume(conn, auth_id, txn_id, seeded["now"])
        other = uuid.uuid4().hex
        bound = authority_sink.AuthorityTxn(
            auth_id, "add-uom", other, seeded["install_id"],
            "verified")
        token = authority_sink.bind(conn, bound)
        with pytest.raises(
                authority_sink.LedgerWriteRefused) as excinfo:
            conn.execute(
                _head_insert_sql(),
                (seeded["company_id"], 0, "GENESIS", STAMP))
        assert excinfo.value.args == ("LEDGER_WRITE_UNAUTHORIZED",)
        assert authority_sink.clear(conn, "wrong") is False
        with pytest.raises(
                authority_sink.LedgerWriteRefused) as excinfo:
            conn.execute("SELECT 1").fetchall()
        assert excinfo.value.args == ("LEDGER_WRITE_UNAUTHORIZED",)
        assert authority_sink.clear(conn, token) is True
        assert authority_sink.current(conn) is None
        table = Table("company")
        query = Q.from_(table).select(Field("id")).get_sql()
        conn.execute(query).fetchall()
    finally:
        _close(conn)
    poisoned = get_connection(db_path)
    try:
        with pytest.raises(
                authority_sink.LedgerWriteRefused):
            poisoned.execute(
                _head_insert_sql(),
                (seeded["company_id"], 0, "GENESIS", STAMP))
        poisoned.close()
        poisoned.close()
    except Exception:
        try:
            poisoned.close()
        except Exception:
            pass
        raise
    live = get_connection(db_path)
    try:
        row = _company_data(name="After Close", abbr="AC")
        fx._insert_row(live, "company", row)
        live.commit()
        assert row["id"] in sorted(
            item["id"] for item in fx.read_rows(
                db_path, "company", COMPANY_COLS))
    finally:
        live.close()


def test_probe_and_rollback_statements_pass_while_poisoned(
        db_path, seeded, monkeypatch):
    """not qualification: patched readiness below proves nothing."""
    _prepare(db_path, seeded, monkeypatch)
    before = sorted(
        row["id"] for row in fx.read_rows(
            db_path, "company", COMPANY_COLS))
    conn = get_connection(db_path)
    try:
        fx._insert_row(
            conn, "company",
            _company_data(name="Doomed Co", abbr="DC"))
        with pytest.raises(
                authority_sink.LedgerWriteRefused) as excinfo:
            conn.execute(
                _head_insert_sql(),
                (seeded["company_id"], 0, "GENESIS", STAMP))
        assert excinfo.value.args == ("LEDGER_WRITE_UNAUTHORIZED",)
        conn.execute("ROLLBACK")
        assert sorted(
            row["id"] for row in fx.read_rows(
                db_path, "company", COMPANY_COLS)) == before
        conn.execute("SAVEPOINT ERPCLAW_INSTALL_PROBE")
        conn.execute("ROLLBACK TO SAVEPOINT ERPCLAW_INSTALL_PROBE")
        conn.execute("RELEASE SAVEPOINT ERPCLAW_INSTALL_PROBE")
        with pytest.raises(
                authority_sink.LedgerWriteRefused) as excinfo:
            conn.execute("SELECT 1").fetchall()
        assert excinfo.value.args == ("LEDGER_WRITE_UNAUTHORIZED",)
    finally:
        _close(conn)


def test_second_connection_has_no_context(db_path, seeded, monkeypatch):
    """not qualification: patched readiness below proves nothing."""
    gate, auth_id = _prepare(db_path, seeded, monkeypatch)
    first = get_connection(db_path)
    try:
        gate.open_transaction(first)
        txn_id = uuid.uuid4().hex
        _consume(first, auth_id, txn_id, seeded["now"])
        bound = authority_sink.AuthorityTxn(
            auth_id, "add-uom", txn_id, seeded["install_id"],
            "verified")
        authority_sink.bind(first, bound)
        second = get_connection(db_path)
        try:
            with pytest.raises(
                    authority_sink.LedgerWriteRefused) as excinfo:
                second.execute(
                    _head_insert_sql(),
                    (seeded["company_id"], 0, "GENESIS", STAMP))
            assert excinfo.value.args == ("LEDGER_WRITE_UNAUTHORIZED",)
        finally:
            _close(second)
        first.execute(
            _head_insert_sql(),
            (seeded["company_id"], 0, "GENESIS", STAMP))
        assert fx.read_rows(
            db_path, "gl_chain_head", HEAD_COLS) == []
    finally:
        _close(first)


def test_executemany_and_executescript(
        db_path, seeded, monkeypatch, tmp_path):
    """not qualification: patched readiness below proves nothing."""
    gate, auth_id = _prepare(db_path, seeded, monkeypatch)
    other_company = fx.seed_company(
        db_path, name="Second Co", abbr="SC")
    conn = get_connection(db_path)
    try:
        gate.open_transaction(conn)
        txn_id = uuid.uuid4().hex
        _consume(conn, auth_id, txn_id, seeded["now"])
        bound = authority_sink.AuthorityTxn(
            auth_id, "add-uom", txn_id, seeded["install_id"],
            "verified")
        authority_sink.bind(conn, bound)
        conn.executemany(_head_insert_sql(), [
            (seeded["company_id"], 0, "GENESIS", STAMP),
            (other_company, 0, "GENESIS", STAMP),
        ])
        table = Table("gl_chain_head")
        query = Q.from_(table).select(Field("company_id")).get_sql()
        got = sorted(
            row["company_id"]
            for row in conn.execute(query).fetchall())
        assert got == sorted([seeded["company_id"], other_company])
    finally:
        _close(conn)
    bare = get_connection(db_path)
    try:
        with pytest.raises(
                authority_sink.LedgerWriteRefused) as excinfo:
            bare.executemany(_head_insert_sql(), [
                (seeded["company_id"], 0, "GENESIS", STAMP),
            ])
        assert excinfo.value.args == ("LEDGER_WRITE_UNAUTHORIZED",)
        assert fx.read_rows(
            db_path, "gl_chain_head", HEAD_COLS) == []
    finally:
        _close(bare)

    def _script(target_company):
        return (
            "INSERT INTO gl_chain_head (company_id, last_sequence,"
            " last_checksum, updated_at) VALUES ('"
            + target_company + "', 0, 'GENESIS', '" + STAMP + "')")

    plain = get_connection(db_path)
    try:
        with pytest.raises(
                authority_sink.LedgerWriteRefused) as excinfo:
            plain.executescript(_script(seeded["company_id"]))
        assert excinfo.value.args == ("LEDGER_WRITE_UNAUTHORIZED",)
        assert fx.read_rows(
            db_path, "gl_chain_head", HEAD_COLS) == []
        with pytest.raises(
                authority_sink.LedgerWriteRefused) as excinfo:
            plain.commit()
        assert excinfo.value.args == ("LEDGER_WRITE_UNAUTHORIZED",)
    finally:
        _close(plain)
    bound_conn = get_connection(db_path)
    try:
        gate.open_transaction(bound_conn)
        txn_id = uuid.uuid4().hex
        _consume(bound_conn, auth_id, txn_id, seeded["now"])
        bound = authority_sink.AuthorityTxn(
            auth_id, "add-uom", txn_id, seeded["install_id"],
            "verified")
        authority_sink.bind(bound_conn, bound)
        with pytest.raises(
                authority_sink.LedgerWriteRefused) as excinfo:
            bound_conn.executescript(_script(seeded["company_id"]))
        assert excinfo.value.args == ("LEDGER_WRITE_UNAUTHORIZED",)
        assert fx.read_rows(
            db_path, "gl_chain_head", HEAD_COLS) == []
    finally:
        _close(bound_conn)
    staged = str(tmp_path / "staged-script.sqlite")
    init_all_tables(staged)
    staged_company = fx.seed_company(staged)
    runner = get_connection(staged)
    try:
        runner.executescript(_script(staged_company))
        runner.commit()
    finally:
        runner.close()
    assert sorted(
        row["company_id"] for row in fx.read_rows(
            staged, "gl_chain_head", HEAD_COLS)) == [staged_company]


def test_cursor_is_a_known_hole(db_path, seeded, monkeypatch):
    """not qualification: patched readiness below proves nothing."""
    _prepare(db_path, seeded, monkeypatch)
    conn = get_connection(db_path)
    try:
        sql = _head_insert_sql()
        params = (seeded["company_id"], 0, "GENESIS", STAMP)
        conn.cursor().execute(sql, params)
        table = Table("gl_chain_head")
        query = Q.from_(table).select(Field("company_id")).get_sql()
        got = [row["company_id"]
               for row in conn.execute(query).fetchall()]
        assert got == [seeded["company_id"]]
    finally:
        _close(conn)
    assert fx.read_rows(db_path, "gl_chain_head", HEAD_COLS) == []


@pytest.mark.skipif(not _PG_URL, reason="live Postgres required")
def test_pg_legs(monkeypatch):
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", _PG_URL)
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    seam.dispose_engines()
    try:
        _pg_identity(_PG_URL)
        gate = _gate()

        def _fresh(key="pg-key"):
            _pg_reset_schema()
            init_all_tables(None)
            company_id = fx.seed_company(None)
            info = fx.seed_authority(None, company_id)
            _setup(monkeypatch, info["now"])
            mod = _issuance()
            handle = get_connection(None)
            try:
                out = mod.issue_envelope(
                    handle, principal_id=fx.SERVICE,
                    delegation_id=fx.DELEGATION, action="add-uom",
                    argv=fx.standard_argv(
                        info["company_id"]),
                    reason_code="ops-need",
                    reason_text="need units",
                    idempotency_key=key)
            finally:
                handle.close()
            fx.make_active(None)
            monkeypatch.setattr(
                authority_readiness, "is_ready", lambda c: True)
            return info, out["authorization_id"]

        def _consume_pg(handle, auth_id, txn_id, now):
            fx._update_where(
                handle, "operation_authorization",
                {"consumed_at": now, "consumed_txn": txn_id},
                {"id": auth_id})

        info, auth_id = _fresh("pg-1")
        handle = get_connection(None)
        try:
            assert hasattr(handle, "executescript") is False
            gate.open_transaction(handle)
            txn_id = uuid.uuid4().hex
            _consume_pg(handle, auth_id, txn_id, info["now"])
            bound = authority_sink.AuthorityTxn(
                auth_id, "add-uom", txn_id, info["install_id"],
                "verified")
            authority_sink.bind(handle, bound)
            table = Table("gl_chain_head")
            query = Q.into(table).columns(
                "company_id", "last_sequence", "last_checksum",
                "updated_at").insert(P(), P(), P(), P()).get_sql()
            handle.execute(
                query, (info["company_id"], 0, "GENESIS", STAMP))
        finally:
            try:
                handle.rollback()
            except Exception:
                pass
            handle.close()
        info, auth_id = _fresh("pg-2")
        for label, make_txn in (
                ("install", lambda real: authority_sink.AuthorityTxn(
                    auth_id, "add-uom", real, "other-install",
                    "verified")),
                ("result", None)):
            handle = get_connection(None)
            try:
                gate.open_transaction(handle)
                real = uuid.uuid4().hex
                _consume_pg(handle, auth_id, real, info["now"])
                if label == "result":
                    bound = authority_sink.AuthorityTxn(
                        auth_id, "add-uom", real,
                        info["install_id"], "verified")
                    authority_sink.bind(handle, bound)
                    gate.record_result(
                        handle, authorization_id=auth_id,
                        consumed_txn=real, result_kind="uom",
                        result_id=str(uuid.uuid4()),
                        result_status="created",
                        recorded_at=info["now"])
                else:
                    authority_sink.bind(handle, make_txn(real))
                table = Table("gl_chain_head")
                query = Q.into(table).columns(
                    "company_id", "last_sequence", "last_checksum",
                    "updated_at").insert(P(), P(), P(), P()).get_sql()
                with pytest.raises(
                        authority_sink.LedgerWriteRefused) as excinfo:
                    handle.execute(
                        query,
                        (info["company_id"], 0, "GENESIS", STAMP))
                assert excinfo.value.args == (
                    "LEDGER_WRITE_UNAUTHORIZED",)
            finally:
                try:
                    handle.rollback()
                except Exception:
                    pass
                handle.close()
        info, auth_id = _fresh("pg-4")
        handle = get_connection(None)
        try:
            table = Table("gl_chain_head")
            query = Q.into(table).columns(
                "company_id", "last_sequence", "last_checksum",
                "updated_at").insert(P(), P(), P(), P()).get_sql()
            with pytest.raises(
                    authority_sink.LedgerWriteRefused):
                handle.execute(
                    query, (info["company_id"], 0, "GENESIS", STAMP))
            with pytest.raises(
                    authority_sink.LedgerWriteRefused) as excinfo:
                handle.commit()
            assert excinfo.value.args == (
                "LEDGER_WRITE_UNAUTHORIZED",)
        finally:
            try:
                handle.rollback()
            except Exception:
                pass
            handle.close()
        info, auth_id = _fresh("pg-5")
        handle = get_connection(None)
        try:
            fx._insert_row(
                handle, "company", _company_data(name="Pg One",
                                                 abbr="P1"))
            table = Table("gl_chain_head")
            query = Q.into(table).columns(
                "company_id", "last_sequence", "last_checksum",
                "updated_at").insert(P(), P(), P(), P()).get_sql()
            try:
                handle.execute(
                    query, (info["company_id"], 0, "GENESIS", STAMP))
            except authority_sink.LedgerWriteRefused:
                pass
            handle.rollback()
            with pytest.raises(
                    authority_sink.LedgerWriteRefused):
                fx._insert_row(
                    handle, "company",
                    _company_data(name="Pg Two", abbr="P2"))
        finally:
            try:
                handle.rollback()
            except Exception:
                pass
            handle.close()
        info, auth_id = _fresh("pg-6")
        handle = get_connection(None)
        try:
            with pytest.raises(
                    authority_sink.LedgerWriteRefused):
                with handle:
                    fx._insert_row(
                        handle, "company",
                        _company_data(name="Pg Held", abbr="PH"))
                    try:
                        table = Table("gl_chain_head")
                        query = Q.into(table).columns(
                            "company_id", "last_sequence",
                            "last_checksum",
                            "updated_at").insert(
                            P(), P(), P(), P()).get_sql()
                        handle.execute(
                            query,
                            (info["company_id"], 0, "GENESIS", STAMP))
                    except authority_sink.LedgerWriteRefused:
                        pass
        finally:
            handle.close()
        info, auth_id = _fresh("pg-7")
        handle = get_connection(None)
        try:
            gate.open_transaction(handle)
            real = uuid.uuid4().hex
            _consume_pg(handle, auth_id, real, info["now"])
            bound = authority_sink.AuthorityTxn(
                auth_id, "add-uom", real, info["install_id"],
                "verified")
            token = authority_sink.bind(handle, bound)
            assert authority_sink.clear(handle, "wrong") is False
            assert authority_sink.clear(handle, token) is True
            table = Table("company")
            query = Q.from_(table).select(Field("id")).get_sql()
            handle.execute(query).fetchall()
        finally:
            try:
                handle.rollback()
            except Exception:
                pass
            handle.close()
        info, auth_id = _fresh("pg-9")
        first = get_connection(None)
        try:
            gate.open_transaction(first)
            real = uuid.uuid4().hex
            _consume_pg(first, auth_id, real, info["now"])
            bound = authority_sink.AuthorityTxn(
                auth_id, "add-uom", real, info["install_id"],
                "verified")
            authority_sink.bind(first, bound)
            second = get_connection(None)
            try:
                table = Table("gl_chain_head")
                query = Q.into(table).columns(
                    "company_id", "last_sequence", "last_checksum",
                    "updated_at").insert(P(), P(), P(), P()).get_sql()
                with pytest.raises(
                        authority_sink.LedgerWriteRefused) as excinfo:
                    second.execute(
                        query,
                        (info["company_id"], 0, "GENESIS", STAMP))
                assert excinfo.value.args == (
                    "LEDGER_WRITE_UNAUTHORIZED",)
            finally:
                try:
                    second.rollback()
                except Exception:
                    pass
                second.close()
        finally:
            try:
                first.rollback()
            except Exception:
                pass
            first.close()
    finally:
        seam.dispose_engines()
