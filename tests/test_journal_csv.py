"""Journal CSV contract: exact bytes, current grants, read-only SQL and safe publication."""
from __future__ import annotations

import csv
import os
import stat
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import event, func, select
from typer.testing import CliRunner

from conftest import drop_everything, empty_books
from erpclaw_cli.main import app
from erpclaw_core import administration, ledger, models as m, principals
from erpclaw_core.common import Refusal, company_ctx, new_id, now
from erpclaw_core.setup import create_company


HEADER = ("posting_date,seq,voucher_type,voucher_id,memo,line_no,account_number,account_name,party,"
          "debit,credit,currency,rate,functional_currency,functional_debit,functional_credit,kind\n")
EXPECTED = (HEADER +
    "2026-10-01,1,sales,INV-1,inv one,1,1200,Accounts Receivable,Acme,140.00,0.00,USD,1,USD,140.00,0.00,measured\n"
    "2026-10-01,1,sales,INV-1,inv one,2,4100,Sales,,0.00,140.00,USD,1,USD,0.00,140.00,measured\n"
    "2026-10-03,2,cash,RCPT-1,paid,1,1100,Bank,,140.00,0.00,USD,1,USD,140.00,0.00,measured\n"
    "2026-10-03,2,cash,RCPT-1,paid,2,1200,Accounts Receivable,Acme,0.00,140.00,USD,1,USD,0.00,140.00,measured\n"
).encode()


def customer(conn, cid, name="Acme"):
    pid = new_id()
    conn.execute(m.party.insert().values(id=pid, company_id=cid, kind="customer", name=name))
    return pid


def sale(conn, cid, pid, ref="INV-1", day=date(2026, 10, 1), amount="140.00", memo="inv one"):
    ctx = company_ctx(conn, company_id=cid)
    return ledger.post(ctx, day, "sales", ref, [
        ledger.Line(ledger.account_by_number(ctx, "1200")["id"], party_id=pid, debit=Decimal(amount)),
        ledger.Line(ledger.account_by_number(ctx, "4100")["id"], credit=Decimal(amount)),
    ], memo=memo)


@pytest.fixture
def books():
    eng = empty_books()
    cid = create_company(eng, "Example Co")
    with eng.begin() as conn:
        pid = customer(conn, cid)
    yield eng, cid, pid
    drop_everything(eng)
    eng.dispose()


@pytest.fixture
def small_book(books):
    eng, cid, pid = books
    with eng.begin() as conn:
        sale(conn, cid, pid)
        ctx = company_ctx(conn, company_id=cid)
        ledger.post(ctx, date(2026, 10, 3), "cash", "RCPT-1", [
            ledger.Line(ledger.account_by_number(ctx, "1100")["id"], debit=Decimal("140.00")),
            ledger.Line(ledger.account_by_number(ctx, "1200")["id"], party_id=pid, credit=Decimal("140.00")),
        ], memo="paid")
    return books


def export(eng, path, **kwargs):
    # Import per test so the pre-implementation run reports every missing contract.
    from erpclaw_core.journal_export import export_journal

    with eng.connect() as conn:
        export_journal(conn, path, from_date=kwargs.pop("from_date", "2026-10-01"),
                       to_date=kwargs.pop("to_date", "2026-10-31"), **kwargs)


def rows(path):
    with path.open(encoding="utf-8", newline="") as src:
        return list(csv.DictReader(src, strict=True))


def cli(monkeypatch, eng, path, *args):
    monkeypatch.setenv("ERPCLAW_DATABASE_URL", eng.url.render_as_string(hide_password=False))
    return CliRunner().invoke(app, ["journal-csv", "--from", "2026-10-01", "--to", "2026-10-31",
                                    "--out", str(path), *args])


def test_t1_small_book_cli_bytes(small_book, tmp_path, monkeypatch):
    eng, _, _ = small_book
    path = tmp_path / "journal.csv"
    result = cli(monkeypatch, eng, path)
    assert result.exit_code == 0, result.output
    assert path.read_bytes() == EXPECTED
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_t2_inclusive_boundaries_and_empty_period(books, tmp_path):
    eng, cid, pid = books
    with eng.begin() as conn:
        for ref, day in [("PRE", date(2026, 9, 30)), ("LO", date(2026, 10, 1)),
                         ("MID", date(2026, 10, 15)), ("HI", date(2026, 10, 31)), ("POST", date(2026, 11, 1))]:
            sale(conn, cid, pid, ref, day)
    path = tmp_path / "journal.csv"
    export(eng, path)
    assert [(r["voucher_id"], r["line_no"]) for r in rows(path)] == [
        (ref, line) for ref in ("LO", "MID", "HI") for line in ("1", "2")]
    empty = tmp_path / "empty.csv"
    export(eng, empty, from_date="2025-01-01", to_date="2025-01-31")
    assert empty.read_bytes() == HEADER.encode()


def test_t3_currency_scales_verbatim(books, tmp_path):
    eng, cid, pid = books
    for code, amount, zero in [("USD", "140.00", "0.00"), ("JPY", "140", "0"), ("KWD", "140.000", "0.000")]:
        name = "Example Co" if code == "USD" else code + " Co"
        if code != "USD":
            cid = create_company(eng, name, currency=code)
        with eng.begin() as conn:
            if code != "USD":
                pid = customer(conn, cid)
            sale(conn, cid, pid, amount=amount)
        path = tmp_path / (code + ".csv")
        export(eng, path, company_name=name)
        data = rows(path)
        assert [(r["debit"], r["credit"], r["functional_debit"], r["functional_credit"])
                for r in data] == [(amount, zero, amount, zero), (zero, amount, zero, amount)]
        assert all((r["currency"], r["functional_currency"], r["rate"], r["kind"]) ==
                   (code, code, "1", "measured") for r in data)


def test_t4_hostile_text_exact_quoting(books, tmp_path):
    eng, cid, pid = books
    with eng.begin() as conn:
        conn.execute(m.account.update().where(m.account.c.company_id == cid, m.account.c.number == "1200")
                     .values(name='Café, "Beans"\nLtd'))
        conn.execute(m.party.update().where(m.party.c.id == pid).values(name='Acme "buyer"'))
        sale(conn, cid, pid, memo="memo,\nnext\rline")
    path = tmp_path / "journal.csv"
    export(eng, path)
    expected = (HEADER +
        '2026-10-01,1,sales,INV-1,"memo,\nnext\rline",1,1200,"Café, ""Beans""\nLtd",'
        '"Acme ""buyer""",140.00,0.00,USD,1,USD,140.00,0.00,measured\n'
        '2026-10-01,1,sales,INV-1,"memo,\nnext\rline",2,4100,Sales,,0.00,140.00,USD,1,USD,0.00,140.00,measured\n')
    assert path.read_bytes() == expected.encode()
    data = rows(path)
    assert data[0]["account_name"] == 'Café, "Beans"\nLtd'
    assert data[0]["party"] == 'Acme "buyer"'
    assert data[0]["memo"] == "memo,\nnext\rline"


@pytest.mark.parametrize("case,permissions", [
    ("view-only", ["view"]), ("export-only", ["export"]), ("neither", []),
    ("disabled", ["view", "export"]), ("revoked", ["view", "export"]),
])
def test_t5_current_principal_and_both_grants(small_book, tmp_path, monkeypatch, case, permissions):
    eng, cid, _ = small_book
    with eng.begin() as conn:
        administration.add_principal(conn, case, permissions, [cid])
        stale = dict(conn.execute(select(m.principal).where(m.principal.c.name == case)).mappings().one())
        if case == "disabled":
            administration.disable(conn, case)
        if case == "revoked":
            administration.revoke(conn, case, ["export"], cid)
    path = tmp_path / "journal.csv"
    with pytest.raises(Refusal) as caught:
        export(eng, path, principal=stale)
    assert caught.value.code == "forbidden"
    assert str(caught.value) == "You may not export this company's journal."
    assert list(tmp_path.iterdir()) == []
    result = cli(monkeypatch, eng, path, "--as", case)
    assert result.exit_code == 1 and "You may not export this company's journal." in result.output
    assert list(tmp_path.iterdir()) == []


def test_t5_version_bump_with_grants_still_exports(small_book, tmp_path):
    eng, cid, _ = small_book
    with eng.begin() as conn:
        stale = principals.owner(conn)
        administration.grant(conn, "owner", ["view", "export"], cid)
    path = tmp_path / "journal.csv"
    export(eng, path, principal=stale)
    assert path.read_bytes() == EXPECTED


def snapshot(conn):
    return (conn.execute(select(m.posting_chain_head)).all(),
            conn.execute(select(func.count()).select_from(m.ledger_entry)).scalar_one(),
            conn.execute(select(m.audit_log)).all())


def test_t6_read_only_even_in_committed_transaction(small_book, tmp_path):
    from erpclaw_core.journal_export import export_journal

    eng, _, _ = small_book
    with eng.begin() as conn:
        before = snapshot(conn)
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(eng, "before_cursor_execute", record)
    try:
        with eng.begin() as conn:
            export_journal(conn, tmp_path / "journal.csv", from_date="2026-10-01", to_date="2026-10-31")
    finally:
        event.remove(eng, "before_cursor_execute", record)
    assert statements and all(s.lstrip().upper().startswith("SELECT ") for s in statements)
    with eng.connect() as conn:
        assert snapshot(conn) == before


def test_t7_company_scoping_and_order_on_postgresql(books, tmp_path):
    eng, cid, pid = books
    other = create_company(eng, "Other Co")
    with eng.begin() as conn:
        assert conn.dialect.name == "postgresql"
        sale(conn, other, customer(conn, other), ref="OTHER")
        ctx = company_ctx(conn, company_id=cid)
        # Build the same checked, hashed shapes as post, then insert valid rows in
        # heap order 3,1,2 and line order 2,1, without modifying append-only rows.
        clean = ledger._check_lines(ctx, [
            ledger.Line(ledger.account_by_number(ctx, "1200")["id"], party_id=pid, debit=Decimal("140.00")),
            ledger.Line(ledger.account_by_number(ctx, "4100")["id"], credit=Decimal("140.00"))])
        entries = ledger._entries(clean, "USD")
        postings, previous = [], "0" * 64
        for seq in (1, 2, 3):
            ref = "INV-" + str(seq)
            header = ledger._header(cid, seq, date(2026, 10, 1), "sales", ref, None, None, None, "owner")
            digest = ledger._hash(previous, header, [ledger._entry_dict(e, "") for e in entries])
            postings.append(dict(header, id=new_id(), posting_date=date(2026, 10, 1), created_at=now(),
                                 prev_hash=previous, hash=digest, hash_format=ledger.HASH_FORMAT))
            previous = digest
        for index in (2, 0, 1):
            posting = postings[index]
            conn.execute(m.posting.insert().values(**posting))
            for entry in reversed(entries):
                conn.execute(m.ledger_entry.insert().values(id=new_id(), company_id=cid,
                                                           posting_id=posting["id"], **entry))
        conn.execute(m.posting_chain_head.update().where(m.posting_chain_head.c.company_id == cid)
                     .values(last_seq=3, last_hash=previous))
        assert ledger.verify_chain(conn, cid) is None
    path = tmp_path / "journal.csv"
    # Fix a legitimate PostgreSQL plan so removing ORDER BY is observable even
    # if a server version would otherwise happen to choose an ordered index scan.
    # Source mutation verified on PostgreSQL: removing the query's order_by
    # fails this assertion with first pair (3, 1), instead of (1, 1).
    from erpclaw_core.journal_export import export_journal

    with eng.connect() as conn:
        conn.exec_driver_sql("SET LOCAL enable_indexscan = off")
        conn.exec_driver_sql("SET LOCAL enable_bitmapscan = off")
        conn.exec_driver_sql("SET LOCAL enable_mergejoin = off")
        export_journal(conn, path, from_date="2026-10-01", to_date="2026-10-31", company_name="Example Co")
    assert [(r["seq"], r["line_no"]) for r in rows(path)] == [
        (seq, line) for seq in ("1", "2", "3") for line in ("1", "2")]
    assert {r["voucher_id"] for r in rows(path)} == {"INV-1", "INV-2", "INV-3"}


def test_t8_raced_destination_keeps_sentinel_and_cleans_temp(small_book, tmp_path, monkeypatch):
    eng, _, _ = small_book
    path = tmp_path / "journal.csv"
    sentinel = b"another creator's bytes\x00\xff\n"
    real_fsync = os.fsync
    seen = []

    def race(fd):
        # fsync is after the existence check and full write, before either link
        # or a mutant's rename; it intercepts both implementations equally.
        assert not path.exists()
        assert stat.S_IMODE(os.fstat(fd).st_mode) == 0o600
        assert os.fstat(fd).st_size == len(EXPECTED)
        seen.append(True)
        path.write_bytes(sentinel)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", race)
    result = cli(monkeypatch, eng, path)
    assert seen == [True]
    assert result.exit_code == 1 and "already exists" in result.output
    assert path.read_bytes() == sentinel
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", "\t", "\r"],
                         ids=["equals", "plus", "minus", "at", "tab", "carriage-return"])
def test_formula_injection_every_text_column(books, tmp_path, prefix):
    eng, cid, pid = books
    text = prefix + "danger"
    with eng.begin() as conn:
        conn.execute(m.account.update().where(m.account.c.company_id == cid, m.account.c.number == "1200")
                     .values(number=text, name=text))
        conn.execute(m.party.update().where(m.party.c.id == pid).values(name=text))
        ctx = company_ctx(conn, company_id=cid)
        account_id = conn.execute(select(m.account.c.id).where(m.account.c.company_id == cid,
                                                              m.account.c.number == text)).scalar_one()
        ledger.post(ctx, date(2026, 10, 1), text, text, [
            ledger.Line(account_id, party_id=pid, debit=Decimal("140.00")),
            ledger.Line(ledger.account_by_number(ctx, "4100")["id"], credit=Decimal("140.00"))], memo=text)
    path = tmp_path / "journal.csv"
    export(eng, path)
    row = rows(path)[0]
    for column in ("voucher_type", "voucher_id", "memo", "account_number", "account_name", "party"):
        assert row[column] == "'" + text
    for column, expected in [("debit", "140.00"), ("credit", "0.00"), ("rate", "1"),
                             ("functional_debit", "140.00"), ("functional_credit", "0.00"),
                             ("seq", "1"), ("line_no", "1")]:
        assert row[column] == expected


@pytest.mark.parametrize("start,end,message", [
    ("2026-02-30", "2026-10-31", "Give --from and --to as YYYY-MM-DD dates."),
    ("20261001", "2026-10-31", "Give --from and --to as YYYY-MM-DD dates."),
    ("2026-10-01", "bad", "Give --from and --to as YYYY-MM-DD dates."),
    ("2026-10-31", "2026-10-01", "The end date is before the start date."),
])
def test_bad_period_leaves_no_file(books, tmp_path, start, end, message):
    eng, _, _ = books
    with pytest.raises(Refusal) as caught:
        export(eng, tmp_path / "journal.csv", from_date=start, to_date=end)
    assert caught.value.code == "bad_period" and str(caught.value) == message
    assert list(tmp_path.iterdir()) == []


def test_company_selection_and_existing_destination(small_book, tmp_path, monkeypatch):
    eng, _, _ = small_book
    create_company(eng, "Other Co")
    path = tmp_path / "journal.csv"
    for kwargs, code in [({}, "company_ambiguous"), ({"company_name": "Unknown"}, "no_company"),
                         ({"company_name": "Example Co", "principal": "Unknown"}, "no_principal")]:
        with pytest.raises(Refusal) as caught:
            export(eng, path, **kwargs)
        assert caught.value.code == code
        assert list(tmp_path.iterdir()) == []
    result = cli(monkeypatch, eng, path, "--company", "Example Co", "--as", "owner")
    assert result.exit_code == 0, result.output
    assert path.read_bytes() == EXPECTED
    result = cli(monkeypatch, eng, path, "--company", "Example Co")
    assert result.exit_code == 1 and "already exists" in result.output
    assert path.read_bytes() == EXPECTED and list(tmp_path.iterdir()) == [path]


def test_runtime_login_can_export_with_read_only_transaction(engine, runtime_login, tmp_path, monkeypatch):
    from erpclaw_core.db import make_engine

    with engine.connect() as conn:
        name = conn.execute(select(m.company.c.name)).scalar_one()
    eng = make_engine(runtime_login)
    try:
        from erpclaw_core.journal_export import export_journal

        with eng.begin() as conn:
            conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            export_journal(conn, tmp_path / "core.csv", from_date="2026-01-01", to_date="2026-12-31",
                           company_name=name)
        monkeypatch.setenv("ERPCLAW_DATABASE_URL", runtime_login)
        monkeypatch.setenv("ERPCLAW_MIGRATION_URL", "must not be used")
        result = CliRunner().invoke(app, ["journal-csv", "--from", "2026-01-01", "--to", "2026-12-31",
                                        "--out", str(tmp_path / "cli.csv"), "--company", name])
        assert result.exit_code == 0, result.output
        assert (tmp_path / "core.csv").read_bytes() == (tmp_path / "cli.csv").read_bytes()
    finally:
        eng.dispose()
