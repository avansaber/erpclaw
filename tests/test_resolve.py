"""The resolution step's rules, without the books: names, figures, quantities, dates, documents, the parts not done,
and the invariant of the bounded full name as a property over generated cases."""
from datetime import date
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, seed, settings
from hypothesis import strategies as st

from erpclaw_assistant import resolve as rv
from erpclaw_assistant.resolve import Planned, Session, resolve_date, resolve_record
from erpclaw_core.common import Ctx, Refusal, is_question
from erpclaw_core.masters import _norm, same_word

TODAY = date(2026, 10, 29)  # a Thursday


def name(rows, text, arg=None, *, item=False, emails=None, cities=None, peers=None):
    """The resolved stored name, or the question's (code, options). `peers`: the reply's arguments for the field's
    kind (every line's item), when a test has several."""
    recs = [{"name": n, "email": (emails or {}).get(n), "city": (cities or {}).get(n)} for n in rows]
    try:
        return resolve_record(recs, "item" if item else "customer", text, arg, Session(), "f", item=item,
                              peers=peers)
    except Refusal as rf:
        assert is_question(rf.code), rf.code
        return rf.code, rf.options


def when(text, order=None, model=None):
    try:
        return resolve_date(text, TODAY, Session(), "date", model, order)
    except Refusal as rf:
        return rf.code, rf.options


# ---------- 2.2: bounded full names ----------

@pytest.mark.parametrize("text, expect", [
    ("invoice Acme, Inc UK for 2 widgets", ("customer_ambiguous", ["Acme", "someone else"])),
    ("invoice Acme, 2 widgets", "Acme"),
    ("invoice Acme. 2 widgets please", "Acme"),
])
def test_punctuation_never_bounds(text, expect):
    assert name(["Acme"], text) == expect


@pytest.mark.parametrize("arg", ["Acme Corp", "Acme", "Corp", None])
def test_the_bounded_full_name_wins_over_the_models_cut(arg):
    assert name(["Acme", "Acme Corp"], "invoice Acme Corp for 2 widgets", arg) == "Acme Corp"
    assert name(["Acme", "Acme Corp"], "invoice Acme for 2 widgets", arg) == "Acme"
    assert name(["Smith and Sons", "Sons"], "invoice Smith and Sons for 2 widgets", arg) == "Smith and Sons"
    assert name(["Smith and Sons", "Sons"], "invoice Sons for 2 widgets", arg) == "Sons"


def test_a_name_inside_a_longer_found_name_is_set_aside():
    # a figure bounds and the continuation guard is for words, so here the set-aside alone decides
    assert name(["Acme", "Acme 2000"], "invoice Acme 2000 for 2 widgets", "Acme") == "Acme 2000"
    assert name(["Acme", "Acme 2000"], "invoice Acme for 2 widgets") == "Acme"


def test_a_word_outside_the_table_never_bounds():
    assert name(["Smith"], "invoice May Smith for 2 widgets")[0] == "customer_ambiguous"
    assert name(["May Smith", "Smith"], "invoice May Smith for 2 widgets") == "May Smith"
    assert name(["Brothers"], "invoice Two Brothers for 2 widgets")[0] == "customer_ambiguous"
    assert name(["Two Brothers", "Brothers"], "invoice Two Brothers for 2 widgets") == "Two Brothers"
    assert name(["Star"], "invoice North Star for 2 widgets") == ("customer_ambiguous", ["Star", "someone else"])
    assert name(["Acme Trading"], "invoice Acme UK for 2 widgets") == ("customer_ambiguous",
                                                                       ["Acme Trading", "someone else"])


QUANTITY_WORDS = sorted(rv.table()["quantity"])
DATE_WORDS = ["today", "yesterday", "monday", "friday", "may", "june", "oct"]


@pytest.mark.parametrize("word", QUANTITY_WORDS + DATE_WORDS)
def test_quantity_and_date_words_never_bound_a_customer(word):
    assert name(["Smith"], f"invoice {word} Smith for 2 widgets")[0] == "customer_ambiguous"


@pytest.mark.parametrize("word", QUANTITY_WORDS)
def test_a_quantity_word_bounds_an_item(word):
    assert name(["Widget"], f"invoice Acme for {word} widgets", item=True) == "Widget"


@pytest.mark.parametrize("word", DATE_WORDS)
def test_a_date_word_does_not_bound_an_item(word):
    assert name(["Widget"], f"invoice Acme for {word} widgets", item=True)[0] == "item_ambiguous"


# table version 3: a day word just after a name ends it; before a name nothing changed
V3_DAY_WORDS = ["today", "yesterday", "tomorrow", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
                "sunday"]
NOT_V3 = ["may", "june", "march", "fri", "oct", "tonight"]  # months, abbreviations and tonight stay non-bounding
# a day word before one of these is part of a name (r7f), stated here, not read from the engine's table
SUFFIXES = {"ltd", "limited", "inc", "llc", "co", "corp", "corporation", "plc", "gmbh", "pty", "llp"}


@pytest.mark.parametrize("word", V3_DAY_WORDS)
def test_a_day_word_just_after_an_item_ends_it(word):
    assert name(["Copper Wire"], f"invoice Hooli for 5 copper wire {word}", item=True) == "Copper Wire"


@pytest.mark.parametrize("word", V3_DAY_WORDS)
def test_a_day_word_just_after_a_customer_ends_it(word):
    assert name(["Hooli"], f"invoice Hooli {word} for 5 copper wire") == "Hooli"


@pytest.mark.parametrize("word", V3_DAY_WORDS)
def test_a_day_word_before_a_name_still_never_bounds(word):
    assert name(["Smith"], f"invoice {word} Smith for 2 widgets")[0] == "customer_ambiguous"
    assert name(["Widget"], f"invoice Acme for {word} widgets", item=True)[0] == "item_ambiguous"


@pytest.mark.parametrize("word", NOT_V3)
def test_a_month_an_abbreviation_or_tonight_after_a_name_still_asks(word):
    assert name(["Copper Wire"], f"invoice Hooli for 5 copper wire {word}", item=True)[0] == "item_ambiguous"
    assert name(["Hooli"], f"invoice Hooli {word} for 5 copper wire")[0] == "customer_ambiguous"


@pytest.mark.parametrize("stored, text, longer, item", [
    (["Acme", "Acme Today Ltd"], "invoice Acme today for 2 widgets", "invoice Acme Today Ltd for 2 widgets", False),
    (["Acme", "Acme Tomorrow Ltd"], "invoice Acme tomorrow for 2 widgets", "invoice Acme Tomorrow Ltd for 2 widgets",
     False),
    (["Copper Wire", "Copper Wire Monday Pack"], "invoice Hooli for 5 copper wire monday",
     "invoice Hooli for 5 copper wire monday pack", True),
], ids=["acme_today_ltd", "acme_tomorrow_ltd", "copper_wire_monday_pack"])
def test_the_continuation_guard_holds_for_a_day_word(stored, text, longer, item):
    """A stored name going on past the same day word: the shorter name is a question, the longer one written in
    full resolves."""
    assert name(stored, text, item=item)[0] == ("item_ambiguous" if item else "customer_ambiguous")
    assert name(stored, longer, item=item) == stored[1]


@pytest.mark.parametrize("stored", [["Acme", "Acme Today"], ["Acme", "Acme Tomorrow"]])
def test_a_stored_name_ending_in_the_day_word_written_in_full_is_that_name(stored):
    """"invoice Acme today" with "Acme Today" stored writes that longer name in full: it is found and the shorter
    name inside it is set aside, before and after version 3 alike."""
    assert name(stored, f"invoice {stored[1].lower()} for 2 widgets") == stored[1]


@pytest.mark.parametrize("longer", ["Copper Wires Monday Pack", "Copper Wire Monday Pack"])
@pytest.mark.parametrize("written", ["copper wire", "copper wires"])
def test_the_continuation_guard_compares_an_item_the_way_it_was_found(longer, written):
    """An item's final word is found singular or plural, so a stored item going on with
    the day word continues it in either spelling, and the shorter name is a question, never a silent match."""
    got = name(["Copper Wire", longer], f"invoice Hooli for 5 {written} monday", item=True)
    assert got[0] == "item_ambiguous", got


def test_the_plural_aware_guard_holds_for_a_table_version_2_word_too():
    """The same gap with "in" (version 2), open on main before version 3."""
    got = name(["Copper Wire", "Copper Wires In Stock Pack"], "invoice Hooli for 2 copper wires in stock", item=True)
    assert got[0] == "item_ambiguous", got
    assert name(["Copper Wire", "Copper Wires In Stock Pack"], "invoice Hooli for 2 copper wires in stock pack",
                item=True) == "Copper Wires In Stock Pack"


# a day word ends a name only where it ends the name's phrase (build round 1)

@pytest.mark.parametrize("text", ["invoice Acme Sunday Ltd for 2 widgets", "invoice Acme Today Inc for 2 widgets",
                                  "invoice Acme Monday Corporation for 2 widgets",
                                  "invoice Acme Sunday Times for 2 widgets", "invoice Acme Sunday Holdings for 2 widgets",
                                  "invoice Acme Monday, Inc. for 2 widgets", "invoice Acme Today, LLC for 2 widgets",
                                  "invoice Acme Sunday (Pty) Ltd for 2 widgets", "invoice Acme Monday 2000 Ltd for 2 widgets"])
def test_a_day_word_the_name_may_go_on_from_is_part_of_a_name(text):
    """Day words occur inside real company names: with only Acme stored, a day word followed by anything but an end,
    a mark, a figure or a bounding word leaves Acme a question, never bound."""
    assert name(["Acme"], text) == ("customer_ambiguous", ["Acme", "someone else"])


@pytest.mark.parametrize("text, stored, item", [
    ("invoice Acme today for 2 widgets", "Acme", False),  # a bounding word
    ("invoice Hooli for 5 copper wire today", "Copper Wire", True),  # the end
    ("invoice Hooli for 5 copper wire today, thanks", "Copper Wire", True),  # a mark
    ("invoice Acme today 2 widgets", "Acme", False),  # a figure
    ("Wayne wants 500 Circuit Board tomorrow; book the order.", "Circuit Board", True),  # a mark, then any word
])
def test_a_day_word_before_an_end_a_mark_a_figure_or_a_bounding_word_ends_the_name(text, stored, item):
    assert name([stored], text, item=item) == stored


@pytest.mark.parametrize("stored, text, item", [
    (["Acme", "Monday Foods"], "invoice acme monday foods for 2 widgets", False),
    (["Copper Wire", "Sunday Roast"], "invoice Hooli for 5 copper wire sunday roast", True),
    (["Copper Wire", "Copper Wire Pack"], "invoice Hooli for 5 copper wire monday pack", True),
], ids=["a_customer_starting_with_the_day_word", "an_item_starting_with_the_day_word",
        "a_longer_item_going_on_after_the_day_word"])
def test_a_day_word_never_ends_a_name_where_a_stored_name_takes_it_up(stored, text, item):
    got = name(stored, text, item=item)
    assert isinstance(got, tuple) and got[0].endswith("_ambiguous"), got


def test_the_left_side_compares_an_item_the_way_it_was_found():
    """A version 2 word before an item, a stored item ending with that word and the item's plural: a question."""
    got = name(["Box", "Glass In Boxes"], "invoice Hooli 2 coil in box", item=True)
    assert isinstance(got, tuple) and got[0] == "item_ambiguous", got


@pytest.mark.parametrize("stored, text, item, expect", [
    (["Copper Wire", "Copper Wires Monday Ltd"], "invoice Hooli for 5 copper wire monday", True, "item_ambiguous"),
    (["Acme Wire", "Acme Wires Monday Ltd"], "invoice Acme Wire monday for 2 widgets", False, "Acme Wire"),
    (["Box", "Glass In Boxes"], "invoice Hooli for 2 coil in box", True, "item_ambiguous"),
    (["Leeds", "Acme in Leeds"], "invoice Bob in Leeds for 2 widgets", False, "customer_ambiguous"),
    (["Boxes", "Glass In Box"], "invoice Glass in Boxes for 2 widgets", False, "Boxes"),
], ids=["right_item_plural", "right_customer_never_plural", "left_item_plural", "left_customer_literal",
        "left_customer_never_plural"])
def test_the_guard_by_hand_on_each_side(stored, text, item, expect):
    """Hand-written cases beside the property test: items compare their final word singular or plural, customers
    only literally (a customer's name is never matched by its plural)."""
    got = name(stored, text, item=item)
    assert (got if isinstance(got, str) else got[0]) == expect, got


@pytest.mark.parametrize("stored, text, item", [
    (["Acme", "Bank Of Acme Monday Club", "Bank Of Acme Monday Club Today Ltd"],
     "invoice Bank Of Acme Monday Club today for 2 widgets", False),
    (["Box Of Paper And Pens", "Paper", "Box Of Paper And Pen In Stock Pack"],
     "invoice Hooli for 2 box of paper and pens in stock", True),
], ids=["a_customer_inside_a_blocked_name", "an_item_inside_a_blocked_name"])
def test_a_name_inside_a_longer_name_the_guard_blocked_is_never_taken_instead(stored, text, item):
    """Codex's build round 1: when the guard stops a longer name the owner wrote, a stored name inside it is set aside
    too, so the request asks; it never resolves the shorter name in its place."""
    got = name(stored, text, item=item)
    assert isinstance(got, tuple) and got[0].endswith("_ambiguous"), got


@pytest.mark.parametrize("stored, text, item", [
    (["Acme", "Hooli", "The Hooli Monday Club"], "invoice Acme and the Hooli Monday Club members for 2 widgets", False),
    (["Acme", "Hooli", "Friends Of Hooli And Co"], "invoice Acme and friends of hooli and co members for 2 widgets",
     False),
    (["Copper Wire", "Circuit Board", "Kit Of Circuit Board For Kids"],
     "invoice Hooli for 5 copper wire and kit of circuit board for kids box", True),
], ids=["a_blocked_club_beside_acme", "a_blocked_name_with_no_day_word", "a_blocked_kit_beside_copper_wire"])
def test_a_stored_name_written_in_full_but_not_bounded_is_a_claimant(stored, text, item):
    """The reviewer's confirming check: a stored name the owner wrote in full, though not bounded, may be what they
    meant; the field never resolves another name silently beside it, it asks, listing both."""
    got = name(stored, text, item=item)
    assert isinstance(got, tuple) and got[0].endswith("_ambiguous"), got


def test_an_items_quantity_is_read_against_every_stored_item():
    """The quantity reading bounds the item among every stored item, so the guard sees the longer name: the 3 belongs
    to the pack, the 5 to Copper Wire (open on main with "in" as well)."""
    text = "invoice Hooli for 3 copper wire monday pack and 5 copper wire"
    names = ["Copper Wire", "Copper Wire Monday Pack"]
    assert rv.literal_quantity(None, text, "Copper Wire", Session(), "q", names=names) == "5"
    text = "invoice Hooli for 3 copper wire in box and 5 copper wire"
    assert rv.literal_quantity(None, text, "Copper Wire", Session(), "q", names=["Copper Wire", "Copper Wire In Box"]) \
        == "5"


def test_m4s_exit_case_names_its_item_before_tomorrow():
    """C0 (tests/expected_books/sales_order_above_available_stock.md): the item is named before a future day a sales
    order takes; the day ends the name and the date stays the date rules' to read."""
    text = "Wayne wants 500 Circuit Board tomorrow; book the order."
    assert name(["Circuit Board"], text, "Circuit Board", item=True) == "Circuit Board"


@pytest.mark.parametrize("form", ["Ltd", "Corp", "Corp UK", "Inc", "LLC"])
def test_legal_forms_bound_nothing(form):
    assert name(["Acme"], f"invoice Acme {form} for 2 widgets") == ("customer_ambiguous", ["Acme", "someone else"])


def test_the_models_name_adds_no_candidate():
    assert name(["Hooli", "Acme"], "invoice our client for 2 widgets", "Hooli") == ("customer_missing", [])


def test_two_items_sharing_a_word():
    text = "invoice Acme for 2 copper wire and 3 copper pipe"
    lines = ["copper wire", "copper pipe"]  # each line's argument, as the reply gives them
    assert name(["Copper Wire", "Copper Pipe"], text, "copper wire", item=True, peers=lines) == "Copper Wire"
    assert name(["Copper Wire", "Copper Pipe"], text, "copper pipe", item=True, peers=lines) == "Copper Pipe"
    text = "invoice Acme for 2 copper wire and 3 copper wire coil"
    items, lines = ["Copper Wire", "Copper Wire Coil"], ["Copper Wire", "Copper Wire Coil"]
    assert name(items, text, "Copper Wire", item=True, peers=lines) == "Copper Wire"
    assert name(items, text, "Copper Wire Coil", item=True, peers=lines) == "Copper Wire Coil"
    assert name(items, text, "Copper", item=True,  # no tie-break picks the shorter name
                peers=["Copper", "Copper"])[0] == "item_ambiguous"


def test_one_candidate_is_a_confirm_and_none_found_is_a_question():
    for said in ("invoice Acme for 2 widgets", "invoice acm for 2 widgets"):
        assert name(["Acme Corp"], said) == ("customer_ambiguous", ["Acme Corp", "someone else"])
    assert name([], "invoice Acme for 2 widgets") == ("customer_unknown", [])
    assert name(["Acme Corp", "Acme Ltd"], "invoice Acme for 2")[1] == ["Acme Corp", "Acme Ltd"]


def test_exact_references_resolve():
    rows = ["Acme Corp", "Acme Ltd"]
    assert name(rows, "invoice ops@acmeltd.example for 2 widgets",
                emails={"Acme Ltd": "ops@acmeltd.example"}) == "Acme Ltd"
    assert name(rows, "invoice the Acme in Leeds for 2", cities={"Acme Ltd": "Leeds", "Acme Corp": "Boston"}) \
        == "Acme Ltd"
    assert name(rows, "invoice the Acme in Leeds for 2", cities={"Acme Ltd": "Leeds", "Acme Corp": "Leeds"})[0] \
        == "customer_ambiguous"


def test_collisions_resolve_only_when_written_raw():
    assert name(["A&B", "AB"], "invoice A&B for 2 widgets") == "A&B"
    assert name(["A&B", "AB"], "invoice AB for 2 widgets") == "AB"
    assert name(["A&B", "AB"], "invoice ab for 2 widgets")[1] == ["A&B", "AB"]
    assert name(["Glass", "Glasses"], "invoice Acme for 2 glasses", item=True)[0] == "item_ambiguous"


@pytest.mark.parametrize("said, stored, found", [
    ("boxes", "Box", True), ("batteries", "Battery", True), ("cases", "Case", True),
    ("caps", "Cape", False), ("taps", "Tape", False), ("dies", "Dye", False),
])
def test_generated_plurals_pair_and_near_pairs_never_meet(said, stored, found):
    got = name([stored], f"invoice Acme for 2 {said}", item=True)
    assert (got == stored) is found


@pytest.mark.parametrize("text", [
    "invoice Acme Corporation for 2 circuit boards, dated 30 September",
    "invoice Acme Corporation for 2 circuit boards, memo PO 4471",
    "invoice Acme Corporation for 2 circuit boards, note: rush",
    "invoice Acme Corporation for 2 circuit boards, reference 88",
    "invoice Acme Corporation for 2 circuit boards on 30 September",
    "invoice Acme Corporation for 2 circuit boards due 15 November",
    "invoice Acme Corporation for 2 circuit boards, in stock",
])
def test_table_v2_words_end_a_name(text):
    assert name(["Acme Corporation"], text) == "Acme Corporation"
    assert name(["Circuit Board"], text, item=True) == "Circuit Board"


def test_the_continuation_guard_asks_where_a_stored_name_goes_on():
    rows = ["Acme", "Acme in Leeds Ltd"]
    assert name(rows, "invoice Acme in Leeds for 2 widgets")[0] == "customer_ambiguous"
    assert name(rows, "invoice Acme in Leeds Ltd for 2 widgets") == "Acme in Leeds Ltd"
    assert name(rows, "invoice Acme for 2 widgets") == "Acme"
    rows = ["Johnson", "Johnson and Johnson"]  # the same gap in a version 1 word
    assert name(rows, "invoice Johnson and Smith for 2 widgets")[0] == "customer_ambiguous"
    assert name(rows, "invoice Johnson and Johnson for 2 widgets") == "Johnson and Johnson"
    rows = ["Leeds", "Acme in Leeds"]  # and on the left
    assert name(rows, "invoice them in Leeds for 2 widgets")[0] == "customer_ambiguous"


def test_an_still_never_bounds_a_customer():
    assert name(["Wayne Enterprises"], "send Wayne Enterprises an invoice for 2 widgets")[0] == "customer_ambiguous"


# ---------- 2.2e: the invariant, as a property ----------

VOCAB = ["acme", "corp", "smith", "sons", "north", "star", "may", "two", "brothers", "trading", "copper", "wire",
         "coil", "glass", "box", "cap", "cape", "battery", "and", "in", "leeds", "wires", "boxes", "monday", "today",
         "tomorrow"]
FILLER = ["ltd", "inc", "uk", "corp", ",", ".", "two", "a", "dozen", "may", "today", "friday", "tomorrow", "tonight",
          "for", "to", "the",
          "please", "and", "2", "hq", "group", "in", "on", "dated", "memo"]


def _cap(ws):
    return " ".join(w.capitalize() for w in ws)


names_st = st.lists(st.lists(st.sampled_from(VOCAB), min_size=1, max_size=3).map(_cap), min_size=1, max_size=4,
                    unique_by=lambda n: n)


def _oracle(stored: list[str], text: str, got: str, item: bool) -> bool:
    """Independent statement of 2.2e: the owner's maximal phrase at some place equals the stored full name, bounded
    only by the closed table, a digit, a number word (before an item, or after a name when it is not an article,
    and never where the name's own edge word is a number word), or the message's ends; no longer stored name found
    around it; a name colliding with another resolves only when written raw."""
    t = rv.table()
    ws = _norm(text).split()
    gw = _norm(got).split()
    marks, n = set(), -1  # the words a mark follows: the generator writes "," and "." as words of their own
    for token in text.split():
        if token in (",", "."):
            marks.add(n)
        else:
            n += 1

    def eq(a, b):
        return a[:-1] == b[:-1] and (a[-1] == b[-1] or (item and same_word(a[-1], b[-1])))

    longer = [_norm(s).split() for s in stored]

    def goes_on(w, before):  # a stored name continuing past the found one with this word, compared as it is found
        n = len(gw)
        return any(len(x) > n and ((x[-n - 1] == w and eq(x[-n:], gw)) if before else (eq(x[:n], gw) and x[n] == w))
                   for x in longer)

    def number(w):
        return w in t["quantity"] or w in t["scales"]

    def day_ends(w, nxt, marked, then=None):  # r7g/r7h: an end, a mark, a figure or a bounding word follows it, and
        # the next word (marks between or not) is no suffix, nor the word after a figure
        follows = nxt not in SUFFIXES and not (nxt is not None and nxt.isdigit() and then in SUFFIXES) and \
            (nxt is None or marked or nxt.isdigit() or nxt in t["bound"])
        return follows and not (any(x[0] == w for x in longer)
                                or any(len(x) > len(gw) and eq(x[:len(gw)], gw) and x[len(gw)] == nxt for x in longer))

    def bound(w, before, nxt=None, marked=False, then=None):
        if w is None or w.isdigit():
            return True
        if number(w) and ((item and before) or (not before and w not in t["articles"])):
            return not number(gw[0] if before else gw[-1]) and not goes_on(w, before)
        if w in t["bound"]:
            return not goes_on(w, before)
        if not before and w in V3_DAY_WORDS:
            return day_ends(w, nxt, marked, then) and not goes_on(w, before)
        return False

    places = [i for i in range(len(ws) - len(gw) + 1) if eq(ws[i:i + len(gw)], gw)
              and bound(ws[i - 1] if i else None, True)
              and bound(ws[i + len(gw)] if i + len(gw) < len(ws) else None, False,
                        ws[i + len(gw) + 1] if i + len(gw) + 1 < len(ws) else None, i + len(gw) in marks,
                        ws[i + len(gw) + 2] if i + len(gw) + 2 < len(ws) else None)]
    if not places:
        return False
    twins = [s for s in stored if s != got and (_norm(s) == _norm(got) or (item and len(_norm(s).split()) == len(gw)
                                                                            and eq(_norm(s).split(), gw)))]
    return not twins or got in text


@seed(20261029)
@settings(max_examples=3000, deadline=None, suppress_health_check=list(HealthCheck))
@given(stored=names_st, data=st.data(), item=st.booleans())
def test_every_silent_resolution_is_a_bounded_full_name(stored, data, item):
    target = data.draw(st.sampled_from(stored))
    tw = target.split()
    cut = data.draw(st.sampled_from(["full", "part", "other"]))
    phrase = target if cut == "full" else " ".join(tw[:max(1, len(tw) - 1)]) if cut == "part" else \
        data.draw(st.sampled_from(stored))
    left = data.draw(st.lists(st.sampled_from(FILLER), max_size=2))
    right = data.draw(st.lists(st.sampled_from(FILLER), max_size=2))
    text = " ".join(["invoice", *left, phrase, *right, "for", "2", "widgets"])
    arg = data.draw(st.sampled_from([target, tw[0], data.draw(st.sampled_from(stored)), None]))
    got = name(stored, text, arg, item=item)
    if isinstance(got, str):
        assert _oracle(stored, text, got, item), (stored, text, arg, got)
    else:
        assert got[0].endswith(("_ambiguous", "_missing", "_unknown"))


@seed(20261030)
@settings(max_examples=1000, deadline=None, suppress_health_check=list(HealthCheck))
@given(base=st.sampled_from(["Box", "Battery", "Glass", "Case", "Cap", "Wire"]), raw=st.booleans())
def test_a_singular_and_its_plural_both_stored_never_resolve_silently_unless_raw(base, raw):
    plural = rv.regular_plural(base.lower()).capitalize()
    said = (plural if raw else plural.lower())
    got = name([base, plural], f"invoice Acme for 2 {said}", item=True)
    assert got == plural if raw else got[0] == "item_ambiguous"


# ---------- 2.4: figures and quantities ----------

def figure(text, value, currency="USD"):
    try:
        return rv.literal_figure(value, text, currency, Session(), "amount", ("How much did Acme Corp pay?", []))
    except Refusal as rf:
        return rf.code


@pytest.mark.parametrize("text, value, currency, expect", [
    ("Acme paid 1,200", "1200", "USD", "1200"), ("Acme paid $1,200.00", "1200", "USD", "1200"),
    ("Acme paid 500", "600", "USD", "amount_missing"), ("Acme paid", None, "USD", "amount_missing"),
    ("Acme paid €500", "500", "USD", "foreign_currency"), ("Acme paid 500€", "500", "USD", "foreign_currency"),
    ("Acme paid 500 €", "500", "USD", "foreign_currency"), ("Acme paid EUR500", "500", "USD", "foreign_currency"),
    ("Acme paid eur 500", "500", "USD", "foreign_currency"), ("Acme paid 500 euros", "500", "USD", "foreign_currency"),
    ("Acme paid 500 quid", "500", "USD", "foreign_currency"), ("Acme paid ₿500", "500", "USD", "foreign_currency"),
    ("Acme paid US$500", "500", "CAD", "foreign_currency"), ("Acme paid USD 500", "500", "CAD", "foreign_currency"),
    ("Acme paid $500", "500", "CAD", "500"), ("Acme paid 500 dollars", "500", "CAD", "500"),
    ("Acme paid €500", "500", "EUR", "500"), ("Acme paid all 500", "500", "USD", "500"),
    ("Acme paid ALL 500", "500", "USD", "foreign_currency"), ("Acme paid USD500", "500", "USD", "500"),
    ("Acme paid 500, PO4471", "4471", "USD", "amount_missing"),  # a figure inside another word is not one
])
def test_the_literal_figure_and_its_currency(text, value, currency, expect):
    assert figure(text, value, currency) == expect


def qty(text, value, item="Widget"):
    try:
        return rv.literal_quantity(value, text, item, Session(), "items.0.quantity")
    except Refusal as rf:
        return rf.code, rf.message


@pytest.mark.parametrize("text, value, expect", [
    ("invoice Acme for widgets", 1, ("quantity_missing", "How many widgets?")),
    ("invoice Acme for a widget", 1, "1"), ("invoice Acme for one widget", None, "1"),
    ("invoice Acme for a dozen widgets", 12, "12"), ("invoice Acme for 5 widgets", 5, "5"),
    ("invoice Acme for 5 widgets", None, "5"), ("invoice Acme for 5 widgets", 6, ("quantity_missing",
                                                                                  "How many widgets?")),
    ("invoice Acme for €5 widgets", 5, ("quantity_missing", "How many widgets?")),  # five, or €5 worth? asked
])
def test_quantities(text, value, expect):
    assert qty(text, value) == expect


# ---------- 2.5: dates ----------

@pytest.mark.parametrize("text, expect", [
    ("paid today", TODAY), ("paid yesterday", date(2026, 10, 28)), ("paid 2026-10-01", date(2026, 10, 1)),
    ("paid 3 days ago", date(2026, 10, 26)), ("paid last Friday", date(2026, 10, 23)),
    ("paid on Monday", date(2026, 10, 26)), ("paid Thursday", date(2026, 10, 22)),  # a Thursday: the one before
    ("paid 3 May", date(2026, 5, 3)), ("paid May 3, 2025", date(2025, 5, 3)), ("paid Dec 30", date(2025, 12, 30)),
    ("paid 13/4", date(2026, 4, 13)), ("paid 4/13", date(2026, 4, 13)), ("paid 03.04.2026", None),
    ("invoice May Smith for 2 widgets", None), ("paid 500", None),
])
def test_date_phrases(text, expect):
    got = when(text)
    assert got == expect or (expect is None and text.endswith("2026") and got[0] == "date_ambiguous")


def test_figure_dates_by_order():
    assert when("paid 3/4") == ("date_ambiguous", ["3 April", "4 March"])
    assert when("paid 3/4", "DMY") == date(2026, 4, 3)
    assert when("paid 3/4", "MDY") == date(2026, 3, 4)
    assert when("Acme paid 500 on 10/5", "MDY") == date(2026, 10, 5)
    assert when("the 25/10 one and paid 3/4")[1] == ["3 Apr 2026", "25 Oct 2026"]  # 3/4 read day first
    assert when("paid 5/5") == date(2026, 5, 5)


def test_unclear_dates_ask():
    assert when("paid sometime last week")[0] == "date_unknown"
    assert when("paid yesterday or Monday") == ("date_ambiguous", ["26 Oct 2026", "28 Oct 2026"])
    assert when("bezahlt am Freitag", model="2026-10-23")[0] == "date_unknown"  # a word in no shipped table
    assert when("Acme paid 500", model=TODAY.isoformat()) is None  # the model's today is the default anyway


# ---------- 2.3: document numbers ----------

def test_document_numbers_are_the_owners():
    assert rv.document_mentions("pay invoice 7, 380", "INV", ("invoice",)) == ["7"]
    assert rv.document_mentions("cancel INV-2026-00007", "INV", ("invoice",)) == ["INV-2026-00007"]
    assert rv._same_document("7", "INV-2026-00007") and not rv._same_document("7", "INV-2026-00008")
    assert not rv._same_document("INV-2025-00007", "INV-2026-00007")


# ---------- 2.7: the parts not done, and the planned replies ----------

def spans(text, names, item=False):
    return rv.bounded_names(rv.words(text), names, item=item)


def money(said, names=(), item=True):
    rv.planned_money(said, spans(said, list(names), item=item), TODAY)


def planned_route(said, names=()):
    with pytest.raises(Planned) as e:
        money(said, names)
    return e.value.route, e.value.message


def test_the_tax_row():
    route, msg = planned_route("invoice Wayne for 3 audits and add 8% sales tax")
    assert route == "planned.tax_engine" and "haven't drafted" in msg
    for said in ("invoice Wayne plus tax", "invoice Wayne with VAT", "invoice Wayne, GST included"):
        assert planned_route(said)[0] == "planned.tax_engine"
    for said in ("invoice Wayne for a taxi ride", "invoice Wayne for taxonomy work"):
        money(said)
    money("invoice Wayne for 2 tax consulting", ["Tax Consulting"])  # a resolved name never trips it


def test_the_discount_row():
    route, msg = planned_route("invoice Wayne for 2 widgets at 40% off")
    assert route == "planned.discounts" and msg == "Discounts aren't available yet, so I haven't drafted this invoice."
    for said in ("invoice Wayne for 2 widgets, discounted", "invoice Wayne for 2 widgets with a discount",
                 "invoice Wayne for 2 widgets, 10 off list"):
        assert planned_route(said)[0] == "planned.discounts"
    for said in ("invoice Wayne for a kick-off workshop", "invoice Wayne for the offer we sent",
                 "invoice Wayne, signed off by Bruce"):
        money(said)  # never a bare "off"
    money("invoice Wayne for 2 discount coupons", ["Discount Coupon"])


@pytest.mark.parametrize("said", [
    "invoice Wayne for 2 widgets due 15 November", "invoice Wayne for 2 widgets, due in 45 days",
    "invoice Wayne for 2 widgets, net 60", "invoice Wayne for 2 widgets with payment terms of 45 days",
    "Invoice Hooli for one website audit on net-60 terms.",
    "invoice Wayne for 2 widgets due on 2026-12-01", "invoice Wayne for 2 widgets due next Friday",
])
def test_the_due_date_and_terms_row(said):
    route, msg = planned_route(said)
    assert route == "planned.payment_terms" and msg.startswith("Setting a due date or payment terms isn't available")


def test_the_terms_row_needs_a_date_or_terms():
    for said in ("invoice Wayne for 2 widgets", "invoice Wayne for the overdue audit",
                 "invoice Wayne for 2 widgets, the net amount", "invoice Wayne for 2 widgets due"):
        money(said)
    assert name(["Wayne Enterprises"], "invoice Wayne Enterprises due 15 November") == "Wayne Enterprises"
    money("invoice Wayne for 2 Net-60 Consulting", ["Net-60 Consulting"])
    money("invoice Net-60 Services for 2 widgets", ["Net-60 Services"], item=False)


def test_the_stock_row():
    with pytest.raises(Planned) as e:
        rv.planned_stock("invoice Wayne for 5 copy paper from stock", [])
    assert e.value.route == "planned.stock"
    for said in ("invoice Wayne for a trip to stockholm", "invoice Wayne, warehoused goods"):
        rv.planned_stock(said, [])


def not_done(text, lines=(("Widget", "2"),), emailed=False):
    ctx = Ctx(conn=None, company_id="c", company_name="Co", currency="USD", today=TODAY)
    items = [{"name": "Widget", "standard_rate": Decimal("45.00")}, {"name": "Bolt", "standard_rate": Decimal("2.50")}]
    sp = spans(text, ["Widget", "Bolt"], item=True)
    return [e["part"] for e in rv.invoice_not_done(ctx, text, sp, [{"item": i, "quantity": q} for i, q in lines],
                                                   items, emailed)], rv.invoice_not_done(
        ctx, text, sp, [{"item": i, "quantity": q} for i, q in lines], items, emailed)


def test_not_done_events():
    assert not_done("send Wayne an invoice for 2 widgets")[0] == ["email"]
    assert not_done("invoice Wayne for 2 widgets", emailed=True)[0] == ["email"]
    assert not_done("invoice Wayne for 2 widgets")[0] == []
    parts, ev = not_done("invoice Wayne for 2 widgets at 40")
    assert parts == ["rate"] and "40.00 each for widgets" in ev[0]["text"] and "45.00" in ev[0]["text"]
    assert not_done("invoice Wayne for 2 widgets at 45")[0] == []
    assert not_done("invoice Wayne for 2 widgets 40 each and 3 bolts", (("Widget", "2"), ("Bolt", "3")))[0] == ["rate"]
    assert not_done("invoice Wayne for 2 widgets dated yesterday")[0] == ["date"]
    assert not_done("invoice Wayne for 2 widgets, memo: spring order")[0] == ["memo"]
    assert not_done("invoice Wayne for 2 widgets, PO 4471")[0] == ["memo"]


# ---------- 2.6: questions and choices ----------

def test_codes_are_single_use_and_bound_to_the_pending_question():
    s = Session()
    with pytest.raises(Refusal) as first:
        resolve_record([{"name": "Acme Corp"}, {"name": "Acme Ltd"}], "customer", "invoice Acme for 2", None, s,
                       "customer", item=False)
    q6 = first.value.data
    assert q6["question_id"] == "q1" and [c["code"] for c in q6["choices"]] == ["q1-1", "q1-2"]
    assert all("ref" not in c for c in q6["choices"])  # the event carries no reference
    with pytest.raises(Refusal) as second:
        resolve_date("paid 3/4", TODAY, s, "date")
    assert second.value.data["question_id"] == "q2"
    assert not s.answer("q1-2")  # an earlier question's code binds nothing
    assert s.answer("q2-1") and s.bindings == {"date": "2026-04-03"}
    assert not s.answer("q2-1")  # single use
    s.end()
    assert s.bindings == {} and not s.answer("q2-2")


def test_a_bound_choice_wins_over_the_models_argument():
    s = Session(bindings={"customer": "Acme Ltd"})
    got = resolve_record([{"name": "Acme Corp"}, {"name": "Acme Ltd"}], "customer", "invoice Acme for 2",
                         "Acme Corp", s, "customer", item=False)
    assert got == "Acme Ltd"


def test_a_session_survives_its_saved_state():
    s = Session()
    with pytest.raises(Refusal):
        resolve_date("paid 3/4", TODAY, s, "date")
    back = Session.load(s.dump())
    assert back.answer("q1-1") and back.bindings == {"date": "2026-04-03"}
    with pytest.raises(Refusal) as e:  # numbering goes on, so no code is reused
        resolve_date("paid 4/5", TODAY, Session.load(back.dump()), "other_date")
    assert e.value.data["question_id"] == "q2"
    assert Session.load(None).dump() == {"serial": 0, "pending": None, "bindings": {}}


def test_rate_notice_stays_with_the_longer_day_word_item():
    text = "invoice Wayne for 2 Copper Wire Monday Pack at 40 each and 3 Copper Wire"
    items = [
        {"name": "Copper Wire", "standard_rate": Decimal("45.00")},
        {"name": "Copper Wire Monday Pack", "standard_rate": Decimal("2.50")},
    ]
    ctx = Ctx(conn=None, company_id="c", company_name="Co", currency="USD", today=TODAY)
    events = rv.invoice_not_done(ctx, text, spans(text, [i["name"] for i in items], item=True),
                                [{"item": "Copper Wire Monday Pack", "quantity": "2"},
                                 {"item": "Copper Wire", "quantity": "3"}], items, False)
    rates = [event for event in events if event["part"] == "rate"]
    assert len(rates) == 1, events
    assert "40.00 each for copper wire monday packs;" in rates[0]["text"], rates
    assert "2.50" in rates[0]["text"] and "45.00" not in rates[0]["text"], rates
