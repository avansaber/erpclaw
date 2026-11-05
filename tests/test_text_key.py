"""Owner text compared without regard to case is compared by a key the engine computes in Python (Unicode NFKC, then
full case folding), stored beside the text and matched by equality, so the database's own case rules never decide a
match: under PostgreSQL's builtin C.UTF-8 locale, SQL lower() and Python disagree on letters such as the dotted "İ",
and neither lower() folds "ß" to "ss"."""
from __future__ import annotations

import unicodedata
from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from erpclaw_core import models as m
from erpclaw_core.common import company_ctx
from erpclaw_core.masters import create_party
from erpclaw_core.migrate import upgrade
from erpclaw_core.periods import resolve
from erpclaw_core.textkey import text_key

# ---------------------------------------------------------------- the key

# (the text as written, its key)
KEYS = [
    ("İstanbul", "i̇stanbul"),  # the dotted capital I folds to i and a combining dot above, as Unicode says
    ("ISTANBUL", "istanbul"),
    ("istanbul", "istanbul"),
    ("Straße", "strasse"),  # full folding: ß is ss, which lower() never gives
    ("STRASSE", "strasse"),
    ("STRAẞE", "strasse"),  # the capital sharp s too
    ("ılıca", "ılıca"),  # the dotless ı is already folded: it never becomes i
    ("ILICA", "ilica"),
    ("Zürich", "zürich"),  # a u and a combining diaeresis are the composed ü
    ("ＩＳＴＡＮＢＵＬ", "istanbul"),  # full-width letters are their compatibility form
    ("ﬁeld", "field"),  # a ligature too
    ("  Pune ", "pune"),  # outer whitespace, a no-break space included, is not part of the key
]


@pytest.mark.parametrize("written, key", KEYS)
def test_each_text_has_its_key(written, key):
    assert text_key(written) == key


# Spellings that meet, and spellings that must not: the same key, or a different one.
MEET = [("ISTANBUL", "istanbul"), ("Istanbul", "ＩＳＴＡＮＢＵＬ"), ("Straße", "STRASSE"), ("strasse", "STRAẞE"),
        ("ILICA", "ilica"), ("Zürich", "ZÜRICH")]
APART = [("İstanbul", "istanbul"), ("İstanbul", "ISTANBUL"), ("ılıca", "ilica"), ("ılıca", "ILICA"),
         ("ısparta", "Isparta"), ("Straße", "Strase")]


@pytest.mark.parametrize("a, b", MEET)
def test_spellings_that_differ_only_in_case_or_form_meet(a, b):
    assert text_key(a) == text_key(b)


@pytest.mark.parametrize("a, b", APART)
def test_letters_that_unicode_folds_apart_stay_apart(a, b):
    assert text_key(a) != text_key(b)


def test_no_text_has_no_key():
    assert text_key(None) is None


@given(st.text())
def test_every_normalisation_form_of_a_text_has_the_same_key(s):
    """Canonically or compatibly equivalent texts meet, however the owner's keyboard composed them."""
    assert {text_key(unicodedata.normalize(form, s)) for form in ("NFC", "NFD", "NFKC", "NFKD")} == {text_key(s)}


# ---------------------------------------------------------------- the stored key, on the books

# customers and the city each is stored with
CITIES = [("Kara Ltd", "İstanbul"), ("Bora Ltd", "ISTANBUL"), ("Deniz Ltd", "istanbul"), ("Weber GmbH", "Straße"),
          ("Kraus GmbH", "STRASSE"), ("Ilgaz Ltd", "Isparta"), ("Irmak Ltd", "ısparta")]
# what the owner says, and exactly the customers it finds: those whose stored key equals its key
FOUND = [
    ("İstanbul", {"Kara Ltd"}),
    ("ISTANBUL", {"Bora Ltd", "Deniz Ltd"}),
    ("istanbul", {"Bora Ltd", "Deniz Ltd"}),
    (" Istanbul ", {"Bora Ltd", "Deniz Ltd"}),
    ("Straße", {"Weber GmbH", "Kraus GmbH"}),
    ("STRASSE", {"Weber GmbH", "Kraus GmbH"}),
    ("strasse", {"Weber GmbH", "Kraus GmbH"}),
    ("ısparta", {"Irmak Ltd"}),
    ("Isparta", {"Ilgaz Ltd"}),
    ("ISPARTA", {"Ilgaz Ltd"}),
    ("Ist%", set()),  # a pattern character is a character, never a wildcard
]


def test_each_expected_set_is_exactly_the_rows_whose_key_matches():
    """The table above is the key's rule written out, not the engine's output copied."""
    for said, names in FOUND:
        assert names == {n for n, city in CITIES if text_key(city) == text_key(said)}, said


@pytest.fixture
def cities(engine):
    """The demo books with the customers above, each with one submitted invoice."""
    from erpclaw_sales import service as s

    with engine.begin() as conn:
        ctx = company_ctx(conn)
        item = dict(conn.execute(select(m.item).where(m.item.c.name == "Copper Wire")).mappings().one())
        for i, (name, city) in enumerate(CITIES, 1):
            create_party(ctx, "customer", name, None, city)
            customer = dict(conn.execute(select(m.party).where(m.party.c.name == name)).mappings().one())
            s.create_invoice(ctx, customer, [(item, Decimal(i))], date(2026, 10, 20), submit=True)
    return engine


def _found(engine, metric, said) -> set[str]:
    with engine.connect() as conn:
        result = metric(company_ctx(conn), resolve("", date(2026, 10, 26)), None, said)
    return {r.label for r in result.rows}


@pytest.mark.parametrize("said, names", FOUND)
def test_the_sales_city_filter_finds_exactly_the_rows_its_key_matches(cities, said, names):
    from erpclaw_sales import top_customers_metric

    assert _found(cities, top_customers_metric, said) == names


@pytest.mark.parametrize("said, names", FOUND)
def test_the_receivables_city_filter_finds_exactly_the_rows_its_key_matches(cities, said, names):
    from erpclaw_core.core_contracts import receivables

    assert _found(cities, receivables, said) == names


def test_a_customer_is_stored_with_its_citys_key(engine):
    """create_party writes the key beside the city, for the demo's customers and new ones; a customer without a city
    has no key."""
    cases = [*CITIES, *[(f"Key example {i}", city) for i, (city, _) in enumerate(KEYS)],
             ("Nowhere Ltd", None)]
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        for name, city in cases:
            create_party(ctx, "customer", name, None, city)
        rows = conn.execute(select(m.party.c.name, m.party.c.city, m.party.c.city_key)).all()
    assert len(rows) == 5 + len(cases)
    assert {r.name: r.city_key for r in rows if r.name.startswith("Key example ")} == {
        f"Key example {i}": key for i, (_, key) in enumerate(KEYS)}
    assert {r.name: r.city_key for r in rows} == {r.name: text_key(r.city) for r in rows}
    assert {r.name: r.city_key for r in rows}["Nowhere Ltd"] is None


def test_the_books_refuse_a_city_without_its_key(engine):
    """A writer that forgets the key is refused, so no row can have a city the filters never find."""
    with engine.begin() as conn:
        cid = company_ctx(conn).company_id
    with pytest.raises(IntegrityError, match="ck_party_city_key"), engine.begin() as conn:
        conn.execute(text("INSERT INTO party (id, company_id, kind, name, city) "
                          "VALUES ('probe', :c, 'customer', 'Probe Ltd', 'Pune')"), {"c": cid})
    with pytest.raises(IntegrityError, match="ck_party_city_key"), engine.begin() as conn:
        conn.execute(text("INSERT INTO party (id, company_id, kind, name, city_key) "
                          "VALUES ('probe', :c, 'customer', 'Probe Ltd', 'pune')"), {"c": cid})


def _key_revision() -> tuple[str, str]:
    """The revision that adds the key, and the one before it, whatever numbers they have."""
    from alembic.script import ScriptDirectory

    from erpclaw_core.migrate import config

    script = ScriptDirectory.from_config(config(plugins=False))
    rev = next(r for r in script.walk_revisions() if r.path.endswith("_owner_text_keys.py"))
    return rev.revision, rev.down_revision


def test_older_books_get_each_citys_key_and_nothing_else_changes(engine):
    from conftest import drop_everything
    from test_revision_0006 import _rows

    revision, before_it = _key_revision()
    assert (revision, before_it) == ("0012", "0011")
    cases = [*CITIES, *[(f"Key example {i}", city) for i, (city, _) in enumerate(KEYS)],
             ("Nowhere Ltd", None)]
    older = {f"p{i}": city for i, (_, city) in enumerate(cases)}
    drop_everything(engine)
    with engine.begin() as conn:
        upgrade(conn, before_it)
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0011"
        conn.execute(text("INSERT INTO company (id, name, functional_currency, fiscal_year_start_month, created_at) "
                          "VALUES ('c1', 'Old Co', 'USD', 1, '2026-10-26 12:00:00+00:00')"))
        for pid, city in older.items():
            conn.execute(text("INSERT INTO party (id, company_id, kind, name, city) "
                              "VALUES (:id, 'c1', 'customer', :n, :city)"), {"id": pid, "n": f"Customer {pid}",
                                                                              "city": city})
        before = _rows(conn)
    with engine.begin() as conn:
        upgrade(conn, revision)  # this revision only: later ones may add their own
        after = _rows(conn)
    del before["alembic_version"], after["alembic_version"]
    parties = after.pop("party")
    assert {r["id"]: r["city_key"] for r in parties} == {pid: text_key(city) for pid, city in older.items()}
    assert [{k: v for k, v in r.items() if k != "city_key"} for r in parties] == before.pop("party")
    assert after == before  # every other row exactly as it was
