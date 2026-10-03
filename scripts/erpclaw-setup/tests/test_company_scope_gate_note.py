"""One-call scope records and the stored rows that carry them."""
import os
import sys
import uuid

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_PAYMENTS_TESTS = os.path.abspath(
    os.path.join(_HERE, "..", "..", "erpclaw-payments", "tests"))
_JOURNALS_TESTS = os.path.abspath(
    os.path.join(_HERE, "..", "..", "erpclaw-journals", "tests"))
for _path in (_HERE, _PAYMENTS_TESTS, _JOURNALS_TESTS):
    if _path not in sys.path:
        sys.path.insert(0, _path)

_PG_URL = os.environ.get("ERPCLAW_PG_TEST_URL")


@pytest.fixture
def two_companies(tmp_path):
    """Two held drafts under one install; not qualification."""
    import payments_helpers as helpers
    import authority_fixtures as fx
    path = str(tmp_path / "gate-note.sqlite")
    helpers.init_all_tables(path)
    from erpclaw_lib.db import get_connection
    handle = get_connection(path)
    try:
        env_a = helpers.build_ar_env(handle)
        env_b = helpers.build_ar_env(handle)
        handle.commit()
        seeded = fx.seed_authority(path, env_a["company_id"])
        pay = helpers.load_db_query()

        def _draft(env):
            return helpers.call_action(
                pay.add_payment, handle, helpers.ns(
                    company_id=env["company_id"],
                    payment_type="receive",
                    posting_date="2026-06-15",
                    party_type="customer",
                    party_id=env["customer"],
                    paid_from_account=env["ar"],
                    paid_to_account=env["bank"],
                    paid_amount="100.00",
                    exchange_rate=None,
                    payment_currency=None,
                    reference_number=None,
                    reference_date=None,
                    allocations=None,
                    deductions=None))

        first = _draft(env_a)
        assert first.get("status") == "ok", first
        second = _draft(env_b)
        assert second.get("status") == "ok", second
        handle.commit()
    finally:
        handle.close()
    return {
        "path": path,
        "env_a": env_a,
        "env_b": env_b,
        "pe_a": first["payment_entry_id"],
        "pe_b": second["payment_entry_id"],
        "now": seeded["now"],
    }


def _refused_code(excinfo, wanted):
    assert excinfo.value.code == wanted
    assert excinfo.value.args == (wanted,)


def test_staged_notes(two_companies, monkeypatch):
    """Staged records for one held draft; not qualification."""
    import authority_fixtures as fx
    from erpclaw_lib import actor as actor_mod
    from erpclaw_lib import company_scope as scope_mod
    from erpclaw_lib.db import get_connection
    path = two_companies["path"]
    env_a = two_companies["env_a"]
    env_b = two_companies["env_b"]
    pe_a = two_companies["pe_a"]
    handle = get_connection(path)
    try:
        absent = actor_mod.ActorContext(None, None, None, (), actor_mod.ABSENT)
        attested = actor_mod.ActorContext(
            None, None, fx.SERVICE, (), actor_mod.ATTESTED)
        claimed = actor_mod.ActorContext(
            None, None, fx.SERVICE, (), actor_mod.CLAIMED)
        other = actor_mod.ActorContext(
            None, None, fx.OTHER_SERVICE, (), actor_mod.ATTESTED)
        monkeypatch.setattr(actor_mod, "current", lambda: absent)
        assert scope_mod.gate_note(
            handle, "submit-payment",
            ["--payment-entry-id", pe_a], "STAGED") == scope_mod.Note(
            (env_a["company_id"],), "no_principal")
        monkeypatch.setattr(actor_mod, "current", lambda: attested)
        assert scope_mod.gate_note(
            handle, "submit-payment",
            ["--payment-entry-id", pe_a], "STAGED") == scope_mod.Note(
            (env_a["company_id"],), "in_scope")
        monkeypatch.setattr(actor_mod, "current", lambda: claimed)
        assert scope_mod.gate_note(
            handle, "submit-payment",
            ["--payment-entry-id", pe_a], "STAGED") == scope_mod.Note(
            (env_a["company_id"],), "in_scope")
        monkeypatch.setattr(actor_mod, "current", lambda: attested)
        assert scope_mod.gate_note(
            handle, "add-payment",
            ["--company-id", env_b["company_id"]],
            "STAGED") == scope_mod.Note(
            (env_b["company_id"],), "out_of_scope")
        monkeypatch.setattr(actor_mod, "current", lambda: other)
        assert scope_mod.gate_note(
            handle, "submit-payment",
            ["--payment-entry-id", pe_a], "STAGED") == scope_mod.Note(
            (env_a["company_id"],), "no_scope")
        monkeypatch.setattr(actor_mod, "current", lambda: attested)
        assert scope_mod.gate_note(
            handle, "submit-payment",
            ["--payment-entry", pe_a], "STAGED") == scope_mod.Note(
            None, "underived")
        assert scope_mod.gate_note(
            handle, "list-payments", [], "STAGED") == scope_mod.Note(
            None, "underived")
        assert scope_mod.gate_note(
            handle, "no-such-action-here", [], "STAGED") == scope_mod.Note(
            None, "underived")
        assert scope_mod.gate_note(
            handle, "add-uom", [], "STAGED") == scope_mod.Note(
            None, "not_applicable")
        with pytest.raises(ValueError):
            scope_mod.Note((), "in_scope")
        with pytest.raises(ValueError):
            scope_mod.Note(("x",), "underived")
    finally:
        handle.close()


def test_staged_never_raises(two_companies, monkeypatch):
    """Staged path stays total under faults; not qualification."""
    import authority_fixtures as fx
    from erpclaw_lib import actor as actor_mod
    from erpclaw_lib import company_scope as scope_mod
    from erpclaw_lib.db import get_connection
    pe_a = two_companies["pe_a"]
    handle = get_connection(two_companies["path"])
    try:
        attested = actor_mod.ActorContext(
            None, None, fx.SERVICE, (), actor_mod.ATTESTED)
        argv = ["--payment-entry-id", pe_a]
        with monkeypatch.context() as sub:
            sub.setattr(actor_mod, "current", lambda: attested)

            def _boom(*args, **kwargs):
                raise RuntimeError("boom")

            sub.setattr(scope_mod, "derive_companies", _boom)
            assert scope_mod.gate_note(
                handle, "submit-payment", list(argv),
                "STAGED") == scope_mod.Note(None, "underived")
        with monkeypatch.context() as sub:
            sub.setattr(actor_mod, "current", lambda: attested)

            def _boom(*args, **kwargs):
                raise RuntimeError("boom")

            sub.setattr(scope_mod, "check", _boom)
            assert scope_mod.gate_note(
                handle, "submit-payment", list(argv),
                "STAGED") == scope_mod.Note(None, "underived")
        with monkeypatch.context() as sub:
            sub.setattr(actor_mod, "current", lambda: attested)
            sub.setattr(scope_mod, "core_present", _boom)
            assert scope_mod.gate_note(
                handle, "submit-payment", list(argv),
                "STAGED") == scope_mod.Note(None, "underived")
        with monkeypatch.context() as sub:
            def _boom(*args, **kwargs):
                raise RuntimeError("boom")

            sub.setattr(actor_mod, "current", _boom)
            assert scope_mod.gate_note(
                handle, "submit-payment", list(argv),
                "STAGED") == scope_mod.Note(None, "underived")
        monkeypatch.setattr(actor_mod, "current", lambda: attested)
        with pytest.raises(ValueError):
            scope_mod.gate_note(handle, "get-payment", list(argv), "LIVE")
    finally:
        handle.close()


def test_principal_matrix_at_active(two_companies, monkeypatch):
    """Active actor matrix for one held draft; not qualification."""
    import authority_fixtures as fx
    from erpclaw_lib import actor as actor_mod
    from erpclaw_lib import company_scope as scope_mod
    from erpclaw_lib.db import get_connection
    from erpclaw_lib.query import Field, P, Q, Table
    path = two_companies["path"]
    env_a = two_companies["env_a"]
    pe_a = two_companies["pe_a"]
    fx.make_active(path)
    handle = get_connection(path)
    try:
        argv = ["--payment-entry-id", pe_a]
        absent = actor_mod.ActorContext(None, None, None, (), actor_mod.ABSENT)
        other = actor_mod.ActorContext(
            None, None, fx.OTHER_SERVICE, (), actor_mod.ATTESTED)
        claimed = actor_mod.ActorContext(
            None, None, fx.SERVICE, (), actor_mod.CLAIMED)
        attested = actor_mod.ActorContext(
            None, None, fx.SERVICE, (), actor_mod.ATTESTED)
        monkeypatch.setattr(actor_mod, "current", lambda: absent)
        with pytest.raises(scope_mod.ScopeRefused) as excinfo:
            scope_mod.gate_note(handle, "get-payment", list(argv), "ACTIVE")
        _refused_code(excinfo, scope_mod.REFUSAL_CODE)
        monkeypatch.setattr(actor_mod, "current", lambda: other)
        with pytest.raises(scope_mod.ScopeRefused) as excinfo:
            scope_mod.gate_note(handle, "get-payment", list(argv), "ACTIVE")
        _refused_code(excinfo, scope_mod.REFUSAL_CODE)
        monkeypatch.setattr(actor_mod, "current", lambda: claimed)
        with pytest.raises(scope_mod.ScopeRefused) as excinfo:
            scope_mod.gate_note(handle, "get-payment", list(argv), "ACTIVE")
        _refused_code(excinfo, scope_mod.REFUSAL_CODE)
        editor = get_connection(path)
        try:
            fx._insert_row(editor, "authority_membership", {
                "install_id": fx._install_id(editor),
                "principal_id": fx.SERVICE,
                "company_id": env_a["company_id"],
                "effect": "deny"})
            editor.commit()
        finally:
            editor.close()
        monkeypatch.setattr(actor_mod, "current", lambda: attested)
        with pytest.raises(scope_mod.ScopeRefused) as excinfo:
            scope_mod.gate_note(handle, "get-payment", list(argv), "ACTIVE")
        _refused_code(excinfo, scope_mod.REFUSAL_CODE)
        cleaner = get_connection(path)
        try:
            membership = Table("authority_membership")
            cleaner.execute(
                Q.from_(membership).delete().where(
                    Field("principal_id") == P()).where(
                    Field("company_id") == P()).where(
                    Field("effect") == P()).get_sql(),
                (fx.SERVICE, env_a["company_id"], "deny"))
            cleaner.commit()
        finally:
            cleaner.close()
        fx.set_principal_disabled(path, fx.SERVICE, two_companies["now"])
        with pytest.raises(scope_mod.ScopeRefused) as excinfo:
            scope_mod.gate_note(handle, "get-payment", list(argv), "ACTIVE")
        _refused_code(excinfo, scope_mod.REFUSAL_CODE)
        fx.set_principal_disabled(path, fx.SERVICE, None)
        assert scope_mod.gate_note(
            handle, "get-payment", list(argv),
            "ACTIVE") == scope_mod.Note((env_a["company_id"],), "in_scope")
    finally:
        handle.close()


def test_refusals_look_identical(two_companies, monkeypatch):
    """Active refusals carry no identifiers; not qualification."""
    import authority_fixtures as fx
    from erpclaw_lib import actor as actor_mod
    from erpclaw_lib import company_scope as scope_mod
    from erpclaw_lib.db import get_connection
    path = two_companies["path"]
    env_b = two_companies["env_b"]
    pe_a = two_companies["pe_a"]
    pe_b = two_companies["pe_b"]
    fx.make_active(path)
    handle = get_connection(path)
    try:
        attested = actor_mod.ActorContext(
            None, None, fx.SERVICE, (), actor_mod.ATTESTED)
        monkeypatch.setattr(actor_mod, "current", lambda: attested)
        missing = str(uuid.uuid4())
        seen = []
        for argv in (
            ["--payment-entry-id", missing],
            ["--payment-entry-id", pe_b],
            ["--payment-entry-id", pe_a, "--company-id",
             env_b["company_id"]],
            ["--payment-entry", pe_a],
        ):
            with pytest.raises(scope_mod.ScopeRefused) as excinfo:
                scope_mod.gate_note(handle, "get-payment", list(argv),
                                    "ACTIVE")
            _refused_code(excinfo, scope_mod.REFUSAL_CODE)
            seen.append(str(excinfo.value))
        assert seen[0] == seen[1] == seen[2] == seen[3]
        for token in (missing, pe_a, pe_b, env_b["company_id"]):
            assert token not in seen[0]
    finally:
        handle.close()


def test_no_company_at_active(two_companies, monkeypatch):
    """Active unnamed reads use the single allowed value; not qualification."""
    import authority_fixtures as fx
    from erpclaw_lib import actor as actor_mod
    from erpclaw_lib import company_scope as scope_mod
    from erpclaw_lib.db import get_connection
    path = two_companies["path"]
    env_a = two_companies["env_a"]
    env_b = two_companies["env_b"]
    fx.make_active(path)
    handle = get_connection(path)
    try:
        attested = actor_mod.ActorContext(
            None, None, fx.SERVICE, (), actor_mod.ATTESTED)
        monkeypatch.setattr(actor_mod, "current", lambda: attested)
        assert scope_mod.gate_note(
            handle, "list-payments", [],
            "ACTIVE") == scope_mod.Note((env_a["company_id"],), "in_scope")
        with pytest.raises(scope_mod.ScopeRefused) as excinfo:
            scope_mod.gate_note(handle, "list-payments",
                                ["--company-id", env_b["company_id"]],
                                "ACTIVE")
        _refused_code(excinfo, scope_mod.REFUSAL_CODE)
        editor = get_connection(path)
        try:
            fx._insert_row(editor, "authority_membership", {
                "install_id": fx._install_id(editor),
                "principal_id": fx.SERVICE,
                "company_id": env_b["company_id"],
                "effect": "allow"})
            editor.commit()
        finally:
            editor.close()
        with pytest.raises(scope_mod.ScopeRefused) as excinfo:
            scope_mod.gate_note(handle, "list-payments", [], "ACTIVE")
        _refused_code(excinfo, scope_mod.SCOPE_AMBIGUOUS)
        assert scope_mod.gate_note(
            handle, "list-payments", ["--company-id", env_b["company_id"]],
            "ACTIVE") == scope_mod.Note((env_b["company_id"],), "in_scope")
    finally:
        handle.close()


def test_install_global_and_setup_company(two_companies, monkeypatch):
    """Active install-wide writes and the one exemption; not qualification."""
    import authority_fixtures as fx
    from erpclaw_lib import actor as actor_mod
    from erpclaw_lib import company_scope as scope_mod
    from erpclaw_lib.db import get_connection
    path = two_companies["path"]
    env_b = two_companies["env_b"]
    fx.make_active(path)
    handle = get_connection(path)
    try:
        attested = actor_mod.ActorContext(
            None, None, fx.SERVICE, (), actor_mod.ATTESTED)
        monkeypatch.setattr(actor_mod, "current", lambda: attested)
        with pytest.raises(scope_mod.ScopeRefused) as excinfo:
            scope_mod.gate_note(handle, "add-uom", [], "ACTIVE")
        _refused_code(excinfo, scope_mod.REFUSAL_CODE)
        editor = get_connection(path)
        try:
            fx._insert_row(editor, "authority_membership", {
                "install_id": fx._install_id(editor),
                "principal_id": fx.SERVICE,
                "company_id": env_b["company_id"],
                "effect": "allow"})
            editor.commit()
        finally:
            editor.close()
        assert scope_mod.gate_note(
            handle, "add-uom", [],
            "ACTIVE") == scope_mod.Note(None, "not_applicable")
        other = actor_mod.ActorContext(
            None, None, fx.OTHER_SERVICE, (), actor_mod.ATTESTED)
        monkeypatch.setattr(actor_mod, "current", lambda: other)
        assert scope_mod.gate_note(
            handle, "setup-company", [],
            "ACTIVE") == scope_mod.Note(None, "no_scope")
        absent = actor_mod.ActorContext(None, None, None, (), actor_mod.ABSENT)
        monkeypatch.setattr(actor_mod, "current", lambda: absent)
        with pytest.raises(scope_mod.ScopeRefused) as excinfo:
            scope_mod.gate_note(handle, "setup-company", [], "ACTIVE")
        _refused_code(excinfo, scope_mod.REFUSAL_CODE)
        claimed = actor_mod.ActorContext(
            None, None, fx.SERVICE, (), actor_mod.CLAIMED)
        monkeypatch.setattr(actor_mod, "current", lambda: claimed)
        with pytest.raises(scope_mod.ScopeRefused) as excinfo:
            scope_mod.gate_note(handle, "setup-company", [], "ACTIVE")
        _refused_code(excinfo, scope_mod.REFUSAL_CODE)
    finally:
        handle.close()


def test_no_company_needs_the_allowlist(two_companies, monkeypatch):
    """Active unnamed reads stay closed outside the resolver set; not qualification."""
    import authority_fixtures as fx
    from erpclaw_lib import actor as actor_mod
    from erpclaw_lib import company_scope as scope_mod
    from erpclaw_lib.db import get_connection
    path = two_companies["path"]
    env_a = two_companies["env_a"]
    env_b = two_companies["env_b"]
    fx.make_active(path)
    handle = get_connection(path)
    try:
        attested = actor_mod.ActorContext(
            None, None, fx.SERVICE, (), actor_mod.ATTESTED)
        monkeypatch.setattr(actor_mod, "current", lambda: attested)
        with pytest.raises(scope_mod.ScopeRefused) as excinfo:
            scope_mod.gate_note(handle, "get-company", [], "ACTIVE")
        _refused_code(excinfo, scope_mod.REFUSAL_CODE)
        assert scope_mod.gate_note(
            handle, "get-company", ["--company-id", env_a["company_id"]],
            "ACTIVE") == scope_mod.Note((env_a["company_id"],), "in_scope")
        with pytest.raises(scope_mod.ScopeRefused) as excinfo:
            scope_mod.gate_note(handle, "get-company",
                                ["--company-id", env_b["company_id"]],
                                "ACTIVE")
        _refused_code(excinfo, scope_mod.REFUSAL_CODE)
        assert scope_mod.NO_COMPANY_RESOLVES == frozenset({
            "list-payments", "get-unallocated-payments",
            "list-open-advances", "list-journal-entries",
            "list-recurring-templates"})
        assert scope_mod.INSTALL_GLOBAL_EXEMPT == frozenset({"setup-company"})
    finally:
        handle.close()


def test_probe_failures_refuse_at_active(two_companies, monkeypatch):
    """Active probe faults refuse, including the outer liveness read; not qualification."""
    import authority_fixtures as fx
    from erpclaw_lib import actor as actor_mod
    from erpclaw_lib import company_scope as scope_mod
    from erpclaw_lib.db import get_connection
    path = two_companies["path"]
    env_a = two_companies["env_a"]
    pe_a = two_companies["pe_a"]
    fx.make_active(path)
    handle = get_connection(path)
    try:
        attested = actor_mod.ActorContext(
            None, None, fx.SERVICE, (), actor_mod.ATTESTED)
        argv = ["--payment-entry-id", pe_a]

        def _boom(*args, **kwargs):
            raise RuntimeError("boom")

        with monkeypatch.context() as sub:
            sub.setattr(actor_mod, "current", lambda: attested)
            sub.setattr(scope_mod, "core_present", lambda conn: False)
            with pytest.raises(scope_mod.ScopeRefused) as excinfo:
                scope_mod.gate_note(handle, "get-payment", list(argv),
                                    "ACTIVE")
            _refused_code(excinfo, scope_mod.REFUSAL_CODE)
        with monkeypatch.context() as sub:
            sub.setattr(actor_mod, "current", lambda: attested)
            sub.setattr(scope_mod, "install_id", _boom)
            with pytest.raises(scope_mod.ScopeRefused) as excinfo:
                scope_mod.gate_note(handle, "get-payment", list(argv),
                                    "ACTIVE")
            _refused_code(excinfo, scope_mod.REFUSAL_CODE)
        with monkeypatch.context() as sub:
            sub.setattr(actor_mod, "current", lambda: attested)
            sub.setattr(scope_mod, "derive_companies", _boom)
            with pytest.raises(scope_mod.ScopeRefused) as excinfo:
                scope_mod.gate_note(handle, "get-payment", list(argv),
                                    "ACTIVE")
            _refused_code(excinfo, scope_mod.REFUSAL_CODE)
        with monkeypatch.context() as sub:
            sub.setattr(actor_mod, "current", lambda: attested)
            sub.setattr(scope_mod, "check", _boom)
            with pytest.raises(scope_mod.ScopeRefused) as excinfo:
                scope_mod.gate_note(handle, "get-payment", list(argv),
                                    "ACTIVE")
            _refused_code(excinfo, scope_mod.REFUSAL_CODE)
        with monkeypatch.context() as sub:
            sub.setattr(actor_mod, "current", _boom)
            with pytest.raises(scope_mod.ScopeRefused) as excinfo:
                scope_mod.gate_note(handle, "get-payment", list(argv),
                                    "ACTIVE")
            _refused_code(excinfo, scope_mod.REFUSAL_CODE)
        with monkeypatch.context() as sub:
            sub.setattr(actor_mod, "current", lambda: attested)
            sub.setattr(scope_mod, "resolution_scope", _boom)
            with pytest.raises(scope_mod.ScopeRefused) as excinfo:
                scope_mod.gate_note(handle, "list-payments", [], "ACTIVE")
            _refused_code(excinfo, scope_mod.REFUSAL_CODE)
        with monkeypatch.context() as sub:
            sub.setattr(actor_mod, "current", lambda: attested)
            sub.setattr(scope_mod, "principal_scope", _boom)
            with pytest.raises(scope_mod.ScopeRefused) as excinfo:
                scope_mod.gate_note(handle, "add-uom", [], "ACTIVE")
            _refused_code(excinfo, scope_mod.REFUSAL_CODE)
        with monkeypatch.context() as sub:
            sub.setattr(actor_mod, "current", lambda: attested)
            verdict = scope_mod.Verdict(
                "in_scope", False, None, "attested", fx.SERVICE,
                (env_a["company_id"],), "ACTIVE")
            sub.setattr(scope_mod, "check", lambda *a, **k: verdict)
            sub.setattr(scope_mod, "core_present", lambda conn: False)
            with pytest.raises(scope_mod.ScopeRefused) as excinfo:
                scope_mod.gate_note(handle, "get-payment", list(argv),
                                    "ACTIVE")
            _refused_code(excinfo, scope_mod.REFUSAL_CODE)
    finally:
        handle.close()


def test_bind_and_unbind(two_companies, monkeypatch):
    """Binding follows the single-attribute pattern; not qualification."""
    from erpclaw_lib import actor as actor_mod
    from erpclaw_lib import authority_gate as gate_mod
    from erpclaw_lib import company_scope as scope_mod
    from erpclaw_lib.db import get_connection
    monkeypatch.setattr(
        actor_mod, "current",
        lambda: actor_mod.ActorContext(None, None, None, (),
                                      actor_mod.ABSENT))
    handle = get_connection(two_companies["path"])
    other = get_connection(two_companies["path"])
    try:
        assert scope_mod.bound_note(handle) is None
        note = scope_mod.Note(None, "not_applicable")
        token = scope_mod.bind_note(handle, note)
        assert scope_mod.bound_note(handle) == note
        assert scope_mod.bound_note(gate_mod._DeferredHandle(handle)) == note
        with pytest.raises(ValueError):
            scope_mod.bind_note(handle, note)
        with pytest.raises(ValueError):
            scope_mod.bind_note(other, "x")
        scope_mod.unbind_note(handle, "wrong")
        assert scope_mod.bound_note(handle) == note
        scope_mod.unbind_note(handle, token)
        assert scope_mod.bound_note(handle) is None
    finally:
        handle.close()
        other.close()


def test_audit_writes_the_bound_note(two_companies, monkeypatch, capsys):
    """Bound records flow into stored rows unless overridden; not qualification."""
    import authority_fixtures as fx
    from erpclaw_lib import actor as actor_mod
    from erpclaw_lib import audit as audit_mod
    from erpclaw_lib import company_scope as scope_mod
    from erpclaw_lib.db import get_connection
    from setup_helpers import read_all
    monkeypatch.setattr(
        actor_mod, "current",
        lambda: actor_mod.ActorContext(None, None, fx.SERVICE, (),
                                      actor_mod.ATTESTED))
    path = two_companies["path"]
    env_a = two_companies["env_a"]
    handle = get_connection(path)
    try:
        audit_mod.audit(handle, "erpclaw-payments", "x", "t", "c0",
                        scope_company_ids=[env_a["company_id"]],
                        scope_status="in_scope")
        handle.commit()
        note = scope_mod.Note((env_a["company_id"],), "in_scope")
        token = scope_mod.bind_note(handle, note)
        try:
            audit_mod.audit(handle, "erpclaw-payments", "x", "t", "e1")
            handle.commit()
            rows = {row["entity_id"]: row for row in read_all(
                handle, "audit_log",
                ["entity_id", "scope_company_ids", "scope_status"])}
            assert rows["e1"]["scope_company_ids"] == rows["c0"][
                "scope_company_ids"]
            assert rows["e1"]["scope_status"] == rows["c0"]["scope_status"]
            audit_mod.audit(handle, "erpclaw-payments", "x", "t", "e2",
                            scope_status="underived")
            handle.commit()
            rows = {row["entity_id"]: row for row in read_all(
                handle, "audit_log",
                ["entity_id", "scope_company_ids", "scope_status"])}
            assert rows["e2"]["scope_company_ids"] is None
            assert rows["e2"]["scope_status"] == "underived"
        finally:
            scope_mod.unbind_note(handle, token)

        def _boom(conn):
            raise RuntimeError("boom")

        with monkeypatch.context() as sub:
            sub.setattr(scope_mod, "bound_note", _boom)
            audit_mod.audit(handle, "erpclaw-payments", "x", "t", "e3")
            handle.commit()
        rows = {row["entity_id"]: row for row in read_all(
            handle, "audit_log",
            ["entity_id", "scope_company_ids", "scope_status"])}
        assert rows["e3"]["scope_company_ids"] is None
        assert rows["e3"]["scope_status"] is None
    finally:
        handle.close()
    fresh = get_connection(path)
    try:
        token = scope_mod.bind_note(
            fresh, scope_mod.Note((env_a["company_id"],), "in_scope"))
        try:
            monkeypatch.setattr(audit_mod, "_probe_scope_columns",
                                lambda conn: False)
            capsys.readouterr()
            audit_mod.audit(fresh, "erpclaw-payments", "x", "t", "e4")
            fresh.commit()
            assert capsys.readouterr().err == ""
            rows = {row["entity_id"]: row for row in read_all(
                fresh, "audit_log",
                ["entity_id", "scope_company_ids", "scope_status"])}
            assert rows["e4"]["scope_company_ids"] is None
            assert rows["e4"]["scope_status"] is None
        finally:
            scope_mod.unbind_note(fresh, token)
    finally:
        fresh.close()


def test_audit_safe_scope_without_log(two_companies, monkeypatch, capsys,
                                      tmp_path):
    """Best-effort writes degrade without the extra columns; not qualification."""
    import authority_fixtures as fx
    from erpclaw_lib import actor as actor_mod
    from erpclaw_lib import audit as audit_mod
    from erpclaw_lib import company_scope as scope_mod
    from erpclaw_lib.db import get_connection
    from setup_helpers import read_all
    monkeypatch.setattr(
        actor_mod, "current",
        lambda: actor_mod.ActorContext(None, None, fx.SERVICE, (),
                                      actor_mod.ATTESTED))
    empty_path = str(tmp_path / "empty.sqlite")
    bare = get_connection(empty_path)
    try:
        capsys.readouterr()
        assert audit_mod.audit_safe(
            bare, "erpclaw-payments", "x", "t", "e1",
            scope_status="underived") is None
        assert capsys.readouterr().err == ""
    finally:
        bare.close()
    path = two_companies["path"]
    fresh = get_connection(path)
    try:
        monkeypatch.setattr(audit_mod, "_probe_scope_columns",
                            lambda conn: False)
        capsys.readouterr()
        audit_mod.audit_safe(fresh, "erpclaw-payments", "x", "t", "e1",
                             scope_company_ids=["c1"],
                             scope_status="in_scope")
        fresh.commit()
        warned = capsys.readouterr().err.splitlines()
        assert len(warned) == 1 and warned[0].startswith("WARN:")
        rows = {row["entity_id"]: row for row in read_all(
            fresh, "audit_log",
            ["entity_id", "scope_company_ids", "scope_status"])}
        assert rows["e1"]["scope_company_ids"] is None
        assert rows["e1"]["scope_status"] is None
        with pytest.raises(ValueError) as excinfo:
            audit_mod.audit(fresh, "erpclaw-payments", "x", "t", "e2",
                            scope_company_ids=["c1"],
                            scope_status="in_scope")
        assert excinfo.value.args == ("SCOPE_AUDIT_UNAVAILABLE",)
        with pytest.raises(ValueError) as excinfo:
            audit_mod.audit_safe(fresh, "erpclaw-payments", "x", "t", "e3",
                                 scope_status="bogus")
        assert excinfo.value.args == ("SCOPE_STATUS_INVALID",)
        assert audit_mod._SCOPE_STATUSES == scope_mod.STATUSES
    finally:
        fresh.close()


@pytest.mark.skipif(not _PG_URL, reason="live backend required")
def test_pg_gate_note(monkeypatch):
    """Live-backend spot check plus the savepoint probe; not qualification."""
    import uuid as uuid_mod
    import authority_fixtures as fx
    import test_journal_envelope_gate as journal_gate
    import payments_helpers as helpers
    from erpclaw_lib import action_impact as impact_mod
    from erpclaw_lib import actor as actor_mod
    from erpclaw_lib import company_scope as scope_mod
    from erpclaw_lib.db import get_connection
    from erpclaw_lib.query import Field, P, Q, Table
    from setup_helpers import read_all
    journal_gate._SEEDED.clear()
    journal_gate._SEED_CAPS.clear()
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", _PG_URL)
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    from erpclaw_lib import seam as seam_mod
    seam_mod.dispose_engines()
    try:
        journal_gate._pg_reset_schema()
        helpers.init_all_tables(None)
        handle = get_connection(None)
        try:
            attested = actor_mod.ActorContext(
                None, None, fx.SERVICE, (), actor_mod.ATTESTED)
            monkeypatch.setattr(actor_mod, "current", lambda: attested)
            made = []
            for tag in ("pg-a", "pg-b"):
                cid = str(uuid_mod.uuid4())
                fx._insert_row(handle, "company", {
                    "id": cid,
                    "name": "Pg Co %s %s" % (tag, cid[:6]),
                    "abbr": "PG%s%s" % (tag[:1].upper(), cid[:4]),
                    "default_currency": "USD",
                    "country": "United States",
                    "fiscal_year_start_month": 1})
                fy = str(uuid_mod.uuid4())
                fx._insert_row(handle, "fiscal_year", {
                    "id": fy,
                    "name": "FY-%s-%s" % (tag, fy[:6]),
                    "start_date": "2026-01-01",
                    "end_date": "2026-12-31",
                    "company_id": cid})
                cash = str(uuid_mod.uuid4())
                fx._insert_row(handle, "account", {
                    "id": cash, "name": "Cash %s" % cid[:6],
                    "root_type": "asset", "company_id": cid})
                bank = str(uuid_mod.uuid4())
                fx._insert_row(handle, "account", {
                    "id": bank, "name": "Bank %s" % cid[:6],
                    "root_type": "asset", "company_id": cid})
                row = str(uuid_mod.uuid4())
                fx._insert_row(handle, "payment_entry", {
                    "id": row,
                    "payment_type": "receive",
                    "posting_date": "2026-06-15",
                    "paid_from_account": cash,
                    "paid_to_account": bank,
                    "paid_amount": "100.00",
                    "received_amount": "100.00",
                    "status": "draft",
                    "unallocated_amount": "100.00",
                    "company_id": cid})
                made.append((cid, row))
            handle.commit()
            (first_id, first_row), (second_id, second_row) = made
            fx.seed_authority(None, first_id)
            assert scope_mod.gate_note(
                handle, "get-payment", ["--payment-entry-id", first_row],
                "STAGED") == scope_mod.Note((first_id,), "in_scope")
            fx.make_active(None)
            assert scope_mod.gate_note(
                handle, "get-payment", ["--payment-entry-id", first_row],
                "ACTIVE") == scope_mod.Note((first_id,), "in_scope")
            with pytest.raises(scope_mod.ScopeRefused) as excinfo:
                scope_mod.gate_note(
                    handle, "get-payment",
                    ["--payment-entry-id", second_row], "ACTIVE")
            _refused_code(excinfo, scope_mod.REFUSAL_CODE)
            company = Table("company")
            found = handle.execute(
                Q.from_(company).select(Field("id")).get_sql()).fetchall()
            assert len(found) >= 2
            handle.rollback()
        finally:
            handle.close()
        probe = get_connection(None)
        try:
            monkeypatch.setattr(actor_mod, "current", lambda: attested)
            base = impact_mod.IMPACT["get-payment"]
            altered = dict(base, company_source="row:no_such_rows_here")
            monkeypatch.setitem(impact_mod.IMPACT, "get-payment", altered)
            extra_a = str(uuid_mod.uuid4())
            fx._insert_row(probe, "company", {
                "id": extra_a,
                "name": "Probe Co A %s" % extra_a[:6],
                "abbr": "PA%s" % extra_a[:4]})
            assert scope_mod.gate_note(
                probe, "get-payment", ["--payment-entry-id", first_row],
                "STAGED") == scope_mod.Note(None, "underived")
            extra_b = str(uuid_mod.uuid4())
            fx._insert_row(probe, "company", {
                "id": extra_b,
                "name": "Probe Co B %s" % extra_b[:6],
                "abbr": "PB%s" % extra_b[:4]})
            probe.commit()
            company = Table("company")
            back = [row["id"] for row in probe.execute(
                Q.from_(company).select(Field("id")).where(
                    Field("id").isin([P(), P()])).get_sql(),
                (extra_a, extra_b)).fetchall()]
            assert sorted(back) == sorted([extra_a, extra_b])
            assert len(read_all(
                probe, "audit_log", ["id"])) >= 0
        finally:
            probe.close()
    finally:
        seam_mod.dispose_engines()
        journal_gate._SEEDED.clear()
        journal_gate._SEED_CAPS.clear()
