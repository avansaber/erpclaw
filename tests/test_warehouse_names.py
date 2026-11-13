"""Warehouse-name reader: the longest known-name match after "from".

Trailing dates stay outside the span; unknown names ask for stock and are
ignored for service. Reader units set their own items, customers, holdings
and known-name set, and reject database access. The proposed fixture names
are also verified against a PostgreSQL company's warehouse service.
"""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from erpclaw_assistant import resolve as rv
from erpclaw_assistant.resolve_warehouse import _company_warehouse_names as read_company_warehouse_names
from erpclaw_core.common import Ctx, Refusal
from erpclaw_core.masters import _norm
import erpclaw_sales as sales
from erpclaw_sales import stock_sales as stock
from erpclaw_assistant import resolve_amounts, resolve_args, resolve_inventory, resolve_payment, resolve_policy, resolve_records, resolve_types, resolve_warehouse, resolve_words


TODAY = date(2026, 10, 26)  # a Monday; every test pins its own day.
CUSTOMER = {"id": "acme", "name": "Acme Corporation", "email": None, "city": None}
CONTRACT = SimpleNamespace(name="create_invoice", input_model=sales.CreateInvoice)
SCOPE = "create_invoice#0:"


class NoBooks:
    def execute(self, *args, **kwargs):
        raise AssertionError("A warehouse reader unit must not access a database")


@pytest.fixture
def ctx():
    return Ctx(NoBooks(), "company", "Test Company", "USD", TODAY)


def stock_item():
    return {"id": "widgets", "name": "Widgets", "unit": "unit", "standard_rate": "2.5", "kind": "stock"}


def service_item():
    return {"id": "consult", "name": "Consulting", "unit": "hour", "standard_rate": "150", "kind": "service"}


def holding_rows(names):
    return [{"warehouse": name.lower().replace(" ", "-"), "name": name, "qty": "20"} for name in names]


def use_state(monkeypatch, *, items, warehouses, holdings=None):
    """This test's own company state, mirroring the existing reader-unit pattern."""
    _replacement = (lambda c: [CUSTOMER])
    for _module in (rv, resolve_args, resolve_inventory, resolve_records):
        monkeypatch.setattr(_module, 'customers', _replacement)
    _replacement = (lambda c: items)
    for _module in (rv, resolve_args, resolve_inventory, resolve_policy, resolve_types):
        monkeypatch.setattr(_module, '_items', _replacement)
    _replacement = (lambda c: {_norm(n) for n in warehouses})
    for _module in (rv, resolve_warehouse):
        monkeypatch.setattr(_module, '_company_warehouse_names', _replacement)
    _replacement = (lambda *a, **kw: {'warehouses': holding_rows(warehouses)})
    for _module in (rv, resolve_args, resolve_policy, resolve_warehouse):
        monkeypatch.setattr(_module, 'call_service', _replacement)
    # Exercise the service binding even when the phrase reader uses its own company-name stub.
    assert read_company_warehouse_names(None) == {_norm(n) for n in warehouses}
    monkeypatch.setattr(stock, "holdings", lambda c, item: holding_rows(holdings if holdings is not None else warehouses))


def check(text, ctx, lines, session=None):
    args = {"customer": "Acme Corporation", "items": lines}
    session = session if session is not None else rv.Session()
    return rv.resolve_args(ctx, CONTRACT, args, text, session, scope=SCOPE), session


def settle(text, ctx, result, session):
    rv.account(ctx, text, [(CONTRACT, result.args)], [result], 0, session, [SCOPE])


def test_month_day_tail_leaves_the_known_name_whole(ctx, monkeypatch):
    """Rule: the warehouse span survives a trailing month-day; the day stays the date reader's.

    Mutation: restoring the preserved scan break after the month abbreviation
    drops the day from the span, so the source is silently lost and this fails.
    """
    use_state(monkeypatch, items=[service_item()], warehouses=["Explicit Store"])
    text = "invoice Acme Corporation for 5 Consulting from Explicit Store on Nov. 5"
    assert [p.said for p in rv.warehouse_words(text, [service_item()], [CUSTOMER], ctx)] == ["Explicit Store"]
    assert ("warehouse", "Explicit Store") in [
        (o.kind, o.said) for o in rv.inventory(ctx, text, [service_item()])]
    with pytest.raises(Refusal) as error:
        check(text, ctx, [{"item": "Consulting", "quantity": 5}])
    assert error.value.code == "warehouse_ambiguous"
    assert error.value.message == "Consulting is a service item; it cannot come from a warehouse."
    got = rv.resolve_date("on Nov. 5", TODAY, rv.Session(), "date")
    assert (got.month, got.day) == (11, 5)


def test_at_end_of_month_tail_leaves_the_known_name_whole(ctx, monkeypatch):
    """Rule: the connector before a month-end reading is stepped over, so the tail is pure date.

    Mutation: removing the step-back over the connector keeps the tail inside
    the span, so the source is silently dropped and this fails.
    """
    use_state(monkeypatch, items=[service_item()], warehouses=["Explicit Store"])
    text = "invoice Acme Corporation for 5 Consulting from Explicit Store at end of month"
    assert [p.said for p in rv.warehouse_words(text, [service_item()], [CUSTOMER], ctx)] == ["Explicit Store"]
    assert ("warehouse", "Explicit Store") in [
        (o.kind, o.said) for o in rv.inventory(ctx, text, [service_item()])]
    with pytest.raises(Refusal) as error:
        check(text, ctx, [{"item": "Consulting", "quantity": 5}])
    assert error.value.code == "warehouse_ambiguous"
    assert error.value.message == "Consulting is a service item; it cannot come from a warehouse."
    assert isinstance(rv.resolve_date("at end of month", TODAY, rv.Session(), "date"), date)


def test_by_friday_tail_leaves_the_known_name_whole(ctx, monkeypatch):
    """Rule: the connector before a weekday reading is stepped over, so the tail is pure date.

    Mutation: removing the step-back over the connector keeps the tail inside
    the span, so the source is silently dropped and this fails.
    """
    use_state(monkeypatch, items=[service_item()], warehouses=["Explicit Store"])
    text = "invoice Acme Corporation for 5 Consulting from Explicit Store by Friday"
    assert [p.said for p in rv.warehouse_words(text, [service_item()], [CUSTOMER], ctx)] == ["Explicit Store"]
    assert ("warehouse", "Explicit Store") in [
        (o.kind, o.said) for o in rv.inventory(ctx, text, [service_item()])]
    with pytest.raises(Refusal) as error:
        check(text, ctx, [{"item": "Consulting", "quantity": 5}])
    assert error.value.code == "warehouse_ambiguous"
    assert error.value.message == "Consulting is a service item; it cannot come from a warehouse."
    assert rv.resolve_date("by Friday", TODAY, rv.Session(), "date").weekday() == 4


TAILS = ["tonight", "tomorrow", "yesterday", "on 5 Nov", "next Friday", "this Friday", "Friday", "today"]


@pytest.mark.parametrize("tail", TAILS)
def test_preserved_tails_keep_the_known_name_for_stock(ctx, monkeypatch, tail):
    """Rule: the seven previously reported tails plus today stay outside a matched name.

    Mutation: absorbing the tail into the warehouse words, or truncating the
    matched name to a shorter prefix, fails.
    """
    use_state(monkeypatch, items=[stock_item()], warehouses=["Explicit Store"])
    text = f"invoice Acme Corporation for 5 Widgets from Explicit Store {tail}"
    assert [p.said for p in rv.warehouse_words(text, [stock_item()], [CUSTOMER], ctx)] == ["Explicit Store"]
    result, session = check(text, ctx, [{"item": "Widgets", "quantity": 5}])
    assert result.args["items"] == [{"item": "Widgets", "quantity": "5", "warehouse": "Explicit Store"}]
    settle(text, ctx, result, session)
    relative = {"tonight": TODAY, "tomorrow": TODAY + timedelta(days=1),
                "yesterday": TODAY - timedelta(days=1), "today": TODAY}
    got = rv.resolve_date(tail, TODAY, rv.Session(), "date")
    if tail in relative:
        assert got == relative[tail]
    elif "Nov" in tail:
        assert (got.month, got.day) == (11, 5)
    else:
        assert got.weekday() == 4


@pytest.mark.parametrize("tail", TAILS)
def test_preserved_tails_refuse_service_with_sentence(ctx, monkeypatch, tail):
    """Rule: a kept service source asks with the service sentence, never a silent drop.

    Mutation: keeping the tail inside the span drops the source silently, so no
    question is raised and this fails.
    """
    use_state(monkeypatch, items=[service_item()], warehouses=["Explicit Store"])
    text = f"invoice Acme Corporation for 5 Consulting from Explicit Store {tail}"
    assert [p.said for p in rv.warehouse_words(text, [service_item()], [CUSTOMER], ctx)] == ["Explicit Store"]
    with pytest.raises(Refusal) as error:
        check(text, ctx, [{"item": "Consulting", "quantity": 5}])
    assert error.value.code == "warehouse_ambiguous"
    assert error.value.message == "Consulting is a service item; it cannot come from a warehouse."


def test_calendar_word_whole_name_with_shorter_competitor(ctx, monkeypatch):
    """Rule: the longest known name wins whole even when it holds a calendar word.

    Mutation: matching the shortest prefix turns the longer name with a
    date-covered leftover into the shorter one for the bare longer name.
    With tonight, the unchanged coverage helper reconstructs the whole name.
    """
    names = ["Monday Store", "Monday", "Harbor Friday", "Harbor"]
    use_state(monkeypatch, items=[stock_item()], warehouses=names)
    cases = [("invoice Acme Corporation for 5 Widgets from Monday Store", "Monday Store"),
             ("invoice Acme Corporation for 5 Widgets from Monday Store tonight", "Monday Store"),
             ("invoice Acme Corporation for 5 Widgets from Monday tonight", "Monday"),
             ("invoice Acme Corporation for 5 Widgets from Harbor Friday", "Harbor Friday"),
             ("invoice Acme Corporation for 5 Widgets from Harbor Friday tonight", "Harbor Friday")]
    for text, want in cases:
        assert [p.said for p in rv.warehouse_words(text, [stock_item()], [CUSTOMER], ctx)] == [want]
    for text, want in [(cases[1][0], cases[1][1]), (cases[4][0], cases[4][1])]:
        result, session = check(text, ctx, [{"item": "Widgets", "quantity": 5}])
        assert result.args["items"] == [{"item": "Widgets", "quantity": "5", "warehouse": want}]
        settle(text, ctx, result, session)


def test_second_calendar_word_whole_name_with_shorter_competitor(ctx, monkeypatch):
    """Rule: a second calendar-word name also matches whole against its single-word competitor.

    A shortest prefix reconstructs this whole name because Depot is not a
    date. The bare Harbor Friday case above discriminates longest matching.
    """
    use_state(monkeypatch, items=[stock_item()], warehouses=["Friday Depot", "Friday"])
    for text in ("invoice Acme Corporation for 5 Widgets from Friday Depot",
                 "invoice Acme Corporation for 5 Widgets from Friday Depot tonight"):
        assert [p.said for p in rv.warehouse_words(text, [stock_item()], [CUSTOMER], ctx)] == ["Friday Depot"]
    result, session = check("invoice Acme Corporation for 5 Widgets from Friday Depot tonight",
                            ctx, [{"item": "Widgets", "quantity": 5}])
    assert result.args["items"] == [{"item": "Widgets", "quantity": "5", "warehouse": "Friday Depot"}]
    settle("invoice Acme Corporation for 5 Widgets from Friday Depot tonight", ctx, result, session)


def test_ordinary_unknown_asks_stock_with_head_only(ctx, monkeypatch):
    """Rule: a stock unknown keeps the non-date head with the trailing date outside.

    Mutation: a fallback that absorbs the date word into the warehouse words,
    or that drops the phrase, fails.
    """
    use_state(monkeypatch, items=[stock_item()],
              warehouses=["Explicit Store", "Overflow", "Monday Store", "Friday Depot"])
    text = "invoice Acme Corporation for 5 Widgets from Nowhere Depot tonight"
    assert [p.said for p in rv.warehouse_words(text, [stock_item()], [CUSTOMER], ctx)] == ["Nowhere Depot"]
    session = rv.Session()
    with pytest.raises(Refusal) as error:
        check(text, ctx, [{"item": "Widgets", "quantity": 5}], session)
    assert error.value.code.startswith("warehouse_")
    assert session.pending["field"] == SCOPE + "items.0.warehouse"
    plain = "invoice Acme Corporation for 5 Widgets from Nowhere Depot"
    assert [p.said for p in rv.warehouse_words(plain, [stock_item()], [CUSTOMER], ctx)] == ["Nowhere Depot"]


def test_ordinary_unknown_ignored_for_service(ctx, monkeypatch):
    """Rule: an ordinary service source that names no warehouse is ignored, never asked.

    Mutation: emitting a phrase or asking for the ordinary unknown fails, since
    the span must stay ignored.
    """
    use_state(monkeypatch, items=[service_item()],
              warehouses=["Explicit Store", "Overflow", "Monday Store", "Friday Depot"])
    text = "invoice Acme Corporation for 5 Consulting from Nowhere Depot tonight"
    assert rv.warehouse_words(text, [service_item()], [CUSTOMER], ctx) == []
    result, _ = check(text, ctx, [{"item": "Consulting", "quantity": 5}])
    assert "warehouse" not in result.args["items"][0]


def test_pure_date_tail_emits_no_phrase_for_either_ownership(ctx, monkeypatch):
    """Rule: a tail that is pure date leaves no warehouse words for either ownership.

    Mutation: keeping the date word as a stock phrase fails, since the tail is
    wholly the date reader's.
    """
    use_state(monkeypatch, items=[stock_item()], warehouses=["Explicit Store"])
    assert rv.warehouse_words("invoice Acme Corporation for 5 Widgets from tonight",
                              [stock_item()], [CUSTOMER], ctx) == []
    result, _ = check("invoice Acme Corporation for 5 Widgets from tonight",
                      ctx, [{"item": "Widgets", "quantity": 5}])
    assert "warehouse" not in result.args["items"][0]
    use_state(monkeypatch, items=[service_item()], warehouses=["Explicit Store"])
    assert rv.warehouse_words("invoice Acme Corporation for 5 Consulting from tonight",
                              [service_item()], [CUSTOMER], ctx) == []
    result, _ = check("invoice Acme Corporation for 5 Consulting from tonight",
                      ctx, [{"item": "Consulting", "quantity": 5}])
    assert "warehouse" not in result.args["items"][0]


def test_shipment_source_binds_without_any_filter(ctx, monkeypatch):
    """Rule: a shipment source binds by longest match with no supplier filtering.

    Mutation: a filter overreach that drops a true warehouse, or a truncation
    to a shorter prefix, fails.
    """
    use_state(monkeypatch, items=[stock_item()], warehouses=["Overflow"])
    text = "invoice Acme Corporation for 5 Widgets from Overflow"
    assert [p.said for p in rv.warehouse_words(text, [stock_item()], [CUSTOMER], ctx)] == ["Overflow"]
    result, session = check(text, ctx, [{"item": "Widgets", "quantity": 5}])
    assert result.args["items"] == [{"item": "Widgets", "quantity": "5", "warehouse": "Overflow"}]
    settle(text, ctx, result, session)


def test_shipment_keeps_shared_spelling_warehouse(ctx, monkeypatch):
    """Rule: in a shipment the longest warehouse match stands even for a shared spelling.

    Mutation: applying the receipt-only filter to a shipment drops the
    warehouse to the default or question path, so this fails. The receipt side
    of the shared-spelling rule is pinned by the receipt tests.
    """
    use_state(monkeypatch, items=[stock_item()], warehouses=["Copperline", "Overflow"])
    text = "invoice Acme Corporation for 5 Widgets from Copperline"
    assert [p.said for p in rv.warehouse_words(text, [stock_item()], [CUSTOMER], ctx)] == ["Copperline"]
    result, session = check(text, ctx, [{"item": "Widgets", "quantity": 5}])
    assert result.args["items"] == [{"item": "Widgets", "quantity": "5", "warehouse": "Copperline"}]
    settle(text, ctx, result, session)


def test_stock_extended_unknown_keeps_non_date_head(ctx, monkeypatch):
    """Rule: a stock span past a known prefix keeps its non-date head for the unknown question.

    Mutation: truncating to the longest known prefix, or treating the whole
    tail as pure date because the date reader still reads it, fails.
    """
    use_state(monkeypatch, items=[stock_item()], warehouses=["Main Store"])
    text = "invoice Acme Corporation for 5 Widgets from Main Store annex tonight"
    assert [p.said for p in rv.warehouse_words(text, [stock_item()], [CUSTOMER], ctx)] == ["Main Store annex"]
    session = rv.Session()
    with pytest.raises(Refusal) as error:
        check(text, ctx, [{"item": "Widgets", "quantity": 5}], session)
    assert error.value.code.startswith("warehouse_")
    assert session.pending["field"] == SCOPE + "items.0.warehouse"
    assert rv.resolve_date("tonight", TODAY, rv.Session(), "date") == TODAY


def test_service_extended_unknown_without_marker_ignored(ctx, monkeypatch):
    """Rule: a service extended span without the marker word stays ignored.

    Mutation: truncating to the known prefix and selecting or asking with it,
    or bypassing the marker guard to ask with the full head, fails.
    """
    use_state(monkeypatch, items=[service_item()], warehouses=["Main Store"])
    text = "invoice Acme Corporation for 5 Consulting from Main Store annex tonight"
    assert rv.warehouse_words(text, [service_item()], [CUSTOMER], ctx) == []
    result, _ = check(text, ctx, [{"item": "Consulting", "quantity": 5}])
    assert "warehouse" not in result.args["items"][0]


def test_empty_company_service_source_ignored(ctx, monkeypatch):
    """Rule: with no known warehouses a service source is ignored, never invented.

    Mutation: asking the unknown question for service with no warehouses fails.
    """
    use_state(monkeypatch, items=[service_item()], warehouses=[])
    text = "invoice Acme Corporation for 5 Consulting from Anywhere tonight"
    assert rv.warehouse_words(text, [service_item()], [CUSTOMER], ctx) == []
    result, _ = check(text, ctx, [{"item": "Consulting", "quantity": 5}])
    assert "warehouse" not in result.args["items"][0]


def test_empty_company_stock_unknown_asks_head_only(ctx, monkeypatch):
    """Rule: with no known warehouses a stock unknown still asks with the non-date head.

    Mutation: returning no phrase, inventing a known name, or keeping the
    full-tail span with the date word, fails.
    """
    use_state(monkeypatch, items=[stock_item()], warehouses=[])
    text = "invoice Acme Corporation for 5 Widgets from Anywhere tonight"
    assert [p.said for p in rv.warehouse_words(text, [stock_item()], [CUSTOMER], ctx)] == ["Anywhere"]
    session = rv.Session()
    with pytest.raises(Refusal) as error:
        check(text, ctx, [{"item": "Widgets", "quantity": 5}], session)
    assert error.value.code.startswith("warehouse_")
    assert session.pending["field"] == SCOPE + "items.0.warehouse"
    plain = "invoice Acme Corporation for 5 Widgets from Anywhere"
    assert [p.said for p in rv.warehouse_words(plain, [stock_item()], [CUSTOMER], ctx)] == ["Anywhere"]


def test_explicit_marker_service_asks_with_full_head(ctx, monkeypatch):
    """Rule: with the marker word present the full non-date head is kept and asked.

    Mutation: ignoring the span through the ordinary service path, or
    truncating it to the known prefix, fails.
    """
    use_state(monkeypatch, items=[service_item()], warehouses=["Main Store"])
    text = "invoice Acme Corporation for 5 Consulting from Main Store annex warehouse tonight"
    assert [p.said for p in rv.warehouse_words(text, [service_item()], [CUSTOMER], ctx)] == [
        "Main Store annex warehouse"]
    with pytest.raises(Refusal) as error:
        check(text, ctx, [{"item": "Consulting", "quantity": 5}])
    assert error.value.code == "warehouse_ambiguous"
    assert error.value.message == "Consulting is a service item; it cannot come from a warehouse."


def test_names_match_normalised_with_optional_the(ctx, monkeypatch):
    """Rule: matching is word-by-word on the normalised stream on both sides.

    Mutation: comparing raw strings instead of normalised words, or missing
    the optional leading article, fails to bind.
    """
    use_state(monkeypatch, items=[stock_item()], warehouses=["Explicit Store"])
    for text in ("invoice Acme Corporation for 5 Widgets from explicit store tonight",
                 "invoice Acme Corporation for 5 Widgets from the Explicit Store tonight"):
        result, session = check(text, ctx, [{"item": "Widgets", "quantity": 5}])
        assert result.args["items"] == [{"item": "Widgets", "quantity": "5", "warehouse": "Explicit Store"}]
        settle(text, ctx, result, session)


def test_disabled_stock_plugin_reads_as_no_known_warehouses(ctx, monkeypatch):
    """Rule: when the stock plugin is disabled the known set is empty, not an error.

    Mutation: letting the disabled-plugin refusal escape, or inventing names
    from the date vocabulary instead, fails.
    """
    def no_plugin(*args, **kwargs):
        raise Refusal("stock is not enabled", "plugin_not_enabled")

    for _module in (rv, resolve_args, resolve_policy, resolve_warehouse):
        monkeypatch.setattr(_module, 'call_service', no_plugin)
    assert rv._company_warehouse_names(ctx) == set()


def test_unrelated_plugin_refusal_still_raises(ctx, monkeypatch):
    """Rule: only the disabled-plugin refusal is absorbed; any other refusal propagates.

    Mutation: swallowing every refusal as an empty set hides real failures, so
    this fails.
    """
    def broken(*args, **kwargs):
        raise Refusal("backend unavailable", "something_else")

    for _module in (rv, resolve_args, resolve_policy, resolve_warehouse):
        monkeypatch.setattr(_module, 'call_service', broken)
    with pytest.raises(Refusal):
        rv._company_warehouse_names(ctx)


@pytest.mark.parametrize("said", ["Explicit Store", "explicit store", "EXPLICIT STORE"])
def test_case_only_known_name_tie_asks_one_detail(ctx, monkeypatch, said):
    use_state(monkeypatch, items=[stock_item()], warehouses=["Explicit Store", "explicit store"])
    text = f"invoice Acme Corporation for 5 Widgets from {said} tonight"
    session = rv.Session()
    with pytest.raises(Refusal) as error:
        check(text, ctx, [{"item": "Widgets", "quantity": 5}], session)
    assert error.value.code == "warehouse_ambiguous"
    assert "one detail" in error.value.message.lower()
    assert session.pending["field"] == SCOPE + "items.0.warehouse"
    assert [c["ref"] for c in session.pending["choices"]] == ["Explicit Store", "explicit store"]
    assert session.answer(session.pending["choices"][1]["code"])
    result, _ = check(text, ctx, [{"item": "Widgets", "quantity": 5}], session)
    assert result.args["items"][0]["warehouse"] == "explicit store"
    settle(text, ctx, result, session)


@pytest.mark.parametrize("source", ["Nowhere Depot", "Explicit Store from explicit store"])
def test_answered_case_only_warehouse_keeps_selected_identity(ctx, monkeypatch, source):
    names = ["Explicit Store", "explicit store"]
    use_state(monkeypatch, items=[stock_item()], warehouses=names)
    # Distinct warehouse identities even though the name reader folds their case together.
    warehouses = [{"warehouse": "first-store", "name": names[0], "qty": "20"},
                  {"warehouse": "second-store", "name": names[1], "qty": "30"}]
    monkeypatch.setattr(stock, "holdings", lambda c, item: warehouses)
    text = f"invoice Acme Corporation for 5 Widgets from {source}"
    session = rv.Session()
    with pytest.raises(Refusal) as error:
        check(text, ctx, [{"item": "Widgets", "quantity": 5}], session)
    assert error.value.code.startswith("warehouse_")
    assert session.pending["field"] == SCOPE + "items.0.warehouse"
    selected = warehouses[1]
    choice = next(c for c in session.pending["choices"] if c["ref"] == selected["name"])
    assert session.answer(choice["code"])
    assert session.bindings[SCOPE + "items.0.warehouse"] == selected["name"]
    result, _ = check(text, ctx, [{"item": "Widgets", "quantity": 5}], session)
    bound = result.args["items"][0]["warehouse"]
    assert bound == selected["name"]
    assert next(w["warehouse"] for w in warehouses if w["name"] == bound) == selected["warehouse"]


def test_multiple_case_only_sources_still_ask_one_detail(ctx, monkeypatch):
    use_state(monkeypatch, items=[stock_item()], warehouses=["Explicit Store", "explicit store"])
    text = "invoice Acme Corporation for 5 Widgets from Explicit Store from explicit store"
    session = rv.Session()
    with pytest.raises(Refusal) as error:
        check(text, ctx, [{"item": "Widgets", "quantity": 5}], session)
    assert error.value.code == "warehouse_ambiguous"
    assert "one detail" in error.value.message.lower()
    assert session.pending["field"] == SCOPE + "items.0.warehouse"
    assert [c["ref"] for c in session.pending["choices"]] == ["Explicit Store", "explicit store"]


def test_typed_case_only_warehouse_tie_still_asks_one_detail(ctx, monkeypatch):
    names = ["Explicit Store", "explicit Store"]
    use_state(monkeypatch, items=[stock_item()], warehouses=names)
    text = "invoice Acme Corporation for 5 Widgets from Explicit Store"
    session = rv.Session()
    with pytest.raises(Refusal) as error:
        check(text, ctx, [{"item": "Widgets", "quantity": 5}], session)
    assert error.value.code == "warehouse_ambiguous"
    assert session.pending["field"] == SCOPE + "items.0.warehouse"
    assert [c["ref"] for c in session.pending["choices"]] == names
    question_id = session.pending["id"]

    answer = "explicit store"
    assert rv.match_answer(session.pending, answer) is None
    assert session.bindings == {}
    answered_text = rv.request_words(ctx, [text, answer], [
        (CONTRACT, {"customer": "Acme Corporation", "items": [{"item": "Widgets", "quantity": 5}]})])
    with pytest.raises(Refusal) as error:
        check(answered_text, ctx, [{"item": "Widgets", "quantity": 5}], session)
    assert error.value.code == "warehouse_ambiguous"
    assert "one detail" in error.value.message.lower()
    assert session.pending["id"] != question_id
    assert session.pending["field"] == SCOPE + "items.0.warehouse"
    assert [c["ref"] for c in session.pending["choices"]] == names
    assert session.bindings == {}


def test_typed_unique_warehouse_name_binds_pending_question(ctx, monkeypatch):
    use_state(monkeypatch, items=[stock_item()], warehouses=["Explicit Store", "Overflow"])
    text = "invoice Acme Corporation for 5 Widgets from Nowhere Depot"
    session = rv.Session()
    with pytest.raises(Refusal) as error:
        check(text, ctx, [{"item": "Widgets", "quantity": 5}], session)
    assert error.value.code == "warehouse_unknown"
    assert session.pending["field"] == SCOPE + "items.0.warehouse"

    answer = "explicit store"
    choice = next(c for c in session.pending["choices"] if c["ref"] == "Explicit Store")
    matched = rv.match_answer(session.pending, answer)
    assert matched == ("choice", choice["code"])
    assert session.answer(matched[1])
    assert session.bindings == {SCOPE + "items.0.warehouse": "Explicit Store"}
    assert session.pending is None
    answered_text = rv.request_words(ctx, [text, answer], [
        (CONTRACT, {"customer": "Acme Corporation", "items": [{"item": "Widgets", "quantity": 5}]})])
    result, _ = check(answered_text, ctx, [{"item": "Widgets", "quantity": 5}], session)
    assert result.args["items"][0]["warehouse"] == "Explicit Store"
    settle(answered_text, ctx, result, session)


@pytest.mark.parametrize("connector", ["in", "near", "annex", "Store", "Depot"])
def test_other_words_are_never_date_connectors(ctx, monkeypatch, connector):
    use_state(monkeypatch, items=[stock_item()], warehouses=["Explicit Store"])
    text = f"invoice Acme Corporation for 5 Widgets from Explicit Store {connector} tonight"
    assert [p.said for p in rv.warehouse_words(text, [stock_item()], [CUSTOMER], ctx)] == [
        f"Explicit Store {connector}"]
    with pytest.raises(Refusal) as error:
        check(text, ctx, [{"item": "Widgets", "quantity": 5}])
    assert error.value.code.startswith("warehouse_")


@pytest.mark.parametrize("middle,want", [
    ("Nov. 5", "Store Nov 5 Annex"),
    ("Nov. 15", "Store Nov 15 Annex"),
    ("Nov; 5", "Store Nov"),
    ("Nov! 5", "Store Nov"),
    ("Nov . 5", "Store Nov"),
    ("Label. 5", "Store Label"),
    ("Nov. 555", "Store Nov"),
    ("Nov. five", "Store Nov"),
    ("Nov. 5th", "Store Nov"),
])
def test_month_period_scan_exception_requires_all_three_conditions(ctx, monkeypatch, middle, want):
    full_name = _norm(f"Store {middle} Annex")
    use_state(monkeypatch, items=[stock_item()], warehouses=[full_name])
    text = f"invoice Acme Corporation for 5 Widgets from Store {middle} Annex"
    assert [_norm(p.said) for p in rv.warehouse_words(text, [stock_item()], [CUSTOMER], ctx)] == [_norm(want)]


@pytest.mark.parametrize("kind", ["customer", "item", "money_span"])
def test_other_name_readers_keep_the_break_after_month_period(ctx, monkeypatch, kind):
    use_state(monkeypatch, items=[], warehouses=[])
    text = "invoice Tax Nov. 5 Consulting"
    original = rv.bounded_names
    seen = []

    def observe(ws, names, **kwargs):
        if "Tax Nov" in names and "breaks" in kwargs:
            seen.append(kwargs.get("breaks", frozenset()))
        return original(ws, names, **kwargs)

    for _module in (rv, resolve_amounts, resolve_args, resolve_inventory, resolve_payment, resolve_policy, resolve_records, resolve_warehouse, resolve_words):
        monkeypatch.setattr(_module, 'bounded_names', observe)
    rows = [{**CUSTOMER, "name": "Tax Nov"}]
    if kind == "money_span":
        session = rv.Session(owner_messages=[{"id": "message", "text": text, "spans": []}])
        rv.record_message_names(ctx, session, rows, [], "Tax Nov", [])
        assert session.owner_messages[0]["spans"][0]["area"] == "tax"
    else:
        assert rv.resolve_record(rows, kind, text, "Tax Nov", rv.Session(), kind,
                                 item=kind == "item") == "Tax Nov"
    assert seen and all(2 in breaks for breaks in seen)
    assert rv.punct_after(text) == frozenset({2})


def test_proposed_warehouse_fixtures_are_valid_company_names(engine):
    from erpclaw_core.common import company_ctx
    from erpclaw_core.masters import create_party
    from erpclaw_stock import service

    names = ["Monday", "Friday", "Main Store", "Harbor Friday", "Harbor", "Copperline",
             "Anywhere", "Main Store annex warehouse"]
    with engine.begin() as conn:
        company = company_ctx(conn)
        for name in names:
            service.create_warehouse(company, name)
        create_party(company, "supplier", "Copperline")
        assert rv._company_warehouse_names(company) == {_norm(n) for n in names}
        assert "receipt_destination_phrases" in vars(rv)


def test_service_known_name_tie_asks_before_service_omission(ctx, monkeypatch):
    use_state(monkeypatch, items=[service_item()], warehouses=["Explicit Store", "explicit store"])
    text = "invoice Acme Corporation for 5 Consulting from Explicit Store tonight"
    session = rv.Session()
    with pytest.raises(Refusal) as error:
        check(text, ctx, [{"item": "Consulting", "quantity": 5}], session)
    assert error.value.code == "warehouse_ambiguous"
    assert "one detail" in error.value.message.lower()
    assert session.answer(session.pending["choices"][0]["code"])
    with pytest.raises(Refusal) as error:
        check(text, ctx, [{"item": "Consulting", "quantity": 5}], session)
    assert error.value.message == "Consulting is a service item; it cannot come from a warehouse."
    assert session.answer(session.pending["choices"][0]["code"])
    result, _ = check(text, ctx, [{"item": "Consulting", "quantity": 5}], session)
    assert "warehouse" not in result.args["items"][0]
