"""Company derivation: request arguments to company sets.

Seeds through PyPika inserts on the fixture connection, commits, then
calls derive_companies on a get_connection handle. Company ids, sets and
codes are written literally; no expected value uses product code.
"""
import os
import sys

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import pytest

from setup_helpers import freeze_snapshot, open_reader
from erpclaw_lib import action_impact, company_scope
from erpclaw_lib.db import get_connection
from erpclaw_lib.query import P, Q, Table


def _seed(conn):
    company = Table("company")
    for cid, name, abbr in (("co-a", "Alpha Co", "AC"),
                            ("co-b", "Beta Co", "BC")):
        conn.execute(
            Q.into(company)
            .columns("id", "name", "abbr", "default_currency", "country",
                     "fiscal_year_start_month")
            .insert(P(), P(), P(), P(), P(), P())
            .get_sql(),
            (cid, name, abbr, "USD", "United States", 1),
        )
    customer = Table("customer")
    for cid, name, co in (("cu-a", "Cust A", "co-a"),
                          ("cu-b", "Cust B", "co-b")):
        conn.execute(
            Q.into(customer)
            .columns("id", "name", "company_id")
            .insert(P(), P(), P())
            .get_sql(),
            (cid, name, co),
        )
    fiscal = Table("fiscal_year")
    for fid, name, co in (("fy-a", "FY-A 2026", "co-a"),
                          ("fy-b", "FY-B 2026", "co-b")):
        conn.execute(
            Q.into(fiscal)
            .columns("id", "name", "start_date", "end_date", "company_id")
            .insert(P(), P(), P(), P(), P())
            .get_sql(),
            (fid, name, "2026-01-01", "2026-12-31", co),
        )
    conn.commit()


def _read_decl(source, target=None):
    decl = {"domain": "billing", "class": "read", "enveloped": False,
            "money": (), "json_args": (), "money_json_paths": {},
            "bound_args": (), "company_source": source,
            "target_projection": None, "result_projection": None}
    if target is not None:
        decl["target_arg"] = target
    return decl


class _Spy:
    def __init__(self, conn):
        self._conn = conn
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if params is None:
            return self._conn.execute(sql)
        return self._conn.execute(sql, params)


def _refused_code(action, argv, db_path):
    handle = get_connection(db_path)
    try:
        with pytest.raises(company_scope.DerivationRefused) as info:
            company_scope.derive_companies(handle, action, argv)
        return info.value
    finally:
        handle.close()


def test_constants():
    assert company_scope.DERIVE_UNDERIVABLE == "COMPANY_UNDERIVABLE"
    assert company_scope.DERIVE_MISMATCH == "COMPANY_MISMATCH"
    assert company_scope.DERIVE_CODES == (
        "COMPANY_UNDERIVABLE", "COMPANY_MISMATCH")
    assert not issubclass(company_scope.DerivationRefused, ValueError)
    exc = company_scope.DerivationRefused("COMPANY_UNDERIVABLE")
    assert exc.code == "COMPANY_UNDERIVABLE"
    assert exc.args == ("COMPANY_UNDERIVABLE",)
    assert str(exc) == "COMPANY_UNDERIVABLE"


def test_arg_by_id(conn, db_path):
    _seed(conn)
    handle = get_connection(db_path)
    try:
        assert company_scope.derive_companies(
            handle, "add-account", ["--company-id", "co-a"]) == frozenset({"co-a"})
        assert company_scope.derive_companies(
            handle, "add-account", ["--company-id=co-b"]) == frozenset({"co-b"})
        assert company_scope.derive_companies(
            handle, "add-account",
            ["--company-id", "co-a", "--company-id", "co-b"]) == frozenset({"co-b"})
        assert company_scope.derive_companies(
            handle, "add-account", ["--company-id", "co-zz"]) == frozenset({"co-zz"})
        spy = _Spy(handle)
        assert company_scope.derive_companies(
            spy, "add-account", ["--company-id", "co-a"]) == frozenset({"co-a"})
        assert company_scope.derive_companies(
            spy, "add-account", ["--company-id=co-b"]) == frozenset({"co-b"})
        assert spy.calls == []
    finally:
        handle.close()


def test_arg_by_name(conn, db_path):
    _seed(conn)
    handle = get_connection(db_path)
    try:
        assert company_scope.derive_companies(
            handle, "add-account", ["--company", "  alpha co "]) == frozenset({"co-a"})
        assert company_scope.derive_companies(
            handle, "add-account",
            ["--company-id", "co-b", "--company", "Alpha Co"]) == frozenset({"co-b"})
    finally:
        handle.close()
    handle = get_connection(db_path)
    try:
        with pytest.raises(company_scope.DerivationRefused) as info:
            company_scope.derive_companies(
                handle, "add-account", ["--company", "Nobody Co"])
        assert info.value.code == "COMPANY_UNDERIVABLE"
    finally:
        handle.close()


def test_arg_absent_or_ambiguous(conn, db_path, monkeypatch):
    _seed(conn)
    for argv in ([], ["--company-id"], ["--company-id", "--limit"],
                 ["--company-id", ""], ["--company-i", "co-a"]):
        exc = _refused_code("add-account", argv, db_path)
        assert exc.code == "COMPANY_UNDERIVABLE"
        assert exc.args == ("COMPANY_UNDERIVABLE",)
    decl = _read_decl("arg:source-company-id")
    assert action_impact.validate({"synth-source-co": decl}) == []
    monkeypatch.setitem(action_impact.IMPACT, "synth-source-co", decl)
    handle = get_connection(db_path)
    try:
        assert company_scope.derive_companies(
            handle, "synth-source-co",
            ["--source-company-id", "co-b"]) == frozenset({"co-b"})
    finally:
        handle.close()


def test_row_read_from_table(conn, db_path):
    _seed(conn)
    handle = get_connection(db_path)
    try:
        assert company_scope.derive_companies(
            handle, "get-prepaid-balance",
            ["--customer-id", "cu-a"]) == frozenset({"co-a"})
        assert company_scope.derive_companies(
            handle, "get-prepaid-balance",
            ["--customer-id", "cu-a", "--company-id", "co-a"]) == frozenset({"co-a"})
        assert company_scope.derive_companies(
            handle, "reopen-fiscal-year",
            ["--fiscal-year-id", "fy-b"]) == frozenset({"co-b"})
    finally:
        handle.close()


def test_mismatch_refuses(conn, db_path):
    _seed(conn)
    for argv in (["--customer-id", "cu-a", "--company-id", "co-b"],
                 ["--customer-id", "cu-a", "--company", "Beta Co"],
                 ["--customer-id", "cu-a", "--company", "Nobody Co"]):
        exc = _refused_code("get-prepaid-balance", argv, db_path)
        assert exc.code == "COMPANY_MISMATCH"
        assert exc.args == ("COMPANY_MISMATCH",)


def test_missing_target_equals_forbidden(conn, db_path):
    _seed(conn)
    exc = _refused_code(
        "get-prepaid-balance", ["--customer-id", "cu-zz"], db_path)
    assert exc.code == "COMPANY_UNDERIVABLE"
    assert exc.args == ("COMPANY_UNDERIVABLE",)
    assert "cu-zz" not in str(exc)
    absent = _refused_code("get-prepaid-balance", [], db_path)
    assert absent.args == exc.args
    beaten = _refused_code(
        "get-prepaid-balance",
        ["--customer-id", "cu-zz", "--company-id", "co-a"], db_path)
    assert beaten.args == exc.args
    handle = get_connection(db_path)
    try:
        assert company_scope.derive_companies(
            handle, "get-prepaid-balance",
            ["--customer-id", "cu-b"]) == frozenset({"co-b"})
    finally:
        handle.close()


def test_rows_union(conn, db_path, monkeypatch):
    _seed(conn)
    two = _read_decl("rows:customer,fiscal_year",
                     ("customer-id", "fiscal-year-id"))
    assert action_impact.validate({"synth-rows-two": two}) == []
    monkeypatch.setitem(action_impact.IMPACT, "synth-rows-two", two)
    listed = _read_decl("rows:customer", ("customer-ids",))
    assert action_impact.validate({"synth-rows-list": listed}) == []
    monkeypatch.setitem(action_impact.IMPACT, "synth-rows-list", listed)
    handle = get_connection(db_path)
    try:
        assert company_scope.derive_companies(
            handle, "synth-rows-two",
            ["--customer-id", "cu-a", "--fiscal-year-id", "fy-b"]) == frozenset({"co-a", "co-b"})
        assert company_scope.derive_companies(
            handle, "synth-rows-list",
            ["--customer-ids", "[\"cu-a\", \"cu-b\"]"]) == frozenset({"co-a", "co-b"})
        assert company_scope.derive_companies(
            handle, "synth-rows-list",
            ["--customer-ids", "[\"cu-a\"]"]) == frozenset({"co-a"})
        assert company_scope.derive_companies(
            handle, "synth-rows-list",
            ["--customer-ids", "[\"cu-a\", \"cu-b\"]",
             "--company-id", "co-b"]) == frozenset({"co-a", "co-b"})
    finally:
        handle.close()
    for argv in (["--customer-id", "cu-a"],
                 ["--customer-ids", "[]"],
                 ["--customer-ids", "[\"cu-a\", \"cu-zz\"]"],
                 ["--customer-ids", "[1]"],
                 ["--customer-ids", "[not json"]):
        action = ("synth-rows-two" if argv == ["--customer-id", "cu-a"]
                  else "synth-rows-list")
        exc = _refused_code(action, argv, db_path)
        assert exc.code == "COMPANY_UNDERIVABLE"
    exc = _refused_code(
        "synth-rows-list",
        ["--customer-ids", "[\"cu-a\", \"cu-b\"]", "--company-id", "co-c"],
        db_path)
    assert exc.code == "COMPANY_MISMATCH"


def test_none_and_unknown(conn, db_path, monkeypatch):
    _seed(conn)
    handle = get_connection(db_path)
    try:
        spy = _Spy(handle)
        assert company_scope.derive_companies(
            spy, "add-account-type", ["--company-id", "co-a"]) == frozenset()
        assert spy.calls == []
        for action in ("list-meters", "generate-invoices"):
            with pytest.raises(company_scope.DerivationRefused) as info:
                company_scope.derive_companies(spy, action, [])
            assert info.value.code == "COMPANY_UNDERIVABLE"
        assert spy.calls == []
    finally:
        handle.close()
    exc = _refused_code("no-such-action", [], db_path)
    assert exc.code == "COMPANY_UNDERIVABLE"
    malformed = _read_decl("row:customer")
    problems = action_impact.validate({"synth-malformed": malformed})
    assert problems and any("synth-malformed" in problem for problem in problems)
    monkeypatch.setitem(action_impact.IMPACT, "synth-malformed", malformed)
    exc = _refused_code(
        "synth-malformed", ["--customer-id", "cu-a"], db_path)
    assert exc.code == "COMPANY_UNDERIVABLE"


def test_table_without_column_or_not_installed(conn, db_path):
    _seed(conn)
    for action, argv in (
            ("get-meter", ["--meter-id", "m-1"]),
            ("get-billing-period", ["--billing-period-id", "bp-1"]),
            ("legal-write-off-invoice", ["--invoice-id", "li-1"])):
        exc = _refused_code(action, argv, db_path)
        assert exc.code == "COMPANY_UNDERIVABLE"
        assert exc.__cause__ is not None


def test_no_writes_transaction_untouched(conn, db_path):
    _seed(conn)
    reader = open_reader(db_path)
    try:
        first = freeze_snapshot(
            reader, db_path,
            ("company", "customer", "fiscal_year", "audit_log",
             "authority_install", "authority_membership"))
    finally:
        reader.close()
    handle = get_connection(db_path)
    try:
        customer = Table("customer")
        handle.execute(
            Q.into(customer)
            .columns("id", "name", "company_id")
            .insert(P(), P(), P())
            .get_sql(),
            ("cu-new", "Cust New", "co-a"),
        )
        assert company_scope.derive_companies(
            handle, "get-prepaid-balance",
            ["--customer-id", "cu-new"]) == frozenset({"co-a"})
        with pytest.raises(company_scope.DerivationRefused) as info:
            company_scope.derive_companies(
                handle, "get-prepaid-balance",
                ["--customer-id", "cu-a", "--company-id", "co-b"])
        assert info.value.code == "COMPANY_MISMATCH"
        rows = handle.execute(
            Q.from_(customer)
            .select(customer.id)
            .where(customer.id == P())
            .get_sql(),
            ("cu-new",),
        ).fetchall()
        assert len(rows) == 1
        handle.rollback()
    finally:
        handle.close()
    reader = open_reader(db_path)
    try:
        assert freeze_snapshot(
            reader, db_path,
            ("company", "customer", "fiscal_year", "audit_log",
             "authority_install", "authority_membership")) == first
    finally:
        reader.close()
    handle = get_connection(db_path)
    try:
        customer = Table("customer")
        handle.execute(
            Q.into(customer)
            .columns("id", "name", "company_id")
            .insert(P(), P(), P())
            .get_sql(),
            ("cu-err", "Cust Err", "co-a"),
        )
        with pytest.raises(company_scope.DerivationRefused) as info:
            company_scope.derive_companies(
                handle, "get-meter", ["--meter-id", "m-1"])
        assert info.value.code == "COMPANY_UNDERIVABLE"
        rows = handle.execute(
            Q.from_(customer)
            .select(customer.id)
            .where(customer.id == P())
            .get_sql(),
            ("cu-err",),
        ).fetchall()
        assert len(rows) == 1
        handle.rollback()
    finally:
        handle.close()
    reader = open_reader(db_path)
    try:
        customer = Table("customer")
        rows = reader.execute(
            Q.from_(customer)
            .select(customer.id)
            .where(customer.id == P())
            .get_sql(),
            ("cu-err",),
        ).fetchall()
        assert rows == []
    finally:
        reader.close()


def test_bad_inputs_raise_before_any_read(conn, db_path):
    _seed(conn)
    handle = get_connection(db_path)
    try:
        spy = _Spy(handle)
        for action in (None, "", 5):
            with pytest.raises(ValueError):
                company_scope.derive_companies(spy, action, ["--company-id", "co-a"])
        for argv in ("--company-id co-a", None, ["--company-id", 5]):
            with pytest.raises(ValueError):
                company_scope.derive_companies(spy, "add-account", argv)
        assert spy.calls == []
    finally:
        handle.close()


def test_row_read_binds_target_as_parameter(conn, db_path):
    _seed(conn)
    handle = get_connection(db_path)
    try:
        spy = _Spy(handle)
        assert company_scope.derive_companies(
            spy, "get-prepaid-balance", ["--customer-id", "cu-a"]) == frozenset({"co-a"})
        assert len(spy.calls) == 3
        _opened, read, _released = spy.calls
        sql, params = read
        assert "company_id" in sql
        assert "customer" in sql
        assert tuple(params) == ("cu-a",)
    finally:
        handle.close()
