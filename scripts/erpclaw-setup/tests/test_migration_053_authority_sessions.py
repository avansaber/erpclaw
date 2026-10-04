"""Migration 053: session storage lands on fresh and upgraded installs.

Fresh installs carry the five session tables and the session-digest audit
column from the start. Older installs gain them through the migration, which
rewrites nothing. Until the migration runs, the session tables are absent
and the digest column with them.
"""
import importlib.util
import io
import os
import sys
from contextlib import redirect_stdout
from urllib.parse import urlparse

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SETUP_DIR = os.path.dirname(_TESTS_DIR)
_MIGRATION = os.path.join(_SETUP_DIR, "migrations",
                          "053_authority_sessions.py")
_INIT_SCHEMA_PATH = os.path.join(_SETUP_DIR, "init_schema.py")

if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import setup_helpers  # noqa: E402  (binds erpclaw_lib to this tree)
from setup_helpers import read_all  # noqa: E402
from erpclaw_lib import seam  # noqa: E402  (after the lib binding)
from erpclaw_lib.db import get_connection, get_dialect  # noqa: E402
from erpclaw_lib.db import integrity_error_types  # noqa: E402
from erpclaw_lib.query import Field, P, Q, Table, fn  # noqa: E402

AUDIT_COL = "actor_session_digest"

SESSION_TABLES = ("authority_deployment",
                  "authority_credential",
                  "authority_session",
                  "authority_bootstrap_challenge",
                  "operation_authorization_issuer")

# Children first: nothing below references a sibling, so any order among the
# five would do; the issuer names the authorization row it was checked under.
_DROP_TABLES = (
    "DROP TABLE IF EXISTS operation_authorization_issuer",
    "DROP TABLE IF EXISTS authority_session",
    "DROP TABLE IF EXISTS authority_credential",
    "DROP TABLE IF EXISTS authority_bootstrap_challenge",
    "DROP TABLE IF EXISTS authority_deployment",
)
_DROP_COLUMN = "ALTER TABLE audit_log DROP COLUMN actor_session_digest"

_DEPLOYMENT_COLUMNS = ("install_id", "operator_account", "service_account",
                       "model_accounts", "expected_install_id", "recorded_at")
_CREDENTIAL_COLUMNS = ("install_id", "id", "principal_id", "scheme",
                        "verifier", "created_at", "retired_at")
_SESSION_COLUMNS = ("install_id", "id_digest", "principal_id", "created_at",
                    "expires_at", "revoked_at", "created_account", "route")
_CHALLENGE_COLUMNS = ("install_id", "id", "digest", "issued_at", "expires_at",
                      "consumed_at", "rotated_at", "consumed_by", "state")
_ISSUER_COLUMNS = ("authorization_id", "install_id", "issuer_id",
                   "session_digest")

_VERIFIER = "pbkdf2:600000$" + "a" * 32 + "$" + "b" * 64


@pytest.fixture(autouse=True)
def _engines():
    yield
    seam.dispose_engines()


def _mig():
    spec = importlib.util.spec_from_file_location(
        "migration_053", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pre053(conn, db_path):
    """Rewind the database to its shape before the session storage existed."""
    from erpclaw_lib import seam as _seam
    for statement in _DROP_TABLES:
        conn.execute(statement)
    conn.commit()
    if AUDIT_COL in _seam.column_names("audit_log", db_path):
        conn.execute(_DROP_COLUMN)
    conn.commit()
    assert all(name not in _seam.table_names(db_path)
               for name in SESSION_TABLES)
    assert AUDIT_COL not in _seam.column_names("audit_log", db_path)


def _count(conn, table):
    probe = Table(table)
    query = Q.from_(probe).select(fn.Count("*").as_("cnt")).get_sql()
    return conn.execute(query).fetchone()["cnt"]


def _counts(conn, db_path):
    return {name: _count(conn, name)
            for name in seam.table_names(db_path)}


def _full_shape(db_path, skip):
    out = {}
    for name in seam.table_names(db_path):
        if name in skip:
            continue
        out[name] = (seam.describe_table(name, db_path),
                     seam.describe_constraints(name, db_path))
    return out


def _install_id(conn):
    probe = Table("authority_install")
    query = Q.from_(probe).select(Field("install_id")).get_sql()
    return conn.execute(query).fetchone()["install_id"]


def _insert(conn, table, columns, values):
    probe = Table(table)
    query = Q.into(probe).columns(*columns).insert(
        *[P() for _ in columns]).get_sql()
    conn.execute(query, values)


def _exists(conn, table, column, value):
    probe = Table(table)
    query = Q.from_(probe).select(Field(column)).where(
        Field(column) == P()).get_sql()
    return conn.execute(query, (value,)).fetchone() is not None


def _seed_principal_and_authorizations(conn, install_id):
    # Idempotent: the PostgreSQL legs share one database across cases, so a
    # later case must skip the parent rows an earlier case already committed.
    # On a fresh SQLite database nothing exists yet, so this inserts as before.
    if not _exists(conn, "authority_principal", "id", "owner-1"):
        _insert(conn, "authority_principal",
                ("install_id", "id", "kind"), (install_id, "owner-1", "human"))
    for auth_id in ("auth-1", "auth-2"):
        if not _exists(conn, "operation_authorization", "id", auth_id):
            _insert(conn, "operation_authorization",
                    ("id", "install_id", "principal_id", "action",
                     "binding_digest", "issued_at", "expires_at"),
                    (auth_id, install_id, "owner-1", "test-action",
                     "a" * 64, 100, 200))
    conn.commit()


def _deployment_values(install_id, operator="op-1", service="svc-1",
                       expected=None, recorded=100):
    return (install_id, operator, service, '["model-1"]',
            install_id if expected is None else expected, recorded)


def _credential_values(install_id, cred_id="cred-1", principal="owner-1",
                       scheme="pbkdf2-sha256", verifier=None,
                       created=100, retired=None):
    return (install_id, cred_id, principal, scheme,
            _VERIFIER if verifier is None else verifier, created, retired)


def _session_values(install_id, digest=None, created=100, expires=200,
                    revoked=None, route="operator-tty"):
    return (install_id, "d" * 64 if digest is None else digest,
            "owner-1", created, expires, revoked, "op-1", route)


def _challenge_values(install_id, challenge_id="ch-1", digest=None,
                      issued=100, expires=200, consumed=None, rotated=None,
                      by=None, state="issued"):
    return (install_id, challenge_id,
            "e" * 64 if digest is None else digest,
            issued, expires, consumed, rotated, by, state)


def _issuer_values(auth_id, install_id, digest=None):
    return (auth_id, install_id, "owner-1",
            "f" * 64 if digest is None else digest)


def _accepts(conn, table, columns, values):
    before = _count(conn, table)
    _insert(conn, table, columns, values)
    assert _count(conn, table) == before + 1
    conn.rollback()


def _refuses(conn, table, columns, values):
    before = _count(conn, table)
    stored = False
    try:
        _insert(conn, table, columns, values)
        conn.commit()
        stored = True
    except integrity_error_types():
        conn.rollback()
    assert _count(conn, table) == before
    assert not stored, "bad row was stored in %s" % table


def _seeded(target):
    conn = get_connection(target)
    try:
        install_id = _install_id(conn)
        _seed_principal_and_authorizations(conn, install_id)
        return conn, install_id
    except Exception:
        conn.close()
        raise


def _release_read_views(conn):
    # Every SELECT above sits in an open read transaction on PostgreSQL, on
    # this handle and on the seam engines' pooled handles; either one's shared
    # lock on audit_log blocks the migration's ALTER TABLE behind the
    # lock_timeout. Roll back this handle and drop the pooled ones before any
    # run_migration that adds the column. SQLite is unaffected.
    conn.rollback()
    seam.dispose_engines()


# ── storage ───────────────────────────────────────────────────────────────

def _upgrade_case(target):
    conn = get_connection(target)
    try:
        _pre053(conn, target)
        before = _full_shape(target, set(SESSION_TABLES) | {"audit_log"})
        before_counts = _counts(conn, target)
        mig = _mig()
        buf = io.StringIO()
        with redirect_stdout(buf):
            report = mig.run_migration(target, report_only=True)
        assert report == {"would_create": list(seam._AUTHORITY_SESSION_TABLES),
                          "would_add": [AUDIT_COL], "report_only": True}
        text = buf.getvalue()
        for name in SESSION_TABLES:
            assert name in text
        assert AUDIT_COL in text
        assert all(name not in seam.table_names(target)
                   for name in SESSION_TABLES)
        assert AUDIT_COL not in seam.column_names("audit_log", target)
        assert _counts(conn, target) == before_counts
        _release_read_views(conn)
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = mig.run_migration(target)
        assert result == {"created": list(seam._AUTHORITY_SESSION_TABLES),
                          "added": [AUDIT_COL], "report_only": False}
        assert _full_shape(target, set(SESSION_TABLES) | {"audit_log"}) == before
        for name in SESSION_TABLES:
            assert name in seam.table_names(target)
            assert _count(conn, name) == 0
        assert (seam.column_names("audit_log", target)[-1] == AUDIT_COL)
    finally:
        conn.close()


def test_upgrade_creates_and_changes_no_row(conn, db_path):
    _upgrade_case(db_path)


def _fresh_equals_upgraded_case(target):
    conn = get_connection(target)
    try:
        fresh = _full_shape(target, set())
        fresh_audit = seam.column_names("audit_log", target)
        _pre053(conn, target)
        _release_read_views(conn)
        _mig().run_migration(target)
        assert _full_shape(target, set()) == fresh
        assert seam.column_names("audit_log", target) == fresh_audit
    finally:
        conn.close()


def test_fresh_install_equals_upgraded_install(conn, db_path):
    _fresh_equals_upgraded_case(db_path)


def _rerun_case(target):
    conn = get_connection(target)
    try:
        _pre053(conn, target)
        mig = _mig()
        _release_read_views(conn)
        assert mig.run_migration(target) == {
            "created": list(seam._AUTHORITY_SESSION_TABLES),
            "added": [AUDIT_COL], "report_only": False}
        assert mig.run_migration(target) == {
            "created": [], "added": [], "report_only": False}
    finally:
        conn.close()


def test_second_run_is_a_no_op(conn, db_path):
    _rerun_case(db_path)


def _refusals_case(target):
    conn = get_connection(target)
    try:
        _pre053(conn, target)
        columns_before = seam.column_names("audit_log", target)
        for statement in (
                "DROP TABLE IF EXISTS operation_authorization_envelope",
                "DROP TABLE IF EXISTS operation_authorization_result",
                "DROP TABLE IF EXISTS authority_delegation_usage"):
            conn.execute(statement)
        conn.commit()
        import pytest as _pytest
        with _pytest.raises(RuntimeError) as excinfo:
            _mig().run_migration(target)
        assert "ENVELOPE_ABSENT" in str(excinfo.value)
        assert seam.column_names("audit_log", target) == columns_before
    finally:
        conn.close()


def test_refuses_without_the_envelope(db_path):
    _refusals_case(db_path)


def test_refuses_without_the_authority_core(tmp_path):
    path = str(tmp_path / "empty.sqlite")
    import pytest as _pytest
    with _pytest.raises(RuntimeError) as excinfo:
        _mig().run_migration(path)
    assert "ENVELOPE_ABSENT" in str(excinfo.value)


def test_wrong_shape_table_refuses(conn, db_path):
    _pre053(conn, db_path)
    _sa = seam._sqlalchemy()
    _big = _sa.BigInteger().with_variant(_sa.Integer(), "sqlite")
    meta = seam.authority_envelope_metadata()
    seam.Table(
        "authority_session", meta,
        seam.Column("install_id", seam.Text, nullable=False),
        seam.Column("id_digest", seam.Text, nullable=False),
        seam.Column("principal_id", seam.Text, nullable=False),
        seam.Column("created_at", _big, nullable=False),
        seam.Column("expires_at", _big, nullable=False),
        seam.Column("revoked_at", _big, nullable=True),
        seam.Column("created_account", seam.Text, nullable=False),
        seam.Column("route", seam.Text, nullable=False),
        seam.PrimaryKeyConstraint("install_id", "id_digest"),
        seam.ForeignKeyConstraint(
            ["install_id", "principal_id"],
            ["authority_principal.install_id", "authority_principal.id"],
            onupdate="RESTRICT", ondelete="RESTRICT"),
        seam.CheckConstraint("length(id_digest) = 64"),
        seam.CheckConstraint("created_at < expires_at"),
        seam.CheckConstraint("expires_at - created_at <= 28800000"),
        seam.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at"),
    )
    seam.provision(meta, db_path)
    before = _counts(conn, db_path)
    import pytest as _pytest
    with _pytest.raises(RuntimeError) as excinfo:
        _mig().run_migration(db_path)
    assert "STRUCTURE" in str(excinfo.value)
    assert AUDIT_COL not in seam.column_names("audit_log", db_path)
    after = _counts(conn, db_path)
    for name, total in before.items():
        assert after[name] == total


def test_declares_no_data_change():
    mig = _mig()
    assert mig.MIGRATION_DATA_CLASS == "none"
    assert not hasattr(mig, "MIGRATION_DATA_EXEMPTIONS")


def _partial_unique_case(target):
    credential = ("ux_authority_credential_live",
                  ("install_id", "principal_id"), "retired_at IS NULL")
    bootstrap = ("ux_authority_bootstrap_issued",
                 ("install_id",), "state = 'issued'")
    for table, want in (("authority_credential", [credential]),
                        ("authority_bootstrap_challenge", [bootstrap])):
        assert seam.describe_constraints(table, target)["partial_unique"] == want
    envelope = seam.describe_constraints(
        "operation_authorization_envelope", target)
    assert (sorted(envelope) ==
            sorted(["checks", "foreign_keys", "defaults", "uniques",
                    "index_defs", "partial_unique"]))
    assert envelope["partial_unique"] == []


def test_partial_unique_indexes_reported(conn, db_path):
    _pre053(conn, db_path)
    _mig().run_migration(db_path)
    _partial_unique_case(db_path)


def test_partial_unique_indexes_on_fresh_install(db_path):
    _partial_unique_case(db_path)


def test_core_inspector_still_reports_match(conn, db_path):
    _pre053(conn, db_path)
    _mig().run_migration(db_path)
    install_id = _install_id(conn)
    live = get_connection(db_path)
    try:
        live.execute("BEGIN")
        result = seam.inspect_authority_core(
            live, expected_install_id=install_id)
        assert result["status"] == "MATCH"
        live.rollback()
    finally:
        live.close()


# ── constraint cases ──────────────────────────────────────────────────────

def _deployment_cases(target):
    conn, install_id = _seeded(target)
    try:
        _accepts(conn, "authority_deployment", _DEPLOYMENT_COLUMNS,
                 _deployment_values(install_id))
        bad = _deployment_values(install_id, operator="same-1", service="same-1")
        _refuses(conn, "authority_deployment", _DEPLOYMENT_COLUMNS, bad)
    finally:
        conn.close()


def test_deployment_refuses_equal_accounts(conn, db_path):
    _deployment_cases(db_path)


def _deployment_account_text_cases(target):
    conn, install_id = _seeded(target)
    try:
        _accepts(conn, "authority_deployment", _DEPLOYMENT_COLUMNS,
                 _deployment_values(install_id))
        _refuses(conn, "authority_deployment", _DEPLOYMENT_COLUMNS,
                 _deployment_values(install_id, operator=""))
        _refuses(conn, "authority_deployment", _DEPLOYMENT_COLUMNS,
                 _deployment_values(install_id, operator="o" * 65))
    finally:
        conn.close()


def test_deployment_refuses_empty_and_long_accounts(conn, db_path):
    _deployment_account_text_cases(db_path)


def _deployment_expected_cases(target):
    conn, install_id = _seeded(target)
    try:
        _accepts(conn, "authority_deployment", _DEPLOYMENT_COLUMNS,
                 _deployment_values(install_id))
        _refuses(conn, "authority_deployment", _DEPLOYMENT_COLUMNS,
                 _deployment_values(install_id, expected=""))
    finally:
        conn.close()


def test_deployment_refuses_empty_expected_install(conn, db_path):
    _deployment_expected_cases(db_path)


def _deployment_recorded_cases(target):
    conn, install_id = _seeded(target)
    try:
        _accepts(conn, "authority_deployment", _DEPLOYMENT_COLUMNS,
                 _deployment_values(install_id))
        _refuses(conn, "authority_deployment", _DEPLOYMENT_COLUMNS,
                 _deployment_values(install_id, recorded=-1))
    finally:
        conn.close()


def test_deployment_refuses_negative_recorded_at(conn, db_path):
    _deployment_recorded_cases(db_path)


def _credential_scheme_cases(target):
    conn, install_id = _seeded(target)
    try:
        _accepts(conn, "authority_credential", _CREDENTIAL_COLUMNS,
                 _credential_values(install_id))
        _refuses(conn, "authority_credential", _CREDENTIAL_COLUMNS,
                 _credential_values(install_id, cred_id="cred-2",
                                    scheme="argon2id"))
    finally:
        conn.close()


def test_credential_refuses_other_scheme(conn, db_path):
    _credential_scheme_cases(db_path)


def _credential_iteration_cases(target):
    conn, install_id = _seeded(target)
    try:
        _accepts(conn, "authority_credential", _CREDENTIAL_COLUMNS,
                 _credential_values(install_id))
        other = "pbkdf2:600001$" + "a" * 32 + "$" + "b" * 64
        assert len(other) == 111
        _refuses(conn, "authority_credential", _CREDENTIAL_COLUMNS,
                 _credential_values(install_id, cred_id="cred-2",
                                    verifier=other))
    finally:
        conn.close()


def test_credential_refuses_other_iteration_count(conn, db_path):
    _credential_iteration_cases(db_path)


def _credential_uppercase_cases(target):
    conn, install_id = _seeded(target)
    try:
        _accepts(conn, "authority_credential", _CREDENTIAL_COLUMNS,
                 _credential_values(install_id))
        upper = "PBKDF2:600000$" + "a" * 32 + "$" + "b" * 64
        assert len(upper) == 111
        _refuses(conn, "authority_credential", _CREDENTIAL_COLUMNS,
                 _credential_values(install_id, cred_id="cred-2",
                                    verifier=upper))
    finally:
        conn.close()


def test_credential_refuses_uppercase_prefix(conn, db_path):
    _credential_uppercase_cases(db_path)


def _credential_length_cases(target):
    conn, install_id = _seeded(target)
    try:
        _accepts(conn, "authority_credential", _CREDENTIAL_COLUMNS,
                 _credential_values(install_id))
        short = "pbkdf2:600000$" + "a" * 32 + "$" + "b" * 63
        assert len(short) == 110
        _refuses(conn, "authority_credential", _CREDENTIAL_COLUMNS,
                 _credential_values(install_id, cred_id="cred-2",
                                    verifier=short))
    finally:
        conn.close()


def test_credential_refuses_short_verifier(conn, db_path):
    _credential_length_cases(db_path)


def _credential_principal_cases(target):
    conn, install_id = _seeded(target)
    try:
        _accepts(conn, "authority_credential", _CREDENTIAL_COLUMNS,
                 _credential_values(install_id))
        _refuses(conn, "authority_credential", _CREDENTIAL_COLUMNS,
                 _credential_values(install_id, cred_id="cred-2",
                                    principal="ghost-1"))
    finally:
        conn.close()


def test_credential_refuses_unknown_principal(conn, db_path):
    _credential_principal_cases(db_path)


def _credential_retired_cases(target):
    conn, install_id = _seeded(target)
    try:
        _accepts(conn, "authority_credential", _CREDENTIAL_COLUMNS,
                 _credential_values(install_id))
        _refuses(conn, "authority_credential", _CREDENTIAL_COLUMNS,
                 _credential_values(install_id, cred_id="cred-2",
                                    created=100, retired=99))
    finally:
        conn.close()


def test_credential_refuses_retired_before_created(conn, db_path):
    _credential_retired_cases(db_path)


def _credential_live_cases(target):
    conn, install_id = _seeded(target)
    try:
        _insert(conn, "authority_principal",
                ("install_id", "id", "kind"),
                (install_id, "owner-2", "human"))
        conn.commit()
        _insert(conn, "authority_credential", _CREDENTIAL_COLUMNS,
                _credential_values(install_id, cred_id="live-1",
                                   principal="owner-2"))
        conn.commit()
        _refuses(conn, "authority_credential", _CREDENTIAL_COLUMNS,
                 _credential_values(install_id, cred_id="live-2",
                                    principal="owner-2"))
        _insert(conn, "authority_credential", _CREDENTIAL_COLUMNS,
                _credential_values(install_id, cred_id="old-1",
                                   created=100, retired=150))
        conn.commit()
        _insert(conn, "authority_credential", _CREDENTIAL_COLUMNS,
                _credential_values(install_id, cred_id="live-3"))
        conn.commit()
        rows = read_all(conn, "authority_credential", ["id"])
        assert sorted(row["id"] for row in rows) == [
            "live-1", "live-3", "old-1"]
    finally:
        conn.close()


def test_credential_live_uniqueness(conn, db_path):
    _credential_live_cases(db_path)


def _session_digest_cases(target):
    conn, install_id = _seeded(target)
    try:
        _accepts(conn, "authority_session", _SESSION_COLUMNS,
                 _session_values(install_id))
        _refuses(conn, "authority_session", _SESSION_COLUMNS,
                 _session_values(install_id, digest="d" * 63))
    finally:
        conn.close()


def test_session_refuses_short_digest(conn, db_path):
    _session_digest_cases(db_path)


def _session_window_cases(target):
    conn, install_id = _seeded(target)
    try:
        _accepts(conn, "authority_session", _SESSION_COLUMNS,
                 _session_values(install_id))
        _refuses(conn, "authority_session", _SESSION_COLUMNS,
                 _session_values(install_id, created=100, expires=100))
    finally:
        conn.close()


def test_session_refuses_empty_window(conn, db_path):
    _session_window_cases(db_path)


def _session_lifetime_cases(target):
    conn, install_id = _seeded(target)
    try:
        _accepts(conn, "authority_session", _SESSION_COLUMNS,
                 _session_values(install_id, digest="d" * 63 + "e",
                                 created=100, expires=100 + 28800000))
        _refuses(conn, "authority_session", _SESSION_COLUMNS,
                 _session_values(install_id, created=100,
                                 expires=100 + 28800001))
    finally:
        conn.close()


def test_session_refuses_long_lifetime(conn, db_path):
    _session_lifetime_cases(db_path)


def _session_revoked_cases(target):
    conn, install_id = _seeded(target)
    try:
        _accepts(conn, "authority_session", _SESSION_COLUMNS,
                 _session_values(install_id))
        _refuses(conn, "authority_session", _SESSION_COLUMNS,
                 _session_values(install_id, created=100, expires=200,
                                 revoked=99))
    finally:
        conn.close()


def test_session_refuses_revoked_before_created(conn, db_path):
    _session_revoked_cases(db_path)


def _session_route_cases(target):
    conn, install_id = _seeded(target)
    try:
        _accepts(conn, "authority_session", _SESSION_COLUMNS,
                 _session_values(install_id))
        _refuses(conn, "authority_session", _SESSION_COLUMNS,
                 _session_values(install_id, route="cli"))
    finally:
        conn.close()


def test_session_refuses_other_route(conn, db_path):
    _session_route_cases(db_path)


def _challenge_digest_cases(target):
    conn, install_id = _seeded(target)
    try:
        _accepts(conn, "authority_bootstrap_challenge", _CHALLENGE_COLUMNS,
                 _challenge_values(install_id))
        _refuses(conn, "authority_bootstrap_challenge", _CHALLENGE_COLUMNS,
                 _challenge_values(install_id, challenge_id="ch-2",
                                   digest="e" * 63))
    finally:
        conn.close()


def test_challenge_refuses_short_digest(conn, db_path):
    _challenge_digest_cases(db_path)


def _challenge_state_cases(target):
    conn, install_id = _seeded(target)
    try:
        _accepts(conn, "authority_bootstrap_challenge", _CHALLENGE_COLUMNS,
                 _challenge_values(install_id))
        _refuses(conn, "authority_bootstrap_challenge", _CHALLENGE_COLUMNS,
                 _challenge_values(install_id, challenge_id="ch-2",
                                   state="other"))
    finally:
        conn.close()


def test_challenge_refuses_other_state(conn, db_path):
    _challenge_state_cases(db_path)


def _challenge_combo_cases(target):
    conn, install_id = _seeded(target)
    try:
        _accepts(conn, "authority_bootstrap_challenge", _CHALLENGE_COLUMNS,
                 _challenge_values(install_id))
        _refuses(conn, "authority_bootstrap_challenge", _CHALLENGE_COLUMNS,
                 _challenge_values(install_id, challenge_id="ch-2",
                                   consumed=150, by="op-1", state="issued"))
        _refuses(conn, "authority_bootstrap_challenge", _CHALLENGE_COLUMNS,
                 _challenge_values(install_id, challenge_id="ch-3",
                                   by="op-1", state="consumed"))
        _refuses(conn, "authority_bootstrap_challenge", _CHALLENGE_COLUMNS,
                 _challenge_values(install_id, challenge_id="ch-4",
                                   state="rotated"))
    finally:
        conn.close()


def test_challenge_refuses_illegal_state_combinations(conn, db_path):
    _challenge_combo_cases(db_path)


def _challenge_window_cases(target):
    conn, install_id = _seeded(target)
    try:
        _accepts(conn, "authority_bootstrap_challenge", _CHALLENGE_COLUMNS,
                 _challenge_values(install_id))
        _refuses(conn, "authority_bootstrap_challenge", _CHALLENGE_COLUMNS,
                 _challenge_values(install_id, challenge_id="ch-2",
                                   issued=100, expires=100))
    finally:
        conn.close()


def test_challenge_refuses_empty_window(conn, db_path):
    _challenge_window_cases(db_path)


def _challenge_lifetime_cases(target):
    conn, install_id = _seeded(target)
    try:
        _accepts(conn, "authority_bootstrap_challenge", _CHALLENGE_COLUMNS,
                 _challenge_values(install_id, challenge_id="ch-ok",
                                   issued=100, expires=100 + 3600000))
        _refuses(conn, "authority_bootstrap_challenge", _CHALLENGE_COLUMNS,
                 _challenge_values(install_id, challenge_id="ch-2",
                                   issued=100, expires=100 + 3600001))
    finally:
        conn.close()


def test_challenge_refuses_long_lifetime(conn, db_path):
    _challenge_lifetime_cases(db_path)


def _challenge_issued_cases(target):
    conn, install_id = _seeded(target)
    try:
        _insert(conn, "authority_bootstrap_challenge", _CHALLENGE_COLUMNS,
                _challenge_values(install_id, challenge_id="ch-a"))
        conn.commit()
        _refuses(conn, "authority_bootstrap_challenge", _CHALLENGE_COLUMNS,
                 _challenge_values(install_id, challenge_id="ch-b"))
        probe = Table("authority_bootstrap_challenge")
        consume = (Q.update(probe)
                   .set(Field("state"), P())
                   .set(Field("consumed_at"), P())
                   .set(Field("consumed_by"), P())
                   .where(Field("install_id") == P())
                   .where(Field("id") == P()).get_sql())
        conn.execute(consume, ("consumed", 150, "op-1", install_id, "ch-a"))
        conn.commit()
        _insert(conn, "authority_bootstrap_challenge", _CHALLENGE_COLUMNS,
                _challenge_values(install_id, challenge_id="ch-d"))
        conn.commit()
        rows = read_all(conn, "authority_bootstrap_challenge", ["id", "state"])
        assert sorted((row["id"], row["state"]) for row in rows) == [
            ("ch-a", "consumed"), ("ch-d", "issued")]
    finally:
        conn.close()


def test_challenge_issued_uniqueness(conn, db_path):
    _challenge_issued_cases(db_path)


def _issuer_missing_cases(target):
    conn, install_id = _seeded(target)
    try:
        _accepts(conn, "operation_authorization_issuer", _ISSUER_COLUMNS,
                 _issuer_values("auth-1", install_id))
        _refuses(conn, "operation_authorization_issuer", _ISSUER_COLUMNS,
                 _issuer_values("auth-missing", install_id))
    finally:
        conn.close()


def test_issuer_refuses_missing_authorization(conn, db_path):
    _issuer_missing_cases(db_path)


def _issuer_digest_cases(target):
    conn, install_id = _seeded(target)
    try:
        _accepts(conn, "operation_authorization_issuer", _ISSUER_COLUMNS,
                 _issuer_values("auth-1", install_id))
        _refuses(conn, "operation_authorization_issuer", _ISSUER_COLUMNS,
                 _issuer_values("auth-2", install_id, digest="f" * 65))
    finally:
        conn.close()


def test_issuer_refuses_long_digest(conn, db_path):
    _issuer_digest_cases(db_path)


_PG_URL = os.environ.get("ERPCLAW_PG_TEST_URL")


def _pg_guard(monkeypatch):
    """Point the suite at the expendable PostgreSQL target, loudly."""
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", _PG_URL)
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    expected_db = urlparse(_PG_URL).path.strip("/")
    assert expected_db, (
        "refusing the reset: ERPCLAW_PG_TEST_URL names no database")
    assert get_dialect() == "postgresql", (
        "the PostgreSQL leg resolved to %r" % get_dialect())
    probe = get_connection(_PG_URL)
    try:
        actual_db = probe.execute("SELECT current_database()").fetchone()[0]
        assert actual_db == expected_db, (
            "refusing the reset: connected to %r, URL names %r"
            % (actual_db, expected_db))
        probe.execute("DROP SCHEMA IF EXISTS public CASCADE")
        probe.execute("CREATE SCHEMA public")
        probe.commit()
    finally:
        probe.close()
    seam.dispose_engines()
    return expected_db


def _pg_init():
    spec = importlib.util.spec_from_file_location(
        "init_schema_pg", _INIT_SCHEMA_PATH)
    init_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(init_mod)
    init_mod.init_db(None)


@pytest.mark.skipif(
    not _PG_URL,
    reason="ERPCLAW_PG_TEST_URL not set (live Postgres required; the PG lane "
           "runs on the box leg)")
def test_pg_upgrade_matches_fresh(monkeypatch):
    """Expendable database only: the shared public schema is reset, so never point ERPCLAW_PG_TEST_URL at real data."""
    _pg_guard(monkeypatch)
    _pg_init()
    assert get_dialect() == "postgresql"
    _upgrade_case(_PG_URL)
    seam.dispose_engines()
    _pg_guard(monkeypatch)
    _pg_init()
    _fresh_equals_upgraded_case(_PG_URL)
    seam.dispose_engines()
    _pg_guard(monkeypatch)
    _pg_init()
    _rerun_case(_PG_URL)
    seam.dispose_engines()


@pytest.mark.skipif(
    not _PG_URL,
    reason="ERPCLAW_PG_TEST_URL not set (live Postgres required; the PG lane "
           "runs on the box leg)")
def test_pg_constraints(monkeypatch):
    """Expendable database only: the shared public schema is reset, so never point ERPCLAW_PG_TEST_URL at real data."""
    _pg_guard(monkeypatch)
    _pg_init()
    assert get_dialect() == "postgresql"
    _partial_unique_case(_PG_URL)
    _deployment_cases(_PG_URL)
    _deployment_account_text_cases(_PG_URL)
    _deployment_expected_cases(_PG_URL)
    _deployment_recorded_cases(_PG_URL)
    _credential_scheme_cases(_PG_URL)
    _credential_iteration_cases(_PG_URL)
    _credential_uppercase_cases(_PG_URL)
    _credential_length_cases(_PG_URL)
    _credential_principal_cases(_PG_URL)
    _credential_retired_cases(_PG_URL)
    _credential_live_cases(_PG_URL)
    _session_digest_cases(_PG_URL)
    _session_window_cases(_PG_URL)
    _session_lifetime_cases(_PG_URL)
    _session_revoked_cases(_PG_URL)
    _session_route_cases(_PG_URL)
    _challenge_digest_cases(_PG_URL)
    _challenge_state_cases(_PG_URL)
    _challenge_combo_cases(_PG_URL)
    _challenge_window_cases(_PG_URL)
    _challenge_lifetime_cases(_PG_URL)
    _challenge_issued_cases(_PG_URL)
    _issuer_missing_cases(_PG_URL)
    _issuer_digest_cases(_PG_URL)
    seam.dispose_engines()


@pytest.mark.skipif(
    not _PG_URL,
    reason="ERPCLAW_PG_TEST_URL not set (live Postgres required; the PG lane "
           "runs on the box leg)")
def test_pg_refusals(monkeypatch):
    """Expendable database only: the shared public schema is reset, so never point ERPCLAW_PG_TEST_URL at real data."""
    _pg_guard(monkeypatch)
    assert get_dialect() == "postgresql"
    import pytest as _pytest
    with _pytest.raises(RuntimeError) as excinfo:
        _mig().run_migration(_PG_URL)
    assert "ENVELOPE_ABSENT" in str(excinfo.value)
    _pg_init()
    _refusals_case(_PG_URL)
    seam.dispose_engines()
