"""Core books: the schema and the posting service."""
from __future__ import annotations

import threading
import time
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, inspect, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from erpclaw_core import ledger
from erpclaw_core import models as m
from erpclaw_core.common import Refusal, company_ctx, new_id
from erpclaw_core.money import MoneyError, to_amount, to_decimal
from erpclaw_core.setup import create_company

D = date(2026, 10, 1)



def plugin_heads() -> list[str]:
    """Every registered plugin's branch head, read from its own revisions (never a literal, so a new plugin or a new
    revision needs no edit here). A plugin that owns tables has exactly one head, or this raises: deriving the list
    never hides a missing branch. A plugin with no tables has no branch."""
    from erpclaw_core.migrate import branch_scripts
    from erpclaw_core.plugins import REGISTRY, load_plugins
    load_plugins()
    heads = []
    for name in REGISTRY.order:
        p = REGISTRY.plugins[name]
        if name == "core" or (not p.manifest.owned_tables and not p.migrations):
            continue
        found = [sc.revision for sc in branch_scripts(p.migrations) if sc.is_head] if p.migrations else []
        if len(found) != 1:
            raise AssertionError(f"plugin {name} owns tables but has {len(found)} branch heads")
        heads.append(found[0])
    return heads

def acc(ctx, number):
    return ledger.account_by_number(ctx, number)["id"]


def lines(ctx, amount_d, amount_c, d_acc="1100", c_acc="4100"):
    return [ledger.Line(acc(ctx, d_acc), debit=Decimal(amount_d)), ledger.Line(acc(ctx, c_acc), credit=Decimal(amount_c))]


def customer(ctx):
    return ctx.conn.execute(select(m.party.c.id).where(m.party.c.company_id == ctx.company_id,
                                                       m.party.c.kind == "customer")).scalars().first()


def counts(conn):
    return (conn.execute(select(func.count()).select_from(m.posting)).scalar_one(),
            conn.execute(select(func.count()).select_from(m.ledger_entry)).scalar_one(),
            conn.execute(select(m.posting_chain_head.c.last_seq, m.posting_chain_head.c.last_hash)).all())


# ---------------------------------------------------------------- money parsing

@pytest.mark.parametrize("value", [0.1, True, None, "1e3", "1,000.00", "+5", ".5", "05", "NaN", "Infinity",
                                   "1.234", "-1.00", Decimal("1E+15"), "1000000000000000000000000000000.01"])
def test_01_amount_parse_refusals(value):
    with pytest.raises(MoneyError):
        to_amount(value, "USD")


def test_01b_amount_parse_accepts_canonical_forms():
    assert to_amount("5", "USD") == Decimal("5.00") and to_amount(Decimal("1.50"), "USD") == Decimal("1.50")
    assert to_amount("999999999999999.99", "USD") == Decimal("999999999999999.99")
    assert to_amount("1500", "JPY") == Decimal("1500") and to_amount("12.3", "KWD") == Decimal("12.300")
    assert str(to_amount(Decimal("-0.00"), "USD", allow_negative=True)) == "0.00"
    with pytest.raises(MoneyError):
        to_decimal(0.1)


# ---------------------------------------------------------------- the checks, in order (anti-vacuity)

def test_02_balanced_posting_extends_the_chain(engine):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        before = conn.execute(select(m.posting_chain_head.c.last_seq)).scalar_one()
        r = ledger.post(ctx, D, "journal", new_id(), lines(ctx, "100.00", "100.00"))
        assert r.seq == before + 1
        assert ledger.trial_balance_difference(ctx) == 0
        assert ledger.verify_chain(conn, ctx.company_id) is None


def refusal_cases():
    def frozen(ctx):
        ctx.conn.execute(update(m.account).where(m.account.c.id == acc(ctx, "1100")).values(frozen=True))
        return lines(ctx, "1.00", "1.00")

    def locked(ctx):
        ledger.set_period_status(ctx, ledger.find_period(ctx, D)["id"], "locked")
        return lines(ctx, "1.00", "1.00")

    return [
        ("too_few_lines", lambda ctx: lines(ctx, "1.00", "1.00")[:1]),
        ("group_account", lambda ctx: lines(ctx, "1.00", "1.00", c_acc="4000")),
        ("frozen_account", frozen),
        ("receivable_needs_party", lambda ctx: lines(ctx, "1.00", "1.00", d_acc="1200")),
        ("party_not_allowed", lambda ctx: [ledger.Line(acc(ctx, "1100"), debit=Decimal("1.00"), party_id=customer(ctx)),
                                           ledger.Line(acc(ctx, "4100"), credit=Decimal("1.00"))]),
        ("bad_amount", lambda ctx: lines(ctx, "1.234", "1.234")),
        ("bad_amount", lambda ctx: [ledger.Line(acc(ctx, "1100"), debit=0.5), ledger.Line(acc(ctx, "4100"), credit=0.5)]),
        ("one_side_per_line", lambda ctx: lines(ctx, "0.00", "0.00")),
        ("one_side_per_line", lambda ctx: [ledger.Line(acc(ctx, "1100"), debit=Decimal("1.00"), credit=Decimal("1.00")),
                                           ledger.Line(acc(ctx, "4100"), debit=Decimal("1.00"), credit=Decimal("1.00"))]),
        ("unbalanced", lambda ctx: lines(ctx, "100.00", "99.99")),
        ("period_closed", locked),
    ]


@pytest.mark.parametrize("code,make", refusal_cases(), ids=[c for c, _ in refusal_cases()])
def test_03_each_check_refuses_and_writes_nothing(engine, code, make):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        before = counts(conn)
        bad = make(ctx)
        with pytest.raises(Refusal) as e:
            ledger.post(ctx, D, "journal", new_id(), bad)
        assert e.value.code == code
        assert counts(conn) == before


def test_03b_no_period_and_duplicate_voucher_refused(engine):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        with pytest.raises(Refusal) as e:
            ledger.post(ctx, date(2031, 1, 1), "journal", new_id(), lines(ctx, "1.00", "1.00"))
        assert e.value.code == "no_period"
        vid = new_id()
        ledger.post(ctx, D, "journal", vid, lines(ctx, "1.00", "1.00"))
        with pytest.raises(Refusal) as e:
            ledger.post(ctx, D, "journal", vid, lines(ctx, "1.00", "1.00"))
        assert e.value.code == "duplicate_voucher"


def test_04_rounding_residue_is_an_explicit_line_and_bounded(engine):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        rounded = [ledger.Line(acc(ctx, "1100"), debit=Decimal("10.00")),
                   ledger.Line(acc(ctx, "4100"), credit=Decimal("3.33")),
                   ledger.Line(acc(ctx, "4100"), credit=Decimal("3.33")),
                   ledger.Line(acc(ctx, "4100"), credit=Decimal("3.33"))]
        balanced = ledger.balance_with_rounding(ctx, rounded)
        assert balanced[-1].account_id == ledger.account_by_type(ctx, "rounding")["id"]
        assert balanced[-1].credit == Decimal("0.01")
        ledger.post(ctx, D, "journal", new_id(), balanced)
        with pytest.raises(Refusal):
            ledger.balance_with_rounding(ctx, lines(ctx, "10.00", "9.00"))


# ---------------------------------------------------------------- the database enforces it too

def test_05_company_isolation_in_the_database(engine):
    other = create_company(engine, "Other Co")
    with engine.begin() as conn:
        ctx = company_ctx(conn, company_name="Other Co")
        ledger.post(ctx, D, "journal", new_id(), lines(ctx, "5.00", "5.00"))
    with engine.connect() as conn:
        main = company_ctx(conn, company_name=conn.execute(
            select(m.company.c.name).where(m.company.c.id != other)).scalar_one())
        post_id = conn.execute(select(m.posting.c.id).where(m.posting.c.company_id == main.company_id)).scalars().first()
        foreign_account = conn.execute(select(m.account.c.id).where(m.account.c.company_id == other,
                                                                    m.account.c.number == "1100")).scalar_one()
        main_sales = acc(main, "4100")
        conn.commit()
        with pytest.raises(IntegrityError, match="fk_entry_account"):
            with conn.begin():
                conn.execute(m.ledger_entry.insert().values(
                    id=new_id(), company_id=main.company_id, posting_id=post_id, line_no=99,
                    account_id=foreign_account, debit=Decimal("1.00"), credit=Decimal("0.00"), currency="USD",
                    rate=Decimal("1"), functional_currency="USD", functional_debit=Decimal("1.00"),
                    functional_credit=Decimal("0.00"), kind="measured", dimension_set_id=None))
        # The posting service refuses it before the database has to.
        with conn.begin():
            with pytest.raises(Refusal) as e:
                ledger.post(main, D, "journal", new_id(), [ledger.Line(foreign_account, debit=Decimal("1.00")),
                                                           ledger.Line(main_sales, credit=Decimal("1.00"))])
            assert e.value.code == "foreign_account"
        assert ledger.verify_chain(conn, other) is None and ledger.verify_chain(conn, main.company_id) is None


@pytest.mark.parametrize("table", ["posting", "ledger_entry"])
@pytest.mark.parametrize("sql", ["UPDATE {t} SET company_id = company_id", "DELETE FROM {t}"])
def test_06_append_only_tables_refuse_update_and_delete(engine, table, sql):
    with engine.connect() as conn:
        with pytest.raises(DBAPIError):
            with conn.begin():
                conn.execute(text(sql.format(t=table)))
        with conn.begin():
            assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() > 0  # the rows are still there


@pytest.mark.parametrize("debit,credit", [("5", "0.00"), ("5.000", "0.00"), ("-1.00", "0.00"), ("0.00", "0.00"),
                                          ("1.00", "1.00"), ("1e2", "0.00"), (" 1.00", "0.00")])
def test_07_money_checks_refuse_raw_writes(engine, debit, credit):
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        post_id = conn.execute(select(m.posting.c.id)).scalars().first()
        bank = acc(ctx, "1100")
        conn.commit()
        with pytest.raises(DBAPIError):
            with conn.begin():
                conn.execute(text("INSERT INTO ledger_entry (id, company_id, posting_id, line_no, account_id, "
                                  "party_id, debit, credit, currency) VALUES (:i, :c, :p, 98, :a, NULL, :d, :cr, 'USD')"),
                             {"i": new_id(), "c": ctx.company_id, "p": post_id, "a": bank,
                              "d": debit, "cr": credit})


# ---------------------------------------------------------------- reversal and as-of dates

def test_08_reversal_mirrors_and_never_touches_the_original(engine):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        bank = acc(ctx, "1100")
        r = ledger.post(ctx, date(2026, 10, 5), "journal", new_id(), lines(ctx, "70.00", "70.00"))
        raw = text("SELECT * FROM ledger_entry WHERE posting_id = :p ORDER BY line_no")
        before = [tuple(x) for x in conn.execute(raw, {"p": r.posting_id})]
        base = ledger.balance(ctx, bank, date(2026, 10, 4))
        rev = ledger.reverse(ctx, r.posting_id, date(2026, 10, 20))
        assert [tuple(x) for x in conn.execute(raw, {"p": r.posting_id})] == before
        assert ledger.balance(ctx, bank, date(2026, 10, 4)) == base
        assert ledger.balance(ctx, bank, date(2026, 10, 19)) == base + Decimal("70.00")
        assert ledger.balance(ctx, bank, date(2026, 10, 20)) == base
        for bad, code in ((r.posting_id, "already_reversed"), (rev.posting_id, "reversal_of_reversal")):
            with pytest.raises(Refusal) as e:
                ledger.reverse(ctx, bad, date(2026, 10, 21))
            assert e.value.code == code
        r2 = ledger.post(ctx, date(2026, 10, 10), "journal", new_id(), lines(ctx, "1.00", "1.00"))
        with pytest.raises(Refusal) as e:
            ledger.reverse(ctx, r2.posting_id, date(2026, 10, 9))
        assert e.value.code == "reversal_before_original"
        assert ledger.verify_chain(conn, ctx.company_id) is None


# ---------------------------------------------------------------- hash chain

def _bypass_triggers(conn, table):
    conn.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER USER"))


def test_09_chain_detects_a_changed_row_but_states_its_limit(engine):
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        conn.commit()
        with conn.begin():
            for i in range(3):
                ledger.post(ctx, D, "journal", new_id(), lines(ctx, f"{i + 1}.00", f"{i + 1}.00"))
            assert ledger.verify_chain(conn, ctx.company_id) is None
        target = conn.execute(select(m.posting.c.seq).where(m.posting.c.company_id == ctx.company_id)
                              .order_by(m.posting.c.seq.desc())).scalars().all()[1]
        conn.commit()
        trans = conn.begin()
        _bypass_triggers(conn, "posting")
        conn.execute(text("UPDATE posting SET memo = 'edited' WHERE seq = :s AND company_id = :c"),
                     {"s": target, "c": ctx.company_id})
        assert ledger.verify_chain(conn, ctx.company_id) == target
        trans.rollback()  # the test database keeps its triggers


def test_10_rollback_leaves_no_gap(engine):
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        start = conn.execute(select(m.posting_chain_head.c.last_seq)).scalar_one()
        conn.commit()
        trans = conn.begin()
        ledger.post(ctx, D, "journal", new_id(), lines(ctx, "1.00", "1.00"))
        trans.rollback()
        with conn.begin():
            r = ledger.post(ctx, D, "journal", new_id(), lines(ctx, "1.00", "1.00"))
        assert r.seq == start + 1 and ledger.verify_chain(conn, ctx.company_id) is None


# ---------------------------------------------------------------- concurrency

def _run_threads(n, fn):
    errors: list[BaseException] = []

    def wrap(i):
        try:
            fn(i)
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=wrap, args=(i,)) for i in range(n)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    return errors


def test_11_concurrent_posts_into_different_periods_keep_the_chain(engine):
    create_company(engine, "Second Co")
    with engine.connect() as conn:
        names = conn.execute(select(m.company.c.name)).scalars().all()
        start = {r.company_id: r.last_seq for r in conn.execute(select(m.posting_chain_head))}

    def one(i):
        with engine.begin() as conn:
            ctx = company_ctx(conn, company_name=names[i % len(names)])
            ledger.post(ctx, date(2026, 1 + i % 12, 1), "journal", new_id(), lines(ctx, "2.00", "2.00"))

    errors = _run_threads(16, one)
    assert not errors, errors
    with engine.connect() as conn:
        for cid, seq0 in start.items():
            seqs = conn.execute(select(m.posting.c.seq).where(m.posting.c.company_id == cid)
                                .order_by(m.posting.c.seq)).scalars().all()
            assert seqs == list(range(1, len(seqs) + 1)) and len(seqs) == seq0 + 8
            assert ledger.verify_chain(conn, cid) is None


def test_12_close_versus_post_race(engine, monkeypatch):
    # Widen the window between the period check and the write, so a missing
    # lock would let the close commit inside it (checked by a mutation run).
    checks = ledger._check_lines

    def slow_checks(ctx, lines_):
        time.sleep(0.03)
        return checks(ctx, lines_)

    monkeypatch.setattr(ledger, "_check_lines", slow_checks)
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        period = ledger.find_period(ctx, date(2026, 11, 1))
    snapshot: list[set] = []
    refused: list[str] = []

    def poster(i):
        for _ in range(6):
            try:
                with engine.begin() as conn:
                    c = company_ctx(conn)
                    ledger.post(c, date(2026, 11, 3), "journal", new_id(), lines(c, "1.00", "1.00"))
            except Refusal as exc:
                refused.append(exc.code)

    def closer(_):
        time.sleep(0.05)  # posters are mid-posting by now
        with engine.begin() as conn:
            c = company_ctx(conn)
            ledger.set_period_status(c, period["id"], "locked")
            snapshot.append(set(conn.execute(select(m.posting.c.id).where(
                m.posting.c.company_id == c.company_id, m.posting.c.posting_date == date(2026, 11, 3))).scalars()))

    errors = _run_threads(4, lambda i: closer(i) if i == 0 else poster(i))
    assert not errors, errors
    assert set(refused) <= {"period_closed"}
    with engine.connect() as conn:
        after = set(conn.execute(select(m.posting.c.id).where(m.posting.c.posting_date == date(2026, 11, 3))).scalars())
    assert after == snapshot[0]  # nothing committed into the period after its lock


# ---------------------------------------------------------------- migrations

def test_13_migrated_schema_matches_the_models(engine):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from alembic import command

    from erpclaw_core.db import metadata
    from erpclaw_core.migrate import CORE_VERSIONS, config, core_head
    from erpclaw_core.plugins import REGISTRY

    # every loaded plugin's branch, applied here on purpose: new books install only some (test_plugin_branches)
    branches = [REGISTRY.plugins[n].migrations for n in REGISTRY.order if REGISTRY.plugins[n].migrations]
    with engine.begin() as conn:
        command.upgrade(config(conn, locations=[CORE_VERSIONS, *branches]), "heads")
    with engine.connect() as conn:  # core's tables and every plugin's, each made by its own revisions
        mc = MigrationContext.configure(conn, opts={
            "include_object": lambda obj, name, type_, reflected, compare_to:
                name in metadata.tables if type_ == "table" else True,
            "compare_type": True})
        diff = compare_metadata(mc, metadata)
        assert diff == []
        assert set(inspect(conn).get_table_names()) == set(metadata.tables) | {"alembic_version"}
        rows = sorted(conn.execute(text("SELECT version_num FROM alembic_version")).scalars())
        assert rows == sorted([core_head(), *plugin_heads()])  # one row per head: core's and each plugin branch's
        assert {r[0] for r in conn.execute(select(m.currency.c.code))} >= {"USD", "EUR", "JPY", "KWD"}


def test_06d_postgresql_truncate_is_refused(engine):
    """TRUNCATE fires no row triggers, so a statement trigger refuses it on
    both ledger tables."""
    count = "SELECT (SELECT count(*) FROM posting), (SELECT count(*) FROM ledger_entry)"
    with engine.connect() as conn:
        before = tuple(conn.execute(text(count)).one())
        conn.rollback()
        assert min(before) > 0
        # The second names every table that references posting, so no foreign key refuses it first.
        for sql in ("TRUNCATE ledger_entry, supplier_payable, supplier_payment_allocation",
                    "TRUNCATE posting, ledger_entry, fiscal_year_close, supplier_payable, supplier_payment_allocation",
                    "TRUNCATE posting CASCADE"):
            with pytest.raises(DBAPIError, match="TRUNCATE refused"):
                with conn.begin():
                    conn.execute(text(sql))
        assert tuple(conn.execute(text(count)).one()) == before


def _upsert_and_merge(t: str) -> list[str]:
    return [f"INSERT INTO {t} SELECT * FROM {t} LIMIT 1 ON CONFLICT (id) DO UPDATE SET company_id = EXCLUDED.company_id",
            f"MERGE INTO {t} USING (SELECT id FROM {t} LIMIT 1) AS s ON {t}.id = s.id "
            f"WHEN MATCHED THEN UPDATE SET company_id = {t}.company_id",
            f"MERGE INTO {t} USING (SELECT id FROM {t} LIMIT 1) AS s ON {t}.id = s.id WHEN MATCHED THEN DELETE"]


@pytest.mark.parametrize("table", ["posting", "ledger_entry"])
def test_06e_upsert_and_merge_cannot_change_a_row(engine, table):
    """Every other statement that changes or removes existing rows (upsert,
    MERGE) is refused, and the table is unchanged."""
    with engine.connect() as conn:
        snapshot = f"SELECT * FROM {table} ORDER BY id"
        before = [tuple(r) for r in conn.execute(text(snapshot))]
        conn.rollback()
        for sql in _upsert_and_merge(table):
            with pytest.raises(DBAPIError, match="refused"):
                with conn.begin():
                    conn.execute(text(sql))
        assert [tuple(r) for r in conn.execute(text(snapshot))] == before
