"""The ledger line's full shape: the transaction amount's rate and the functional debit and credit, each line's kind,
a reserved reference to an insert-only dimension set (held empty in this release), and the versioned line hash that
covers all of them, with the set's digest as its last field. Verification recomputes each referenced set's digest from
its content."""
from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from erpclaw_core import backup as bk, ledger, models as m
from erpclaw_core.common import company_ctx, new_id, now

D = date(2026, 10, 1)

# The tests below that read or write the new columns need the schema half of this change (the line's columns and
# checks, the dimension_set table, the posting's hash format) in models.py and its core revision, which change in
# another branch: until it lands they are skipped, never silently passed.
shape = pytest.mark.skipif(not hasattr(m, "dimension_set"),
                           reason="needs the ledger line's table changes in models.py and its core revision")

# --- the hash format, written out here, not taken from the engine ------------------------------------------------

HEADER = {"company_id": "c1", "seq": 1, "posting_date": "2026-10-01", "voucher_type": "journal", "voucher_id": "v1",
          "memo": None, "reverses_posting_id": None, "operation_key": None, "created_by": "owner", "ledger": "general"}
ROW = {"line_no": 1, "account_id": "a1", "party_id": None, "debit": Decimal("5.00"), "credit": Decimal("0.00"),
       "currency": "USD", "rate": Decimal("1"), "functional_currency": "USD", "functional_debit": Decimal("5.00"),
       "functional_credit": Decimal("0.00"), "kind": "measured"}
CANONICAL = ('{"format":2,"company_id":"c1","seq":1,"posting_date":"2026-10-01","voucher_type":"journal",'
             '"voucher_id":"v1","memo":null,"reverses_posting_id":null,"operation_key":null,"created_by":"owner",'
             '"ledger":"general","lines":[{"line_no":1,"account_id":"a1","party_id":null,"debit":"5.00",'
             '"credit":"0.00","currency":"USD","rate":"1","functional_currency":"USD","functional_debit":"5.00",'
             '"functional_credit":"0.00","kind":"measured","dimset":""}]}')


def _header():
    return ledger._header("c1", 1, D, "journal", "v1", None, None, None, "owner")


def test_the_canonical_form_is_format_2_with_every_field_in_a_fixed_order_and_dimset_last():
    assert ledger.HASH_FORMAT == 2
    assert ledger._canonical(_header(), [ledger._entry_dict(ROW, "")]) == CANONICAL.encode()
    assert ledger._hash(ledger.GENESIS, _header(), [ledger._entry_dict(ROW, "")]) == \
        hashlib.sha256(ledger.GENESIS.encode() + CANONICAL.encode()).hexdigest()
    assert ledger.LINE_FIELDS[-1] == "dimset"
    assert ledger.LINE_FIELDS.index("dimset") > max(ledger.LINE_FIELDS.index(f) for f in
                                                    ("rate", "functional_debit", "functional_credit"))


def test_a_general_postings_header_names_its_ledger_too():
    """Format 1 left `ledger` out of a general posting's header; format 2 always carries it."""
    assert json.loads(ledger._canonical(_header(), []))["ledger"] == "general"


@pytest.mark.parametrize("field", [*ROW, "dimset"])
def test_changing_any_line_field_changes_the_line_hash(field):
    changed = {"line_no": 2, "account_id": "a2", "party_id": "p1", "debit": Decimal("6.00"),
               "credit": Decimal("1.00"), "currency": "EUR", "rate": Decimal("1.1"), "functional_currency": "EUR",
               "functional_debit": Decimal("6.00"), "functional_credit": Decimal("1.00"), "kind": "clearing"}
    base = ledger._hash(ledger.GENESIS, _header(), [ledger._entry_dict(ROW, "")])
    if field == "dimset":
        line = ledger._entry_dict(ROW, ledger.dimension_digest(ledger.dimension_content({"fund": "f1"})))
    else:
        line = ledger._entry_dict({**ROW, field: changed[field]}, "")
    assert ledger._hash(ledger.GENESIS, _header(), [line]) != base


@pytest.mark.parametrize("field", list(HEADER))
def test_changing_any_header_field_changes_the_hash(field):
    args = dict(company_id="c1", seq=1, posting_date=D, voucher_type="journal", voucher_id="v1", memo=None,
                reverses=None, operation_key=None, actor="owner", ledger="general")
    key = {"posting_date": "posting_date", "reverses_posting_id": "reverses", "created_by": "actor"}.get(field, field)
    args[key] = {"seq": 2, "posting_date": date(2026, 10, 2), "ledger": "sub"}.get(key, "changed")
    line = [ledger._entry_dict(ROW, "")]
    assert ledger._hash(ledger.GENESIS, ledger._header(**args), line) != \
        ledger._hash(ledger.GENESIS, _header(), line)


def test_a_dimension_sets_content_is_canonical_and_its_digest_is_the_contents_sha256():
    content = ledger.dimension_content({"project": "p9", "fund": "f1"})
    assert content == '{"fund":"f1","project":"p9"}'
    assert ledger.dimension_digest(content) == hashlib.sha256(content.encode()).hexdigest()
    assert ledger.dimension_set_holds(content, ledger.dimension_digest(content))


@pytest.mark.parametrize("content,digest", [
    ('{"fund":"f2"}', hashlib.sha256(b'{"fund":"f1"}').hexdigest()),  # content edited, digest kept
    ('{"project":"p9","fund":"f1"}', hashlib.sha256(b'{"project":"p9","fund":"f1"}').hexdigest()),  # not sorted
    ('{"fund": "f1"}', hashlib.sha256(b'{"fund": "f1"}').hexdigest()),  # not the canonical spelling
    ('not json', hashlib.sha256(b"not json").hexdigest()),
    ('["fund","f1"]', hashlib.sha256(b'["fund","f1"]').hexdigest()),  # not a map
    ('{"fund":1}', hashlib.sha256(b'{"fund":1}').hexdigest()),  # a value is an id, a string
    ('{"fund":"f1"}', hashlib.sha256(b'{"fund":"f1"}').hexdigest().upper()),  # the digest is lower-case hex
])
def test_a_set_whose_digest_does_not_hold_for_its_canonical_content_is_refused(content, digest):
    assert not ledger.dimension_set_holds(content, digest)


# --- the posting service takes no dimension, and every voucher type fits ------------------------------------------

def test_the_posting_service_refuses_a_dimension_argument(engine):
    names = [*inspect.signature(ledger.post).parameters, *inspect.signature(ledger.check_post).parameters,
             *(f.name for f in dataclasses.fields(ledger.Line))]
    assert not [n for n in names if "dimension" in n]
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        bank, sales = (ledger.account_by_number(ctx, n)["id"] for n in ("1100", "4100"))
        before = _counts(conn)
        for kw in ("dimensions", "dimension_set_id", "dimension_set"):
            with pytest.raises(TypeError):
                ledger.Line(bank, debit=Decimal("1.00"), **{kw: {"fund": "f1"}})
            with pytest.raises(TypeError):
                ledger.post(ctx, D, "journal", new_id(), [ledger.Line(bank, debit=Decimal("1.00")),
                                                          ledger.Line(sales, credit=Decimal("1.00"))],
                            **{kw: {"fund": "f1"}})
        assert _counts(conn) == before
        conn.rollback()


def test_every_registered_voucher_type_fits_with_its_reversal_suffixes():
    from erpclaw_core.plugins import REGISTRY, load_plugins

    load_plugins()
    width = m.posting.c.voucher_type.type.length
    types = {ledger.OPENING, ledger.CLOSING} | {t for p in REGISTRY.plugins.values() for t in p.manifest.voucher_types}
    assert {"sales_invoice", "sales_payment", "stock_receipt"} <= types  # the registry was read
    too_long = [t + s for t in sorted(types) for s in (":reversal", ":fx_reversal") if len(t + s) > width]
    assert too_long == []


# --- new books: the chain, the stored line, the checks -----------------------------------------------------------

def _counts(conn):
    return tuple(conn.execute(select(func.count()).select_from(t)).scalar_one() for t in (m.posting, m.ledger_entry))


def _text(v):
    return None if v is None else format(v, "f")


def _independent_hash(prev: str, p: dict, entries: list[dict], dimsets: dict) -> str:
    """Format 2, built here from the stored rows, field by field."""
    line_fields = ("line_no", "account_id", "party_id", "debit", "credit", "currency", "rate",
                   "functional_currency", "functional_debit", "functional_credit", "kind")
    money = {"debit", "credit", "rate", "functional_debit", "functional_credit"}
    body = {"format": 2, "company_id": p["company_id"], "seq": p["seq"], "posting_date": p["posting_date"].isoformat(),
            "voucher_type": p["voucher_type"], "voucher_id": p["voucher_id"], "memo": p["memo"],
            "reverses_posting_id": p["reverses_posting_id"], "operation_key": p["operation_key"],
            "created_by": p["created_by"], "ledger": p["ledger"],
            "lines": [{**{f: _text(e[f]) if f in money else e[f] for f in line_fields},
                       "dimset": dimsets.get(e["dimension_set_id"], "")} for e in entries]}
    return hashlib.sha256(prev.encode() + json.dumps(body, separators=(",", ":")).encode()).hexdigest()


def _journal(ctx, amount, d=D):
    bank, sales = (ledger.account_by_number(ctx, n)["id"] for n in ("1100", "4100"))
    return ledger.post(ctx, d, "journal", new_id(), [ledger.Line(bank, debit=Decimal(amount)),
                                                     ledger.Line(sales, credit=Decimal(amount))])


@shape
def test_new_books_chain_verifies_with_every_field_present(engine):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        r = _journal(ctx, "12.34")
        ledger.reverse(ctx, r.posting_id, date(2026, 10, 2))
        assert ledger.verify_chain(conn, ctx.company_id) is None
        prev = ledger.GENESIS
        postings = conn.execute(select(m.posting).where(m.posting.c.company_id == ctx.company_id)
                                .order_by(m.posting.c.seq)).mappings().all()
        assert len(postings) >= 3  # the demo's own postings and these two
        for p in postings:
            entries = conn.execute(select(m.ledger_entry).where(m.ledger_entry.c.posting_id == p["id"])
                                   .order_by(m.ledger_entry.c.line_no)).mappings().all()
            assert p["hash_format"] == 2
            assert p["hash"] == _independent_hash(prev, p, entries, {})
            for e in entries:
                assert (e["rate"], e["functional_currency"], e["functional_debit"], e["functional_credit"], e["kind"],
                        e["dimension_set_id"]) == (Decimal("1"), ctx.currency, e["debit"], e["credit"], "measured",
                                                   None)
                assert e["currency"] == ctx.currency
            prev = p["hash"]
        assert conn.execute(select(func.count()).select_from(m.dimension_set)).scalar_one() == 0


@shape
def test_a_reversal_mirrors_every_field_of_each_line(engine):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        r = _journal(ctx, "40.00")
        rev = ledger.reverse(ctx, r.posting_id, date(2026, 10, 3))
        rows = lambda pid: conn.execute(select(m.ledger_entry).where(m.ledger_entry.c.posting_id == pid)  # noqa: E731
                                        .order_by(m.ledger_entry.c.line_no)).mappings().all()
        swap = {"debit": "credit", "credit": "debit", "functional_debit": "functional_credit",
                "functional_credit": "functional_debit"}
        skip = {"id", "posting_id"}
        for o, v in zip(rows(r.posting_id), rows(rev.posting_id), strict=True):
            assert {k: v[swap.get(k, k)] for k in v.keys() if k not in skip} == \
                {k: o[k] for k in o.keys() if k not in skip}


def _entry_values(ctx, post_id, account_id, **over):
    values = dict(id=new_id(), company_id=ctx.company_id, posting_id=post_id, line_no=90, account_id=account_id,
                  party_id=None, debit=Decimal("1.00"), credit=Decimal("0.00"), currency="USD", rate=Decimal("1"),
                  functional_currency="USD", functional_debit=Decimal("1.00"), functional_credit=Decimal("0.00"),
                  kind="measured", dimension_set_id=None)
    return {**values, **over}


def _a_set(conn, company_id, values=None) -> str:
    content = ledger.dimension_content(values or {"fund": "f1"})
    sid = new_id()
    conn.execute(m.dimension_set.insert().values(id=sid, company_id=company_id, content=content,
                                                 digest=ledger.dimension_digest(content), created_at=now()))
    return sid


V30 = "ck_entry_functional_v30"


@shape
@pytest.mark.parametrize("over,constraint,without", [
    ({"rate": Decimal("2")}, V30, ()),
    ({"rate": Decimal("1.5")}, V30, ()),
    ({"rate": None}, V30, ()),
    ({"functional_debit": Decimal("2.00")}, V30, ()),
    ({"functional_credit": Decimal("1.00")}, V30, ()),
    ({"currency": "EUR"}, V30, ()),  # a line in another currency, until the multi-currency wave
    ({"kind": "clearing"}, V30, ()),
    ({"currency": "EUR", "functional_currency": "EUR"}, "fk_entry_functional_currency", ()),  # not the company's
    ({"dimension_set_id": "SET"}, "ck_entry_no_dimension_v30", ()),
    # the checks that stay when this release's check is dropped, each seen with it out of the way
    ({"kind": "other"}, "ck_entry_kind", (V30,)),
    ({"rate": None}, "ck_entry_rate", (V30,)),  # only an adjustment line has no rate
    ({"rate": Decimal("1.0")}, "ck_entry_rate", (V30,)),  # one spelling per rate: no trailing zero
    ({"rate": Decimal("1.0000000001")}, "ck_entry_rate", (V30,)),  # at most nine places
    ({"rate": Decimal("1.000000001")}, None, (V30,)),  # nine places are accepted
    ({"rate": Decimal("0.5"), "kind": "adjustment"}, "ck_entry_rate", (V30,)),  # an adjustment line has no rate
    ({"functional_debit": Decimal("1.001")}, "ck_entry_functional_debit_scale", ()),
    ({"functional_credit": Decimal("0.0")}, "ck_entry_functional_credit_scale", ()),
])
def test_the_line_checks_refuse_every_other_shape(engine, over, constraint, without):
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        post_id = conn.execute(select(m.posting.c.id).where(m.posting.c.company_id == ctx.company_id)).scalars().first()
        bank = ledger.account_by_number(ctx, "1100")["id"]
        sid = _a_set(conn, ctx.company_id)
        conn.commit()
        if over.get("dimension_set_id") == "SET":
            over = {**over, "dimension_set_id": sid}
        trans = conn.begin()
        for name in without:
            conn.execute(text(f"ALTER TABLE ledger_entry DROP CONSTRAINT {name}"))
        if constraint is None:
            conn.execute(m.ledger_entry.insert().values(**_entry_values(ctx, post_id, bank, **over)))
        else:
            with pytest.raises(IntegrityError, match=constraint):
                with conn.begin_nested():
                    conn.execute(m.ledger_entry.insert().values(**_entry_values(ctx, post_id, bank, **over)))
        trans.rollback()
        trans = conn.begin()  # the same line in this release's shape is accepted, so only the change was refused
        conn.execute(m.ledger_entry.insert().values(**_entry_values(ctx, post_id, bank)))
        trans.rollback()


@shape
def test_each_new_check_in_the_books_is_the_one_the_models_declare(engine):
    """The books are made by the revisions; each new check, as PostgreSQL normalises it, equals the models' own
    check put on a scratch copy of the table's columns."""
    names = {"ledger_entry": ("ck_entry_functional_v30", "ck_entry_no_dimension_v30", "ck_entry_rate", "ck_entry_kind",
                              "ck_entry_functional_debit_scale", "ck_entry_functional_credit_scale"),
             "posting": ("ck_posting_hash_format",),
             "dimension_set": ("ck_dimension_set_digest", "ck_dimension_set_content")}
    defs = "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = :t ::regclass AND conname = :n"
    with engine.connect() as conn:
        for table, wanted in names.items():
            declared = {c.name: str(c.sqltext) for c in m.metadata.tables[table].constraints if c.name in wanted}
            assert set(declared) == set(wanted), table
            conn.execute(text(f"CREATE TEMP TABLE scratch_{table} (LIKE {table})"))
            for name, sql in declared.items():
                conn.execute(text(f"ALTER TABLE scratch_{table} ADD CONSTRAINT {name} CHECK ({sql})"))
                assert conn.execute(text(defs), {"t": table, "n": name}).scalar_one() == \
                    conn.execute(text(defs), {"t": f"scratch_{table}", "n": name}).scalar_one(), name
        conn.rollback()


# --- tampering: the chain covers every field, and a set's content is checked against its digest ------------------

def _tamperable(conn):
    """Triggers off and the shape checks dropped, inside the caller's transaction (rolled back)."""
    for table in ("posting", "ledger_entry", "dimension_set"):
        conn.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER USER"))
    for name in ("ck_entry_functional_v30", "ck_entry_no_dimension_v30", "ck_entry_one_side"):
        conn.execute(text(f"ALTER TABLE ledger_entry DROP CONSTRAINT {name}"))


def _three_journals(engine):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        results = [_journal(ctx, f"{i + 1}.00") for i in range(3)]
    return ctx.company_id, results[1]


@shape
@pytest.mark.parametrize("column,value", [
    ("line_no", 7), ("account_id", "ACCOUNT"), ("party_id", "PARTY"), ("debit", "3.00"), ("credit", "1.00"),
    ("currency", "EUR"), ("rate", "1.5"), ("functional_currency", "EUR"), ("functional_debit", "3.00"),
    ("functional_credit", "1.00"), ("kind", "clearing"), ("dimension_set_id", "SET"),
])
def test_changing_any_stored_line_field_breaks_the_chain(engine, column, value):
    cid, target = _three_journals(engine)
    with engine.connect() as conn:
        trans = conn.begin()
        _tamperable(conn)
        if column == "functional_currency":  # the company check would refuse it first; the chain must see it too
            conn.execute(text("ALTER TABLE ledger_entry DROP CONSTRAINT fk_entry_functional_currency"))
        ctx = company_ctx(conn, company_id=cid)
        value = {"ACCOUNT": lambda: ledger.account_by_number(ctx, "6900")["id"],
                 "PARTY": lambda: conn.execute(select(m.party.c.id).where(m.party.c.company_id == cid)).scalars().first(),
                 "SET": lambda: _a_set(conn, cid)}.get(value, lambda: value)()
        conn.execute(text(f"UPDATE ledger_entry SET {column} = :v WHERE posting_id = :p AND line_no = 1"),
                     {"v": value, "p": target.posting_id})
        assert ledger.verify_chain(conn, cid) == target.seq
        trans.rollback()


@shape
@pytest.mark.parametrize("column,value", [
    ("posting_date", "2026-10-02"), ("voucher_type", "journal2"), ("voucher_id", "other"), ("memo", "edited"),
    ("operation_key", "k"), ("created_by", "someone"), ("ledger", "sub"), ("hash_format", 3),
])
def test_changing_any_stored_header_field_or_the_format_breaks_the_chain(engine, column, value):
    cid, target = _three_journals(engine)
    with engine.connect() as conn:
        trans = conn.begin()
        _tamperable(conn)
        if column == "hash_format":
            # First prove the normal database guard, then model an owner who has bypassed it.
            with pytest.raises(IntegrityError, match="ck_posting_hash_format"):
                with conn.begin_nested():
                    conn.execute(text("UPDATE posting SET hash_format = :v WHERE id = :p"),
                                 {"v": value, "p": target.posting_id})
            conn.execute(text("ALTER TABLE posting DROP CONSTRAINT ck_posting_hash_format"))
        conn.execute(text(f"UPDATE posting SET {column} = :v WHERE id = :p"), {"v": value, "p": target.posting_id})
        assert ledger.verify_chain(conn, cid) == target.seq
        trans.rollback()


def _reseal(conn, company_id) -> None:
    """Recompute every posting's link and hash and the head from the stored rows: a chain an attacker rebuilt."""
    prev = ledger.GENESIS
    for p in conn.execute(select(m.posting).where(m.posting.c.company_id == company_id)
                          .order_by(m.posting.c.seq)).mappings().all():
        h = ledger._posting_hash(conn, p, prev)
        assert h is not None
        conn.execute(text("UPDATE posting SET prev_hash = :pr, hash = :h WHERE id = :i"),
                     {"pr": prev, "h": h, "i": p["id"]})
        prev = h
    conn.execute(text("UPDATE posting_chain_head SET last_hash = :h WHERE company_id = :c"), {"h": prev, "c": company_id})


@shape
def test_a_sets_content_edited_with_its_digest_and_the_chain_kept_is_refused_by_the_chain_and_restores_proof(engine):
    cid, target = _three_journals(engine)
    with engine.connect() as conn:
        trans = conn.begin()
        _tamperable(conn)
        sid = _a_set(conn, cid, {"fund": "f1"})
        conn.execute(text("UPDATE ledger_entry SET dimension_set_id = :s WHERE posting_id = :p AND line_no = 1"),
                     {"s": sid, "p": target.posting_id})
        _reseal(conn, cid)  # a line that names a set, its digest inside the hash: it verifies
        assert ledger.verify_chain(conn, cid) is None
        bk._integrity(conn)
        conn.execute(text("UPDATE dimension_set SET content = :c WHERE id = :s"),
                     {"c": ledger.dimension_content({"fund": "f2"}), "s": sid})  # another fund, the digest kept
        assert ledger.verify_chain(conn, cid) == target.seq
        with pytest.raises(bk.BackupRefused, match="chain does not verify"):
            bk._integrity(conn)
        trans.rollback()


@shape
def test_restore_integrity_refuses_foreign_currency_even_if_the_row_guard_is_bypassed(engine):
    cid, target = _three_journals(engine)
    with engine.connect() as conn:
        trans = conn.begin()
        _tamperable(conn)
        conn.execute(text("UPDATE ledger_entry SET currency = 'EUR' WHERE posting_id = :p AND line_no = 1"),
                     {"p": target.posting_id})
        with pytest.raises(bk.BackupRefused, match="not the company's currency"):
            bk._integrity(conn)
        trans.rollback()


@shape
def test_a_line_cannot_name_a_set_of_another_company(engine):
    """The reference is company-scoped: the foreign key refuses it (and the chain does not trust a set it cannot
    find in the line's own company, below)."""
    from erpclaw_core.setup import create_company

    cid, target = _three_journals(engine)
    other = create_company(engine, "Other Dimensions Co")
    with engine.connect() as conn:
        trans = conn.begin()
        sid = _a_set(conn, other)
        conn.execute(text("ALTER TABLE ledger_entry DROP CONSTRAINT ck_entry_no_dimension_v30"))
        with pytest.raises(IntegrityError, match="fk_entry_dimension_set"):
            with conn.begin_nested():
                conn.execute(m.ledger_entry.insert().values(
                    **_entry_values(company_ctx(conn, company_id=cid), target.posting_id,
                                    ledger.account_by_number(company_ctx(conn, company_id=cid), "1100")["id"],
                                    dimension_set_id=sid)))
        trans.rollback()


@shape
def test_a_chain_rebuilt_over_a_set_of_another_company_does_not_verify(engine):
    """With the foreign key out of the way and the chain rebuilt around another company's (valid) set, verification
    still refuses: it looks a set up only in the line's own company."""
    from erpclaw_core.setup import create_company

    cid, target = _three_journals(engine)
    other = create_company(engine, "Foreign Set Co")
    with engine.connect() as conn:
        trans = conn.begin()
        _tamperable(conn)
        conn.execute(text("ALTER TABLE ledger_entry DROP CONSTRAINT fk_entry_dimension_set"))
        sid = _a_set(conn, other)
        digest = conn.execute(select(m.dimension_set.c.digest).where(m.dimension_set.c.id == sid)).scalar_one()
        conn.execute(text("UPDATE ledger_entry SET dimension_set_id = :s WHERE posting_id = :p AND line_no = 1"),
                     {"s": sid, "p": target.posting_id})
        prev = ledger.GENESIS
        for p in conn.execute(select(m.posting).where(m.posting.c.company_id == cid)
                              .order_by(m.posting.c.seq)).mappings().all():
            entries = conn.execute(select(m.ledger_entry).where(m.ledger_entry.c.posting_id == p["id"])
                                   .order_by(m.ledger_entry.c.line_no)).mappings().all()
            h = _independent_hash(prev, p, entries, {sid: digest})
            conn.execute(text("UPDATE posting SET prev_hash = :pr, hash = :h WHERE id = :i"),
                         {"pr": prev, "h": h, "i": p["id"]})
            prev = h
        conn.execute(text("UPDATE posting_chain_head SET last_hash = :h WHERE company_id = :c"), {"h": prev, "c": cid})
        assert ledger.verify_chain(conn, cid) == target.seq
        trans.rollback()


# --- the table: carried by backup and restore, insert-only ----------------------------------------------------------

@pytest.fixture
def restores():
    """test_backup's restore helper; every database it restored into removed afterwards."""
    import test_backup

    yield test_backup._restored
    test_backup._drop_made()


@shape
def test_backup_and_restore_round_trip_the_empty_dimension_set_table(engine, tmp_path, restores):
    manifest = bk.backup(engine, tmp_path / "b")
    assert manifest["tables"]["dimension_set"]["rows"] == 0
    assert (tmp_path / "b" / "tables" / "dimension_set.jsonl").read_text() == ""
    restored = restores(tmp_path / "b")
    assert bk.book_diff(engine, restored) == []
    with restored.connect() as conn:
        assert conn.execute(select(func.count()).select_from(m.dimension_set)).scalar_one() == 0
        for cid in conn.execute(select(m.company.c.id)).scalars():
            assert ledger.verify_chain(conn, cid) is None


@shape
@pytest.mark.parametrize("sql", ["UPDATE dimension_set SET content = content", "DELETE FROM dimension_set",
                                 "TRUNCATE dimension_set CASCADE"])  # CASCADE: no foreign key refuses it first
def test_no_role_changes_or_removes_a_dimension_set(engine, sql):
    with engine.begin() as conn:
        _a_set(conn, company_ctx(conn).company_id)
    with pytest.raises(DBAPIError, match="refused"):  # the trigger, for the owner too
        with engine.begin() as conn:
            conn.execute(text(sql))
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(m.dimension_set)).scalar_one() == 1


@shape
def test_the_runtime_role_inserts_a_dimension_set_and_cannot_update_or_delete_one(engine, runtime_login):
    from erpclaw_core.db import make_engine

    with engine.begin() as conn:
        cid = company_ctx(conn).company_id
    rt = make_engine(runtime_login)
    try:
        with rt.begin() as conn:
            _a_set(conn, cid, {"fund": "f9"})
        for sql in ("UPDATE dimension_set SET content = content", "DELETE FROM dimension_set",
                    "TRUNCATE dimension_set"):
            with pytest.raises(DBAPIError, match="permission denied"):
                with rt.begin() as conn:
                    conn.execute(text(sql))
    finally:
        rt.dispose()
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(m.dimension_set)).scalar_one() == 1


# --- the revision ---------------------------------------------------------------------------------------------------

@shape
@pytest.mark.parametrize("case", ["marked_empty", "owner_only", "identity_only", "company_only", "with_posting"])
def test_the_revision_refuses_books_that_already_hold_postings(engine, case):
    """Books from before the line's full shape are disposable: the revision says so and changes nothing."""
    from alembic.script import ScriptDirectory

    from conftest import drop_everything
    from erpclaw_core.migrate import CORE_VERSIONS, config, upgrade

    scripts = [s for s in ScriptDirectory.from_config(config(locations=[CORE_VERSIONS])).walk_revisions()
               if s.path.endswith("_ledger_line_shape.py")]
    assert len(scripts) == 1
    this, before = scripts[0].revision, scripts[0].down_revision
    drop_everything(engine)
    cid, pid = new_id(), new_id()
    with engine.begin() as conn:
        upgrade(conn, before)
        if case in {"marked_empty", "owner_only", "identity_only"}:
            if case != "identity_only":
                conn.execute(text("INSERT INTO erpclaw_books (mark) VALUES ('erpclaw-engine-books-v1')"))
            if case != "owner_only":
                conn.execute(text("INSERT INTO books_identity (id, books_id) VALUES (1, :id)"), {"id": new_id()})
        else:
            conn.execute(text("INSERT INTO company (id, name, functional_currency, fiscal_year_start_month, created_at) "
                              "VALUES (:c, 'Old Co', 'USD', 1, now())"), {"c": cid})
        if case == "with_posting":
            conn.execute(text("INSERT INTO posting (id, company_id, seq, posting_date, voucher_type, voucher_id, "
                              "created_at, created_by, prev_hash, hash) VALUES (:p, :c, 1, '2026-10-01', 'journal', 'v1', "
                              "now(), 'old', :g, :g)"), {"p": pid, "c": cid, "g": ledger.GENESIS})
    with pytest.raises(DBAPIError, match="disposable"):
        with engine.begin() as conn:
            upgrade(conn, this)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalars().all() == [before]
        assert "dimension_set" not in conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")).scalars().all()


# The forgery fixture itself must carry format 2 without trusting the production
# hashing functions; otherwise restore-integrity tests can fail for the wrong reason.
def _offline_backup_helpers():
    import ast
    from pathlib import Path

    path = Path(__file__).with_name("test_backup.py")
    source = ast.parse(path.read_text())
    names = {"_rewrite", "_rechain"}
    nodes = [node for node in source.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in nodes} == names
    namespace = {"Path": Path, "json": json, "hashlib": hashlib, "bk": bk}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return namespace["_rechain"]


def _offline_backup_fixture(path, *, hash_format=2, named_dimension=False):
    (path / "tables").mkdir()
    entry = {**ROW, "debit": "5.00", "credit": "0.00", "rate": "1", "functional_debit": "5.00",
             "functional_credit": "0.00", "company_id": "c1", "posting_id": "p1",
             "dimension_set_id": "s1" if named_dimension else None}
    sets = [{"company_id": "c2", "id": "s1", "digest": "f" * 64},
            {"company_id": "c1", "id": "s1", "digest": "a" * 64}] if named_dimension else []
    tables = {"posting": [{**HEADER, "id": "p1", "hash_format": hash_format}], "ledger_entry": [entry],
              "posting_chain_head": [{"company_id": "c1", "last_seq": 1, "last_hash": "0" * 64}],
              "dimension_set": sets}
    manifest = {"tables": {}}
    for name, rows in tables.items():
        lines = [json.dumps(row, separators=(",", ":")) for row in rows]
        (path / "tables" / (name + ".jsonl")).write_text("\n".join(lines) + ("\n" if lines else ""))
        manifest["tables"][name] = {"rows": len(lines), "sha256": bk._sha(lines)}
    (path / "MANIFEST.json").write_text(json.dumps(manifest))


def test_current_backup_forgery_helper_hashes_explicit_format2_bytes_independently(tmp_path, monkeypatch):
    _offline_backup_fixture(tmp_path)

    def production_hash_forbidden(*args, **kwargs):
        raise AssertionError("The forgery oracle must not call production hashing")

    monkeypatch.setattr(ledger, "_hash", production_hash_forbidden)
    monkeypatch.setattr(ledger, "_header", production_hash_forbidden)
    _offline_backup_helpers()(tmp_path)
    posting = json.loads((tmp_path / "tables/posting.jsonl").read_text())
    expected = hashlib.sha256(("0" * 64).encode() + CANONICAL.encode()).hexdigest()
    assert posting["hash"] == expected and posting["prev_hash"] == "0" * 64
    head = json.loads((tmp_path / "tables/posting_chain_head.jsonl").read_text())
    assert head["last_hash"] == expected
    manifest = json.loads((tmp_path / "MANIFEST.json").read_text())
    for table in ("posting", "posting_chain_head"):
        lines = (tmp_path / "tables" / (table + ".jsonl")).read_text().splitlines()
        assert manifest["tables"][table] == {"rows": len(lines), "sha256": bk._sha(lines)}


def test_current_backup_forgery_helper_never_rewrites_a_historical_format(tmp_path):
    _offline_backup_fixture(tmp_path, hash_format=1)
    before = {str(path.relative_to(tmp_path)): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    with pytest.raises(AssertionError, match="never rewrite a historical chain"):
        _offline_backup_helpers()(tmp_path)
    assert {str(path.relative_to(tmp_path)): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


def test_current_backup_forgery_helper_uses_the_lines_own_company_dimension_digest(tmp_path):
    _offline_backup_fixture(tmp_path, named_dimension=True)
    _offline_backup_helpers()(tmp_path)
    expected = hashlib.sha256(("0" * 64).encode() + CANONICAL.replace('"dimset":""', '"dimset":"' + "a" * 64 + '"').encode()).hexdigest()
    assert json.loads((tmp_path / "tables/posting.jsonl").read_text())["hash"] == expected
