"""The company date order: set at init (a flag or the question), unset by default, carried by the company's context,
changed later only by the owner's audited command, and never taken from the currency."""
from __future__ import annotations

import json
import re

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from typer.testing import CliRunner

from conftest import cli_urls, drop_everything
from erpclaw_core import models as m
from erpclaw_core.common import company_ctx
from erpclaw_core.setup import create_company

QUESTION = "How do you write dates: month first (10/5 is 5 October) or day first (10/5 is 10 May)? (m/d/skip)"
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def books_id(conn) -> str:
    """The books' identity, read by the test itself: exactly one row."""
    return conn.execute(text("SELECT books_id FROM books_identity")).scalar_one()


def _said(out) -> str:
    return " ".join(out.output.split())


def _order(engine, company: str = "Stark Manufacturing"):
    with engine.connect() as conn:
        return conn.execute(select(m.company.c.date_order).where(m.company.c.name == company)).scalar_one()


@pytest.mark.parametrize("order", ["MDY", "DMY", None])
def test_create_company_takes_the_date_order_and_the_context_carries_it(engine, order):
    kw = {} if order is None else {"date_order": order}
    create_company(engine, "Second Co", **kw)
    assert _order(engine, "Second Co") == order
    with engine.connect() as conn:  # what the resolvers read: the company's context
        assert company_ctx(conn, company_name="Second Co").date_order == order


def test_a_date_order_create_company_does_not_know_is_refused_before_anything_is_written(engine):
    with pytest.raises(ValueError, match="date order"):
        create_company(engine, "Second Co", date_order="YMD")
    with engine.connect() as conn:
        assert conn.execute(select(m.company.c.id).where(m.company.c.name == "Second Co")).first() is None


def test_the_currency_never_sets_the_date_order(engine):
    with engine.connect() as conn:
        currencies = conn.execute(select(m.currency.c.code)).scalars().all()
    assert "USD" in currencies and len(currencies) > 1
    for code in currencies:
        create_company(engine, f"Co {code}", currency=code)
        assert _order(engine, f"Co {code}") is None, code


def test_the_demo_books_leave_the_date_order_unset(engine):
    assert _order(engine) is None
    with engine.connect() as conn:
        assert company_ctx(conn).date_order is None


# --- init -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("args, typed, order", [
    (["--date-order", "MDY"], None, "MDY"),
    (["--date-order", "dmy"], None, "DMY"),
    ([], "m\n", "MDY"),
    ([], "d\n", "DMY"),
    ([], "later\nD\n", "DMY"),  # an answer it does not know is asked again
    ([], "skip\n", None),
    ([], None, None),  # no answer at all (end of input): unset, as skip
])
def test_init_sets_the_date_order_from_the_flag_or_the_question_and_writes_the_books_id(
        engine, runtime_login, monkeypatch, args, typed, order):
    from erpclaw_cli.main import app

    drop_everything(engine)
    cli_urls(monkeypatch, engine, runtime_login)
    out = CliRunner().invoke(app, ["init", *args], input=typed)
    assert out.exit_code == 0, out.output
    assert (QUESTION in _said(out)) == (not args)  # asked only when no flag is given
    assert _order(engine) == order
    with engine.connect() as conn:
        assert UUID.match(books_id(conn))
        audits = conn.execute(select(m.audit_log.c.detail).where(m.audit_log.c.action == "set_date_order")).all()
    assert [json.loads(d) for (d,) in audits] == ([{"from": None, "to": order}] if order else [])
    if order is None:
        assert "No date order is set" in _said(out)


def test_init_refuses_a_date_order_it_does_not_know_before_touching_the_books(monkeypatch, tmp_path):
    from erpclaw_cli.main import app

    monkeypatch.chdir(tmp_path)
    out = CliRunner().invoke(app, ["init", "--date-order", "YMD"])
    assert out.exit_code == 2 and "MDY (month first) or DMY (day first)" in _said(out)
    assert list(tmp_path.iterdir()) == []


# --- changing it later --------------------------------------------------------------------------------------------

def test_company_set_changes_the_date_order_as_the_owner_and_audits_it(engine, runtime_login, monkeypatch):
    from erpclaw_cli.main import app

    cli_urls(monkeypatch, engine, runtime_login)
    with engine.connect() as conn:
        dates_before = conn.execute(text("SELECT id, posting_date FROM posting ORDER BY id")).all()
    for value, want in (("DMY", "DMY"), ("mdy", "MDY"), ("MDY", "MDY")):
        out = CliRunner().invoke(app, ["company", "set", "--date-order", value])
        assert out.exit_code == 0, out.output
        assert _order(engine) == want
    assert "already read month first" in _said(out)
    with engine.connect() as conn:
        rows = conn.execute(select(m.audit_log.c.actor, m.audit_log.c.document_type, m.audit_log.c.detail)
                            .where(m.audit_log.c.action == "set_date_order").order_by(m.audit_log.c.at)).all()
        assert company_ctx(conn).date_order == "MDY"
        assert conn.execute(text("SELECT id, posting_date FROM posting ORDER BY id")).all() == dates_before
    assert [(a, d, json.loads(x)) for a, d, x in rows] == [  # an unchanged setting writes no audit row
        ("owner", "company", {"from": None, "to": "DMY"}), ("owner", "company", {"from": "DMY", "to": "MDY"})]
    out = CliRunner().invoke(app, ["company", "set", "--date-order", "YMD"])
    assert out.exit_code == 2 and _order(engine) == "MDY"


def test_the_runtime_role_cannot_change_the_date_order(engine, runtime_login):
    from erpclaw_core.db import make_engine

    rt = make_engine(runtime_login)
    try:
        # the runtime role holds UPDATE on date_order, guarded by the company trigger: the trigger refuses it
        with engine.connect() as conn:
            assert conn.execute(text("SELECT has_column_privilege(:r, 'company', 'date_order', 'UPDATE')"),
                                {"r": rt.url.username}).scalar()
        with pytest.raises(DBAPIError) as e:
            with rt.begin() as conn:
                conn.execute(text("UPDATE company SET date_order = 'DMY'"))
        assert (e.value.orig.sqlstate, e.value.orig.diag.message_primary) == ("P0001", "The company's name, currency, "
                                                                              "fiscal year start and date order are "
                                                                              "fixed once the books are ready.")
    finally:
        rt.dispose()
    assert _order(engine) is None


def test_while_pending_the_runtime_role_sets_the_date_order_through_the_setup_card(engine, runtime_login):
    """The trigger guards a ready company only; a set_up_company card approved as the runtime role sets it."""
    from sqlalchemy.engine import make_url

    from erpclaw_core import approvals, setup
    from erpclaw_core.db import make_engine
    from erpclaw_core.grants import apply_grants
    from erpclaw_core.plugins import REGISTRY, load_plugins

    drop_everything(engine)
    load_plugins()
    setup.create_own_books(engine)
    with engine.begin() as conn:
        apply_grants(conn, make_url(runtime_login).username)
    rt = make_engine(runtime_login)
    try:
        card = approvals.propose(rt, REGISTRY.contracts["set_up_company"], {
            "name": "Lakeside Bakery", "currency": "USD", "fiscal_year_start_month": 1, "date_order": "DMY"})
        assert approvals.approve(rt, card.approval_id, card.digest).status == "committed"
    finally:
        rt.dispose()
    assert _order(engine, "Lakeside Bakery") == "DMY"
