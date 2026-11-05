"""Name resolution in core: exact stored names first, collisions never silent, regular plurals generated (never
guessed), customers found by email or city after names, at most five near names, close names told apart on cards."""
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from erpclaw_core import approvals
from erpclaw_core.common import Refusal, company_ctx
from erpclaw_core.masters import (_match, create_item, create_party, customer_label, find_customer, find_item,
                                  regular_plural, same_word)
from erpclaw_core.plugins import REGISTRY


def add_customers(engine, *rows):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        for name, email, city in rows:
            create_party(ctx, "customer", name, email, city)


def add_items(engine, *names):
    from decimal import Decimal
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        for name in names:
            create_item(ctx, name, Decimal("1.00"))


def resolve(engine, fn, said):
    with engine.connect() as conn:
        return fn(company_ctx(conn), said)["name"]


def refused(engine, fn, said) -> Refusal:
    with engine.connect() as conn, pytest.raises(Refusal) as e:
        fn(company_ctx(conn), said)
    return e.value


def test_exact_stored_name_first_and_collisions_never_silent(engine):
    add_customers(engine, ("A&B", None, None), ("AB", None, None))
    assert resolve(engine, find_customer, "A&B") == "A&B"
    assert resolve(engine, find_customer, "AB") == "AB"
    r = refused(engine, find_customer, "a&b")  # not raw-equal to either; both normalise to "ab"
    assert r.code == "customer_ambiguous" and set(r.options) == {"A&B", "AB"}


def test_a_singular_and_its_plural_both_stored_resolve_only_by_their_exact_names(engine):
    add_items(engine, "Glass", "Glasses")
    assert resolve(engine, find_item, "Glass") == "Glass"
    assert resolve(engine, find_item, "Glasses") == "Glasses"
    for said in ("glass", "glasses", "GLASSES"):
        r = refused(engine, find_item, said)
        assert r.code == "item_ambiguous" and set(r.options) == {"Glass", "Glasses"}, said


def test_regular_plural_is_generated():
    pairs = {"battery": "batteries", "box": "boxes", "brush": "brushes", "church": "churches", "bus": "buses",
             "glass": "glasses", "case": "cases", "tie": "ties", "day": "days", "key": "keys", "wire": "wires"}
    for one, many in pairs.items():
        assert regular_plural(one) == many


def test_the_plural_rule_is_data_a_toy_language_swaps_in_without_code_changes():
    toy = [{"endings": ("o",), "drop": 1, "add": "i"}, {"endings": ("",), "drop": 0, "add": "en"}]
    assert regular_plural("libro", toy) == "libri" and regular_plural("haus", toy) == "hausen"
    assert same_word("libri", "libro", toy) and not same_word("libros", "libro", toy)
    assert regular_plural("box") == "boxes"  # the shipped table is unchanged


@pytest.mark.parametrize(("stored", "said"), [
    ("Battery", "batteries"), ("Batteries", "battery"), ("Box", "boxes"), ("Boxes", "box"), ("Case", "cases"),
    ("Cases", "case"), ("Bus", "buses"), ("Buses", "bus"), ("Glasses", "glass"), ("Tie", "ties"), ("Day", "days"),
    ("Key", "keys"), ("Steel Pipes", "steel pipe"), ("Fuse", "fuses"), ("Hose", "hoses")])
def test_a_singular_or_plural_of_a_stored_item_finds_it(engine, stored, said):
    add_items(engine, stored)
    assert resolve(engine, find_item, said) == stored


@pytest.mark.parametrize(("stored", "said"), [("Capes", "caps"), ("Tapes", "taps"), ("Dyes", "dies"),
                                              ("Caps", "capes")])
def test_unrelated_words_never_meet_through_a_guessed_stem(engine, stored, said):
    add_items(engine, stored)
    assert refused(engine, find_item, said).code == "item_unknown"


def test_a_tie_between_two_plural_readings_asks(engine):
    add_items(engine, "Tie", "Ty")
    r = refused(engine, find_item, "ties")
    assert r.code == "item_ambiguous" and set(r.options) == {"Tie", "Ty"}


WORDS = st.sampled_from(["cap", "cape", "caps", "capes", "tap", "tape", "taps", "tapes", "die", "dye", "dies",
                         "dyes", "tie", "ty", "ties", "glass", "glasses", "bus", "buses", "box", "boxes", "case",
                         "cases", "key", "keys", "day", "days"])


@settings(max_examples=3000, deadline=None, derandomize=True)
@given(stored=st.lists(WORDS, min_size=1, max_size=4, unique=True), said=WORDS)
def test_property_a_silent_match_is_only_an_equal_word_or_a_generated_pair(stored, said):
    rows = [{"name": w.title(), "id": w} for w in stored]
    try:
        got = _match(rows, said, "item", plural=True)
    except Refusal as r:
        assert r.code in ("item_ambiguous", "item_unknown")
        return
    g = got["name"].lower()
    assert g == said or regular_plural(g) == said or regular_plural(said) == g, (stored, said, g)
    others = [w for w in stored if w != g]
    assert not any(same_word(said, w) for w in others), (stored, said)  # a collision never resolves silently


def test_email_and_city_find_a_customer_after_names(engine):
    assert resolve(engine, find_customer, "AP@WAYNE.EXAMPLE") == "Wayne Enterprises"
    assert resolve(engine, find_customer, "Gotham") == "Wayne Enterprises"
    r = refused(engine, find_customer, "Pune")  # two customers in Pune
    assert r.code == "customer_ambiguous" and set(r.options) == {"Acme Corporation", "Initech"}


def test_a_name_beats_a_city(engine):
    add_customers(engine, ("Denver", None, "Boston"), ("Globex", None, "Denver"))
    assert resolve(engine, find_customer, "Denver") == "Denver"


def test_an_email_held_by_two_customers_asks(engine):
    add_customers(engine, ("Umbrella", "shared@example.test", None), ("Tyrell", "shared@example.test", None))
    r = refused(engine, find_customer, "shared@example.test")
    assert r.code == "customer_ambiguous" and set(r.options) == {"Umbrella", "Tyrell"}


def test_an_unknown_name_gets_at_most_five_near_names_nearest_first(engine):
    add_customers(engine, *[(f"Acme Branch {i:02d}", None, None) for i in range(50)])
    r = refused(engine, find_customer, "Acmee Corporation")
    assert r.code == "customer_unknown" and 1 <= len(r.options) <= 5
    assert r.options[0] == "Acme Corporation"
    assert "Known customers" not in r.message


def test_an_unknown_name_with_nothing_near_says_so(engine):
    r = refused(engine, find_customer, "Zzyzx")
    assert r.code == "customer_unknown" and r.options == []
    assert "no customers with a similar name" in r.message and "isn't available in chat" in r.message


def test_an_empty_table_says_there_are_none():
    with pytest.raises(Refusal) as e:
        _match([], "Acme", "customer")
    assert e.value.code == "customer_unknown" and "There are no customers yet." in e.value.message


def test_close_names_are_told_apart_on_the_card(engine):
    add_customers(engine, ("Acme Co", None, "Boston"))
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        acme = find_customer(ctx, "Acme Corporation")
        hooli = find_customer(ctx, "Hooli")
        assert customer_label(ctx, acme) == "Acme Corporation (Pune)"
        assert customer_label(ctx, hooli) == "Hooli"
    card = approvals.propose(engine, REGISTRY.contracts["create_invoice"],
                             {"customer": "Acme Corporation", "items": [{"item": "Copper Wire", "quantity": 1}]})
    assert ["Customer", "Acme Corporation (Pune)"] in card.card["lines"]


def test_a_close_name_without_city_or_email_says_so(engine):
    add_customers(engine, ("Hoolie", None, None), ("Hooly", None, None))
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        assert customer_label(ctx, find_customer(ctx, "Hoolie")) == "Hoolie (no city or email on file)"


def test_only_final_words_pair_as_plurals_in_every_rule(engine):
    add_items(engine, "Glasses Case")
    assert refused(engine, find_item, "Glass Cases").code == "item_unknown"  # "glass" never pairs with "glasses"
    assert resolve(engine, find_item, "Glasses Cases") == "Glasses Case"


def test_near_names_count_a_plural_pair_as_a_shared_word(engine):
    add_items(engine, "Heavy Duty Industrial Cable")
    r = refused(engine, find_item, "copper cables")
    assert "Heavy Duty Industrial Cable" in r.options


ADJ = st.sampled_from(["glass", "glasses", "steel", "copper", "cap", "caps"])


@settings(max_examples=3000, deadline=None, derandomize=True)
@given(stored=st.lists(st.tuples(ADJ, WORDS), min_size=1, max_size=4, unique=True),
       said=st.one_of(st.tuples(ADJ, WORDS), WORDS.map(lambda w: (w,))))
def test_property_multiword_a_silent_match_pairs_only_final_words(stored, said):
    rows = [{"name": f"{a} {n}".title(), "id": f"{a} {n}"} for a, n in stored]
    text = " ".join(said)
    try:
        got = _match(rows, text, "item", plural=True)
    except Refusal as r:
        assert r.code in ("item_ambiguous", "item_unknown")
        return
    have = got["name"].lower().split()
    *head, last = said
    assert all(w in have[:-1] or w == have[-1] for w in head), (stored, said, have)  # non-final words exact
    assert last in have or same_word(last, have[-1]), (stored, said, have)
