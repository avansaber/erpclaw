"""Company membership writers, readers and legacy reconcile report."""
import ast
import json
import os
import sys

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import pytest

from setup_helpers import (
    call_action,
    freeze_snapshot,
    load_db_query,
    ns,
    open_reader,
    seed_company,
)
from erpclaw_lib import company_scope
from erpclaw_lib.db import get_connection
from erpclaw_lib.query import Field, P, Q, Table

TABLES = ("authority_install", "authority_principal", "authority_membership",
          "audit_log", "erp_user", "user_role")

GRANT = "grant-company-membership"
DENY = "deny-company-membership"
REVOKE = "revoke-company-membership"
LIST = "list-company-memberships"
RECONCILE = "reconcile-legacy-company-scope"

NOTE = ("principal_found is False for every user until principals "
        "are provisioned; equal id is the only link between the stores.")


def _mod():
    return load_db_query()


def _install_id(db_path):
    handle = get_connection(db_path)
    try:
        return company_scope.install_id(handle)
    finally:
        handle.close()


def _seed_principal(conn, install, pid, kind="human"):
    conn.execute(
        Q.into(Table("authority_principal")).columns(
            "install_id", "id", "kind", "disabled_at"
        ).insert(P(), P(), P(), P()).get_sql(),
        (install, pid, kind, None),
    )


def _seed_membership(conn, install, pid, cid, effect):
    conn.execute(
        Q.into(Table("authority_membership")).columns(
            "install_id", "principal_id", "company_id", "effect"
        ).insert(P(), P(), P(), P()).get_sql(),
        (install, pid, cid, effect),
    )


def _snapshot(db_path):
    reader = open_reader(db_path)
    try:
        return freeze_snapshot(reader, db_path, TABLES)
    finally:
        reader.close()


def _call(fn_name, db_path, **flags):
    mod = _mod()
    handle = get_connection(db_path)
    try:
        return call_action(getattr(mod, fn_name.replace("-", "_")), handle,
                           ns(**flags))
    finally:
        handle.close()


def _grant(db_path, principal, company, effect=None):
    return _call(GRANT, db_path, principal_id=principal,
                 company_id=company, effect=effect)


def _deny(db_path, principal, company, effect=None):
    return _call(DENY, db_path, principal_id=principal,
                 company_id=company, effect=effect)


def _revoke(db_path, principal, company, effect):
    return _call(REVOKE, db_path, principal_id=principal,
                 company_id=company, effect=effect)


def _list(db_path, principal=None, company=None):
    return _call(LIST, db_path, principal_id=principal,
                 company_id=company)


def _reconcile(db_path):
    return _call(RECONCILE, db_path)


def _membership_rows(db_path):
    reader = open_reader(db_path)
    try:
        table = Table("authority_membership")
        rows = reader.execute(
            Q.from_(table).select(table.install_id, table.principal_id,
                                  table.company_id, table.effect).get_sql()
        ).fetchall()
        return [tuple(row) for row in rows]
    finally:
        reader.close()


def _audit_rows(db_path):
    reader = open_reader(db_path)
    try:
        table = Table("audit_log")
        rows = reader.execute(
            Q.from_(table).select(table.action, table.entity_type,
                                  table.entity_id, table.old_values,
                                  table.new_values).get_sql()
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        reader.close()


def test_grant_writes_one_row_and_one_audit_row(conn, db_path):
    mod = _mod()
    install = _install_id(db_path)
    assert install is not None
    comp_a = seed_company(conn)
    _seed_principal(conn, install, "p-one")
    conn.commit()
    result = _grant(db_path, "p-one", comp_a)
    assert result["status"] == "ok"
    assert result["install_id"] == install
    assert result["principal_id"] == "p-one"
    assert result["company_id"] == comp_a
    assert result["effect"] == "allow"
    rows = _membership_rows(db_path)
    assert rows == [(install, "p-one", comp_a, "allow")]
    audits = _audit_rows(db_path)
    assert len(audits) == 1
    row = audits[0]
    assert row["action"] == GRANT
    assert row["entity_type"] == "authority_membership"
    assert row["entity_id"] == "p-one"
    parsed = json.loads(row["new_values"])
    assert parsed["install_id"] == install
    assert parsed["principal_id"] == "p-one"
    assert parsed["company_id"] == comp_a
    assert parsed["effect"] == "allow"
    handle = get_connection(db_path)
    try:
        assert set(company_scope.principal_scope(handle, install, "p-one")) == {comp_a}
    finally:
        handle.close()


def test_deny_keeps_allow(conn, db_path):
    install = _install_id(db_path)
    comp_a = seed_company(conn)
    _seed_principal(conn, install, "p-one")
    conn.commit()
    assert _grant(db_path, "p-one", comp_a)["status"] == "ok"
    assert _deny(db_path, "p-one", comp_a)["status"] == "ok"
    rows = sorted(_membership_rows(db_path))
    assert rows == sorted([(install, "p-one", comp_a, "allow"),
                           (install, "p-one", comp_a, "deny")])
    assert len(_audit_rows(db_path)) == 2
    handle = get_connection(db_path)
    try:
        assert set(company_scope.principal_scope(handle, install, "p-one")) == set()
    finally:
        handle.close()


def test_deny_without_allow(conn, db_path):
    install = _install_id(db_path)
    comp_b = seed_company(conn)
    _seed_principal(conn, install, "p-one")
    conn.commit()
    assert _deny(db_path, "p-one", comp_b)["status"] == "ok"
    assert _membership_rows(db_path) == [(install, "p-one", comp_b, "deny")]
    handle = get_connection(db_path)
    try:
        assert set(company_scope.principal_scope(handle, install, "p-one")) == set()
    finally:
        handle.close()


def test_duplicate_refused(conn, db_path):
    install = _install_id(db_path)
    comp_a = seed_company(conn)
    _seed_principal(conn, install, "p-one")
    conn.commit()
    assert _grant(db_path, "p-one", comp_a)["status"] == "ok"
    before = _snapshot(db_path)
    dup = _grant(db_path, "p-one", comp_a)
    assert dup == {"status": "error", "message": "COMPANY_MEMBERSHIP_EXISTS"}
    assert _snapshot(db_path) == before
    assert _deny(db_path, "p-one", comp_a)["status"] == "ok"
    before2 = _snapshot(db_path)
    dup2 = _deny(db_path, "p-one", comp_a)
    assert dup2 == {"status": "error", "message": "COMPANY_MEMBERSHIP_EXISTS"}
    assert _snapshot(db_path) == before2


def test_revoke_removes_exactly_one_row(conn, db_path):
    install = _install_id(db_path)
    comp_a = seed_company(conn)
    _seed_principal(conn, install, "p-one")
    conn.commit()
    assert _grant(db_path, "p-one", comp_a)["status"] == "ok"
    assert _deny(db_path, "p-one", comp_a)["status"] == "ok"
    first = _revoke(db_path, "p-one", comp_a, "deny")
    assert first["status"] == "ok"
    assert first["revoked"] is True
    assert _membership_rows(db_path) == [(install, "p-one", comp_a, "allow")]
    handle = get_connection(db_path)
    try:
        assert set(company_scope.principal_scope(handle, install, "p-one")) == {comp_a}
    finally:
        handle.close()
    audits = _audit_rows(db_path)
    revoke_rows = [row for row in audits if row["action"] == REVOKE]
    assert len(revoke_rows) == 1
    parsed = json.loads(revoke_rows[0]["old_values"])
    assert parsed == {"install_id": install, "principal_id": "p-one",
                      "company_id": comp_a, "effect": "deny"}
    second = _revoke(db_path, "p-one", comp_a, "allow")
    assert second["status"] == "ok"
    assert _membership_rows(db_path) == []
    before = _snapshot(db_path)
    mod = _mod()
    handle = get_connection(db_path)
    try:
        third = call_action(mod.revoke_company_membership, handle,
                            ns(principal_id="p-one", company_id=comp_a,
                               effect="allow"))
        assert third == {"status": "error",
                         "message": "COMPANY_MEMBERSHIP_NOT_FOUND"}
        membership_table = Table("authority_membership")
        seen = handle.execute(
            Q.from_(membership_table).select(
                membership_table.install_id).where(
                membership_table.principal_id == P()).get_sql(),
            ("p-one",),
        ).fetchall()
        assert list(seen) == []
        audit_table = Table("audit_log")
        heard = handle.execute(
            Q.from_(audit_table).select(
                audit_table.action,
                audit_table.old_values).where(
                audit_table.action == P()).get_sql(),
            (REVOKE,),
        ).fetchall()
        assert len(list(heard)) == 2
        effects = sorted(json.loads(row["old_values"])["effect"]
                         for row in heard)
        assert effects == ["allow", "deny"]
    finally:
        handle.close()
    assert _snapshot(db_path) == before


@pytest.mark.parametrize("fn_name,flags,code", [
    (GRANT, {"principal_id": None, "company_id": "x", "effect": None},
     "COMPANY_MEMBERSHIP_INPUT_INVALID"),
    (GRANT, {"principal_id": "", "company_id": "x", "effect": None},
     "COMPANY_MEMBERSHIP_INPUT_INVALID"),
    (GRANT, {"principal_id": "p-one", "company_id": None, "effect": None},
     "COMPANY_MEMBERSHIP_INPUT_INVALID"),
    (GRANT, {"principal_id": "p-one", "company_id": "", "effect": None},
     "COMPANY_MEMBERSHIP_INPUT_INVALID"),
    (DENY, {"principal_id": None, "company_id": "x", "effect": None},
     "COMPANY_MEMBERSHIP_INPUT_INVALID"),
    (DENY, {"principal_id": "p-one", "company_id": None, "effect": None},
     "COMPANY_MEMBERSHIP_INPUT_INVALID"),
    (GRANT, {"principal_id": "p-one", "company_id": "c", "effect": "allow"},
     "COMPANY_MEMBERSHIP_INPUT_INVALID"),
    (DENY, {"principal_id": "p-one", "company_id": "c", "effect": "deny"},
     "COMPANY_MEMBERSHIP_INPUT_INVALID"),
    (REVOKE, {"principal_id": "p-one", "company_id": "c", "effect": None},
     "COMPANY_MEMBERSHIP_INPUT_INVALID"),
    (REVOKE, {"principal_id": "p-one", "company_id": "c", "effect": "maybe"},
     "COMPANY_MEMBERSHIP_INPUT_INVALID"),
    (REVOKE, {"principal_id": None, "company_id": "c", "effect": "allow"},
     "COMPANY_MEMBERSHIP_INPUT_INVALID"),
    (REVOKE, {"principal_id": "p-one", "company_id": None, "effect": "allow"},
     "COMPANY_MEMBERSHIP_INPUT_INVALID"),
    (GRANT, {"principal_id": "no-such", "company_id": "c", "effect": None},
     "PRINCIPAL_NOT_FOUND"),
    (REVOKE, {"principal_id": "no-such", "company_id": "c", "effect": "allow"},
     "PRINCIPAL_NOT_FOUND"),
    (GRANT, {"principal_id": "p-one", "company_id": "no-such-co", "effect": None},
     "COMPANY_NOT_FOUND"),
    (DENY, {"principal_id": "p-one", "company_id": "no-such-co", "effect": None},
     "COMPANY_NOT_FOUND"),
])
def test_input_refusals(conn, db_path, fn_name, flags, code):
    install = _install_id(db_path)
    comp_a = seed_company(conn)
    _seed_principal(conn, install, "p-one")
    conn.commit()
    fixed = dict(flags)
    if fixed.get("company_id") == "c":
        fixed["company_id"] = comp_a
    if fixed.get("company_id") == "x":
        fixed["company_id"] = comp_a
    before = _snapshot(db_path)
    result = _call(fn_name, db_path, **fixed)
    assert result == {"status": "error", "message": code}
    assert _snapshot(db_path) == before
    assert _audit_rows(db_path) == []


def test_writers_refuse_at_active(conn, db_path):
    install = _install_id(db_path)
    comp_a = seed_company(conn)
    _seed_principal(conn, install, "p-one")
    conn.commit()
    table = Table("authority_install")
    conn.execute(
        Q.update(table).set(Field("phase"), P()).get_sql(), ("ACTIVE",))
    conn.commit()
    before = _snapshot(db_path)
    assert _grant(db_path, "p-one", comp_a) == {
        "status": "error",
        "message": "COMPANY_MEMBERSHIP_ISSUER_UNAVAILABLE"}
    assert _deny(db_path, "p-one", comp_a) == {
        "status": "error",
        "message": "COMPANY_MEMBERSHIP_ISSUER_UNAVAILABLE"}
    assert _revoke(db_path, "p-one", comp_a, "allow") == {
        "status": "error",
        "message": "COMPANY_MEMBERSHIP_ISSUER_UNAVAILABLE"}
    assert _call(GRANT, db_path, principal_id=None, company_id=None,
                 effect=None) == {
        "status": "error",
        "message": "COMPANY_MEMBERSHIP_ISSUER_UNAVAILABLE"}
    assert _call(DENY, db_path, principal_id=None, company_id=None,
                 effect=None) == {
        "status": "error",
        "message": "COMPANY_MEMBERSHIP_ISSUER_UNAVAILABLE"}
    assert _call(REVOKE, db_path, principal_id=None, company_id=None,
                 effect=None) == {
        "status": "error",
        "message": "COMPANY_MEMBERSHIP_ISSUER_UNAVAILABLE"}
    assert _snapshot(db_path) == before
    assert _list(db_path)["status"] == "ok"
    assert _reconcile(db_path)["status"] == "ok"


def test_phase_change_before_commit_refuses(conn, db_path, monkeypatch):
    mod = _mod()
    install = _install_id(db_path)
    comp_a = seed_company(conn)
    _seed_principal(conn, install, "p-one")
    conn.commit()
    answers = [("STAGED", install), ("ACTIVE", install)]

    def _fake(_conn):
        return answers.pop(0)

    monkeypatch.setattr(mod, "_authority_phase", _fake)
    handle = get_connection(db_path)
    try:
        result = call_action(mod.grant_company_membership, handle,
                             ns(principal_id="p-one", company_id=comp_a,
                                effect=None))
        membership_table = Table("authority_membership")
        seen = handle.execute(
            Q.from_(membership_table).select(
                membership_table.install_id).where(
                membership_table.principal_id == P()).get_sql(),
            ("p-one",),
        ).fetchall()
        assert list(seen) == []
        audit_table = Table("audit_log")
        heard = handle.execute(
            Q.from_(audit_table).select(audit_table.action).where(
                audit_table.action == P()).get_sql(),
            (GRANT,),
        ).fetchall()
        assert list(heard) == []
    finally:
        handle.close()
    assert result == {"status": "error",
                      "message": "COMPANY_MEMBERSHIP_ISSUER_UNAVAILABLE"}
    assert _membership_rows(db_path) == []
    assert _audit_rows(db_path) == []


def test_audit_failure_rolls_back(conn, db_path, monkeypatch):
    mod = _mod()
    install = _install_id(db_path)
    comp_a = seed_company(conn)
    _seed_principal(conn, install, "p-one")
    conn.commit()

    def _boom(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(mod, "audit", _boom)
    handle = get_connection(db_path)
    try:
        with pytest.raises(RuntimeError):
            mod.grant_company_membership(
                handle, ns(principal_id="p-one", company_id=comp_a,
                           effect=None))
        table = Table("authority_membership")
        seen = handle.execute(
            Q.from_(table).select(table.install_id).where(
                table.install_id == P()).where(
                table.principal_id == P()).get_sql(),
            (install, "p-one"),
        ).fetchall()
        assert list(seen) == []
    finally:
        handle.close()
    assert _membership_rows(db_path) == []


def test_core_absent(tmp_path):
    mod = _mod()
    empty = str(tmp_path / "empty.sqlite")
    handle = get_connection(empty)
    try:
        missing = call_action(mod.grant_company_membership, handle,
                              ns(principal_id="p-one", company_id="c",
                                 effect=None))
        assert missing == {"status": "error",
                           "message": "AUTHORITY_CORE_UNAVAILABLE"}
    finally:
        handle.close()
    handle2 = get_connection(empty)
    try:
        listed = call_action(mod.list_company_memberships, handle2,
                             ns(principal_id=None, company_id=None))
    finally:
        handle2.close()
    assert listed["status"] == "ok"
    assert listed["core_present"] is False
    assert listed["memberships"] == []
    assert listed["total_count"] == 0


def test_list_filters_and_scope(conn, db_path):
    install = _install_id(db_path)
    comp_a = seed_company(conn)
    comp_b = seed_company(conn)
    _seed_principal(conn, install, "p-one")
    _seed_principal(conn, install, "p-two")
    _seed_membership(conn, install, "p-one", comp_a, "allow")
    _seed_membership(conn, install, "p-one", comp_b, "deny")
    _seed_membership(conn, install, "p-two", comp_b, "allow")
    conn.commit()
    before = _snapshot(db_path)
    full = _list(db_path)
    assert full["status"] == "ok"
    assert full["core_present"] is True
    assert full["total_count"] == 3
    assert full["memberships"] == sorted(full["memberships"], key=lambda item: (
        item["principal_id"], item["company_id"], item["effect"]))
    assert full["memberships"] == sorted([
        {"principal_id": "p-one", "company_id": comp_a, "effect": "allow"},
        {"principal_id": "p-one", "company_id": comp_b, "effect": "deny"},
        {"principal_id": "p-two", "company_id": comp_b, "effect": "allow"},
    ], key=lambda item: (item["principal_id"], item["company_id"],
                          item["effect"]))
    assert "effective_scope" not in full
    scoped = _call(LIST, db_path, principal_id="p-one", company_id=None)
    assert scoped["total_count"] == 2
    assert scoped["effective_scope"] == sorted([comp_a])
    by_company = _call(LIST, db_path, principal_id=None, company_id=comp_b)
    assert by_company["total_count"] == 2
    assert "effective_scope" not in by_company
    assert _snapshot(db_path) == before
    assert _audit_rows(db_path) == []


def _seed_user(conn, user_id, company_ids):
    conn.execute(
        Q.into(Table("erp_user")).columns(
            "id", "username", "status", "company_ids"
        ).insert(P(), P(), P(), P()).get_sql(),
        (user_id, user_id, "active", company_ids),
    )


def test_reconcile_reports_and_writes_nothing(conn, db_path):
    install = _install_id(db_path)
    comp_a = seed_company(conn)
    comp_b = seed_company(conn)
    comp_c = seed_company(conn)
    every = sorted([comp_a, comp_b, comp_c])
    conn.execute(
        Q.into(Table("role")).columns("id", "name").insert(
            P(), P()).get_sql(), ("r-one", " Tester "),
    )
    _seed_user(conn, "u1", json.dumps([comp_a]))
    _seed_user(conn, "u2", json.dumps([comp_a, comp_b]))
    _seed_user(conn, "u3", None)
    _seed_user(conn, "u4", "not json")
    _seed_user(conn, "u5", json.dumps([comp_a]))
    _seed_user(conn, "u6", json.dumps([comp_a]))
    _seed_user(conn, "u7", json.dumps([]))
    _seed_user(conn, "u8", json.dumps("A"))
    _seed_user(conn, "u9", json.dumps([comp_a, 7]))
    _seed_user(conn, "u10", json.dumps([comp_a]))
    conn.execute(
        Q.into(Table("user_role")).columns(
            "id", "user_id", "role_id", "company_id"
        ).insert(P(), P(), P(), P()).get_sql(),
        ("ur-five", "u5", "r-one", comp_c),
    )
    conn.execute(
        Q.into(Table("user_role")).columns(
            "id", "user_id", "role_id", "company_id"
        ).insert(P(), P(), P(), P()).get_sql(),
        ("ur-ten", "u10", "r-one", None),
    )
    for pid in ("u1", "u2", "u6", "p-orphan"):
        _seed_principal(conn, install, pid)
    _seed_membership(conn, install, "u1", comp_a, "allow")
    _seed_membership(conn, install, "u2", comp_a, "allow")
    _seed_membership(conn, install, "u6", comp_a, "allow")
    _seed_membership(conn, install, "u6", comp_a, "deny")
    _seed_membership(conn, install, "p-orphan", comp_b, "allow")
    conn.commit()
    before = _snapshot(db_path)
    result = _reconcile(db_path)
    assert result["status"] == "ok"
    assert result["core_present"] is True
    assert result["note"] == NOTE
    by_id = {item["user_id"]: item for item in result["users"]}
    assert sorted(by_id) == ["u1", "u10", "u2", "u3", "u4", "u5", "u6",
                             "u7", "u8", "u9"]
    assert [item["user_id"] for item in result["users"]] == sorted(by_id)
    one = by_id["u1"]
    assert one["username"] == "u1"
    assert one["user_status"] == "active"
    assert one["legacy_company_ids"] == [comp_a]
    assert one["legacy_unrestricted"] is False
    assert one["legacy_company_ids_malformed"] is False
    assert one["legacy_global_role"] is False
    assert one["principal_found"] is True
    assert one["principal_disabled"] is False
    assert one["membership_company_ids"] == [comp_a]
    assert one["legacy_only"] == []
    assert one["membership_only"] == []
    assert one["agrees"] is True
    two = by_id["u2"]
    assert two["legacy_company_ids"] == sorted([comp_a, comp_b])
    assert two["membership_company_ids"] == [comp_a]
    assert two["legacy_only"] == [comp_b]
    assert two["membership_only"] == []
    assert two["agrees"] is False
    three = by_id["u3"]
    assert three["legacy_unrestricted"] is True
    assert three["legacy_company_ids_malformed"] is False
    assert three["legacy_company_ids"] == every
    assert three["principal_found"] is False
    assert three["membership_company_ids"] == []
    assert three["agrees"] is False
    four = by_id["u4"]
    assert four["legacy_unrestricted"] is True
    assert four["legacy_company_ids_malformed"] is True
    assert four["legacy_company_ids"] == every
    assert four["agrees"] is False
    five = by_id["u5"]
    assert five["legacy_company_ids"] == sorted([comp_a, comp_c])
    assert five["legacy_unrestricted"] is False
    assert five["legacy_company_ids_malformed"] is False
    assert five["legacy_global_role"] is False
    assert five["principal_found"] is False
    assert five["agrees"] is False
    six = by_id["u6"]
    assert six["legacy_company_ids"] == [comp_a]
    assert six["membership_company_ids"] == []
    assert six["legacy_only"] == [comp_a]
    assert six["membership_only"] == []
    assert six["principal_found"] is True
    assert six["agrees"] is False
    seven = by_id["u7"]
    assert seven["legacy_unrestricted"] is True
    assert seven["legacy_company_ids_malformed"] is False
    assert seven["legacy_company_ids"] == every
    eight = by_id["u8"]
    assert eight["legacy_unrestricted"] is True
    assert eight["legacy_company_ids_malformed"] is True
    assert eight["legacy_company_ids"] == every
    nine = by_id["u9"]
    assert nine["legacy_company_ids_malformed"] is True
    assert nine["legacy_unrestricted"] is False
    assert nine["legacy_company_ids"] == [comp_a]
    ten = by_id["u10"]
    assert ten["legacy_global_role"] is True
    assert ten["legacy_company_ids"] == [comp_a]
    assert ten["legacy_unrestricted"] is False
    assert ten["legacy_company_ids_malformed"] is False
    assert result["principals_without_legacy_user"] == [
        {"principal_id": "p-orphan",
         "membership_company_ids": [comp_b]}]
    assert result["disagreement_count"] == 10
    assert _snapshot(db_path) == before
    assert _audit_rows(db_path) == []


def test_routed_documented_and_carved_out():
    repo = os.path.abspath(os.path.join(_TESTS_DIR, os.pardir, os.pardir,
                                        os.pardir, os.pardir, os.pardir))
    router_path = os.path.join(repo, "source", "erpclaw", "scripts",
                               "db_query.py")
    with open(router_path, encoding="utf-8") as handle:
        router_tree = ast.parse(handle.read())
    action_map = {}
    dangerous = set()
    for node in router_tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "ACTION_MAP":
                    if isinstance(node.value, ast.Dict):
                        for key, value in zip(node.value.keys,
                                              node.value.values):
                            if (isinstance(key, ast.Constant)
                                    and isinstance(value, ast.Constant)):
                                action_map[key.value] = value.value
                if isinstance(target, ast.Name) and target.id == "DANGEROUS_ACTIONS":
                    call = node.value
                    if isinstance(call, ast.Call) and call.args:
                        first = call.args[0]
                        if isinstance(first, (ast.Set, ast.List, ast.Tuple)):
                            dangerous = {elt.value for elt in first.elts
                                         if isinstance(elt, ast.Constant)}
    for name in (GRANT, DENY, REVOKE, LIST, RECONCILE):
        assert action_map.get(name) == "erpclaw-setup", name
    for name in (GRANT, DENY, REVOKE):
        assert name in dangerous, name
    for name in (LIST, RECONCILE):
        assert name not in dangerous, name
    skill_path = os.path.join(repo, "source", "erpclaw", "SKILL.md")
    with open(skill_path, encoding="utf-8") as handle:
        skill_text = handle.read()
    assert len(skill_text.splitlines()) <= 300
    for name in (GRANT, DENY, REVOKE, LIST, RECONCILE):
        assert ("`" + name + "`") in skill_text, name
    confirm_path = os.path.join(repo, "source", "erpclaw", "mcp",
                                "confirm.py")
    with open(confirm_path, encoding="utf-8") as handle:
        confirm_tree = ast.parse(handle.read())
    carved = set()
    for node in confirm_tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "CREDENTIAL_CARVE_OUT":
                    call = node.value
                    if isinstance(call, ast.Call) and call.args:
                        first = call.args[0]
                        if isinstance(first, (ast.Set, ast.List, ast.Tuple)):
                            carved = {elt.value for elt in first.elts
                                      if isinstance(elt, ast.Constant)}
    for name in (GRANT, DENY, REVOKE):
        assert name in carved, name
    for name in (LIST, RECONCILE):
        assert name not in carved, name
