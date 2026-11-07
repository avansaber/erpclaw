"""Backup, restore into new books, and the book diff."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, inspect, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from conftest import PG_URL
from erpclaw_core import backup as bk, ledger, models as m
from erpclaw_core.common import company_ctx, new_id
from erpclaw_core.db import make_engine
from erpclaw_core.setup import create_company

ADMIN = make_url(PG_URL).set(database="postgres")
_MADE: list[str] = []  # the new databases this module's tests named for restore


def _target(runtime_role=None) -> bk.NewPostgresDatabase:
    """A new PostgreSQL database for restore, removed after the test."""
    name = f"restored_{secrets.token_hex(4)}_test"
    _MADE.append(name)
    return bk.NewPostgresDatabase(PG_URL, name, runtime_role)


def _open(target):
    return make_engine(make_url(target.server_url).set(database=target.name).render_as_string(hide_password=False))


def _restored(src):
    target = _target()
    result = bk.restore(src, target)
    assert result.location == target.name
    return _open(target)


def _databases(pattern: str) -> list[str]:
    admin = create_engine(ADMIN, poolclass=NullPool)
    try:
        with admin.connect() as c:
            return list(c.exec_driver_sql("SELECT datname FROM pg_database WHERE datname LIKE %(p)s",
                                          {"p": pattern}).scalars())
    finally:
        admin.dispose()


def _nothing_left(target) -> None:
    assert _databases(target.name) == [] and _databases("erpclaw\\_restore\\_%") == []


def _refused(path, match):
    target = _target()
    with pytest.raises(bk.BackupRefused, match=match):
        bk.restore(path, target)
    _nothing_left(target)


def _drop_made() -> None:
    """Every database a test restored into, and any staging database left behind."""
    admin = create_engine(ADMIN, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    with admin.connect() as c:
        names = _MADE + list(c.exec_driver_sql("SELECT datname FROM pg_database WHERE datname LIKE 'erpclaw_restore_%%'").scalars())
        for n in names:
            c.exec_driver_sql(f'DROP DATABASE IF EXISTS "{n}" WITH (FORCE)')
    _MADE.clear()
    admin.dispose()


@pytest.fixture(autouse=True)
def restored_databases():
    """Every database a test restored into removed afterwards."""
    yield
    _drop_made()


@pytest.fixture
def pg_new():
    """New PostgreSQL databases for restore, removed when this fixture ends: before a runtime login requested
    ahead of it is dropped, since a role with grants in a database cannot be."""
    yield _target
    _drop_made()


def _post(engine, company, amount, d=date(2026, 10, 1), credit="4100"):
    with engine.begin() as conn:
        ctx = company_ctx(conn, company_name=company)
        a = lambda n: ledger.account_by_number(ctx, n)["id"]  # noqa: E731
        return ledger.post(ctx, d, "journal", new_id(), [ledger.Line(a("1100"), debit=Decimal(amount)),
                                                         ledger.Line(a(credit), credit=Decimal(amount))])


@pytest.fixture
def books(engine):
    """The demo books plus a second company in USD and companies at each other
    money scale, with a reversal: what a backup has to carry exactly."""
    for name, cur, amount in (("Second Co", "USD", "12.34"), ("Yen Co", "JPY", "1500"), ("Dinar Co", "KWD", "1.250")):
        create_company(engine, name, currency=cur)
        r = _post(engine, name, amount)
    with engine.begin() as conn:
        ledger.reverse(company_ctx(conn, company_name="Dinar Co"), r.posting_id, date(2026, 10, 2))
    return engine


@pytest.fixture
def books(engine):
    """The demo books plus a second company in USD and companies at each other
    money scale, with a reversal: what a backup has to carry exactly."""
    for name, cur, amount in (("Second Co", "USD", "12.34"), ("Yen Co", "JPY", "1500"), ("Dinar Co", "KWD", "1.250")):
        create_company(engine, name, currency=cur)
        r = _post(engine, name, amount)
    with engine.begin() as conn:
        ledger.reverse(company_ctx(conn, company_name="Dinar Co"), r.posting_id, date(2026, 10, 2))
    return engine


def test_a_backup_is_private_and_states_it_is_not_encrypted(books, tmp_path):
    manifest = bk.backup(books, tmp_path / "b")
    assert stat.S_IMODE(os.stat(tmp_path / "b").st_mode) == 0o700
    assert {stat.S_IMODE(os.stat(f).st_mode) for f in (tmp_path / "b").rglob("*.jsonl")} == {0o600}
    assert stat.S_IMODE(os.stat(tmp_path / "b" / "MANIFEST.json").st_mode) == 0o600
    assert manifest["encrypted"] is False and "Not encrypted" in manifest["note"]
    from erpclaw_core.migrate import core_head

    assert manifest["core_revision"] == core_head()
    assert manifest["ownership"]


def test_round_trip_into_a_new_postgresql_database(books, tmp_path, pg_new):
    bk.backup(books, tmp_path / "b")
    target = pg_new()
    result = bk.restore(tmp_path / "b", target)
    assert result.location == target.name
    pg = _open(target)
    try:
        again = bk.backup(pg, tmp_path / "again")
        first = json.loads((tmp_path / "b" / "MANIFEST.json").read_text())
        assert {k: v["sha256"] for k, v in again["tables"].items()} == {k: v["sha256"] for k, v in first["tables"].items()}
        assert bk.book_diff(books, pg) == []
        with pg.connect() as conn:
            for cid in conn.execute(select(m.company.c.id)).scalars():
                assert ledger.verify_chain(conn, cid) is None
    finally:
        pg.dispose()
    assert bk.verify(tmp_path / "b", target) == []


def test_timestamps_have_one_utc_form(books, tmp_path):
    instant = datetime(2026, 1, 1, 12, 0, 0, 123456, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    with books.begin() as conn:
        conn.execute(update(m.company).where(m.company.c.name == "Second Co").values(created_at=instant))
    bk.backup(books, tmp_path / "b")
    line = next(json.loads(x) for x in (tmp_path / "b" / "tables" / "company.jsonl").read_text().splitlines()
                if json.loads(x)["name"] == "Second Co")
    assert line["created_at"] == "2026-01-01T06:30:00.123456Z"
    target = _restored(tmp_path / "b")
    with target.connect() as conn:
        assert bk._dump(conn, m.company) == (tmp_path / "b" / "tables" / "company.jsonl").read_text().splitlines()


def test_rows_restore_in_dependency_order_whatever_their_ids(books, tmp_path):
    """A parent account whose id sorts after its child's restores first."""
    with books.begin() as conn:
        ctx = company_ctx(conn, company_name="Second Co")
        parent, child = "f" * 8 + "-0000-0000-0000-" + "f" * 12, "0" * 8 + "-0000-0000-0000-" + "0" * 12
        for aid, number, group, up in ((parent, "7000", True, None), (child, "7100", False, parent)):
            conn.execute(m.account.insert().values(id=aid, company_id=ctx.company_id, number=number, name=number,
                                                   root_type="expense", account_type=None, is_group=group,
                                                   parent_id=up, frozen=False))
    bk.backup(books, tmp_path / "b")
    assert bk.book_diff(books, _restored(tmp_path / "b")) == []


def test_a_backup_taken_while_posting_is_one_snapshot(books, tmp_path):
    stop = threading.Event()

    def poster():
        while not stop.is_set():
            _post(books, "Second Co", "1.00")
            time.sleep(0.005)

    t = threading.Thread(target=poster)
    t.start()
    try:
        time.sleep(0.05)
        bk.backup(books, tmp_path / "b")
    finally:
        stop.set()
        t.join()
    _restored(tmp_path / "b")  # restore re-proves chain and head: a torn snapshot would fail


# --- refusals ------------------------------------------------------------------------

def _rewrite(path: Path, table: str, fn) -> None:
    """Edit a table file and recompute its manifest entry, as a careful forger would."""
    f = path / "tables" / f"{table}.jsonl"
    lines = fn(f.read_text().splitlines())
    f.write_text("\n".join(lines) + ("\n" if lines else ""))
    man = json.loads((path / "MANIFEST.json").read_text())
    man["tables"][table] = {**man["tables"][table], "rows": len(lines), "sha256": bk._sha(lines)}
    (path / "MANIFEST.json").write_text(json.dumps(man))


def test_a_damaged_or_miscounted_file_is_refused(books, tmp_path):
    bk.backup(books, tmp_path / "b")
    f = tmp_path / "b" / "tables" / "party.jsonl"
    f.write_text(f.read_text().replace("Acme", "Acne", 1))
    _refused(tmp_path / "b", "damaged: tables/party.jsonl")
    bk.backup(books, tmp_path / "c")
    man = json.loads((tmp_path / "c" / "MANIFEST.json").read_text())
    man["tables"]["party"]["rows"] += 1
    (tmp_path / "c" / "MANIFEST.json").write_text(json.dumps(man))
    _refused(tmp_path / "c", "damaged: tables/party.jsonl")


@pytest.mark.parametrize("field", ["core_revision", "plugin"])
def test_a_backup_from_another_engine_version_is_refused(books, tmp_path, field):
    bk.backup(books, tmp_path / "b")
    man = json.loads((tmp_path / "b" / "MANIFEST.json").read_text())
    if field == "core_revision":
        man["core_revision"] = "0002"
    else:
        man["plugins"][0]["artifact_digest"] = "0" * 64
    (tmp_path / "b" / "MANIFEST.json").write_text(json.dumps(man))
    _refused(tmp_path / "b", "core revision 0002" if field == "core_revision" else "different artifact digest")


def test_seeded_currencies_that_differ_are_refused(books, tmp_path):
    bk.backup(books, tmp_path / "b")
    _rewrite(tmp_path / "b", "currency", lambda ls: [x.replace("Yen", "Old Yen") for x in ls])
    _refused(tmp_path / "b", "currency rows differ")


def test_an_account_parent_cycle_is_refused(books, tmp_path):
    bk.backup(books, tmp_path / "b")

    def cycle(lines):
        rows = [json.loads(x) for x in lines]
        a, b = rows[0], rows[1]
        a["parent_id"], b["parent_id"] = b["id"], a["id"]
        b["company_id"] = a["company_id"]
        return [json.dumps(r, separators=(",", ":"), ensure_ascii=False) for r in rows]

    _rewrite(tmp_path / "b", "account", cycle)
    _refused(tmp_path / "b", "cycle")


def test_a_database_with_an_unknown_table_is_not_backed_up(books, tmp_path):
    with books.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE stray_notes (x INTEGER)")
    try:
        with pytest.raises(bk.BackupRefused, match="does not know: stray_notes"):
            bk.backup(books, tmp_path / "b")
    finally:
        with books.begin() as conn:
            conn.exec_driver_sql("DROP TABLE stray_notes")


# --- hash-consistent invalid backups: only the integrity checks can catch them --------

def _rechain(path: Path) -> None:
    """Recompute format-2 fixture hashes independently, including every stored line field.

    Historical format-1 preservation has its own explicit-byte oracle in the
    integration-kernel revision test; this helper forges current-format backups.
    """
    rd = lambda t: [json.loads(x) for x in (path / "tables" / f"{t}.jsonl").read_text().splitlines()]  # noqa: E731
    postings, entries = rd("posting"), rd("ledger_entry")
    dimsets = {(r["company_id"], r["id"]): r["digest"] for r in rd("dimension_set")}
    header_fields = ("company_id", "seq", "posting_date", "voucher_type", "voucher_id", "memo",
                     "reverses_posting_id", "operation_key", "created_by", "ledger")
    line_fields = ("line_no", "account_id", "party_id", "debit", "credit", "currency", "rate", "functional_currency",
                   "functional_debit", "functional_credit", "kind")
    heads = {}
    for p in sorted(postings, key=lambda r: (r["company_id"], r["seq"])):
        assert p["hash_format"] == 2, "current-backup fixture requires format 2; never rewrite a historical chain"
        prev = heads.get(p["company_id"], "0" * 64)
        lines = [{**{f: e[f] for f in line_fields},
                  "dimset": dimsets[(e["company_id"], e["dimension_set_id"])] if e["dimension_set_id"] else ""}
                 for e in sorted(entries, key=lambda e: e["line_no"]) if e["posting_id"] == p["id"]]
        body = {"format": 2, **{f: p[f] for f in header_fields}, "lines": lines}
        canonical = json.dumps(body, separators=(",", ":")).encode()
        p["prev_hash"], p["hash"] = prev, hashlib.sha256(prev.encode() + canonical).hexdigest()
        heads[p["company_id"]] = p["hash"]
    dump = lambda rows: [json.dumps(r, separators=(",", ":"), ensure_ascii=False) for r in rows]  # noqa: E731
    _rewrite(path, "posting", lambda _: dump(postings))
    head_rows = rd("posting_chain_head")
    for h in head_rows:
        if h["company_id"] in heads:
            h["last_hash"] = heads[h["company_id"]]
    _rewrite(path, "posting_chain_head", lambda _: dump(head_rows))


def _edit_entries(path: Path, fn) -> None:
    def go(lines):
        rows = [json.loads(x) for x in lines]
        fn(rows)
        return [json.dumps(r, separators=(",", ":"), ensure_ascii=False) for r in rows]
    _rewrite(path, "ledger_entry", go)
    _rechain(path)


def _company_postings(path, name):
    rd = lambda t: [json.loads(x) for x in (path / "tables" / f"{t}.jsonl").read_text().splitlines()]  # noqa: E731
    cid = next(c["id"] for c in rd("company") if c["name"] == name)
    return [p["id"] for p in sorted(rd("posting"), key=lambda r: r["seq"]) if p["company_id"] == cid]


def _cancelling(path):
    first, second = _company_postings(path, "Stark Manufacturing")[:2]

    def fn(rows):
        for pid, delta in ((first, Decimal("1.00")), (second, Decimal("-1.00"))):
            r = next(r for r in rows if r["posting_id"] == pid and Decimal(r["debit"]) > 0)
            r["debit"] = format(Decimal(r["debit"]) + delta, "f")
            r["functional_debit"] = r["debit"]
    return fn


def _one_line(path):
    pid = _company_postings(path, "Second Co")[0]
    return lambda rows: rows.remove(next(r for r in rows if r["posting_id"] == pid and Decimal(r["credit"]) > 0))


def _other_currency(path):
    pid = _company_postings(path, "Second Co")[0]

    def fn(rows):
        for r in rows:
            if r["posting_id"] == pid:
                r["currency"] = "EUR"
    return fn


def _no_party(path):
    pid = _company_postings(path, "Stark Manufacturing")[0]  # an invoice: its receivable line names a customer

    def fn(rows):
        for r in rows:
            if r["posting_id"] == pid and r["party_id"]:
                r["party_id"] = None
    return fn


def _bad_mirror(path):
    original, reversal = _company_postings(path, "Dinar Co")[:2]

    def fn(rows):
        for r in rows:
            if r["posting_id"] == reversal:
                r["debit"], r["credit"] = r["credit"], r["debit"]  # the same direction as the original
                r["functional_debit"], r["functional_credit"] = r["debit"], r["credit"]
    return fn


@pytest.mark.parametrize("forge,match", [(_cancelling, "does not balance"), (_one_line, "fewer than two lines"),
                                         (_other_currency, "ck_entry_functional_v30"),
                                         (_no_party, "receivable line without a customer"),
                                         (_bad_mirror, "does not mirror its original")])
def test_a_hash_consistent_forgery_is_refused(books, tmp_path, forge, match):
    bk.backup(books, tmp_path / "b")
    _edit_entries(tmp_path / "b", forge(tmp_path / "b"))
    _refused(tmp_path / "b", match)


def _huge(path):
    """A 0.01 surplus hidden beyond 28 significant digits."""
    pid = _company_postings(path, "Second Co")[0]

    def fn(rows):
        for r in rows:
            if r["posting_id"] == pid:
                big = "1000000000000000000000000000000"
                r["debit"], r["credit"] = (big + ".01", "0.00") if Decimal(r["debit"]) > 0 else ("0.00", big + ".00")
                r["functional_debit"], r["functional_credit"] = r["debit"], r["credit"]
    return fn


def test_amounts_beyond_the_money_bounds_are_refused_not_rounded(books, tmp_path):
    bk.backup(books, tmp_path / "b")
    _edit_entries(tmp_path / "b", _huge(tmp_path / "b"))
    _refused(tmp_path / "b", "amount")


def test_restored_install_rows_must_match_the_manifest(books, tmp_path):
    bk.backup(books, tmp_path / "b")

    def stale(lines):  # a row that still passes the plugin checks, so only the equality with the manifest sees it
        rows = [json.loads(x) for x in lines]
        rows[0]["installed_at"] = "2020-01-01T00:00:00.000000Z"
        return [json.dumps(r, separators=(",", ":"), ensure_ascii=False) for r in rows]

    _rewrite(tmp_path / "b", "plugin_install", stale)  # the manifest's plugin list still has the real rows
    _refused(tmp_path / "b", "install records differ from its manifest")


def _foreign_account(path):
    rd = lambda t: [json.loads(x) for x in (path / "tables" / f"{t}.jsonl").read_text().splitlines()]  # noqa: E731
    companies = {c["name"]: c["id"] for c in rd("company")}
    theirs = next(a["id"] for a in rd("account") if a["company_id"] == companies["Yen Co"] and a["number"] == "1100")
    pid = _company_postings(path, "Second Co")[0]

    def fn(rows):
        r = next(r for r in rows if r["posting_id"] == pid and Decimal(r["debit"]) > 0)
        r["account_id"] = theirs
    return fn


def test_cross_company_references_are_refused(books, tmp_path, pg_new):
    """Acceptance: a ledger line pointing at another company's
    account is refused, and the target is removed."""
    bk.backup(books, tmp_path / "b")
    _edit_entries(tmp_path / "b", _foreign_account(tmp_path / "b"))
    target = pg_new()
    with pytest.raises(bk.BackupRefused):
        bk.restore(tmp_path / "b", target)
    _nothing_left(target)


def test_a_posting_committed_in_the_middle_of_a_backup_is_not_half_in_it(books, tmp_path, monkeypatch):
    """Deterministic: between dumping postings and dumping their lines, another
    connection commits a posting. A torn read would carry its lines without it."""
    dump = bk._dump
    fired = []

    def dump_with_a_commit_between(conn, table):
        if getattr(table, "name", None) == "ledger_entry" and not fired:
            _post(books, "Second Co", "3.00")
            fired.append("committed")  # the snapshot does not see it
        return dump(conn, table)

    monkeypatch.setattr(bk, "_dump", dump_with_a_commit_between)
    bk.backup(books, tmp_path / "b")
    monkeypatch.setattr(bk, "_dump", dump)
    assert fired
    _restored(tmp_path / "b")  # restore re-proves chain and head: a torn read would fail


# --- the target: new books, never books in use ------------------------

def test_restore_takes_no_engine_and_reads_no_books_url():
    """Acceptance: the restore module takes no Engine and reads no database URL
    from the environment (a static check of its source)."""
    import inspect as pyinspect

    params = pyinspect.signature(bk.restore).parameters
    assert list(params) == ["path", "target"]
    source = Path(bk.__file__).read_text()
    assert "ERPCLAW_DATABASE_URL" not in source and "database_url(" not in source and "make_engine(" not in source


@pytest.mark.parametrize("fail_at", ["staged_ready", "inserted", "committed", "proved"])
def test_a_running_engine_and_its_books_are_untouched_whatever_restore_does(books, tmp_path, monkeypatch, fail_at):
    """Restores that fail at each step leave the running engine's books
    byte-identical and its pooled connection transactional."""
    bk.backup(books, tmp_path / "b")
    with books.connect() as conn:
        before = [bk._dump(conn, t) for t in bk.books_tables(conn)]

    def fail(step, **kw):
        if step == fail_at:
            raise bk.BackupRefused(f"forced failure at {step}")

    monkeypatch.setattr(bk, "_HOOK", fail)
    _refused(tmp_path / "b", f"forced failure at {fail_at}")
    monkeypatch.setattr(bk, "_HOOK", None)
    _restored(tmp_path / "b")  # and a retry succeeds
    with books.connect() as conn:
        assert [bk._dump(conn, t) for t in bk.books_tables(conn)] == before
    with books.connect() as conn:  # the engine's connections still roll back: a write then a rollback leaves nothing
        cid = conn.execute(select(m.company.c.id)).scalars().first()
        conn.execute(m.party.insert().values(id=new_id(), company_id=cid, kind="customer", name="Probe Ltd"))
        conn.rollback()
    with books.connect() as conn:
        assert conn.execute(select(m.party.c.id).where(m.party.c.name == "Probe Ltd")).first() is None


def test_a_session_in_the_staging_database_stops_restore(books, tmp_path, pg_new, monkeypatch):
    """Even a trusted (superuser) session connected before restore makes the
    rename barrier refuse; nothing is removed and the name is reported."""
    bk.backup(books, tmp_path / "b")
    held = []

    def intrude(step, stage=None, **kw):
        if step == "staged":
            e = create_engine(make_url(PG_URL).set(database=stage.name))
            c = e.connect()
            c.exec_driver_sql("SELECT 1")
            held.append((e, c))

    monkeypatch.setattr(bk, "_HOOK", intrude)
    target = pg_new()
    try:
        with pytest.raises(bk.BackupRefused, match="another session is connected"):
            bk.restore(tmp_path / "b", target)
    finally:
        for e, c in held:
            c.close()
            e.dispose()


def test_an_object_made_by_another_role_or_a_late_session_stops_publication(books, tmp_path, pg_new, monkeypatch):
    bk.backup(books, tmp_path / "b")
    held = []

    def late(step, stage=None, **kw):
        if step == "proved":
            e = create_engine(make_url(PG_URL).set(database=stage.name))
            c = e.connect()
            c.exec_driver_sql("SELECT 1")
            held.append((e, c))

    monkeypatch.setattr(bk, "_HOOK", late)
    target = pg_new()
    try:
        with pytest.raises(bk.BackupRefused, match="not restore's alone"):
            bk.restore(tmp_path / "b", target)
    finally:
        for e, c in held:
            c.close()
            e.dispose()
    admin = create_engine(make_url(PG_URL).set(database="postgres"))
    with admin.connect() as c:
        assert not c.exec_driver_sql("SELECT 1 FROM pg_database WHERE datname = %(n)s", {"n": target.name}).first()
    admin.dispose()


def test_a_change_between_commit_and_proof_is_caught(books, tmp_path, monkeypatch):
    """The proof reads what was committed, on a new connection, and compares it with the backup."""
    bk.backup(books, tmp_path / "b")

    def tamper(step, stage=None, **kw):
        if step == "committed":
            e = create_engine(make_url(PG_URL).set(database=stage.name), poolclass=NullPool)
            with e.begin() as c:  # a trusted session, gone again before publication's admission check
                c.exec_driver_sql("UPDATE party SET city = 'Elsewhere' WHERE id = (SELECT min(id) FROM party)")
            e.dispose()

    monkeypatch.setattr(bk, "_HOOK", tamper)
    _refused(tmp_path / "b", "the committed party differs")


@pytest.mark.parametrize("at", ["staged", "proved"])
def test_an_object_another_role_made_in_the_staging_database_stops_restore(books, tmp_path, pg_new, monkeypatch, at):
    bk.backup(books, tmp_path / "b")
    other = f"erpclaw_o_{secrets.token_hex(3)}"
    admin = create_engine(make_url(PG_URL).set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.exec_driver_sql(f"CREATE ROLE {other} NOLOGIN")

    def plant(step, stage=None, **kw):
        if step == at:
            e = create_engine(make_url(PG_URL).set(database=stage.name), isolation_level="AUTOCOMMIT")
            with e.connect() as c:  # made, then gone: no session is left to see
                c.exec_driver_sql("CREATE TABLE planted (x int)")
                c.exec_driver_sql(f"ALTER TABLE planted OWNER TO {other}")
            e.dispose()

    monkeypatch.setattr(bk, "_HOOK", plant)
    target = pg_new()
    try:
        with pytest.raises(bk.BackupRefused, match="not restore's alone|holds relations"):
            bk.restore(tmp_path / "b", target)
    finally:
        with admin.connect() as c:
            for n in c.exec_driver_sql("SELECT datname FROM pg_database WHERE datname LIKE 'erpclaw_restore_%%'").scalars():
                c.exec_driver_sql(f'DROP DATABASE IF EXISTS "{n}" WITH (FORCE)')
            c.exec_driver_sql(f"DROP ROLE {other}")
        admin.dispose()


def test_postgresql_publication_grants_the_runtime_role_only(books, tmp_path, runtime_login, pg_new):
    from erpclaw_core import grants

    bk.backup(books, tmp_path / "b")
    role = make_url(runtime_login).username
    target = pg_new(runtime_role=role)
    bk.restore(tmp_path / "b", target)
    rt = make_engine(make_url(runtime_login).set(database=target.name).render_as_string(hide_password=False))
    try:
        assert grants.validate_runtime_session(rt) is None  # it connects and holds exactly its policy
    finally:
        rt.dispose()
    admin = create_engine(make_url(PG_URL).set(database="postgres"))
    with admin.connect() as c:
        assert c.exec_driver_sql("SELECT has_database_privilege('public', %(n)s, 'CONNECT')", {"n": target.name}).scalar() is False
        assert c.exec_driver_sql("SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = %(n)s",
                                 {"n": target.name}).scalar() is None
    admin.dispose()


# --- crashes and uncertain outcomes ---------------------------------------

PG_CRASH = """
import os, sys
from pathlib import Path
from erpclaw_core import backup as bk
def hook(step, **kw):
    if step == "publishing":
        os._exit(17)
bk._HOOK = hook
bk.restore(Path(sys.argv[1]), bk.NewPostgresDatabase(sys.argv[2], sys.argv[3]))
"""


def _until_no_session(admin, name: str, within: float = 60.0) -> None:
    """The killed client's server session ends a moment after its process, and later on a busy server: wait until it
    is gone, as an operator would before cleaning up (cleanup rightly refuses a staging database with a session in
    it). Five seconds was too short under load; a session still there after a minute is a failure, said plainly."""
    deadline = time.monotonic() + within
    with admin.connect() as c:
        while True:
            if not c.exec_driver_sql("SELECT count(*) FROM pg_stat_activity WHERE datname = %(n)s", {"n": name}).scalar():
                return
            c.rollback()
            if time.monotonic() > deadline:
                raise AssertionError(f"a session on {name} was still connected after {within:.0f} s")
            time.sleep(0.05)


def test_a_crash_inside_postgresql_publication_leaves_no_final_name(books, tmp_path, pg_new):
    bk.backup(books, tmp_path / "b")
    target = pg_new()
    proc = subprocess.run([sys.executable, "-c", PG_CRASH, str(tmp_path / "b"), PG_URL, target.name],
                          capture_output=True, text=True)
    assert proc.returncode == 17, proc.stderr
    admin = create_engine(make_url(PG_URL).set(database="postgres"))
    with admin.connect() as c:
        assert not c.exec_driver_sql("SELECT 1 FROM pg_database WHERE datname = %(n)s", {"n": target.name}).first()
    admin.dispose()
    stale = bk.stale_stagings(PG_URL)
    assert len(stale) == 1 and stale[0].startswith(bk.PG_STAGE_PREFIX)
    _until_no_session(admin, stale[0])  # wait until the stale restore has no session
    bk.clean_staging(stale[0], PG_URL)
    assert bk.stale_stagings(PG_URL) == []
    bk.restore(tmp_path / "b", target)  # and a retry publishes
    assert bk.verify(tmp_path / "b", target) == []


def _admin():
    return create_engine(make_url(PG_URL).set(database="postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool)


def _staging(rows: int = 0, owner: str | None = None) -> str:
    """A staging database by name (cleanup checks the name), with no client left on it. With rows, one table that
    autovacuum takes up at once and works through slowly, half its rows dead."""
    name = f"{bk.PG_STAGE_PREFIX}{secrets.token_hex(8)}_b"
    admin = _admin()
    with admin.connect() as c:
        c.exec_driver_sql(f'CREATE DATABASE "{name}"' + (f' OWNER "{owner}"' if owner else ""))
    admin.dispose()
    if rows:
        db = create_engine(make_url(PG_URL).set(database=name), poolclass=NullPool)
        with db.begin() as c:
            c.exec_driver_sql("CREATE TABLE t (id int PRIMARY KEY, v text) WITH (autovacuum_vacuum_threshold = 0, "
                              "autovacuum_analyze_threshold = 0, autovacuum_vacuum_scale_factor = 0, "
                              "autovacuum_analyze_scale_factor = 0, autovacuum_vacuum_cost_delay = 100, "
                              "autovacuum_vacuum_cost_limit = 1)")
            c.exec_driver_sql(f"INSERT INTO t SELECT g, md5(g::text) FROM generate_series(1, {rows}) g")
            c.exec_driver_sql("DELETE FROM t WHERE mod(id, 2) = 0")
        db.dispose()
    return name


def _exists(name: str) -> bool:
    admin = _admin()
    with admin.connect() as c:
        found = c.exec_driver_sql("SELECT 1 FROM pg_database WHERE datname = %(n)s", {"n": name}).first()
    admin.dispose()
    return found is not None


def _drop(name: str) -> None:
    admin = _admin()
    with admin.connect() as c:  # the test's own teardown, never the engine's way
        c.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    admin.dispose()


def _autovacuum_on(c, name: str) -> int:
    return c.exec_driver_sql("SELECT count(*) FROM pg_stat_activity WHERE datname = %(n)s "
                             "AND backend_type = 'autovacuum worker'", {"n": name}).scalar()


def test_an_autovacuum_worker_does_not_keep_a_stale_staging_database():
    """The server's own autovacuum visits a restored staging database moments after its last client leaves; it is
    the trusted server, not a session in the way, and dropping the database makes it leave."""
    name = _staging(rows=200_000)
    admin = _admin()
    try:
        deadline = time.monotonic() + 90  # one autovacuum naptime (60 s) and a margin
        with admin.connect() as c:
            while not _autovacuum_on(c, name):
                assert time.monotonic() < deadline, f"no autovacuum worker reached {name} within 90 s"
                time.sleep(0.1)
            assert _autovacuum_on(c, name)  # still there as cleanup runs
        bk.clean_staging(name, PG_URL)
        assert not _exists(name)
    finally:
        admin.dispose()
        _drop(name)


@pytest.fixture
def unprivileged():
    """An operator role that owns what it cleans up, with no statistics access (not a superuser, no
    pg_read_all_stats), and another login role for a client; yields (operator URL, client role, client password)."""
    operator, client = f"erpclaw_op_{secrets.token_hex(3)}", f"erpclaw_cl_{secrets.token_hex(3)}"
    op_pw, cl_pw = secrets.token_hex(12), secrets.token_hex(12)
    admin = _admin()
    with admin.connect() as c:
        c.exec_driver_sql(f"CREATE ROLE {operator} LOGIN PASSWORD '{op_pw}'")
        c.exec_driver_sql(f"CREATE ROLE {client} LOGIN PASSWORD '{cl_pw}'")
    try:
        yield (make_url(PG_URL).set(username=operator, password=op_pw).render_as_string(hide_password=False),
               client, cl_pw)
    finally:
        with admin.connect() as c:
            c.exec_driver_sql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename IN "
                              f"('{operator}', '{client}')")
            for role in (operator, client):
                c.exec_driver_sql(f"DROP OWNED BY {role}")
                c.exec_driver_sql(f"DROP ROLE {role}")
        admin.dispose()


def test_another_roles_client_refuses_the_cleanup_of_an_operator_without_statistics_access(unprivileged):
    """The operator sees the client's session with its type hidden; it still counts, and nothing is dropped."""
    url, client_role, client_pw = unprivileged
    name = _staging(owner=make_url(url).username)
    client = create_engine(make_url(PG_URL).set(username=client_role, password=client_pw, database=name),
                           poolclass=NullPool)
    statements: list[str] = []

    def seen(conn, cursor, statement, *args):
        statements.append(statement)

    try:
        with client.connect() as held:
            held.exec_driver_sql("SELECT 1")
            op = create_engine(make_url(url).set(database="postgres"), poolclass=NullPool)
            with op.connect() as c:  # the premise: this operator cannot see the client's backend type
                assert c.exec_driver_sql("SELECT backend_type FROM pg_stat_activity WHERE datname = %(n)s",
                                         {"n": name}).scalars().all() == [None]
            op.dispose()
            event.listen(Engine, "before_cursor_execute", seen)
            with pytest.raises(bk.BackupRefused, match="has a session connected"):
                bk.clean_staging(name, url)
            assert not [s for s in statements if s.lstrip().upper().startswith("DROP DATABASE")], statements
            assert held.exec_driver_sql("SELECT 1").scalar() == 1
        assert _exists(name)
    finally:
        event.remove(Engine, "before_cursor_execute", seen)
        client.dispose()
        _drop(name)


def test_an_autovacuum_worker_does_not_keep_a_staging_database_from_an_operator_without_statistics_access(
        unprivileged):
    """Autovacuum's session is hidden from this operator too, but it has no user id, so it is not counted."""
    url = unprivileged[0]
    name = _staging(rows=200_000, owner=make_url(url).username)
    admin = _admin()
    try:
        deadline = time.monotonic() + 90  # one autovacuum naptime (60 s) and a margin
        with admin.connect() as c:
            while not _autovacuum_on(c, name):
                assert time.monotonic() < deadline, f"no autovacuum worker reached {name} within 90 s"
                time.sleep(0.1)
        op = create_engine(make_url(url).set(database="postgres"), poolclass=NullPool)
        with op.connect() as c:  # the premise: hidden from this operator, and with no user id
            assert c.exec_driver_sql("SELECT backend_type, usesysid FROM pg_stat_activity WHERE datname = %(n)s",
                                     {"n": name}).all() == [(None, None)]
        op.dispose()
        bk.clean_staging(name, url)
        assert not _exists(name)
    finally:
        admin.dispose()
        _drop(name)


def test_a_client_on_a_staging_database_refuses_its_cleanup_before_any_drop():
    name = _staging()
    client = create_engine(make_url(PG_URL).set(database=name), poolclass=NullPool)
    statements: list[str] = []

    def seen(conn, cursor, statement, *args):
        statements.append(statement)

    event.listen(Engine, "before_cursor_execute", seen)
    try:
        with client.connect() as held:
            held.exec_driver_sql("SELECT 1")
            with pytest.raises(bk.BackupRefused, match="has a session connected"):
                bk.clean_staging(name, PG_URL)
            assert not [s for s in statements if s.lstrip().upper().startswith("DROP DATABASE")], statements
            assert held.exec_driver_sql("SELECT 1").scalar() == 1
        assert _exists(name)
    finally:
        event.remove(Engine, "before_cursor_execute", seen)
        client.dispose()
        _drop(name)


def test_a_client_arriving_before_the_drop_refuses_the_cleanup_and_keeps_its_session():
    """A client that connects after the count and before the drop: the drop fails, and that is the same refusal."""
    name = _staging()
    late = create_engine(make_url(PG_URL).set(database=name), poolclass=NullPool)
    held: list = []

    def arrive(conn, cursor, statement, *args):
        if not held and statement.lstrip().upper().startswith("DROP DATABASE"):
            c = late.connect()
            held.append(c)
            c.exec_driver_sql("SELECT 1")

    event.listen(Engine, "before_cursor_execute", arrive)
    try:
        with pytest.raises(bk.BackupRefused, match="has a session connected"):
            bk.clean_staging(name, PG_URL)
        assert held and held[0].exec_driver_sql("SELECT 1").scalar() == 1  # its session survived
        assert _exists(name)
    finally:
        event.remove(Engine, "before_cursor_execute", arrive)
        for c in held:
            c.close()
        late.dispose()
        _drop(name)


def _client_on(name: str, url: str = PG_URL):
    """A client session on database `name` (the test's own superuser by default, which needs no CONNECT grant)."""
    e = create_engine(make_url(url).set(database=name), poolclass=NullPool)
    c = e.connect()
    c.exec_driver_sql("SELECT 1")
    return e, c


def _drop_stagings() -> None:
    admin = _admin()
    with admin.connect() as c:  # the test's own teardown, never the engine's way
        for n in c.exec_driver_sql("SELECT datname FROM pg_database WHERE datname LIKE 'erpclaw\\_restore\\_%%'").scalars():
            c.exec_driver_sql(f'DROP DATABASE IF EXISTS "{n}" WITH (FORCE)')
    admin.dispose()


class _Statements:
    """Every statement any engine sends while it is listening."""

    def __init__(self):
        self.seen: list[str] = []

    def __call__(self, conn, cursor, statement, *args):
        self.seen.append(statement)

    def __enter__(self):
        event.listen(Engine, "before_cursor_execute", self)
        return self

    def __exit__(self, *exc):
        event.remove(Engine, "before_cursor_execute", self)

    def starting(self, prefix: str) -> list[str]:
        return [s for s in self.seen if s.lstrip().upper().startswith(prefix)]


def test_a_session_leaving_within_the_wait_does_not_keep_a_failed_restores_staging_database(books, tmp_path,
                                                                                            monkeypatch):
    """A session that has just closed can still be listed for a moment; the cleanup after a failure waits for it."""
    bk.backup(books, tmp_path / "b")
    threads = []

    def leave_soon(step, stage=None, **kw):
        if step == "committed":
            e, c = _client_on(stage.name)

            def close():
                time.sleep(1)
                c.close()
                e.dispose()

            t = threading.Thread(target=close)
            t.start()
            threads.append(t)
            raise bk.BackupRefused("forced failure at committed")

    monkeypatch.setattr(bk, "_HOOK", leave_soon)
    try:
        _refused(tmp_path / "b", "forced failure at committed")
    finally:
        for t in threads:
            t.join(15)
        _drop_stagings()


def test_a_session_leaving_within_the_wait_does_not_refuse_the_publication(books, tmp_path, monkeypatch):
    bk.backup(books, tmp_path / "b")
    threads = []

    def leave_soon(step, stage=None, **kw):
        if step == "proved":
            e, c = _client_on(stage.name)

            def close():
                time.sleep(1)
                c.close()
                e.dispose()

            t = threading.Thread(target=close)
            t.start()
            threads.append(t)

    monkeypatch.setattr(bk, "_HOOK", leave_soon)
    target = _target()
    try:
        assert bk.restore(tmp_path / "b", target).location == target.name
    finally:
        for t in threads:
            t.join(15)
        _drop_stagings()


def test_a_session_that_stays_keeps_a_failed_restores_staging_database_and_no_drop_is_issued(books, tmp_path,
                                                                                              monkeypatch):
    bk.backup(books, tmp_path / "b")
    held = []

    def stay(step, stage=None, **kw):
        if step == "committed":
            held.append(_client_on(stage.name))
            raise bk.BackupRefused("forced failure at committed")

    monkeypatch.setattr(bk, "_HOOK", stay)
    try:
        with _Statements() as sent, pytest.raises(bk.BackupRefused, match="staging left for an administrator"):
            bk.restore(tmp_path / "b", _target())
        assert not sent.starting("DROP DATABASE"), sent.seen
        assert _databases("erpclaw\\_restore\\_%")
        assert held[0][1].exec_driver_sql("SELECT 1").scalar() == 1
    finally:
        for e, c in held:
            c.close()
            e.dispose()
        _drop_stagings()


def test_a_client_arriving_just_before_the_cleanups_drop_is_never_dropped_from_under(books, tmp_path, monkeypatch):
    """The count finds no one; a client connects as the drop is sent: the plain drop fails and the database stays."""
    bk.backup(books, tmp_path / "b")
    stages, late = [], []

    def fail(step, stage=None, **kw):
        if step == "committed":
            stages.append(stage.name)
            raise bk.BackupRefused("forced failure at committed")

    def arrive(conn, cursor, statement, *args):
        if stages and not late and statement.lstrip().upper().startswith("DROP DATABASE") and stages[0] in statement:
            late.append(_client_on(stages[0]))

    monkeypatch.setattr(bk, "_HOOK", fail)
    event.listen(Engine, "before_cursor_execute", arrive)
    try:
        with pytest.raises(bk.BackupRefused, match="staging left for an administrator"):
            bk.restore(tmp_path / "b", _target())
        assert late, "the cleanup never sent its drop"
        assert late[0][1].exec_driver_sql("SELECT 1").scalar() == 1  # the late client survived
        assert _databases(stages[0])
    finally:
        event.remove(Engine, "before_cursor_execute", arrive)
        for e, c in late:
            c.close()
            e.dispose()
        _drop_stagings()


def test_a_session_hidden_from_an_operator_without_statistics_access_refuses_publication_before_its_rename(
        books, tmp_path, monkeypatch, unprivileged):
    """The operator sees the superuser's session with its type hidden; it still counts, and no rename is tried."""
    url = unprivileged[0]
    operator = make_url(url).username
    admin = _admin()
    with admin.connect() as c:
        c.exec_driver_sql(f"ALTER ROLE {operator} CREATEDB")
    bk.backup(books, tmp_path / "b")
    name = f"restored_{secrets.token_hex(4)}_test"
    held, hidden = [], []

    def stay(step, stage=None, **kw):
        if step == "proved":
            held.append(_client_on(stage.name))
            op = create_engine(make_url(url).set(database="postgres"), poolclass=NullPool)
            with op.connect() as c:  # the premise: the operator cannot see that session's type
                hidden.extend(c.exec_driver_sql(
                    "SELECT backend_type FROM pg_stat_activity WHERE datname = %(n)s AND usename = %(u)s",
                    {"n": stage.name, "u": make_url(PG_URL).username}).scalars())
            op.dispose()

    monkeypatch.setattr(bk, "_HOOK", stay)
    try:
        with _Statements() as sent, pytest.raises(bk.BackupRefused, match="not restore's alone"):
            bk.restore(tmp_path / "b", bk.NewPostgresDatabase(url, name))
        assert hidden == [None]
        assert not [s for s in sent.starting("ALTER DATABASE") if f'RENAME TO "{name}"' in s], sent.seen
        assert not _databases(name)
        assert held[0][1].exec_driver_sql("SELECT 1").scalar() == 1
    finally:
        for e, c in held:
            c.close()
            e.dispose()
        _drop_stagings()
        with admin.connect() as c:
            c.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.dispose()


def test_a_session_on_the_database_at_the_publication_rename_is_restores_refusal(books, tmp_path, monkeypatch):
    """With the count bypassed, PostgreSQL's own rename interlock refuses, and that is the same refusal."""
    bk.backup(books, tmp_path / "b")
    held = []

    def stay(step, stage=None, **kw):
        if step == "proved":
            held.append(_client_on(stage.name))

    monkeypatch.setattr(bk, "_HOOK", stay)
    monkeypatch.setattr(bk, "_client_sessions", lambda c, oid: 0)
    target = _target()
    try:
        with pytest.raises(bk.BackupRefused, match="not restore's alone"):
            bk.restore(tmp_path / "b", target)
        assert not _databases(target.name)
    finally:
        for e, c in held:
            c.close()
            e.dispose()
        _drop_stagings()


def test_cleanup_never_lists_a_live_restore(books, tmp_path, monkeypatch):
    bk.backup(books, tmp_path / "b")
    seen = []

    def look(step, **kw):
        if step == "inserted":
            seen.append(bk.stale_stagings(PG_URL))

    monkeypatch.setattr(bk, "_HOOK", look)
    _restored(tmp_path / "b")
    assert seen == [[]]  # its own staging, alive, is not stale


# --- the CLI -------------------------------------------------------------------------

def test_the_cli_backs_up_restores_into_new_books_and_verifies(books, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from erpclaw_cli.main import app

    monkeypatch.setenv("ERPCLAW_DATABASE_URL", books.url.render_as_string(hide_password=False))
    monkeypatch.setenv("ERPCLAW_MIGRATION_URL", PG_URL)  # restore's server, as the owner
    said = lambda r: " ".join(r.output.split())  # noqa: E731 (the console wraps long lines)
    out = CliRunner().invoke(app, ["backup", str(tmp_path / "books")])
    assert out.exit_code == 0 and "Not encrypted" in said(out), out.output
    z = tmp_path / "books.zip"
    assert stat.S_IMODE(os.stat(z).st_mode) == 0o600
    again = CliRunner().invoke(app, ["backup", str(z)])
    assert again.exit_code == 1 and "never overwrites" in said(again)
    final = _target()
    out = CliRunner().invoke(app, ["restore", str(z), "--to-database", final.name])
    assert out.exit_code == 0 and "Restored and proven" in said(out) and "old books are unchanged" in said(out), out.output
    restored = _open(final)
    assert bk.book_diff(books, restored) == []
    restored.dispose()
    out = CliRunner().invoke(app, ["restore", str(z), "--to-database", final.name])
    assert out.exit_code == 1 and "Nothing was published" in said(out) and "restore-verify" in said(out)
    out = CliRunner().invoke(app, ["restore-verify", str(z), "--database", final.name])
    assert out.exit_code == 0 and "equal the backup" in said(out), out.output
    out = CliRunner().invoke(app, ["restore-cleanup"])
    assert out.exit_code == 0 and "Nothing to clean" in said(out)


def test_the_cli_zip_is_private_from_the_start(books, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from erpclaw_cli.main import app

    monkeypatch.setenv("ERPCLAW_DATABASE_URL", books.url.render_as_string(hide_password=False))
    monkeypatch.setattr(os, "chmod", lambda *a, **k: None)  # nothing may rely on tightening afterwards
    old = os.umask(0o022)
    try:
        out = CliRunner().invoke(app, ["backup", str(tmp_path / "early")])
    finally:
        os.umask(old)
    assert out.exit_code == 0, out.output
    assert stat.S_IMODE(os.stat(tmp_path / "early.zip").st_mode) == 0o600


# --- restore safety regressions -------------------------------------------------------------

@pytest.mark.parametrize("param", ["dbname", "options", "service"])
def test_a_server_url_that_could_choose_the_database_is_refused(books, tmp_path, pg_new, param):
    """A dbname parameter must not redirect restore into existing books."""
    bk.backup(books, tmp_path / "b")
    target = pg_new()
    decoy = f"decoy_{secrets.token_hex(3)}_test"  # an existing, empty database
    admin = create_engine(make_url(PG_URL).set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.exec_driver_sql(f'CREATE DATABASE "{decoy}"')
    try:
        url = make_url(PG_URL).update_query_dict({param: decoy if param == "dbname" else "x"})
        with pytest.raises(bk.BackupRefused, match="connection parameters"):
            bk.restore(tmp_path / "b", bk.NewPostgresDatabase(url.render_as_string(hide_password=False), target.name))
        probe = create_engine(make_url(PG_URL).set(database=decoy))
        with probe.connect() as c:
            assert inspect(c).get_table_names() == []  # nothing was written into the existing database
        probe.dispose()
    finally:
        with admin.connect() as c:
            c.exec_driver_sql(f'DROP DATABASE "{decoy}" WITH (FORCE)')
        admin.dispose()


def test_a_working_connection_on_another_database_is_refused(books, tmp_path, pg_new, monkeypatch):
    bk.backup(books, tmp_path / "b")
    decoy = f"decoy_{secrets.token_hex(3)}_test"
    admin = create_engine(make_url(PG_URL).set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.exec_driver_sql(f'CREATE DATABASE "{decoy}"')
    monkeypatch.setattr(bk._PostgresStage, "engine",
                        lambda self: create_engine(make_url(PG_URL).set(database=decoy)))
    try:
        with pytest.raises(bk.BackupRefused, match="not on its staging database"):
            bk.restore(tmp_path / "b", pg_new())
        probe = create_engine(make_url(PG_URL).set(database=decoy))
        with probe.connect() as c:
            assert inspect(c).get_table_names() == []  # nothing was written there
        probe.dispose()
    finally:
        with admin.connect() as c:
            c.exec_driver_sql(f'DROP DATABASE "{decoy}" WITH (FORCE)')
        admin.dispose()


def test_a_backup_without_this_engines_ownership_mark_is_refused(books, tmp_path):
    """Another mark alone is not the engine's books."""
    from erpclaw_core.db import OWNER_MARK

    bk.backup(books, tmp_path / "b")
    _rewrite(tmp_path / "b", "erpclaw_books",
             lambda ls: [x for x in ls if json.loads(x)["mark"] != OWNER_MARK])
    _refused(tmp_path / "b", "ownership mark")
    bk.backup(books, tmp_path / "good")
    good = _target()
    bk.restore(tmp_path / "good", good)
    assert any("ownership mark" in d for d in bk.verify(tmp_path / "b", good))


@pytest.mark.parametrize("ps", ["refused", "timeout", "fails"])
def test_a_restore_whose_process_cannot_be_inspected_is_never_stale(tmp_path, monkeypatch, ps):
    """Uncertain liveness keeps the staging."""
    import socket

    dead_proc = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_proc.wait()
    live, dead = (f"{bk.PG_STAGE_PREFIX}{secrets.token_hex(4)}_{w}" for w in ("live", "dead"))
    admin = create_engine(ADMIN, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    with admin.connect() as c:  # marked as restore marks its staging (the cleanup fixture removes them)
        for name, pid, started in ((live, os.getpid(), "a_start_time_ps_would_have_to_confirm"),
                                   (dead, dead_proc.pid, "whenever")):
            c.exec_driver_sql(f'CREATE DATABASE "{name}"')
            c.exec_driver_sql(f"COMMENT ON DATABASE \"{name}\" IS '{bk.PG_MARKER} 0 {socket.gethostname()} {pid} {started}'")
    admin.dispose()

    def no_ps(*a, **k):
        if ps == "refused":
            raise PermissionError("Operation not permitted")
        if ps == "timeout":
            raise subprocess.TimeoutExpired("ps", 10)
        return subprocess.CompletedProcess(a, 1, "", "ps: failed")

    monkeypatch.setattr(bk.subprocess, "run", no_ps)
    stale = bk.stale_stagings(PG_URL)
    assert live not in stale  # alive or uncertain: kept
    assert dead in stale  # certainly gone: listed


PG_CRASH_AT = """
import os, sys
from pathlib import Path
from erpclaw_core import backup as bk
point = sys.argv[4]
def hook(step, **kw):
    if step == point:
        os._exit(17)
bk._HOOK = hook
bk.restore(Path(sys.argv[1]), bk.NewPostgresDatabase(sys.argv[2], sys.argv[3]))
"""


@pytest.mark.parametrize("point", ["staged", "staged_ready", "inserted", "committed", "proved", "before_publish",
                                   "publishing"])
def test_postgresql_crashes_before_publication_leave_no_final_name(books, tmp_path, pg_new, point):
    """Exercise every restore crash point on PostgreSQL, including those outside publication."""
    bk.backup(books, tmp_path / "b")
    target = pg_new()
    proc = subprocess.run([sys.executable, "-c", PG_CRASH_AT, str(tmp_path / "b"), PG_URL, target.name, point],
                          capture_output=True, text=True)
    assert proc.returncode == 17, proc.stderr
    admin = create_engine(make_url(PG_URL).set(database="postgres"))
    with admin.connect() as c:
        assert not c.exec_driver_sql("SELECT 1 FROM pg_database WHERE datname = %(n)s", {"n": target.name}).first()
    admin.dispose()
    stale = bk.stale_stagings(PG_URL)
    assert len(stale) == 1
    _until_no_session(admin, stale[0])
    bk.clean_staging(stale[0], PG_URL)
    bk.restore(tmp_path / "b", target)
    assert bk.verify(tmp_path / "b", target) == []


def test_a_login_held_in_startup_makes_the_barrier_refuse(books, tmp_path, pg_new, monkeypatch):
    """A deterministic login in progress, held by a login
    event trigger (PostgreSQL 17 and later), while the rename barrier runs."""
    bk.backup(books, tmp_path / "b")
    role, password = f"erpclaw_slow_{secrets.token_hex(3)}", secrets.token_hex(12)
    admin = create_engine(make_url(PG_URL).set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        if c.exec_driver_sql("SHOW server_version_num").scalar() < "170000":
            pytest.skip("login event triggers need PostgreSQL 17")
        c.exec_driver_sql(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'")
    threads = []

    def hold(step, stage=None, **kw):
        if step != "staged":
            return
        su = create_engine(make_url(PG_URL).set(database=stage.name), isolation_level="AUTOCOMMIT")
        with su.connect() as c:
            c.exec_driver_sql(f'GRANT CONNECT ON DATABASE "{stage.name}" TO {role}')
            c.exec_driver_sql("CREATE FUNCTION slow_login() RETURNS event_trigger LANGUAGE plpgsql AS "
                              f"$$ BEGIN IF session_user = '{role}' THEN PERFORM pg_sleep(4); END IF; END $$")
            c.exec_driver_sql("CREATE EVENT TRIGGER slow_login ON login EXECUTE FUNCTION slow_login()")
        su.dispose()
        url = make_url(PG_URL).set(username=role, password=password, database=stage.name)

        def login():
            e = create_engine(url)
            try:
                with e.connect() as c:
                    c.exec_driver_sql("SELECT pg_sleep(2)")
            except Exception:  # noqa: BLE001 - the database may be renamed or dropped under it
                pass
            finally:
                e.dispose()

        t = threading.Thread(target=login)
        t.start()
        threads.append(t)
        with admin.connect() as c:  # wait until the login is on the server, held in its startup by the trigger
            for _ in range(100):
                if c.exec_driver_sql("SELECT count(*) FROM pg_stat_activity WHERE usename = %(r)s",
                                     {"r": role}).scalar():
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("the held login never reached the server")

    monkeypatch.setattr(bk, "_HOOK", hold)
    try:
        with pytest.raises(bk.BackupRefused, match="another session|not restore's alone|holds relations"):
            bk.restore(tmp_path / "b", pg_new())
    finally:
        for t in threads:
            t.join(15)
        with admin.connect() as c:
            for n in c.exec_driver_sql("SELECT datname FROM pg_database WHERE datname LIKE 'erpclaw_restore_%%'").scalars():
                c.exec_driver_sql(f'DROP DATABASE IF EXISTS "{n}" WITH (FORCE)')
            c.exec_driver_sql(f"DROP OWNED BY {role}")
            c.exec_driver_sql(f"DROP ROLE {role}")
        admin.dispose()


def test_a_start_time_unknown_when_marked_keeps_the_staging_database(books, tmp_path, pg_new, monkeypatch):
    """Inspection failing at marking and working at cleanup must not make a live restore stale."""
    bk.backup(books, tmp_path / "b")
    real = bk._process_started
    seen = []

    def look(step, **kw):
        if step == "inserted":
            monkeypatch.setattr(bk, "_process_started", real)  # inspection works again during cleanup
            seen.append(bk.stale_stagings(PG_URL))

    monkeypatch.setattr(bk, "_process_started", lambda pid: None)  # unknown when the marker is written
    monkeypatch.setattr(bk, "_HOOK", look)
    target = pg_new()
    bk.restore(tmp_path / "b", target)
    assert seen == [[]]  # the live restore was never stale
    assert bk.verify(tmp_path / "b", target) == []



@pytest.fixture
def extra_plugin(monkeypatch):
    """A loaded plugin with its own branch and table."""
    from sqlalchemy import Column, String, Table

    from erpclaw_core.db import metadata
    from erpclaw_core.plugins import REGISTRY
    from test_plugins import EXTRA_BRANCH, plug

    t = Table("extra_thing", metadata, Column("id", String(36), primary_key=True), info={"owner": "extra"})
    monkeypatch.setitem(REGISTRY.plugins, "extra", plug("extra", owned_tables=["extra_thing"], migrations=EXTRA_BRANCH,
                                                        after_core_revision="0004"))
    yield
    metadata.remove(t)


def test_a_loaded_plugin_never_installed_in_the_books_is_not_in_their_backup(books, tmp_path, extra_plugin):
    manifest = bk.backup(books, tmp_path / "b")
    assert "extra_thing" not in manifest["tables"]
    target = _restored(tmp_path / "b")
    assert bk.book_diff(books, target) == []


def test_an_uninstalled_plugins_tables_and_branch_stay_in_the_backup(books, tmp_path, extra_plugin):
    from erpclaw_core.plugins import install_plugin, uninstall_plugin

    install_plugin(books, "extra")
    with books.begin() as conn:
        conn.exec_driver_sql("INSERT INTO extra_thing VALUES ('kept')")
        uninstall_plugin(conn, "extra")
    manifest = bk.backup(books, tmp_path / "b")
    assert manifest["tables"]["extra_thing"]["rows"] == 1
    target = _restored(tmp_path / "b")
    assert bk.book_diff(books, target) == []
    with target.connect() as conn:
        assert "extra_0001" in conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalars().all()
    with books.begin() as conn:
        conn.exec_driver_sql("DROP TABLE extra_thing")
        conn.exec_driver_sql("DELETE FROM alembic_version WHERE version_num = 'extra_0001'")


# --- the books' identity ---------------------------------------------------------------------------------------

def test_a_restore_copies_the_books_identity_exactly_under_a_new_name(books, tmp_path):
    def books_id(conn):
        return conn.exec_driver_sql("SELECT books_id FROM books_identity").scalar_one()

    with books.connect() as conn:
        source = books_id(conn)
        source_name = conn.exec_driver_sql("SELECT current_database()").scalar()
    manifest = bk.backup(books, tmp_path / "b")
    assert manifest["books_id"] == source
    assert [json.loads(x) for x in (tmp_path / "b" / "tables" / "books_identity.jsonl").read_text().splitlines()] == \
        [{"id": 1, "books_id": source}]
    target = _target()
    restored = _restored_to(tmp_path / "b", target)
    try:
        with restored.connect() as conn:
            assert books_id(conn) == source
            assert conn.exec_driver_sql("SELECT current_database()").scalar() == target.name != source_name
    finally:
        restored.dispose()
    assert bk.verify(tmp_path / "b", target) == []  # the exact proof covers it


def _restored_to(src, target):
    assert bk.restore(src, target).location == target.name
    return _open(target)


OTHER_ID = "00000000-0000-4000-8000-000000000000"


@pytest.mark.parametrize("change", ["no row", "another id in the table", "another id in the manifest"])
def test_a_backup_whose_identity_is_missing_or_does_not_match_is_refused(books, tmp_path, change):
    bk.backup(books, tmp_path / "b")
    if change == "no row":
        _rewrite(tmp_path / "b", "books_identity", lambda ls: [])
    elif change == "another id in the table":
        _rewrite(tmp_path / "b", "books_identity", lambda ls: [json.dumps({"id": 1, "books_id": OTHER_ID})])
    else:
        man = json.loads((tmp_path / "b" / "MANIFEST.json").read_text())
        (tmp_path / "b" / "MANIFEST.json").write_text(json.dumps({**man, "books_id": OTHER_ID}))
    _refused(tmp_path / "b", "books identity")
    bk.backup(books, tmp_path / "good")
    good = _target()
    bk.restore(tmp_path / "good", good)
    found = bk.verify(tmp_path / "b", good)
    assert any("books identity" in d for d in found)
    if change != "another id in the manifest":
        assert "books_identity differs from the backup" in found
