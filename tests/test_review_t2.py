"""Regressions for the engine skeleton review."""
from decimal import Decimal

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from conftest import _check_pg_test_url


@pytest.mark.parametrize("url", [
    "postgresql+psycopg://u:p@127.0.0.1/safe_test?dbname=books",
    "postgresql+psycopg://u:p@127.0.0.1/books?application_name=/looks_test",
    "postgresql+psycopg://u:p@127.0.0.1/x_test?host=elsewhere",
])
def test_pg_test_url_overrides_refused(url):
    with pytest.raises(RuntimeError):
        _check_pg_test_url(url)


def test_float_amount_refused_before_coercion():
    from erpclaw_sales import RecordPayment

    with pytest.raises(ValidationError):
        RecordPayment(customer="Acme", amount=0.1)
    assert RecordPayment(customer="Acme", amount="0.10").amount == Decimal("0.10")


def _runtime_url(url: str) -> str:
    """The same database under another role's name: the runtime URL beside the owner's."""
    from sqlalchemy.engine import make_url

    return make_url(url).set(username="erpclaw_rt_other").render_as_string(hide_password=False)


def test_init_never_writes_into_foreign_books(monkeypatch):
    from erpclaw_cli.main import app
    from conftest import drop_everything, empty_books

    eng = empty_books()
    try:
        with eng.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE gl_entry (id text)")
        url = eng.url.render_as_string(hide_password=False)
        monkeypatch.setenv("ERPCLAW_DATABASE_URL", _runtime_url(url))
        monkeypatch.setenv("ERPCLAW_MIGRATION_URL", url)
        result = CliRunner().invoke(app, ["init"])
        assert result.exit_code != 0
        with eng.connect() as conn:
            assert conn.exec_driver_sql("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()").scalars().all() == ["gl_entry"]
    finally:
        drop_everything(eng)
        eng.dispose()


def test_init_hides_the_password(monkeypatch):
    from erpclaw_cli import main

    monkeypatch.setenv("ERPCLAW_DATABASE_URL", "postgresql+psycopg://u:q7z@127.0.0.1/x_test")
    monkeypatch.setenv("ERPCLAW_MIGRATION_URL", "postgresql+psycopg://owner:k3w@127.0.0.1/x_test")
    monkeypatch.setattr("erpclaw_sales.demo.seed_demo", lambda eng: None)
    monkeypatch.setattr("erpclaw_core.db.make_engine", lambda url=None: _NoBooks())
    monkeypatch.setattr("erpclaw_core.db.holds_tables", lambda eng: False)
    monkeypatch.setattr("erpclaw_core.grants.apply_grants", lambda conn, role: None)
    monkeypatch.setattr("erpclaw_core.administration.issue_token", lambda conn, name: "owner-token")
    result = CliRunner().invoke(main.app, ["init"])
    assert result.exit_code == 0, result.output
    assert "q7z" not in result.output and "k3w" not in result.output


class _NoBooks:
    def begin(self):
        from contextlib import nullcontext
        return nullcontext()


def test_posted_books_refuse_a_currency_change_and_keep_the_existing_card(engine):
    """The format-2 FK protects posted amounts; failure leaves the original card untouched."""
    from sqlalchemy import select, update
    from sqlalchemy.exc import IntegrityError

    from erpclaw_core import approvals, models as m
    from erpclaw_core.plugins import REGISTRY

    contract = REGISTRY.contracts["record_payment"]
    out = approvals.propose(engine, contract, {"customer": "Pied Piper", "amount": "100.00",
                                               "oldest_first": True, "method": "check"})
    assert out.status == "card"
    with engine.connect() as conn:
        card = conn.execute(select(m.approval).where(m.approval.c.id == out.approval_id)).mappings().one()
        cid = card["company_id"]
        currency = conn.execute(select(m.company.c.functional_currency).where(m.company.c.id == cid)).scalar_one()
        postings = conn.execute(select(m.posting).where(m.posting.c.company_id == cid).order_by(m.posting.c.id)).mappings().all()
        entries = conn.execute(select(m.ledger_entry).where(m.ledger_entry.c.company_id == cid).order_by(m.ledger_entry.c.id)).mappings().all()
        head = conn.execute(select(m.posting_chain_head).where(m.posting_chain_head.c.company_id == cid)).mappings().one()
        assert currency == "USD" and postings and entries
    with pytest.raises(IntegrityError, match="fk_entry_functional_currency"):
        with engine.begin() as conn:
            conn.execute(update(m.company).where(m.company.c.id == cid).values(functional_currency="INR"))
    with engine.connect() as conn:
        assert conn.execute(select(m.company.c.functional_currency).where(m.company.c.id == cid)).scalar_one() == currency
        assert conn.execute(select(m.approval).where(m.approval.c.id == out.approval_id)).mappings().one() == card
        assert conn.execute(select(m.posting).where(m.posting.c.company_id == cid).order_by(m.posting.c.id)).mappings().all() == postings
        assert conn.execute(select(m.ledger_entry).where(m.ledger_entry.c.company_id == cid).order_by(m.ledger_entry.c.id)).mappings().all() == entries
        assert conn.execute(select(m.posting_chain_head).where(m.posting_chain_head.c.company_id == cid)).mappings().one() == head


def test_currency_change_in_unposted_books_makes_a_genuine_card_stale(engine, monkeypatch):
    """When a change is legal, a card drafted in the old currency still cannot commit."""
    from dataclasses import replace
    from sqlalchemy import select, update

    from erpclaw_core import approvals, models as m
    from erpclaw_core.contracts import Result
    from erpclaw_core.setup import create_company
    from service_plugins import install
    from test_moves_money import PLANTED
    from test_plugins import plug

    calls = []

    def commit_sentinel(ctx, inp, key):
        calls.append(key)
        return Result("Sentinel committed.")

    contract = replace(PLANTED, commit=commit_sentinel)
    install(monkeypatch, engine, plug("nomoney", contracts=[contract]))
    cid = create_company(engine, "Unposted Currency Test", currency="USD", years=range(2026, 2027))
    with engine.connect() as conn:
        assert not conn.execute(select(m.ledger_entry.c.id).where(m.ledger_entry.c.company_id == cid)).all()
    out = approvals.propose(engine, contract, {"act": "nothing"}, company_id=cid)
    assert out.status == "card"
    with engine.begin() as conn:
        conn.execute(update(m.company).where(m.company.c.id == cid).values(functional_currency="INR"))
    res = approvals.approve(engine, out.approval_id, out.digest)
    assert res.status == "superseded", (res.status, res.text)
    assert calls == []
    assert res.card is not None and res.card["currency"] == "INR"
    with engine.connect() as conn:
        assert conn.execute(select(m.company.c.functional_currency).where(m.company.c.id == cid)).scalar_one() == "INR"
        assert conn.execute(select(m.approval.c.status).where(m.approval.c.id == out.approval_id)).scalar_one() == "superseded"
        assert not conn.execute(select(m.operation_outcome.c.key).where(
            m.operation_outcome.c.approval_id == out.approval_id)).all()
        assert not conn.execute(select(m.ledger_entry.c.id).where(m.ledger_entry.c.company_id == cid)).all()


def test_init_refuses_books_that_already_hold_tables(engine, monkeypatch):
    from erpclaw_cli.main import app

    url = engine.url.render_as_string(hide_password=False)
    monkeypatch.setenv("ERPCLAW_DATABASE_URL", _runtime_url(url))
    monkeypatch.setenv("ERPCLAW_MIGRATION_URL", url)
    with engine.connect() as conn:
        before = conn.exec_driver_sql("SELECT count(*) FROM company").scalar_one()
    result = CliRunner().invoke(app, ["init"])
    assert result.exit_code == 1 and "already holds books" in " ".join(result.output.split())
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT count(*) FROM company").scalar_one() == before


def test_safe_url_drops_query_credentials():
    from erpclaw_core.db import safe_url

    shown = safe_url("postgresql+psycopg://u:pw1@127.0.0.1:5432/x_test?password=qpw2&sslpassword=qpw3")
    assert "pw1" not in shown and "qpw2" not in shown and "qpw3" not in shown and "x_test" in shown


@pytest.mark.parametrize("kwargs", [{"k": 0}, {"k": 1, "only": ["no-such-case"]}])
def test_eval_refuses_empty_runs(kwargs):
    from erpclaw_cli.evals import run

    assert run.main(**kwargs) == 2


@pytest.mark.parametrize("option", ["dbname", "user", "service", "options", "host"])
def test_an_eval_server_url_naming_other_books_is_refused_and_they_stay_unchanged(engine, option, monkeypatch):
    """A server URL carrying ?dbname=<existing books> made the eval seed demo data and grant
    privileges inside those books. Only transport and timeout options pass; nothing is created otherwise: the
    refusal comes before the eval opens any connection or makes any database, whatever other runs on the same
    server are doing."""
    from conftest import PG_URL
    from erpclaw_cli.evals import run as ev

    def never(*args, **kwargs):
        raise AssertionError("the eval reached the server before refusing its URL")

    monkeypatch.setattr(ev, "create_engine", never)
    monkeypatch.setattr(ev.EvalBooks, "_create", never)
    before = ev.snapshot(engine)
    value = {"dbname": engine.url.database, "user": "postgres", "service": "books", "options": "-c search_path=x",
             "host": "127.0.0.1"}[option]
    sep = "&" if "?" in PG_URL else "?"
    with pytest.raises(ev.EvalRefused, match=option):
        ev.EvalBooks(f"{PG_URL}{sep}{option}={value}")
    assert ev.snapshot(engine) == before


def test_an_eval_connection_that_lands_elsewhere_is_refused_before_seeding(monkeypatch):
    """The second guard: whatever the URL says, the server must report the eval's own database before seeding."""
    from conftest import PG_URL
    from erpclaw_cli.evals import run as ev

    books = ev.EvalBooks(PG_URL)
    real = books._url
    monkeypatch.setattr(books, "_url", lambda database, runtime=False: real("postgres", runtime))
    try:
        with pytest.raises(ev.EvalRefused, match="not the eval's own"):
            books.seed()
    finally:
        books.close()


def test_the_eval_books_are_disposable_postgresql_databases(monkeypatch):
    """Each eval run seeds its own database, copies it per trial, opens each copy as a runtime login, and drops
    every database and the role at the end."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    from conftest import PG_URL
    from erpclaw_api.app import create_app
    from erpclaw_assistant.model import ScriptedModel
    from erpclaw_cli.evals.run import EvalBooks, snapshot

    monkeypatch.delenv("ERPCLAW_ALLOW_OWNER_RUNTIME")  # the eval connects as a genuine runtime login
    books = EvalBooks(PG_URL)
    admin = create_engine(PG_URL, poolclass=NullPool)

    def present():
        with admin.connect() as c:
            dbs = set(c.exec_driver_sql("SELECT datname FROM pg_database WHERE datname LIKE %(p)s",
                                        {"p": books.prefix + "%"}).scalars())
            role = c.exec_driver_sql("SELECT 1 FROM pg_roles WHERE rolname = %(r)s", {"r": books.role}).first()
        return dbs, role is not None

    try:
        books.seed()
        first = books.trial()
        assert first.url.username == books.role and first.url.database != books.pristine
        assert create_app(first, ScriptedModel([]), embedded_owner=True)  # the runtime role checks pass
        with first.begin() as conn:
            conn.exec_driver_sql("INSERT INTO party (id, company_id, kind, name) SELECT 'probe', id, 'customer', "
                                 "'Probe Ltd' FROM company LIMIT 1")  # a trial's change stays in its copy
        books.finish(first)
        second = books.trial()
        assert all(r["name"] != "Probe Ltd" for r in snapshot(second)["party"].values())
        assert present() == ({books.pristine, second.url.database}, True)
        second.dispose()
    finally:
        books.close()
    assert present() == (set(), False)
    admin.dispose()


def test_the_eval_needs_a_postgresql_server(monkeypatch):
    from erpclaw_cli.evals import run

    monkeypatch.delenv("ERPCLAW_EVAL_PG_URL", raising=False)
    monkeypatch.delenv("ERPCLAW_PG_TEST_URL")
    assert run.server_url() is None and run.main(k=1) == 2


def test_a_trial_that_fails_mid_turn_is_still_dropped_by_a_createdb_createrole_login(monkeypatch):
    """A model timeout left the trial's engine open, and the eval's own non-superuser login (the
    documented CREATEDB and CREATEROLE) could not end the runtime role's session, so its database and the role
    stayed behind and the timeout was masked."""
    import secrets as _secrets

    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from sqlalchemy.pool import NullPool

    from conftest import PG_URL
    from erpclaw_cli.evals import run as ev

    class ModelTimedOut(BaseException):
        pass

    class TimingOut:
        name = "timing-out"

        def complete(self, messages, tools):
            raise ModelTimedOut("the model did not answer in time")

    monkeypatch.delenv("ERPCLAW_ALLOW_OWNER_RUNTIME")
    monkeypatch.setenv("ERPCLAW_MAIL_DIR", "/nonexistent")
    login, password = f"erpclaw_evaladm_{_secrets.token_hex(3)}", _secrets.token_hex(12)
    root = create_engine(PG_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    with root.connect() as c:
        c.exec_driver_sql(f"CREATE ROLE \"{login}\" LOGIN CREATEDB CREATEROLE PASSWORD '{password}'")
    url = make_url(PG_URL).set(username=login, password=password).render_as_string(hide_password=False)
    books = ev.EvalBooks(url)
    try:
        books.seed()
        try:
            ev.run_trial(ev.CASES[0], books, TimingOut())
        except ModelTimedOut:
            pass  # the failure itself reaches the caller, not a cleanup error in its place
        books.close()
        with root.connect() as c:
            left = c.exec_driver_sql("SELECT count(*) FROM pg_database WHERE datname LIKE %(p)s",
                                     {"p": books.prefix + "%"}).scalar()
            role = c.exec_driver_sql("SELECT count(*) FROM pg_roles WHERE rolname = %(r)s", {"r": books.role}).scalar()
        assert (left, role) == (0, 0)
    finally:
        with root.connect() as c:
            for name in c.exec_driver_sql("SELECT datname FROM pg_database WHERE datname LIKE %(p)s",
                                          {"p": books.prefix + "%"}).scalars().all():
                c.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            c.exec_driver_sql(f'DROP ROLE IF EXISTS "{books.role}"')
            c.exec_driver_sql(f'DROP ROLE IF EXISTS "{login}"')
        root.dispose()
