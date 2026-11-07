"""Money words belong to their immutable message and to the names actually consumed there."""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from erpclaw_assistant import resolve as rv, store
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, ToolCall, call
from erpclaw_core import approvals, models as m
from erpclaw_core.common import company_ctx, new_id
from erpclaw_core.masters import create_item, create_party
from erpclaw_core.plugins import REGISTRY, load_plugins
from erpclaw_sales import tables as sales
from erpclaw_stock import service as stock
from erpclaw_stock.service import Trusted


TAX = [
    "add tax", "tax at 20%", "with V.A.T.", "with v a t", "with VAT",
    "plus sales tax", "20% tax", "tax-inclusive", "VAT-exclusive", "incl. VAT",
    "ex tax", "VAT@20%", "tax", "VAT", "GST", "HST", "with GST", "with HST",
    "plus VAT", "excl GST", "VAT included", "tax excluded", "GST inclusive",
    "HST exclusive", "with tax", "plus tax", "sales tax", "a V.A.T. charge",
    "a g s t charge", "a H.S.T. charge", "pay v a t", "invoice v a t and a b",
]
DISCOUNTS = [
    "20 percent off", "20 pct off", "20pc off", "ten percent off", "half price",
    "less 5%", "5 % off", "5% off", "5％ off", "with a discount", "discount 10%",
    "discount:10%", "5 per cent off", "less 5 %", "discount", "discounted",
    "5% discount", "a discount of", "half percent off", "one hundred per cent off",
]
CONTROLS = ["syntax", "taxi", "vatican", "Vatican", "taxonomy", "ship to a b c ltd",
            "kick-off workshop", "levy", "duty", "a b c", "avat", "vatical"]
CLASSIFICATIONS = [(s, "tax") for s in TAX] + [(s, "discount") for s in DISCOUNTS]
ROUTES = {"tax": "planned.tax_engine", "discount": "planned.discounts"}
TEXT = {
    "tax": "Sales tax isn't available yet, so I haven't drafted this invoice.",
    "discount": "Discounts aren't available yet, so I haven't drafted this invoice.",
}


def disguise(text, form):
    if form == "invisible":
        return "\u200b".join(text)
    if form == "homoglyph":
        return text.translate(str.maketrans("AaEeOoPpCcXxTtSIB", "АаЕеОоРрСсХхТтЅІΒ"))
    return text


@pytest.mark.parametrize("phrase,area", CLASSIFICATIONS)
@pytest.mark.parametrize("form", ["plain", "invisible", "homoglyph"])
def test_money_patterns_classify_the_supported_spellings(phrase, area, form):
    assert rv.money_area(disguise(phrase, form)) == area


@pytest.mark.parametrize("phrase", CONTROLS)
def test_money_patterns_require_bounded_words(phrase):
    assert rv.money_area(phrase) is None


@pytest.mark.parametrize("text,expected", [
    ("VAT@20%", "vat 20%"), ("discount:10%", "discount 10%"),
    ("a V.A.T. charge", "a v a t charge"), ("ship to a b c ltd", "ship to a b c ltd"),
    ("５％ off", "5% off"), ("oﬀ", "off"), ("e\u0301 VAT", "é vat"),
    ("\U0001f600VAT", "vat"), ("TАΧ", "tax"), ("ＧЅΤ", "gst"),
    ("\t VAT \n @ 20% ", "vat 20%"),
    ("Аа Ее Оо Рр Сс Хх Тт Ѕѕ Іі ΑΒΕΟΤΧ", "aa ee oo pp cc xx tt ss ii abeotx"),
])
def test_money_normalisation_preserves_separators(text, expected):
    assert rv.normalise_money(text) == expected


@pytest.mark.parametrize("char", [chr(c) for c in (
    0xAD, 0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x202A, 0x202B, 0x202C,
    0x202D, 0x202E, 0x2060, 0x2066, 0x2067, 0x2068, 0x2069, 0xFEFF)])
def test_invisible_format_characters_cannot_split_an_instruction(char):
    assert rv.normalise_money("V" + char + "AT") == "vat"


def number_name(n):
    units = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
    tens = "twenty thirty forty fifty sixty seventy eighty ninety".split()
    if n < 20:
        return units[n]
    if n == 100:
        return "one hundred"
    return tens[n // 10 - 2] + (" " + units[n % 10] if n % 10 else "")


@pytest.mark.parametrize("n", range(101))
def test_number_words_through_one_hundred_can_describe_a_discount(n):
    assert rv.money_area(number_name(n) + " percent off") == "discount"


@pytest.mark.parametrize("text,ranges,expected", [
    ("VAT@20%", [(0, 3)], "   @20%"),
    ("VAT VAT VAT", [(0, 3), (8, 11)], "    VAT    "),
    ("abcdef", [(0, 3), (2, 5)], "     f"),
    ("abcdef", [(0, 3), (3, 6)], "      "),
    ("VAT; 5 % off", [], "VAT; 5 % off"),
    ("\U0001f600ﬃ e\u0301 VAT; with VAT", [(1, 11)], "\U0001f600          with VAT"),
])
def test_masking_blanks_only_original_code_point_ranges(text, ranges, expected):
    result = rv.mask_money_spans(text, ranges)
    assert result == expected
    assert len(result) == len(text)


@pytest.mark.parametrize("prefix", ["ﬃ", "e\u0301", "\U0001f600", "５"])
def test_names_are_masked_before_compatibility_normalisation(prefix):
    text = prefix + " VAT Depot with VAT@20%"
    start = len(prefix) + 1
    assert rv.money_area(text, [(start, start + len("VAT Depot"))]) == "tax"
    assert rv.money_area(prefix + " VAT Depot", [(start, start + len("VAT Depot"))]) is None


@pytest.mark.parametrize("text,expected", [
    ("discount 10%, with VAT", "discount"), ("VAT with a discount", "tax"),
    ("less 5% plus tax", "discount"), ("a V.A.T. charge and 5% off", "tax"),
])
def test_the_first_money_instruction_determines_the_area(text, expected):
    assert rv.money_area(text) == expected


def test_only_consumed_characters_of_a_partial_name_are_masked():
    assert rv.money_area("Acme Discount Hardware", [(0, 4)]) == "discount"


@pytest.fixture
def money_books(engine):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        create_party(ctx, "customer", "Acme Supplies", None, None)
        create_party(ctx, "customer", "Discount Depot", None, None)
        create_item(ctx, "Copper Wire Deluxe", Decimal("28"))
        for name in ("Design Work", "Sales Tax Consulting", "Sales Tax"):
            create_item(ctx, name, Decimal("28"))
        warehouse = stock.create_warehouse(ctx, "Explicit Store", default=True)
        item = conn.execute(select(m.item.c.id).where(m.item.c.name == "Copper Wire")).scalar_one()
        stock.make_stock_item(ctx, item, "moving_average", warehouse)
        trusted = Trusted("Opening inventory for question answers")
        stock.receive(ctx, "opening-" + new_id()[:8], item, warehouse, "20", "10",
                      date(2026, 10, 1), trusted=trusted)
        trusted.verify(ctx)
    return engine


def assistant(engine, *, customer="Acme Corporation", items=None):
    invoice = call("create_invoice", customer=customer,
                   items=items or [{"item": "Copper Wire", "quantity": 5}])
    return Assistant(engine, ScriptedModel([Reply("", [invoice])] * 12))


PATHS = {
    "warehouse": ("Acme Corporation", "5 Copper Wire", "Explicit", "warehouse_unknown"),
    "item": ("Acme Corporation", "5 Copper Wire extra", "Explicit Store", "item_ambiguous"),
    "customer": ("Acme", "5 Copper Wire", "Explicit Store", "customer_ambiguous"),
    "quantity": ("Acme Corporation", "Copper Wire", "Explicit Store", "quantity_missing"),
}
ANSWERS = [("warehouse", "name"), ("warehouse", "label"), ("warehouse", "code"),
           ("item", "code"), ("customer", "code"), ("quantity", "code")]


def request(path, suffix=""):
    customer, item, source, _ = PATHS[path]
    return f"Invoice {customer} for {item} from {source} {suffix}".strip()


def question(events):
    asks = [e for e in events if e["type"] == "ask"]
    assert len(asks) == 1 and not any(e["type"] == "card" for e in events), events
    return asks[0]


def choose(a, events, label, *, by="code", extra="", cid="money"):
    q = question(events)
    c = next(c for c in q["choices"] if c["label"] == label or c["label"].startswith(label + " ("))
    text = extra if by == "code" else (label if by == "name" else c["label"]) + extra
    return a.turn(cid, text, choice=c["code"] if by == "code" else None)


def answer(a, events, *, by="code", extra="", cid="money"):
    q = question(events)
    if q["code"] == "quantity_missing":
        return a.turn(cid, "5" + extra)
    label = {"warehouse_unknown": "Explicit Store", "warehouse_ambiguous": "Explicit Store",
             "item_ambiguous": "Copper Wire", "customer_ambiguous": "Acme Corporation"}[q["code"]]
    return choose(a, events, label, by=by, extra=extra, cid=cid)


def card(events):
    cards = [e for e in events if e["type"] == "card"]
    assert len(cards) == 1 and not any(e["type"] == "ask" for e in events), events
    return cards[0]


def planned(engine, events, area):
    assert [e["type"] for e in events] == ["answer"], events
    assert events[0]["outcome"] == "c" and events[0]["capability"] == ROUTES[area], events
    assert events[0]["text"] == TEXT[area]
    with engine.connect() as conn:
        assert not conn.execute(select(m.approval.c.id).where(m.approval.c.status == "pending")).all()


def confirmation(events, name):
    q = question(events)
    assert [c["label"] for c in q["choices"]] == [name, "no, I mean tax or a discount"], q
    assert q["question_id"]
    return q


SPELLINGS = [(s, "discount") for s in ("5 % off", "5% off", "less 5 %", "5％ off")] + [
    (s, "tax") for s in ("tax-inclusive", "VAT-exclusive", "with sales tax", "with V.A.T.")]


@pytest.mark.parametrize("phrase,area", SPELLINGS)
@pytest.mark.parametrize("path,by", ANSWERS)
@pytest.mark.parametrize("placement", ["first", "later", "answer"])
def test_money_instruction_survives_each_question_path(money_books, phrase, area, path, by, placement):
    a = assistant(money_books)
    events = a.turn("money", request(path, phrase if placement == "first" else ""))
    if placement == "later":
        assert question(events)["code"] == PATHS[path][3]
        events = assistant(money_books).turn("money", phrase)
    elif placement == "answer":
        assert question(events)["code"] == PATHS[path][3]
        events = answer(assistant(money_books), events, by=by, extra=" " + phrase)
    planned(money_books, events, area)


@pytest.mark.parametrize("path,by", ANSWERS)
def test_ordinary_answers_still_make_the_card(money_books, path, by):
    a = assistant(money_books)
    events = a.turn("money", request(path))
    assert question(events)["code"] == PATHS[path][3]
    result = card(answer(assistant(money_books), events, by=by))
    assert ("Total", "140.00 USD") in [tuple(row) for row in result["card"]["lines"]]


@pytest.mark.parametrize("suffix,area", [("with VAT@20%", "tax"), ("with a discount:10%", "discount")])
@pytest.mark.parametrize("by", ["name", "label", "code"])
def test_a_warehouse_answer_exempts_only_its_consumed_name(money_books, suffix, area, by):
    a = assistant(money_books)
    events = a.turn("money", request("warehouse"))
    planned(money_books, choose(a, events, "Explicit Store", by=by,
                              extra=" Explicit Store " + suffix if by == "code" else " " + suffix), area)


@pytest.mark.parametrize("suffix", ["", " with VAT"])
def test_a_second_service_item_is_a_name_only_after_confirmation(money_books, suffix):
    a = assistant(money_books, items=[{"item": "Design Work", "quantity": 5},
                                    {"item": "Sales Tax Consulting", "quantity": 2}])
    events = a.turn("money", "Invoice Acme Corporation for 5 Design Work and 2 Sales Tax Consulting" + suffix)
    if suffix:
        planned(money_books, events, "tax")
    else:
        confirmation(events, "Sales Tax Consulting")
        result = card(choose(a, events, "Sales Tax Consulting"))
        assert "Sales Tax Consulting" in str(result["card"]["lines"])


@pytest.mark.parametrize("name", ["Copper Wire with VAT", "VAT", "Sales Tax Consulting", "V\u200bAT", "VАT"])
@pytest.mark.parametrize("as_name", [True, False])
def test_a_money_bearing_item_requires_an_explicit_classification(money_books, name, as_name):
    if name != "Sales Tax Consulting":
        with money_books.begin() as conn:
            create_item(company_ctx(conn), name, Decimal("28"))
    a = assistant(money_books, items=[{"item": name, "quantity": 5}])
    events = a.turn("money", f"Invoice Acme Corporation for 5 {name}")
    confirmation(events, name)
    events = choose(a, events, name if as_name else "no, I mean tax or a discount")
    if as_name:
        shown = [str(value) for line in card(events)["card"]["lines"] for value in
                 (line.values() if isinstance(line, dict) else line)]
        assert any(name in value for value in shown)  # the actual strings, not a list repr that escapes characters
    else:
        planned(money_books, events, "tax")


@pytest.mark.parametrize("by", ["code", "label", "name"])
def test_the_exact_customer_label_is_a_choice_even_when_it_contains_money(money_books, by):
    a = assistant(money_books, customer="Discount Depot")
    events = a.turn("money", "Invoice Discount Depot for 5 Design Work")
    confirmation(events, "Discount Depot")
    rid = store.open_request("money")["id"]
    a = assistant(money_books, customer="Discount Depot", items=[{"item": "Design Work", "quantity": 5}])
    result = card(choose(a, events, "Discount Depot", by=by))
    assert ("Customer", "Discount Depot") in [tuple(row) for row in result["card"]["lines"]]
    assert resolution(store.messages(rid)[-1]["id"])["spans"] == []


def test_free_text_keeps_the_confirmation_pending_and_cannot_recurse(money_books):
    a = assistant(money_books, customer="Discount Depot", items=[{"item": "Design Work", "quantity": 5}])
    first = a.turn("money", "Invoice Discount Depot for 5 Design Work")
    q = confirmation(first, "Discount Depot")
    again = a.turn("money", "yes")
    assert confirmation(again, "Discount Depot")["question_id"] == q["question_id"]
    card(choose(a, again, "Discount Depot", by="label"))


def test_a_label_with_appended_money_is_a_new_instruction(money_books):
    a = assistant(money_books, customer="Discount Depot", items=[{"item": "Design Work", "quantity": 5}])
    confirmation(a.turn("money", "Invoice Discount Depot for 5 Design Work"), "Discount Depot")
    planned(money_books, a.turn("money", "Discount Depot with VAT"), "tax")


def test_a_customer_correction_preserves_the_earlier_name_classification(money_books):
    a = assistant(money_books, customer="Discount Depot")
    events = a.turn("money", "Invoice Discount Depot for 5 Copper Wire from Explicit")
    confirmation(events, "Discount Depot")
    choose(a, events, "Discount Depot")
    a = assistant(money_books, customer="Hooli")
    events = a.turn("money", "Invoice Hooli instead")
    if question(events)["code"] == "customer_ambiguous":
        events = choose(a, events, "Hooli", by="label")
    result = card(choose(a, events, "Explicit Store", by="label"))
    assert ("Customer", "Hooli") in [tuple(row) for row in result["card"]["lines"]]


def test_a_correction_does_not_answer_a_pending_name_confirmation(money_books):
    a = assistant(money_books, customer="Discount Depot", items=[{"item": "Design Work", "quantity": 5}])
    first = a.turn("money", "Invoice Discount Depot for 5 Design Work")
    q = confirmation(first, "Discount Depot")
    again = assistant(money_books, customer="Hooli").turn("money", "change the customer to Hooli")
    assert confirmation(again, "Discount Depot")["question_id"] == q["question_id"]


def test_two_money_names_are_confirmed_in_message_order(money_books):
    a = assistant(money_books, customer="Discount Depot", items=[{"item": "Sales Tax Consulting", "quantity": 5}])
    events = a.turn("money", "Invoice Discount Depot for 5 Sales Tax Consulting")
    first = confirmation(events, "Discount Depot")
    events = choose(a, events, "Discount Depot")
    second = confirmation(events, "Sales Tax Consulting")
    assert first["question_id"] != second["question_id"]
    card(choose(a, events, "Sales Tax Consulting"))


def test_a_later_occurrence_of_the_same_money_name_needs_its_own_confirmation(money_books):
    a = assistant(money_books, customer="Discount Depot")
    events = a.turn("money", "Invoice Discount Depot for 5 Copper Wire from Explicit")
    first = confirmation(events, "Discount Depot")
    choose(a, events, "Discount Depot")
    again = a.turn("money", "Invoice Discount Depot instead")
    assert confirmation(again, "Discount Depot")["question_id"] != first["question_id"]


def test_a_name_in_an_earlier_message_does_not_hide_a_later_instruction(money_books):
    a = assistant(money_books, items=[{"item": "Sales Tax Consulting", "quantity": 5}])
    events = a.turn("money", "Invoice Acme for 5 Sales Tax Consulting")
    # The ordinary customer question may precede the money-name question.
    if question(events)["choices"][0]["label"] == "Acme Corporation":
        events = choose(a, events, "Acme Corporation")
    confirmation(events, "Sales Tax Consulting")
    choose(a, events, "Sales Tax Consulting")
    planned(money_books, a.turn("money", "with sales tax"), "tax")


def test_pending_confirmation_sets_aside_an_existing_card(money_books):
    a = assistant(money_books, items=[{"item": "Design Work", "quantity": 5}])
    first = card(a.turn("money", "Invoice Acme Corporation for 5 Design Work"))
    events = assistant(money_books, customer="Discount Depot").turn("money", "Invoice Discount Depot instead")
    confirmation(events, "Discount Depot")
    with money_books.connect() as conn:
        assert conn.execute(select(m.approval.c.status).where(
            m.approval.c.id == first["approval_id"])).scalar_one() == "superseded"
        assert not conn.execute(select(m.approval.c.id).where(m.approval.c.status == "pending")).all()


def test_an_earlier_card_cannot_be_approved_after_the_confirmation_checkpoint(money_books, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    first = card(assistant(money_books, items=[{"item": "Design Work", "quantity": 5}]).turn(
        "money", "Invoice Acme Corporation for 5 Design Work"))
    with money_books.connect() as conn:
        invoices_before = set(conn.execute(select(sales.invoice.c.id)).scalars())
    real_save = store.save_resolution
    outcomes = []

    def approve_after_checkpoint(claim, request_id, state):
        saved = real_save(claim, request_id, state)
        if saved and (state.get("pending") or {}).get("span_id") and not outcomes:
            assert store.get_request(request_id)["pending_question"]["span_id"]
            with ThreadPoolExecutor(max_workers=1) as pool:
                outcomes.append(pool.submit(approvals.approve, money_books, first["approval_id"],
                                            first["digest"]).result(timeout=10))
        return saved

    monkeypatch.setattr(store, "save_resolution", approve_after_checkpoint)
    events = assistant(money_books, customer="Discount Depot").turn("money", "Invoice Discount Depot instead")
    confirmation(events, "Discount Depot")
    assert [out.status for out in outcomes] == ["superseded"]
    with money_books.connect() as conn:
        assert not conn.execute(select(m.approval.c.id).where(m.approval.c.status == "pending")).all()
        assert set(conn.execute(select(sales.invoice.c.id)).scalars()) == invoices_before


def test_an_instruction_choice_holds_across_the_models_next_reply(money_books):
    replies = [
        Reply("", [call("record_payment", customer="Discount Depot", amount=5)]),
        Reply("", [call("create_invoice", customer="Discount Depot",
                         items=[{"item": "Design Work", "quantity": 5}])]),
        Reply("Ready."),
    ]
    results = []
    for index, reply in enumerate(replies):
        cid = f"money-{index}"
        first = assistant(money_books, customer="Discount Depot", items=[{"item": "Design Work", "quantity": 5}]).turn(
            cid, "Invoice Discount Depot for 5 Design Work")
        confirmation(first, "Discount Depot")
        a = Assistant(money_books, ScriptedModel([reply]))
        results.append(choose(a, first, "no, I mean tax or a discount", cid=cid))
    assert [[e.get("capability") for e in events] for events in results] == [[ROUTES["discount"]]] * 3
    for events in results:
        planned(money_books, events, "discount")


@pytest.mark.parametrize("include_items", [True, False], ids=["empty_items", "missing_items"])
@pytest.mark.parametrize("as_name", [True, False], ids=["name", "instruction"])
def test_an_implicitly_resolved_money_item_requires_confirmation(money_books, include_items, as_name):
    args = {"customer": "Acme Corporation"}
    if include_items:
        args["items"] = []
    a = Assistant(money_books, ScriptedModel([Reply("", [call("create_invoice", **args)])] * 3))
    text = "Invoice Acme Corporation for 5 Sales Tax Consulting"
    first = a.turn("money", text)
    confirmation(first, "Sales Tax Consulting")
    mid = store.messages(store.open_request("money")["id"])[0]["id"]
    span = next(s for s in resolution(mid)["spans"] if s["kind"] == "item")
    assert text[span["start"]:span["end"]] == "Sales Tax Consulting"
    assert span["classification"] is None
    events = choose(a, first, "Sales Tax Consulting" if as_name else "no, I mean tax or a discount")
    if as_name:
        assert "Sales Tax Consulting" in str(card(events)["card"]["lines"])
    else:
        planned(money_books, events, "tax")
    assert next(s for s in resolution(mid)["spans"] if s["id"] == span["id"])["classification"] == (
        "name" if as_name else "instruction")


def resolution(message_id):
    reader = getattr(store, "message_resolution", None)
    assert callable(reader), "The store must expose completed name consumption by immutable message id"
    return reader(message_id)


def test_completed_empty_name_consumption_is_explicit_and_survives_restart(engine):
    Assistant(engine, ScriptedModel([Reply("Hello.")])).turn("money", "hello")
    with store._tx() as conn:
        mid = conn.execute("SELECT id FROM request_message WHERE conversation_id = ?", ("money",)).fetchone()["id"]
    saved = resolution(mid)
    assert saved["completed"] is True and saved["spans"] == []
    store._ready.clear()
    assert resolution(mid) == saved


def test_confirmation_classification_survives_a_new_assistant_and_a_correction(money_books):
    a = assistant(money_books, customer="Discount Depot")
    events = a.turn("money", "Invoice Discount Depot for 5 Copper Wire from Explicit")
    confirmation(events, "Discount Depot")
    req = store.open_request("money")
    mid = store.messages(req["id"])[0]["id"]
    original = resolution(mid)
    assert original["completed"] is True
    span = next(s for s in original["spans"] if s["kind"] == "customer")
    assert (span["start"], span["end"]) == (8, 22)
    with money_books.connect() as conn:
        assert span["record_id"] == conn.execute(select(m.party.c.id).where(
            m.party.c.name == "Discount Depot")).scalar_one()
    store._ready.clear()
    choose(assistant(money_books, customer="Discount Depot"), events, "Discount Depot")
    saved = resolution(mid)
    assert next(s for s in saved["spans"] if s["id"] == span["id"])["classification"] == "name"
    assistant(money_books, customer="Hooli").turn("money", "Invoice Hooli instead")
    assert resolution(mid) == saved
    newer = store.messages(req["id"])[-1]
    assert newer["id"] != mid and resolution(newer["id"])["completed"] is True


def test_a_choice_answer_records_its_question_and_field(money_books):
    a = assistant(money_books)
    events = a.turn("money", request("warehouse"))
    q = question(events)
    req = store.open_request("money")
    card(choose(a, events, "Explicit Store", by="label"))
    msg = store.messages(req["id"])[-1]
    saved = resolution(msg["id"])
    assert saved["question_id"] == q["question_id"]
    assert saved["field"] == q["data"]["field"]
    span = next(s for s in saved["spans"] if s["kind"] == "warehouse")
    assert msg["text"][span["start"]:span["end"]] == "Explicit Store"


@pytest.mark.parametrize("name", ["ﬃ VAT", "Cafe\u0301 VAT", "\U0001f600 VAT"])
def test_actual_name_consumption_uses_original_unicode_offsets(money_books, name):
    with money_books.begin() as conn:
        create_party(company_ctx(conn), "customer", name, None, None)
    events = assistant(money_books, customer=name).turn("money", f"Invoice {name} for 5 Design Work")
    confirmation(events, name)
    mid = store.messages(store.open_request("money")["id"])[0]["id"]
    saved = resolution(mid)
    span = next(s for s in saved["spans"] if s["kind"] == "customer")
    assert (span["start"], span["end"]) == (8, 8 + len(name))


def test_resolution_is_committed_before_a_proposal_is_created(money_books, monkeypatch):
    real = approvals.propose

    def checked(*args, **kwargs):
        req = store.open_request("money")
        for msg in store.messages(req["id"]):
            assert resolution(msg["id"])["completed"] is True
        return real(*args, **kwargs)

    monkeypatch.setattr(approvals, "propose", checked)
    card(assistant(money_books, items=[{"item": "Design Work", "quantity": 5}]).turn(
        "money", "Invoice Acme Corporation for 5 Design Work"))


def test_a_stale_worker_cannot_finalise_resolution(money_books, monkeypatch):
    now = store._now()
    monkeypatch.setattr(store, "_now", lambda: now)
    c = store.claim("money", "Invoice Acme Corporation for 5 Design Work")
    req = store.new_request(c)
    assert store.assign(c, req["id"])
    monkeypatch.setattr(store, "_now", lambda: now + store.LEASE + timedelta(seconds=1))
    assert store.claim("money", "Invoice Hooli instead") is not None
    before = resolution(c.message_id)
    assert not store.finalise(c, req["id"], [], pending_question=None, pending_card=None,
                              state="open", why=None, serial_now=0, bindings_now={}, intent_state="unused")
    assert resolution(c.message_id) == before


@pytest.mark.parametrize("text,area", [("with a discount then VAT", "discount"),
                                       ("with VAT then a discount", "tax")])
def test_a_turn_reports_the_first_money_area_in_text_order(money_books, text, area):
    planned(money_books, assistant(money_books).turn("money", request("warehouse", text)), area)


@pytest.mark.parametrize("text", ["with a V.A.T. charge", "with V\u200bAT", "with VАT", "with VAT@20%"])
def test_obfuscated_money_in_an_actual_request_is_still_an_instruction(money_books, text):
    planned(money_books, assistant(money_books).turn(
        "money", "Invoice Acme Corporation for 5 Copper Wire " + text), "tax")


@pytest.mark.parametrize("text", ["syntax", "taxi", "vatican", "taxonomy", "ship to a b c ltd"])
def test_ordinary_words_inside_an_invoice_are_not_money_instructions(money_books, text):
    with money_books.begin() as conn:
        create_item(company_ctx(conn), text, Decimal("28"))
    card(assistant(money_books, items=[{"item": text, "quantity": 5}]).turn(
        "money", "Invoice Acme Corporation for 5 " + text))


def test_payment_preserves_an_explicit_tax_instruction(engine):
    a = Assistant(engine, ScriptedModel([Reply("", [call("record_payment", customer="Hooli", amount=500)])]))
    planned(engine, a.turn("money", "Hooli paid 500 with VAT"), "tax")


def test_a_sales_order_keeps_its_existing_card_path(money_books):
    order = call("create_sales_order", customer="Acme Corporation",
                 items=[{"item": "Copper Wire", "quantity": 5}], delivery_date="2026-11-30")
    a = Assistant(money_books, ScriptedModel([Reply("", [order])]))
    card(a.turn("money", "Order for Acme Corporation for 5 Copper Wire on 30 November 2026"))


def test_a_crash_after_proposal_keeps_completed_resolution(money_books, monkeypatch):
    def killed(*args, **kwargs):
        raise RuntimeError("stopped after proposal")

    monkeypatch.setattr(store, "finalise", killed)
    with pytest.raises(RuntimeError, match="stopped after proposal"):
        assistant(money_books, items=[{"item": "Design Work", "quantity": 5}]).turn(
            "money", "Invoice Acme Corporation for 5 Design Work")
    req = store.open_request("money")
    mid = store.messages(req["id"])[0]["id"]
    saved = resolution(mid)
    assert saved["completed"] is True and saved["spans"]
    store._ready.clear()
    assert resolution(mid) == saved


def test_repeated_names_in_one_message_are_distinct_confirmation_spans(money_books):
    a = assistant(money_books, customer="Discount Depot", items=[{"item": "Design Work", "quantity": 5}])
    events = a.turn("money", "Invoice Discount Depot for 5 Design Work for Discount Depot")
    first = confirmation(events, "Discount Depot")
    events = choose(a, events, "Discount Depot")
    second = confirmation(events, "Discount Depot")
    assert first["question_id"] != second["question_id"]
    card(choose(a, events, "Discount Depot"))


def test_only_the_consumed_part_of_a_customer_name_can_be_exempted(money_books):
    with money_books.begin() as conn:
        create_party(company_ctx(conn), "customer", "Acme", None, None)
    planned(money_books, assistant(money_books, customer="Acme").turn(
        "money", "Invoice Acme Discount Hardware for 5 Copper Wire"), "discount")


@pytest.mark.parametrize("name", ["VAT Depot", "The GST Depot", "Discount Depot"])
@pytest.mark.parametrize("by", ["code", "label"])
def test_a_money_bearing_warehouse_needs_confirmation_before_its_card(money_books, name, by):
    with money_books.begin() as conn:
        ctx = company_ctx(conn)
        warehouse = stock.create_warehouse(ctx, name)
        item = conn.execute(select(m.item.c.id).where(m.item.c.name == "Copper Wire")).scalar_one()
        trusted = Trusted("Opening stock at a named warehouse")
        stock.receive(ctx, "opening-" + new_id()[:8], item, warehouse, "20", "10",
                      date(2026, 10, 1), trusted=trusted)
        trusted.verify(ctx)
    a = assistant(money_books)
    events = a.turn("money", f"Invoice Acme Corporation for 5 Copper Wire from {name}")
    q = question(events)
    if q["code"] in ("warehouse_unknown", "warehouse_ambiguous"):
        events = choose(a, events, name, by=by)
    confirmation(events, name)
    result = card(choose(a, events, name, by=by))
    assert ("From", name + " (20 on hand)") in [tuple(row) for row in result["card"]["lines"]]


@pytest.mark.parametrize("text", ["yes", "first", "1", "Discount Depot!"])
def test_confirmation_accepts_only_its_codes_and_exact_labels(money_books, text):
    a = assistant(money_books, customer="Discount Depot", items=[{"item": "Design Work", "quantity": 5}])
    first = a.turn("money", "Invoice Discount Depot for 5 Design Work")
    q = confirmation(first, "Discount Depot")
    again = a.turn("money", text)
    if text == "Discount Depot!":
        # This occurrence is a new message; punctuation does not turn it into a choice answer.
        assert question(again)["question_id"] == q["question_id"]
    else:
        assert confirmation(again, "Discount Depot")["question_id"] == q["question_id"]
    with money_books.connect() as conn:
        assert not conn.execute(select(m.approval.c.id).where(m.approval.c.status == "pending")).all()


def test_a_model_text_reply_cannot_close_a_pending_confirmation(money_books):
    first = assistant(money_books, customer="Discount Depot", items=[{"item": "Design Work", "quantity": 5}]).turn(
        "money", "Invoice Discount Depot for 5 Design Work")
    q = confirmation(first, "Discount Depot")
    again = Assistant(money_books, ScriptedModel([Reply("Please choose.")])).turn("money", "yes")
    assert confirmation(again, "Discount Depot")["question_id"] == q["question_id"]


@pytest.mark.parametrize("text,area", [("with VAT", "tax"), ("with a discount", "discount")])
def test_a_confirmation_code_does_not_consume_appended_instructions(money_books, text, area):
    a = assistant(money_books, customer="Discount Depot", items=[{"item": "Design Work", "quantity": 5}])
    first = a.turn("money", "Invoice Discount Depot for 5 Design Work")
    planned(money_books, choose(a, first, "Discount Depot", extra=text), area)


def test_a_different_operation_cannot_bypass_a_pending_confirmation(money_books):
    first = assistant(money_books, customer="Discount Depot", items=[{"item": "Design Work", "quantity": 5}]).turn(
        "money", "Invoice Discount Depot for 5 Design Work")
    q = confirmation(first, "Discount Depot")
    payment = call("record_payment", customer="Hooli", amount=500)
    again = Assistant(money_books, ScriptedModel([Reply("", [payment])])).turn("money", "Hooli paid 500")
    assert confirmation(again, "Discount Depot")["question_id"] == q["question_id"]
    with money_books.connect() as conn:
        assert not conn.execute(select(m.approval.c.id).where(m.approval.c.status == "pending")).all()


def prepared_resolution(engine, claim):
    session = rv.Session(owner_messages=[{"id": claim.message_id,
        "text": "Invoice Discount Depot for 5 Design Work", "completed": False, "spans": []}])
    with engine.connect() as conn:
        rv.record_message_names(company_ctx(conn), session, rv.customers(company_ctx(conn)),
                                rv._items(company_ctx(conn)), "Discount Depot", ["Design Work"])
    with pytest.raises(rv.Refusal):
        rv.guard_messages(session)
    session.bindings["customer"] = "Discount Depot"
    return session


def test_expired_claim_cannot_checkpoint_spans_questions_or_bindings(money_books, monkeypatch):
    now = store._now()
    monkeypatch.setattr(store, "_now", lambda: now)
    c = store.claim("money", "Invoice Discount Depot for 5 Design Work")
    req = store.new_request(c)
    assert store.assign(c, req["id"])
    session = prepared_resolution(money_books, c)
    before = resolution(c.message_id)
    monkeypatch.setattr(store, "_now", lambda: now + store.LEASE + timedelta(seconds=1))
    assert store.claim("money", "hello") is not None
    assert not store.save_resolution(c, req["id"], session.dump())
    assert resolution(c.message_id) == before
    assert store.bindings(req["id"]) == {}
    assert store.get_request(req["id"])["pending_question"] is None
    assert store.serial("money") == 0


def test_checkpoint_rollback_leaves_no_partial_name_classification(money_books, monkeypatch):
    from contextlib import contextmanager

    c = store.claim("money", "Invoice Discount Depot for 5 Design Work")
    req = store.new_request(c)
    assert store.assign(c, req["id"])
    session = prepared_resolution(money_books, c)
    real_tx = store._tx

    @contextmanager
    def interrupted():
        with real_tx() as conn:
            yield conn
            raise RuntimeError("interrupted before commit")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_tx", interrupted)
        with pytest.raises(RuntimeError, match="interrupted before commit"):
            store.save_resolution(c, req["id"], session.dump())
    assert resolution(c.message_id)["completed"] is False
    assert resolution(c.message_id)["spans"] == []
    assert store.bindings(req["id"]) == {}
    assert store.get_request(req["id"])["pending_question"] is None
    assert store.serial("money") == 0
    assert store.save_resolution(c, req["id"], session.dump())
    assert resolution(c.message_id)["completed"] is True
    assert store.get_request(req["id"])["pending_question"]["span_id"]
    assert store.bindings(req["id"]) == session.bindings


def test_completed_messages_are_never_sent_back_through_name_resolution(money_books, monkeypatch):
    a = assistant(money_books, customer="Discount Depot", items=[{"item": "Design Work", "quantity": 5}])
    confirmation(a.turn("money", "Invoice Discount Depot for 5 Design Work"), "Discount Depot")
    msg = store.messages(store.open_request("money")["id"])[0]
    saved = resolution(msg["id"])
    session = rv.Session(owner_messages=[{**msg, **saved}])

    def forbidden(*args, **kwargs):
        raise AssertionError("completed consumption was resolved again")

    monkeypatch.setattr(rv, "_resolve_record", forbidden)
    with money_books.connect() as conn:
        rv.record_message_names(company_ctx(conn), session, [], [], "Changed name", [])
    assert session.owner_messages[0]["spans"] == saved["spans"]
    assert resolution(msg["id"]) == saved


def test_incomplete_message_discards_uncommitted_spans_before_resolution(money_books):
    session = rv.Session(owner_messages=[{"id": 1, "text": "Invoice Discount Depot for 5 Design Work",
        "completed": False, "spans": [{"id": "unfinished", "classification": "name"}]}])
    with money_books.connect() as conn:
        ctx = company_ctx(conn)
        rv.record_message_names(ctx, session, rv.customers(ctx), rv._items(ctx), "Discount Depot", ["Design Work"])
    msg = session.owner_messages[0]
    assert msg["completed"] is True
    span = next(s for s in msg["spans"] if s["kind"] == "customer")
    assert span["id"] != "unfinished" and span["classification"] is None
    with pytest.raises(rv.Refusal, match="Discount Depot"):
        rv.guard_messages(session)


def test_a_confirmed_invisible_name_is_retained_in_the_actual_card_text(money_books):
    name = "V\u200bAT"
    with money_books.begin() as conn:
        create_item(company_ctx(conn), name, Decimal("28"))
    a = assistant(money_books, items=[{"item": name, "quantity": 5}])
    events = a.turn("money", f"Invoice Acme Corporation for 5 {name}")
    confirmation(events, name)
    result = card(choose(a, events, name))
    assert any(name in value for _, value in result["card"]["lines"])


def test_cancelling_a_confirmation_records_completed_empty_consumption(money_books):
    a = assistant(money_books, customer="Discount Depot", items=[{"item": "Design Work", "quantity": 5}])
    confirmation(a.turn("money", "Invoice Discount Depot for 5 Design Work"), "Discount Depot")
    req = store.open_request("money")
    a.turn("money", "cancel")
    saved = resolution(store.messages(req["id"])[-1]["id"])
    assert saved["completed"] is True and saved["spans"] == []
    assert store.get_request(req["id"])["state"] == "ended"


def test_existing_store_migrates_without_rewriting_owner_messages(engine):
    c = store.claim("money", "hello")
    req = store.new_request(c)
    assert store.assign(c, req["id"])
    original = store.messages(req["id"])
    with store._tx() as conn:
        conn.execute("DROP TABLE span_confirmation")
        conn.execute("DROP TABLE message_name_span")
        conn.execute("DROP TABLE message_resolution")
        conn.execute("UPDATE store_version SET version = 2")
    store._ready.clear()
    assert store.version() == store.VERSION == 3
    assert store.messages(req["id"]) == original
    assert resolution(c.message_id)["completed"] is False


@pytest.mark.parametrize("field", ["quantity", "customer"])
def test_a_consumed_free_answer_records_its_question_identity(money_books, field):
    a = assistant(money_books, items=[{"item": "Design Work", "quantity": 5}])
    text = "Invoice Acme Corporation for Design Work" if field == "quantity" else "Invoice for 5 Design Work"
    first = a.turn("money", text)
    q = question(first)
    assert q["code"] == field + "_missing"
    req = store.open_request("money")
    card(a.turn("money", "5" if field == "quantity" else "Acme Corporation"))
    saved = resolution(store.messages(req["id"])[-1]["id"])
    assert saved["question_id"] == q["question_id"]
    assert saved["field"] == q["data"]["field"]


def test_incomplete_stored_consumption_cannot_exempt_a_name(money_books):
    a = assistant(money_books, customer="Discount Depot", items=[{"item": "Design Work", "quantity": 5}])
    confirmation(a.turn("money", "Invoice Discount Depot for 5 Design Work"), "Discount Depot")
    msg = store.messages(store.open_request("money")["id"])[0]
    original = resolution(msg["id"])
    with store._tx() as conn:
        conn.execute("UPDATE message_resolution SET completed = 0 WHERE message_id = ?", (msg["id"],))
        conn.execute("UPDATE span_confirmation SET classification = 'name'")
    incomplete = resolution(msg["id"])
    assert incomplete["completed"] is False and incomplete["spans"] == []
    session = rv.Session(owner_messages=[{**msg, **incomplete}])
    with money_books.connect() as conn:
        ctx = company_ctx(conn)
        rv.record_message_names(ctx, session, rv.customers(ctx), rv._items(ctx), "Discount Depot", ["Design Work"])
    assert all(s["id"] not in {old["id"] for old in original["spans"]} for s in session.owner_messages[0]["spans"])
    with pytest.raises(rv.Refusal, match="Discount Depot"):
        rv.guard_messages(session)


def test_a_read_reply_still_checks_new_money_while_confirmation_is_pending(money_books):
    confirmation(assistant(money_books, customer="Discount Depot", items=[{"item": "Design Work", "quantity": 5}]).turn(
        "money", "Invoice Discount Depot for 5 Design Work"), "Discount Depot")
    a = Assistant(money_books, ScriptedModel([Reply("", [call("get_metric", metric="receivables")])]))
    planned(money_books, a.turn("money", "with VAT"), "tax")


@pytest.mark.parametrize("customer", ["Hooli", "Discount Depot"])
def test_payment_records_consumed_customer_while_preserving_tax(money_books, customer):
    payment = call("record_payment", customer=customer, amount=500)
    a = Assistant(money_books, ScriptedModel([Reply("", [payment])]))
    planned(money_books, a.turn("money", f"{customer} paid 500 with VAT"), "tax")
    with store._tx() as conn:
        msg = dict(conn.execute("SELECT id, text FROM request_message WHERE conversation_id = ?",
                                ("money",)).fetchone())
    saved = resolution(msg["id"])
    span = next(s for s in saved["spans"] if s["kind"] == "customer")
    assert msg["text"][span["start"]:span["end"]] == customer
    assert span["question"] is None


# Enumerate the same contracts the assistant uses, including writes with no resolver marks.
load_plugins()
WRITE_TOOLS = sorted(n for n, c in REGISTRY.contracts.items() if c.effect != "read")


def write_example(engine, tool):
    examples = {
        "set_up_company": ({"name": "New Company", "currency": "USD"}, "Set up New Company in USD"),
        "add_customer": ({"name": "New Customer"}, "Add customer New Customer"),
        "add_item": ({"name": "New Item", "price": 5}, "Add item New Item for 5"),
        "create_invoice": ({"customer": "Acme Corporation", "items": [{"item": "Design Work", "quantity": 5}]},
                           "Invoice Acme Corporation for 5 Design Work"),
        "submit_invoice": ({}, "Submit an invoice"),
        "record_payment": ({"customer": "Hooli", "amount": 5}, "Hooli paid 5"),
        "cancel_invoice": ({}, "Cancel an invoice"),
        "reverse_payment": ({}, "Reverse a payment"),
        "create_sales_order": ({"customer": "Acme Corporation", "items": [{"item": "Design Work", "quantity": 5}],
                                "delivery_date": "2026-11-30"},
                               "Order for Acme Corporation for 5 Design Work on 30 November 2026"),
        "deliver_order": ({}, "Deliver an order"),
        "create_credit_note": ({}, "Credit an invoice"),
        "record_opening_stock": ({"lines": [{"item": "Unlisted Wire", "warehouse": "Explicit Store",
                                              "quantity": "5", "rate": "10"}]},
                                 "Record opening stock of 5 Unlisted Wire at 10 in Explicit Store"),
        "write_off_stock": ({"item": "Copper Wire", "warehouse": "Explicit Store", "quantity": "5"},
                            "Write off 5 Copper Wire from Explicit Store"),
        "record_stock_count": ({"lines": [{"item": "Copper Wire", "warehouse": "Explicit Store", "counted": "5"}]},
                               "Count 5 Copper Wire in Explicit Store"),
    }
    assert set(examples) | {"cancel_stock_movement"} == set(WRITE_TOOLS)
    if tool == "set_up_company":
        with engine.begin() as conn:
            conn.execute(m.company.update().values(status="pending"))
    if tool == "cancel_stock_movement":
        contract = REGISTRY.contracts["write_off_stock"]
        out = approvals.propose(engine, contract, {"item": "Copper Wire", "warehouse": "Explicit Store",
                                                 "quantity": "1"})
        done = approvals.approve(engine, out.approval_id, out.digest)
        assert done.status == "committed", done
        number = done.data["number"]
        return call(tool, document=number, date="2026-10-26"), f"Cancel stock movement {number} on 26 October 2026"
    args, text = examples[tool]
    return ToolCall("write-example", tool, args), text


@pytest.mark.parametrize("next_tool", ["record_payment", "create_invoice", "create_sales_order", "none"])
@pytest.mark.parametrize("extra,area", [("with VAT", "tax"), ("with a discount", "discount"),
                                        ("with VAT@20%", "tax"), ("with ten percent off", "discount")])
def test_a_name_choice_exempts_no_text_beside_it_across_tools(money_books, next_tool, extra, area):
    first = assistant(money_books, customer="Discount Depot", items=[{"item": "Design Work", "quantity": 5}]).turn(
        "money", "Invoice Discount Depot for 5 Design Work")
    confirmation(first, "Discount Depot")
    args = {"customer": "Discount Depot"}
    if next_tool == "record_payment":
        args["amount"] = 5
    else:
        args["items"] = [{"item": "Design Work", "quantity": 5}]
        if next_tool == "create_sales_order":
            args["delivery_date"] = "2026-11-30"
    reply = Reply("Ready.") if next_tool == "none" else Reply("", [call(next_tool, **args)])
    a = Assistant(money_books, ScriptedModel([reply] * 4))
    planned(money_books, choose(a, first, "Discount Depot", extra=extra), area)


@pytest.mark.parametrize("tool", WRITE_TOOLS)
@pytest.mark.parametrize("placement", ["first", "later", "name", "instruction"])
@pytest.mark.parametrize("phrase,area", [("with VAT@20%", "tax"), ("with ten percent off", "discount")])
def test_every_registered_write_preserves_money_instructions(money_books, tool, placement, phrase, area):
    if placement in ("name", "instruction"):
        first = assistant(money_books, customer="Discount Depot", items=[{"item": "Design Work", "quantity": 5}]).turn(
            "money", "Invoice Discount Depot for 5 Design Work")
        confirmation(first, "Discount Depot")
    elif placement == "later":
        first = assistant(money_books).turn("money", request("warehouse"))
        question(first)
    proposed, text = write_example(money_books, tool)
    a = Assistant(money_books, ScriptedModel([Reply("", [proposed])] * 4))
    if placement in ("name", "instruction"):
        label = "Discount Depot" if placement == "name" else "no, I mean tax or a discount"
        events = choose(a, first, label, extra=phrase)
        # The original customer's classified instruction precedes the appended answer text.
        area = "discount" if placement == "instruction" else area
    else:
        events = a.turn("money", (text + " " if placement == "first" else "") + phrase)
    planned(money_books, events, area)


@pytest.mark.parametrize("tool", WRITE_TOOLS)
def test_every_registered_write_keeps_its_ordinary_card_or_question(money_books, tool):
    proposed, text = write_example(money_books, tool)
    a = Assistant(money_books, ScriptedModel([Reply("", [proposed])] * 4))
    events = a.turn("money", text)
    assert any(e["type"] in ("card", "ask") for e in events), events
    assert not any(e.get("capability") in ROUTES.values() for e in events), events


def test_a_pure_name_choice_allows_a_payment_for_the_confirmed_customer(money_books):
    first = assistant(money_books, customer="Discount Depot", items=[{"item": "Design Work", "quantity": 5}]).turn(
        "money", "Invoice Discount Depot for 5 Design Work")
    confirmation(first, "Discount Depot")
    a = Assistant(money_books, ScriptedModel([
        Reply("", [call("record_payment", customer="Discount Depot", amount=5)])]))
    result = card(choose(a, first, "Discount Depot"))
    assert ("Customer", "Discount Depot") in [tuple(row) for row in result["card"]["lines"]]
