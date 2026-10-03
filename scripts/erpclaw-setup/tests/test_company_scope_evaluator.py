"""Company scope evaluator: allow/deny scope reads and request verdicts.

Seeds through PyPika inserts on the fixture connection, commits, then
calls the evaluator on a get_connection handle. Membership rows need no
company rows.
"""
import os
import sys

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import pytest

from setup_helpers import (
    freeze_snapshot,
    init_all_tables,
    open_reader,
)
from erpclaw_lib import actor, seam, company_scope
from erpclaw_lib.db import get_connection
from erpclaw_lib.query import Field, P, Q, Table

P_ONE = "p-one"
P_TWO = "p-two"
CO_A = "co-a"
CO_B = "co-b"
CO_C = "co-c"

_CORE_AND_LOG = tuple(seam._AUTHORITY_CORE_TABLES) + ("audit_log",)


def _ctx(claim, status):
    return actor.ActorContext(
        os_account=None,
        channel="cli",
        principal_claim=claim,
        hop=(),
        status=status,
    )


def _own_install_id(conn, db_path):
    handle = get_connection(db_path)
    try:
        found = company_scope.install_id(handle)
    finally:
        handle.close()
    if found is None:
        seam.provision_authority_core(db_path)
        handle = get_connection(db_path)
        try:
            found = company_scope.install_id(handle)
        finally:
            handle.close()
    assert found is not None
    return found


def _seed_principal(conn, install, pid, kind="human", disabled_at=None):
    table = Table("authority_principal")
    conn.execute(
        Q.into(table)
        .columns("install_id", "id", "kind", "disabled_at")
        .insert(P(), P(), P(), P())
        .get_sql(),
        (install, pid, kind, disabled_at),
    )


def _seed_membership(conn, install, pid, cid, effect):
    table = Table("authority_membership")
    conn.execute(
        Q.into(table)
        .columns("install_id", "principal_id", "company_id", "effect")
        .insert(P(), P(), P(), P())
        .get_sql(),
        (install, pid, cid, effect),
    )


def _read_membership(conn):
    table = Table("authority_membership")
    return conn.execute(
        Q.from_(table)
        .select(
            table.install_id,
            table.principal_id,
            table.company_id,
            table.effect,
        )
        .get_sql()
    ).fetchall()


def _handle(db_path):
    return get_connection(db_path)


def test_constants():
    assert company_scope.IN_SCOPE == "in_scope"
    assert company_scope.OUT_OF_SCOPE == "out_of_scope"
    assert company_scope.NO_SCOPE == "no_scope"
    assert company_scope.NO_PRINCIPAL == "no_principal"
    assert company_scope.UNDERIVED == "underived"
    assert company_scope.NOT_APPLICABLE == "not_applicable"
    assert company_scope.STATUSES == (
        "in_scope",
        "out_of_scope",
        "no_scope",
        "no_principal",
        "underived",
        "not_applicable",
    )
    assert company_scope.REFUSAL_CODE == "COMPANY_SCOPE_REFUSED"


def test_core_present_and_install_id(conn, db_path):
    install = _own_install_id(conn, db_path)
    handle = _handle(db_path)
    try:
        assert company_scope.core_present(handle) is True
        assert company_scope.install_id(handle) == install
    finally:
        handle.close()


def test_allow_only(conn, db_path):
    install = _own_install_id(conn, db_path)
    _seed_principal(conn, install, P_ONE)
    _seed_membership(conn, install, P_ONE, CO_A, "allow")
    _seed_membership(conn, install, P_ONE, CO_B, "allow")
    conn.commit()
    handle = _handle(db_path)
    try:
        assert company_scope.principal_scope(handle, install, P_ONE) == frozenset({CO_A, CO_B})
    finally:
        handle.close()


def test_deny_beats_allow(conn, db_path):
    install = _own_install_id(conn, db_path)
    _seed_principal(conn, install, P_ONE)
    _seed_membership(conn, install, P_ONE, CO_A, "allow")
    _seed_membership(conn, install, P_ONE, CO_B, "allow")
    _seed_membership(conn, install, P_ONE, CO_B, "deny")
    conn.commit()
    handle = _handle(db_path)
    try:
        assert company_scope.principal_scope(handle, install, P_ONE) == frozenset({CO_A})
        assert len(_read_membership(handle)) == 3
    finally:
        handle.close()


def test_deny_without_allow(conn, db_path):
    install = _own_install_id(conn, db_path)
    _seed_principal(conn, install, P_ONE)
    _seed_membership(conn, install, P_ONE, CO_C, "deny")
    conn.commit()
    handle = _handle(db_path)
    try:
        assert company_scope.principal_scope(handle, install, P_ONE) == frozenset()
    finally:
        handle.close()


def test_no_membership_rows(conn, db_path):
    install = _own_install_id(conn, db_path)
    _seed_principal(conn, install, P_ONE)
    conn.commit()
    handle = _handle(db_path)
    try:
        assert company_scope.principal_scope(handle, install, P_ONE) == frozenset()
        verdict = company_scope.check(
            handle, _ctx(P_ONE, actor.ATTESTED), [CO_A], "STAGED"
        )
        assert verdict.status == company_scope.NO_SCOPE
        assert verdict.refuse is False
        assert verdict.code is None
    finally:
        handle.close()


def test_disabled_principal(conn, db_path):
    install = _own_install_id(conn, db_path)
    _seed_principal(conn, install, P_ONE, disabled_at=1)
    _seed_membership(conn, install, P_ONE, CO_A, "allow")
    _seed_membership(conn, install, P_ONE, CO_B, "allow")
    conn.commit()
    handle = _handle(db_path)
    try:
        assert company_scope.principal_scope(handle, install, P_ONE) == frozenset()
        verdict = company_scope.check(
            handle, _ctx(P_ONE, actor.ATTESTED), [CO_A], "STAGED"
        )
        assert verdict.status == company_scope.NO_SCOPE
        assert verdict.refuse is False
        assert verdict.code is None
    finally:
        handle.close()


def test_empty_ids_give_empty_scope(conn, db_path):
    install = _own_install_id(conn, db_path)
    handle = _handle(db_path)
    try:
        assert company_scope.principal_scope(handle, None, P_ONE) == frozenset()
        assert company_scope.principal_scope(handle, "", P_ONE) == frozenset()
        assert company_scope.principal_scope(handle, install, None) == frozenset()
        assert company_scope.principal_scope(handle, install, "") == frozenset()
    finally:
        handle.close()


class _Spy:
    def __init__(self, conn):
        self._conn = conn
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if params is None:
            return self._conn.execute(sql)
        return self._conn.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_install_id_bound_on_every_read(conn, db_path):
    install = _own_install_id(conn, db_path)
    _seed_principal(conn, install, P_ONE)
    _seed_membership(conn, install, P_ONE, CO_A, "allow")
    _seed_membership(conn, install, P_ONE, CO_B, "allow")
    conn.commit()
    handle = _handle(db_path)
    try:
        spy = _Spy(handle)
        scope = company_scope.principal_scope(spy, install, P_ONE)
        assert scope == frozenset({CO_A, CO_B})
        param_reads = [
            (sql, params)
            for sql, params in spy.calls
            if params is not None
            and (
                "authority_principal" in sql or "authority_membership" in sql
            )
        ]
        assert any("authority_principal" in sql for sql, _ in param_reads)
        assert any("authority_membership" in sql for sql, _ in param_reads)
        for sql, params in param_reads:
            assert "WHERE" in sql and "install_id" in sql
            assert install in list(params)
    finally:
        handle.close()
    other = _handle(db_path)
    try:
        assert company_scope.principal_scope(other, "other-install", P_ONE) == frozenset()
    finally:
        other.close()


def test_status_table_at_staged(conn, db_path):
    install = _own_install_id(conn, db_path)
    _seed_principal(conn, install, P_ONE)
    _seed_membership(conn, install, P_ONE, CO_A, "allow")
    _seed_membership(conn, install, P_ONE, CO_B, "allow")
    _seed_principal(conn, install, P_TWO)
    conn.commit()
    cases = [
        (actor.ABSENT, None, [CO_A], company_scope.NO_PRINCIPAL, None),
        (actor.INVALID, P_ONE, [CO_A], company_scope.NO_PRINCIPAL, None),
        (actor.ATTESTED, P_ONE, None, company_scope.NOT_APPLICABLE, "attested"),
        (actor.ATTESTED, P_ONE, [], company_scope.UNDERIVED, "attested"),
        (actor.ATTESTED, P_TWO, [CO_A], company_scope.NO_SCOPE, "attested"),
        (actor.ATTESTED, P_ONE, [CO_A], company_scope.IN_SCOPE, "attested"),
        (
            actor.ATTESTED,
            P_ONE,
            [CO_A, CO_C],
            company_scope.OUT_OF_SCOPE,
            "attested",
        ),
        (actor.ATTESTED, "p-nope", None, company_scope.NO_SCOPE, "attested"),
    ]
    handle = _handle(db_path)
    try:
        for status, claim, wanted, expected, basis in cases:
            verdict = company_scope.check(
                handle, _ctx(claim, status), wanted, "STAGED"
            )
            assert verdict.status == expected
            assert verdict.refuse is False
            assert verdict.code is None
            assert verdict.phase == "STAGED"
            assert verdict.basis == basis
            assert verdict.principal_id == (claim if basis else None)
            if wanted is None:
                assert verdict.company_ids is None
            else:
                assert verdict.company_ids == tuple(sorted(set(wanted)))
    finally:
        handle.close()


def test_absent_context_is_no_principal(conn, db_path):
    install = _own_install_id(conn, db_path)
    _seed_principal(conn, install, P_ONE)
    _seed_membership(conn, install, P_ONE, CO_A, "allow")
    conn.commit()
    handle = _handle(db_path)
    try:
        verdict = company_scope.check(handle, None, [CO_A], "STAGED")
        assert verdict.status == company_scope.NO_PRINCIPAL
        assert verdict.refuse is False
        assert verdict.code is None
        assert verdict.basis is None
        assert verdict.principal_id is None
    finally:
        handle.close()


def test_active_phase(conn, db_path):
    install = _own_install_id(conn, db_path)
    _seed_principal(conn, install, P_ONE)
    _seed_membership(conn, install, P_ONE, CO_A, "allow")
    _seed_membership(conn, install, P_ONE, CO_B, "allow")
    _seed_principal(conn, install, P_TWO, disabled_at=7)
    _seed_membership(conn, install, P_TWO, CO_A, "allow")
    conn.commit()
    handle = _handle(db_path)
    try:
        verdict = company_scope.check(
            handle, _ctx(P_ONE, actor.ATTESTED), [CO_A], "ACTIVE"
        )
        assert verdict.status == company_scope.IN_SCOPE
        assert verdict.refuse is False
        assert verdict.code is None
        assert verdict.basis == "attested"

        verdict = company_scope.check(
            handle, _ctx(P_ONE, actor.CLAIMED), [CO_A], "ACTIVE"
        )
        assert verdict.status == company_scope.IN_SCOPE
        assert verdict.refuse is True
        assert verdict.code == company_scope.REFUSAL_CODE
        assert verdict.basis == "claim"

        verdict = company_scope.check(
            handle, _ctx(P_ONE, actor.ATTESTED), [CO_C], "ACTIVE"
        )
        assert verdict.status == company_scope.OUT_OF_SCOPE
        assert verdict.refuse is True
        assert verdict.code == company_scope.REFUSAL_CODE

        verdict = company_scope.check(
            handle, _ctx(P_TWO, actor.ATTESTED), [CO_A], "ACTIVE"
        )
        assert verdict.status == company_scope.NO_SCOPE
        assert verdict.refuse is True

        verdict = company_scope.check(
            handle, _ctx(P_ONE, actor.ATTESTED), [], "ACTIVE"
        )
        assert verdict.status == company_scope.UNDERIVED
        assert verdict.refuse is True

        verdict = company_scope.check(handle, None, [CO_A], "ACTIVE")
        assert verdict.status == company_scope.NO_PRINCIPAL
        assert verdict.refuse is True
        assert verdict.code == company_scope.REFUSAL_CODE

        verdict = company_scope.check(
            handle, _ctx(P_ONE, actor.ATTESTED), None, "ACTIVE"
        )
        assert verdict.status == company_scope.NOT_APPLICABLE
        assert verdict.refuse is False
        assert verdict.code is None

        verdict = company_scope.check(
            handle, _ctx(P_ONE, actor.CLAIMED), None, "ACTIVE"
        )
        assert verdict.status == company_scope.NOT_APPLICABLE
        assert verdict.refuse is True
        assert verdict.code == company_scope.REFUSAL_CODE

        verdict = company_scope.check(
            handle, _ctx(P_TWO, actor.ATTESTED), None, "ACTIVE"
        )
        assert verdict.status == company_scope.NO_SCOPE
        assert verdict.refuse is True
        assert verdict.code == company_scope.REFUSAL_CODE
    finally:
        handle.close()


def test_absent_core(tmp_path, conn, db_path):
    bare = str(tmp_path / "nocore.sqlite")
    handle = _handle(bare)
    try:
        assert company_scope.core_present(handle) is False
        assert company_scope.install_id(handle) is None
        assert company_scope.principal_scope(handle, "x", P_ONE) == frozenset()
        verdict = company_scope.check(handle, None, [CO_A], None)
        assert verdict.phase == "STAGED"
        assert verdict.refuse is False
        assert verdict.code is None
        verdict = company_scope.check(
            handle, _ctx(P_ONE, actor.ATTESTED), [CO_A], "ACTIVE"
        )
        assert verdict.phase == "ACTIVE"
        assert verdict.status == company_scope.NO_SCOPE
        assert verdict.refuse is True
        assert verdict.code == company_scope.REFUSAL_CODE
    finally:
        handle.close()
    present = _handle(db_path)
    try:
        with pytest.raises(ValueError):
            company_scope.check(
                present, _ctx(P_ONE, actor.ATTESTED), [CO_A], None
            )
    finally:
        present.close()


def test_bad_phase(conn, db_path):
    _own_install_id(conn, db_path)
    handle = _handle(db_path)
    try:
        spy = _Spy(handle)
        with pytest.raises(ValueError):
            company_scope.check(
                spy, _ctx(P_ONE, actor.ATTESTED), [CO_A], "LIVE"
            )
        assert spy.calls == []
    finally:
        handle.close()


def test_bad_inputs_raise_before_any_read(conn, db_path):
    _own_install_id(conn, db_path)
    handle = _handle(db_path)
    try:
        spy = _Spy(handle)
        with pytest.raises(ValueError):
            company_scope.check(spy, "p-one", [CO_A], "STAGED")
        with pytest.raises(ValueError):
            company_scope.check(
                spy, _ctx(P_ONE, actor.ATTESTED), CO_A, "STAGED"
            )
        with pytest.raises(ValueError):
            company_scope.check(
                spy, _ctx(P_ONE, actor.ATTESTED), ["", "ok"], "STAGED"
            )
        with pytest.raises(ValueError):
            company_scope.check(
                spy, _ctx(P_ONE, actor.ATTESTED), [123], "STAGED"
            )
        assert spy.calls == []
    finally:
        handle.close()


def test_no_writes_transaction_untouched(conn, db_path):
    install = _own_install_id(conn, db_path)
    _seed_principal(conn, install, P_ONE)
    _seed_membership(conn, install, P_ONE, CO_A, "allow")
    conn.commit()
    reader = open_reader(db_path)
    try:
        first = freeze_snapshot(reader, db_path, _CORE_AND_LOG)
    finally:
        reader.close()

    handle = _handle(db_path)
    try:
        _seed_membership(handle, install, P_ONE, "co-zz", "allow")
        company_scope.check(
            handle, _ctx(P_ONE, actor.ATTESTED), [CO_A], "STAGED"
        )
        company_scope.principal_scope(handle, install, P_ONE)
        membership = Table("authority_membership")
        rows = handle.execute(
            Q.from_(membership)
            .select(membership.company_id)
            .where(membership.company_id == P())
            .get_sql(),
            ("co-zz",),
        ).fetchall()
        assert len(rows) == 1
        handle.rollback()
    finally:
        handle.close()
    reader = open_reader(db_path)
    try:
        assert freeze_snapshot(reader, db_path, _CORE_AND_LOG) == first
    finally:
        reader.close()

    _seed_membership(conn, install, P_ONE, "co-zz", "allow")
    company_scope.check(conn, _ctx(P_ONE, actor.ATTESTED), [CO_A], "STAGED")
    company_scope.principal_scope(conn, install, P_ONE)
    membership = Table("authority_membership")
    rows = conn.execute(
        Q.from_(membership)
        .select(membership.company_id)
        .where(membership.company_id == P())
        .get_sql(),
        ("co-zz",),
    ).fetchall()
    assert len(rows) == 1
    conn.rollback()
    reader = open_reader(db_path)
    try:
        assert freeze_snapshot(reader, db_path, _CORE_AND_LOG) == first
    finally:
        reader.close()


def test_error_path_leaves_transaction(tmp_path):
    other = str(tmp_path / "second.sqlite")
    init_all_tables(other)
    handle = _handle(other)
    try:
        handle.execute("DROP TABLE authority_membership")
        handle.commit()
        assert company_scope.core_present(handle) is False
        unit = Table("uom")
        handle.execute(
            Q.into(unit)
            .columns("id", "name")
            .insert(P(), P())
            .get_sql(),
            ("u-scope-zz", "Scope Each"),
        )
        verdict = company_scope.check(
            handle, _ctx(P_ONE, actor.ATTESTED), [CO_A], "STAGED"
        )
        assert verdict.status == company_scope.NO_SCOPE
        assert verdict.refuse is False
        rows = handle.execute(
            Q.from_(unit)
            .select(unit.id)
            .where(unit.id == P())
            .get_sql(),
            ("u-scope-zz",),
        ).fetchall()
        assert len(rows) == 1
        handle.rollback()
    finally:
        handle.close()
    reader = open_reader(other)
    try:
        unit = Table("uom")
        rows = reader.execute(
            Q.from_(unit)
            .select(unit.id)
            .where(unit.id == P())
            .get_sql(),
            ("u-scope-zz",),
        ).fetchall()
        assert rows == []
    finally:
        reader.close()


def test_company_ids_sorted_and_deduplicated(conn, db_path):
    install = _own_install_id(conn, db_path)
    _seed_principal(conn, install, P_ONE)
    _seed_membership(conn, install, P_ONE, CO_A, "allow")
    _seed_membership(conn, install, P_ONE, CO_B, "allow")
    conn.commit()
    handle = _handle(db_path)
    try:
        verdict = company_scope.check(
            handle,
            _ctx(P_ONE, actor.ATTESTED),
            [CO_B, CO_A, CO_A],
            "STAGED",
        )
        assert verdict.company_ids == (CO_A, CO_B)
        assert verdict.status == company_scope.IN_SCOPE
    finally:
        handle.close()


def test_core_present_no_install_row(conn, db_path):
    _own_install_id(conn, db_path)
    install = Table("authority_install")
    conn.execute(Q.from_(install).delete().get_sql())
    conn.commit()
    handle = _handle(db_path)
    try:
        assert company_scope.core_present(handle) is True
        assert company_scope.install_id(handle) is None
        verdict = company_scope.check(
            handle, _ctx(P_ONE, actor.ATTESTED), [CO_A], "STAGED"
        )
        assert verdict.status == "no_scope"
        assert verdict.refuse is False
        assert verdict.code is None
        assert verdict.phase == "STAGED"
        verdict = company_scope.check(
            handle, _ctx(P_ONE, actor.ATTESTED), [CO_A], "ACTIVE"
        )
        assert verdict.status == "no_scope"
        assert verdict.refuse is True
        assert verdict.code == "COMPANY_SCOPE_REFUSED"
        assert verdict.phase == "ACTIVE"
    finally:
        handle.close()


def test_install_id_does_not_mask_read_failure(conn, db_path):
    _own_install_id(conn, db_path)
    handle = _handle(db_path)
    try:
        class _Boom:
            def __init__(self, inner):
                self._inner = inner

            def execute(self, sql, params=None):
                if "authority_install" in sql and "LIMIT 0" not in sql:
                    raise RuntimeError("boom")
                if params is None:
                    return self._inner.execute(sql)
                return self._inner.execute(sql, params)

        with pytest.raises(RuntimeError):
            company_scope.install_id(_Boom(handle))
    finally:
        handle.close()


def test_check_rejects_unknown_phase(conn, db_path):
    _own_install_id(conn, db_path)
    handle = _handle(db_path)
    try:
        with pytest.raises(ValueError):
            company_scope.check(
                handle, _ctx(P_ONE, actor.ATTESTED), [CO_A], ""
            )
        with pytest.raises(ValueError):
            company_scope.check(
                handle, _ctx(P_ONE, actor.ATTESTED), [CO_A], "staged"
            )
    finally:
        handle.close()
