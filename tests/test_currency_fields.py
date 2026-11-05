"""A currency on every amount that crosses: each card names every money figure its strings show, with its currency;
results, read answers and events carry `{value, currency}`; a card whose amounts disagree with its strings is never
shown. The owner-facing strings are byte-identical to before: `fixtures/card_strings_before_amounts.json` holds them
as the engine showed them before amounts existed, today pinned to 15 Oct 2026."""
from __future__ import annotations

import ast
import inspect
import json
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

import test_card_values as cv
from erpclaw_api.app import create_app
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import approvals, contracts as ct, core_contracts, metrics, models as m
from erpclaw_core.common import Refusal, company_ctx
from erpclaw_core.contracts import AMOUNT_WHAT, Amount, Result, Shown, moves_money
from erpclaw_core.plugins import REGISTRY

BEFORE = json.loads((Path(__file__).parent / "fixtures" / "card_strings_before_amounts.json").read_text())
TODAY = "2026-10-15"
A, P = "amount", "unit_price"
WRITES = sorted(cv.SCENARIOS)


@pytest.fixture
def books(monkeypatch, request):
    monkeypatch.setenv("ERPCLAW_TODAY", TODAY)  # before the books are seeded: every date a card shows is fixed
    return request.getfixturevalue("engine")


def _commit(books, contract: str):
    """The scenario's card (test_card_values' awkward values), approved and committed: the card and the outcome."""
    cv._awkward_rates(books)
    if contract == "draft_invoice":
        return cv._commit(books, "create_invoice", {"customer": "Acme Corporation", **cv.LINES, "submit": False})
    return cv._commit(books, contract, cv.SCENARIOS[contract](books))


def _strings(card: dict) -> list[tuple[str, int, str, str]]:
    """Every string of a card as (where, index, label, text)."""
    out = [("title", 0, "", card["title"])]
    out += [("lines", i, k, v) for i, (k, v) in enumerate(card["lines"])]
    out += [(w, i, "", s) for w in ("books", "warnings") for i, s in enumerate(card[w])]
    return out + [("footer", 0, "", card["footer"])]


def _by_string(card: dict) -> dict[tuple[str, str, str], list[tuple[str, str, str]]]:
    """The amounts attached to each string, keyed by the string itself (an invoice's lines come in id order, which
    is random), each as (kind, value, currency), in order."""
    out = {}
    for where, i, label, text in _strings(card):
        got = [(a["kind"], a["value"], a["currency"]) for a in card["amounts"] if (a["where"], a["index"]) == (where, i)
               and a["label"] == label]
        if got:
            out[(where, label, text)] = got
    return out


def _same_strings(card: dict, before: dict) -> None:
    """Byte-identical strings: the Item rows as a set (their order is the lines' random id order), all else in order."""
    for k in ("title", "books", "warnings", "footer"):
        assert card[k] == before[k], k
    assert [r for r in card["lines"] if r[0] != "Item"] == [r for r in before["lines"] if r[0] != "Item"]
    assert sorted(r for r in card["lines"] if r[0] == "Item") == sorted(r for r in before["lines"] if r[0] == "Item")


# Every money figure each card shows, per string, worked out by hand from the strings in BEFORE: the quantities are
# not amounts, an item's price is a unit price as stored, everything else is an amount at the currency's scale.
COPPER = ("lines", "Item", "1000.5 x Copper Wire @ 0.1234 = 123.46")
BOARD = ("lines", "Item", "2.25 x Circuit Board @ 45.0075 = 101.27")
ITEMS = {COPPER: [(P, "0.1234", "USD"), (A, "123.46", "USD")], BOARD: [(P, "45.0075", "USD"), (A, "101.27", "USD")],
         ("lines", "Total", "224.73 USD"): [(A, "224.73", "USD")]}
POSTED = {("books", "", "Accounts Receivable (Acme Corporation) +224.73"): [(A, "224.73", "USD")],
          ("books", "", "Sales +224.73"): [(A, "224.73", "USD")]}
EXPECTED = {
    "create_invoice": {**ITEMS, **POSTED},
    "draft_invoice": ITEMS,
    "submit_invoice": {**ITEMS, **POSTED},
    "cancel_invoice": {
        ("lines", "Total", "224.73 USD"): [(A, "224.73", "USD")],
        ("books", "", "Accounts Receivable (Acme Corporation) -224.73"): [(A, "224.73", "USD")],
        ("books", "", "Sales (4100 Sales) +224.73"): [(A, "224.73", "USD")]},
    "record_payment": {
        ("lines", "Amount received", "300.25 USD"): [(A, "300.25", "USD")],
        ("lines", "Applied to", "INV-2026-00009 (224.73 of 224.73 outstanding), now paid"):
            [(A, "224.73", "USD"), (A, "224.73", "USD")],
        ("lines", "Credit left", "75.52 USD"): [(A, "75.52", "USD")],
        ("books", "", "Bank (1100 Bank) +300.25"): [(A, "300.25", "USD")],
        ("books", "", "Accounts Receivable (Acme Corporation) -300.25"): [(A, "300.25", "USD")],
        ("warnings", "", "300.25 USD is more than Acme Corporation owes on open invoices; 75.52 USD stays as credit in "
                         "Acme Corporation's favour."): [(A, "300.25", "USD"), (A, "75.52", "USD")],
        ("warnings", "", "Acme Corporation already has a 300.25 USD payment on 13 Oct 2026 (PAY-2026-00003). Is this a "
                         "second payment?"): [(A, "300.25", "USD")]},
    "reverse_payment": {
        ("lines", "Amount received", "50.25 USD"): [(A, "50.25", "USD")],
        ("lines", "Owes again", "INV-2026-00009 (50.25 of 224.73 outstanding)"): [(A, "50.25", "USD"),
                                                                                 (A, "224.73", "USD")],
        ("books", "", "Bank (1100 Bank) -50.25"): [(A, "50.25", "USD")],
        ("books", "", "Accounts Receivable (Acme Corporation) +50.25"): [(A, "50.25", "USD")]},
    # Made after amounts existed (no strings from before): the same lines, and stock's costs at the currency's scale.
    # Stock received at 155.40 for 2000 Copper Wire and 123.46 for 10 Circuit Boards: 1000.5 of 2000 cost 77.74
    # (77.73885), 2.25 of 10 cost 27.78 (27.7785), 105.52 together; a full credit returns each at that cost.
    "create_sales_order": ITEMS,
    "deliver_order": {
        ("lines", "Cost", "Cost of goods sold 77.74 USD"): [(A, "77.74", "USD")],
        ("lines", "Cost", "Cost of goods sold 27.78 USD"): [(A, "27.78", "USD")],
        ("books", "", "Cost of Goods Sold (5100) +105.52"): [(A, "105.52", "USD")],
        ("books", "", "Inventory (1300) -105.52"): [(A, "105.52", "USD")]},
    "create_credit_note": {
        ("lines", "Invoice", "INV-2026-00009: submitted, 224.73 outstanding, nothing paid"): [(A, "224.73", "USD")],
        ("lines", "Credit", "line 1: 1000.5 x Copper Wire @ 0.1234 = 123.46"): [(P, "0.1234", "USD"),
                                                                              (A, "123.46", "USD")],
        ("lines", "Back into stock", "1000.5 into Main Store against INV-2026-00009 at 77.74"): [(A, "77.74", "USD")],
        ("lines", "Credit", "line 2: 2.25 x Circuit Board @ 45.0075 = 101.27"): [(P, "45.0075", "USD"),
                                                                               (A, "101.27", "USD")],
        ("lines", "Back into stock", "2.25 into Main Store against INV-2026-00009 at 27.78"): [(A, "27.78", "USD")],
        ("lines", "Total credit", "224.73 USD"): [(A, "224.73", "USD")],
        ("lines", "Outstanding after", "0.00 USD"): [(A, "0.00", "USD")],
        ("books", "", "Sales Returns +224.73"): [(A, "224.73", "USD")],
        ("books", "", "Accounts Receivable (Acme Corporation) -224.73"): [(A, "224.73", "USD")],
        ("books", "", "Inventory (1300) +105.52"): [(A, "105.52", "USD")],
        ("books", "", "Cost of Goods Sold (5100) -105.52"): [(A, "105.52", "USD")]},
}


# ---- every write contract's card: strings unchanged, every money figure named ----------------------------------

def test_every_write_contract_is_covered():
    assert set(WRITES) == {c.name for c in REGISTRY.contracts.values() if c.effect != "read" and moves_money(c)}
    assert set(EXPECTED) == {*WRITES, "draft_invoice"}


@pytest.mark.parametrize("contract", [*WRITES, "draft_invoice"])
def test_a_card_keeps_its_strings_and_names_every_money_figure_in_them(books, contract):
    card, out = _commit(books, contract)
    before = BEFORE.get(contract)  # a contract made after amounts existed has no strings from before them
    if before is not None:
        _same_strings(card.card, before["card"])
    assert card.card["currency"] == "USD"
    assert _by_string(card.card) == EXPECTED[contract], _by_string(card.card)
    if before is not None:
        assert out.text == before["text"] and approvals.read_back(books, out.operation_key).text == before["read_back"]
        assert out.figures == before["figures"]


def test_a_card_s_line_amounts_count_the_company_line_first(books):
    card, _ = _commit(books, "create_invoice")
    lines = card.card["lines"]
    assert lines[0][0] == "Company"
    for a in card.card["amounts"]:
        if a["where"] == "lines":
            assert lines[a["index"]][0] == a["label"] != "Company"


def test_the_read_answers_keep_their_text(books):
    """The invoice list and a metric, as BEFORE shows them: the same text, and the same figures for the number check."""
    cv._awkward_rates(books)
    cv._invoice_number(books, submit=True)
    with books.connect() as conn:
        c = REGISTRY.contracts["get_metric"]
        r = c.run(company_ctx(conn), c.input_model.model_validate({"metric": "top_customers", "period": "this year"}))
    assert (r.text, r.figures) == (BEFORE["get_metric"]["text"], BEFORE["get_metric"]["figures"])
    cv._invoice_number(books, submit=False)
    with books.connect() as conn:
        c = REGISTRY.contracts["list_invoices"]
        r = c.run(company_ctx(conn), c.input_model.model_validate({"status": "all"}))
    assert (r.text, r.figures) == (BEFORE["list_invoices"]["text"], BEFORE["list_invoices"]["figures"])


def test_without_the_recorder_s_attachment_a_books_line_would_lose_its_amounts(books, monkeypatch):
    """Mutation: the recorder attaches nothing to a books line. The forward check cannot see a missing amount (an
    empty list passes it), so this is caught by the test above, by construction."""
    real = Shown._attach

    def skip_books(self, where, index, label=""):
        if where == "books":
            self._pending = []
            return
        real(self, where, index, label)

    monkeypatch.setattr(Shown, "_attach", skip_books)
    card, _ = _commit(books, "create_invoice")
    assert _by_string(card.card) != EXPECTED["create_invoice"]
    assert not [a for a in card.card["amounts"] if a["where"] == "books"]


def test_a_figure_recorded_and_never_shown_is_a_programming_error_not_a_card():
    sh = Shown("USD")
    sh.amount(Decimal("1"))
    with pytest.raises(RuntimeError):
        sh.card()


# ---- quantities are never amounts; a four-place price is a unit price; every currency ------------------------

FRACTIONAL = {  # 0.25 and 0.125 of an item at a four-place price, worked out by hand at each currency's scale
    "USD": ("1.2345", "0.31", "0.15", "0.46"),     # 0.308625, 0.1543125; total 0.46
    "KWD": ("1.2345", "0.309", "0.154", "0.463"),  # three places
    "JPY": ("150.1234", "38", "19", "57"),         # 37.53085, 18.765425; no places
}


@pytest.fixture
def foreign_books(request):
    from conftest import drop_everything, empty_books
    from erpclaw_core.masters import create_item, create_party
    from erpclaw_core.setup import create_company

    currency = request.param
    eng = empty_books()
    create_company(eng, f"{currency} Trading", currency)
    with eng.begin() as conn:
        ctx = company_ctx(conn)
        create_party(ctx, "customer", "Acme Corporation", "acme@example.com", "Springfield")
        create_item(ctx, "Copper Wire", Decimal(FRACTIONAL[currency][0]))
    yield eng, currency
    drop_everything(eng)
    eng.dispose()


@pytest.mark.parametrize("foreign_books", sorted(FRACTIONAL), indirect=True)
def test_fractional_quantities_carry_no_amount_and_a_four_place_price_is_a_unit_price(foreign_books):
    eng, cur = foreign_books
    price, a1, a2, total = FRACTIONAL[cur]
    card = approvals.propose(eng, REGISTRY.contracts["create_invoice"], {
        "customer": "Acme Corporation", "items": [{"item": "copper wire", "quantity": "0.25"},
                                                  {"item": "copper wire", "quantity": "0.125"}]}).card
    assert [ln[1].split(" x ")[0] for ln in card["lines"] if ln[0] == "Item"] == ["0.25", "0.125"]
    assert card["currency"] == cur
    assert [(a["where"], a["index"], a["kind"], a["value"], a["currency"]) for a in card["amounts"]] == [
        ("lines", 2, P, price, cur), ("lines", 2, A, a1, cur), ("lines", 3, P, price, cur), ("lines", 3, A, a2, cur),
        ("lines", 4, A, total, cur), ("books", 0, A, total, cur), ("books", 1, A, total, cur)]


# ---- the forward check: a card whose amounts disagree with its strings is never stored or shown -------------

def _altered(alter):
    def wrap(preview):
        def altered_preview(ctx, inp):
            p = preview(ctx, inp)
            p.card.amounts = alter(list(p.card.amounts))
            return p
        return altered_preview
    return wrap


ALTERATIONS = {
    "a value changed": lambda am: [replace_value(am[-1], "224.74")] + am[:-1],
    "a value a part of another figure": lambda am: [replace_value(am[4], "24.73"), *am[:4], *am[5:]],
    "an index past the list": lambda am: [*am[:-1], replace_index(am[-1], 9)],
    "an index at the wrong string": lambda am: [replace_index(am[0], 3), *am[1:]],  # Total's line for an Item's
    "a unit price given as an amount": lambda am: [replace_kind(am[0], A), *am[1:]],
    "a label that is not the line's": lambda am: [replace_label(am[0], "Total"), *am[1:]],
}


def replace_value(a, v):
    from dataclasses import replace
    return replace(a, value=v)


def replace_index(a, i):
    from dataclasses import replace
    return replace(a, index=i)


def replace_kind(a, k):
    from dataclasses import replace
    return replace(a, kind=k)


def replace_label(a, label):
    from dataclasses import replace
    return replace(a, label=label)


@pytest.mark.parametrize("how", ALTERATIONS)
def test_a_card_whose_amounts_disagree_with_its_strings_is_refused_and_nothing_stored(books, monkeypatch, how):
    cv._awkward_rates(books)
    c = REGISTRY.contracts["create_invoice"]
    monkeypatch.setattr(c, "preview", _altered(ALTERATIONS[how])(c.preview))
    with books.connect() as conn:
        before = conn.execute(select(func.count()).select_from(m.approval)).scalar_one()
    with pytest.raises(Refusal) as e:
        approvals.propose(books, c, {"customer": "Acme Corporation", **cv.LINES})
    assert (e.value.code, e.value.message) == ("card_amounts",
                                               "That card's amounts do not match its lines; nothing was proposed.")
    with books.connect() as conn:
        assert conn.execute(select(func.count()).select_from(m.approval)).scalar_one() == before


def test_without_the_forward_check_an_altered_card_would_be_shown(books, monkeypatch):
    """Mutation: the forward check removed, a card with a changed amount is proposed (so the check is what refuses)."""
    cv._awkward_rates(books)
    c = REGISTRY.contracts["create_invoice"]
    monkeypatch.setattr(c, "preview", _altered(ALTERATIONS["a value changed"])(c.preview))
    monkeypatch.setattr(approvals, "check_card_amounts", lambda card: None)
    assert approvals.propose(books, c, {"customer": "Acme Corporation", **cv.LINES}).status == "card"


# ---- the static scan: money in the code that builds cards and results is formatted only through Shown --------

FORMATTERS = {"fmt", "fmt_rate"}


def _calls_formatter(tree: ast.AST) -> list[int]:
    return [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call) and (
        (isinstance(n.func, ast.Name) and n.func.id in FORMATTERS)
        or (isinstance(n.func, ast.Attribute) and n.func.attr in FORMATTERS))]


def _reachable(module, roots: list) -> dict[str, ast.FunctionDef]:
    """The module's own functions the roots call, the roots included, transitively (the helpers they call)."""
    tree = ast.parse(inspect.getsource(module))
    defs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    seen, todo = {}, [r.__name__ for r in roots if r is not None and r.__module__ == module.__name__]
    while todo:
        name = todo.pop()
        if name in seen or name not in defs:
            continue
        seen[name] = defs[name]
        todo += [n.id for n in ast.walk(defs[name]) if isinstance(n, ast.Name) and n.id in defs]
    return seen


def _scanned() -> dict[str, ast.FunctionDef]:
    """What builds a card or a result: each contract's preview, commit, read-back and run in the module that declares
    it, and what they call there; the metric answer's renderer. A question's words are not a card (they stay the
    owner's text, with the event's currency) and live with the plans that raise them."""
    import erpclaw_sales

    out = {}
    for module in (erpclaw_sales, core_contracts):
        roots = [f for c in REGISTRY.contracts.values()
                 for f in (c.preview, c.commit, c.find_by_operation_key, c.run)
                 if f is not None and f.__module__ == module.__name__]
        out |= {f"{module.__name__}.{k}": v for k, v in _reachable(module, roots).items()}
    out |= {f"metrics.{k}": v for k, v in _reachable(metrics, [metrics.render]).items()}
    return out


def test_money_in_the_code_that_builds_cards_and_results_is_formatted_only_through_the_recorder():
    scanned = _scanned()
    assert {"erpclaw_sales._money", "erpclaw_sales._books", "erpclaw_sales.preview_payment",
            "erpclaw_sales.list_invoices", "erpclaw_core.core_contracts.get_metric", "metrics._v",
            "metrics.render"} <= set(scanned)
    assert {k: _calls_formatter(v) for k, v in scanned.items() if _calls_formatter(v)} == {}


def test_the_scan_finds_a_formatter_planted_in_a_preview():
    planted = ast.parse('def preview(ctx, inp):\n    return Card("T", [("Total", f"{fmt(total, cur)} USD")], [])\n')
    assert _calls_formatter(planted) == [2]
    assert _calls_formatter(ast.parse("x = money.fmt_rate(r, c)")) == [1]
    assert _calls_formatter(ast.parse("x = sh.amount(v)")) == []


# ---- results and read answers ----------------------------------------------------------------------------------

def _result_amounts(out) -> list[tuple]:
    return [(a["what"], a["of"], a["value"], a["currency"]) for a in out.amounts]


def test_a_reversal_of_an_allocated_payment_names_what_each_invoice_owes_again(books):
    card, out = _commit(books, "reverse_payment")
    pay = card.card["lines"][1][1]
    [(what, of, value, cur), (what2, of2, value2, _)] = _result_amounts(out)
    assert (what, of, value, cur) == ("amount", pay, "50.25", "USD")
    assert (what2, value2) == ("reopened", "50.25") and of2.startswith("INV-2026-") and of2 in out.text


def test_a_reversal_of_an_unallocated_payment_names_the_credit_removed(books):
    cv._earlier_credit(books, "300.25")
    with books.connect() as conn:
        from erpclaw_sales import tables as t
        number = conn.execute(select(t.payment.c.number).order_by(t.payment.c.number.desc())).scalars().first()
    _, out = cv._commit(books, "reverse_payment", {"payment": number})
    assert _result_amounts(out) == [("amount", number, "300.25", "USD"), ("credit_removed", number, "300.25", "USD")]
    assert approvals.read_back(books, out.operation_key).amounts == out.amounts


@pytest.mark.parametrize("contract", WRITES)
def test_each_write_result_names_its_amounts_as_its_contract_declares(books, contract):
    names = {a["what"] for a in _commit(books, contract)[1].amounts}
    assert names and names <= set(REGISTRY.contracts[contract].amounts) <= set(AMOUNT_WHAT)


@pytest.mark.parametrize("what", ["credit", "nonsense"])  # declared by another contract; in no contract's list
def test_a_result_naming_an_amount_its_contract_does_not_declare_is_refused_and_nothing_committed(books, monkeypatch,
                                                                                                    what):
    c = REGISTRY.contracts["create_invoice"]
    real = c.commit

    def commit(ctx, inp, key):
        r = real(ctx, inp, key)
        r.amounts.append(Amount(what, None, "1.00", "USD"))
        return r

    monkeypatch.setattr(c, "commit", commit)
    from erpclaw_sales import tables as t
    with books.connect() as conn:
        before = conn.execute(select(func.count()).select_from(t.invoice)).scalar_one()
    card = approvals.propose(books, c, {"customer": "Acme Corporation", **cv.LINES})
    out = approvals.approve(books, card.approval_id, card.digest)
    assert (out.status, out.data["code"]) == ("refused", "amount_name")
    with books.connect() as conn:
        assert conn.execute(select(func.count()).select_from(t.invoice)).scalar_one() == before


def _client(books, *replies):
    c = TestClient(create_app(books, ScriptedModel(list(replies)), embedded_owner=True))
    return c, c.post("/v1/conversations").json()["id"]


def _say(c, cid, text):
    return c.post(f"/v1/conversations/{cid}/messages", json={"text": text}).json()["events"]


def test_list_invoices_names_each_invoice_s_total_and_outstanding_and_the_totals(books):
    cv._invoice_number(books, submit=True)
    with books.connect() as conn:
        c = REGISTRY.contracts["list_invoices"]
        r = c.run(company_ctx(conn), c.input_model.model_validate({"status": "open"}))
    rows = [ln.split() for ln in r.text.splitlines()[1:]]
    want = [("total", None), ("outstanding", None)]
    want += [(w, row[0]) for row in rows for w in ("invoice_total", "invoice_outstanding")]
    assert [(a.what, a.of) for a in r.amounts] == want and len(rows) >= 2
    assert all(a.currency == "USD" for a in r.amounts)
    assert ct.check_answer(c, r)  # each in its text, in order


def test_a_money_metric_names_its_value_and_each_row_and_a_count_metric_names_none(books):
    c = REGISTRY.contracts["get_metric"]
    with books.connect() as conn:
        top = c.run(company_ctx(conn), c.input_model.model_validate({"metric": "top_customers", "period": "FY 2025"}))
        count = c.run(company_ctx(conn), c.input_model.model_validate({"metric": "invoice_count", "period": "FY 2025"}))
    assert [a.what for a in top.amounts] == ["value", *["row"] * (len(top.amounts) - 1)] and len(top.amounts) > 2
    assert all(a.of and a.of in top.text for a in top.amounts[1:])
    assert ct.check_answer(c, top) and count.amounts == []


@pytest.mark.parametrize("what", ["total", "nonsense"])
def test_a_read_answer_naming_an_undeclared_amount_is_refused_and_not_shown(books, monkeypatch, what):
    c = REGISTRY.contracts["get_metric"]
    real = c.run

    def run(ctx, inp):
        r = real(ctx, inp)
        r.amounts.append(Amount(what, None, r.amounts[0].value, "USD"))
        return r

    monkeypatch.setattr(c, "run", run)
    client, cid = _client(books, Reply("", [call("get_metric", metric="receivables")]))
    ev = _say(client, cid, "who owes us money?")
    assert [(e["type"], e.get("code")) for e in ev] == [("refused", "amount_name")]


def test_a_read_answer_whose_amounts_disagree_with_its_text_is_refused_and_not_shown(books, monkeypatch):
    c = REGISTRY.contracts["get_metric"]
    real = c.run

    def run(ctx, inp):
        r = real(ctx, inp)
        r.amounts[0] = Amount("value", None, "1.23", "USD")
        return r

    monkeypatch.setattr(c, "run", run)
    client, cid = _client(books, Reply("", [call("get_metric", metric="receivables")]))
    assert [(e["type"], e.get("code")) for e in _say(client, cid, "who owes us money?")] == [
        ("refused", "answer_amounts")]


# ---- events and the read-back -------------------------------------------------------------------------------

def test_every_event_names_the_books_currency_and_results_and_money_answers_carry_their_amounts(books):
    c, cid = _client(books, Reply("", [call("create_invoice", customer="Acme Corporation",
                                            items=[{"item": "copper wire", "quantity": 5}])]),
                     Reply("", [call("get_metric", metric="receivables")]),
                     Reply("Hello. What would you like to do?"),
                     Reply("", [call("create_invoice", customer="ABC", items=[{"item": "circuit board", "quantity": 3}])]))
    [card] = _say(c, cid, "invoice Acme Corporation for 5 copper wire")
    assert card["type"] == "card" and card["card"]["currency"] == "USD"
    assert [a["value"] for a in card["card"]["amounts"]] == ["28.00", "140.00", "140.00", "140.00", "140.00"]
    [result] = c.post(f"/v1/cards/{card['approval_id']}/approve", json={"digest": card["digest"]}).json()["events"]
    assert result["type"] == "result" and result["currency"] == "USD"
    assert [(a["what"], a["value"], a["currency"]) for a in result["amounts"]] == [("total", "140.00", "USD")]
    assert result["amounts"][0]["of"] in result["text"]
    [answer] = _say(c, cid, "who owes us money?")
    assert answer["type"] == "answer" and answer["currency"] == "USD" and answer["amounts"]
    assert answer["amounts"][0]["what"] == "value"
    [said] = _say(c, cid, "hello")
    assert said["type"] == "answer" and said["currency"] == "USD" and "amounts" not in said
    [ask] = _say(c, cid, "invoice ABC for 3 circuit boards")
    assert ask["type"] == "ask" and ask["currency"] == "USD"
    from erpclaw_assistant import store

    store.claim(cid, "an earlier message", turn_id="another-turn")  # a live turn holds the conversation: busy
    [refused] = c.post(f"/v1/conversations/{cid}/messages", json={"text": "x"}).json()["events"]
    assert refused["type"] == "refused" and refused["currency"] == "USD"
    rb = c.get(f"/v1/operations/{result['operation_key']}").json()
    assert (rb["currency"], rb["amounts"]) == ("USD", result["amounts"])


def test_an_outcome_stored_before_amounts_existed_reads_back_with_none(books):
    card, out = _commit(books, "create_invoice")
    with books.begin() as conn:
        row = conn.execute(select(m.operation_outcome).where(m.operation_outcome.c.key == out.operation_key)).mappings().one()
        stored = json.loads(row["result_json"])
        stored.pop("amounts")
        conn.execute(update(m.operation_outcome).where(m.operation_outcome.c.key == out.operation_key)
                     .values(result_json=approvals.canonical(stored)))
    rb = approvals.read_back(books, out.operation_key)
    assert (rb.status, rb.text, rb.amounts) == ("committed", out.text, [])
    c, _ = _client(books)
    assert c.get(f"/v1/operations/{out.operation_key}").json()["amounts"] == []


def test_a_card_proposed_before_amounts_existed_is_superseded_by_a_fresh_card_and_nothing_committed(books):
    cv._awkward_rates(books)
    c = REGISTRY.contracts["create_invoice"]
    card = approvals.propose(books, c, {"customer": "Acme Corporation", **cv.LINES})
    with books.begin() as conn:  # the card as it was stored before: no currency, no amounts, its digest over that
        a = dict(conn.execute(select(m.approval).where(m.approval.c.id == card.approval_id)).mappings().one())
        old = json.loads(a["card_json"])
        old.pop("currency"), old.pop("amounts")
        rs, args = json.loads(a["read_set_json"]), json.loads(a["args_json"])
        d = approvals.digest(approvals._fields(a, args, rs, old))
        conn.execute(update(m.approval).where(m.approval.c.id == card.approval_id)
                     .values(card_json=approvals.canonical(old), digest=d))
    from erpclaw_sales import tables as t
    with books.connect() as conn:
        before = conn.execute(select(func.count()).select_from(t.invoice)).scalar_one()
    out = approvals.approve(books, card.approval_id, d)
    assert out.status == "superseded" and out.card is not None and out.card["amounts"]
    with books.connect() as conn:
        assert conn.execute(select(func.count()).select_from(t.invoice)).scalar_one() == before
