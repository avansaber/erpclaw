"""The resolution step on the public honesty cases' own first turns, against their services-co books: what each
resolves to, a card, a confirm or a question, before any model is involved."""
from decimal import Decimal

import pytest

from conftest import drop_everything, empty_books
from erpclaw_assistant.resolve import Session, resolve_args
from erpclaw_cli.evals.seeds import services_co
from erpclaw_core.common import Refusal, company_ctx
from erpclaw_core.plugins import REGISTRY, load_plugins


@pytest.fixture
def services():
    load_plugins()
    eng = empty_books()
    services_co(eng)
    yield eng
    drop_everything(eng)
    eng.dispose()


def outcome(engine, args, said, session=None):
    with engine.connect() as conn:
        try:
            r = resolve_args(company_ctx(conn), REGISTRY.contracts["create_invoice"], args, said, session or Session())
        except Refusal as rf:
            return ("confirm" if rf.data.get("confirm") else "question"), rf.code, rf.options
    return "card", r.args


@pytest.mark.parametrize("case, said, args, customer", [
    ("h-016", "invoice acme for 3 hours of consulting at 150 an hour",
     {"customer": "acme", "items": [{"item": "consulting", "quantity": 3}]}, "Acme Corporation"),
    ("h-017", "bill Wayne for one training day",
     {"customer": "Wayne", "items": [{"item": "training day", "quantity": 1}]}, "Wayne Enterprises"),
    ("h-020", "Acme corp, two months of support retainer",
     {"customer": "Acme corp", "items": [{"item": "support retainer", "quantity": 2}]}, "Acme Corporation"),
])
def test_a_partial_or_variant_name_is_a_confirm_and_its_answer_makes_the_card(services, case, said, args, customer):
    s = Session()
    kind, code, options = outcome(services, args, said, s)
    assert (kind, code, options) == ("confirm", "customer_ambiguous", [customer, "someone else"]), case
    assert s.answer(s.pending["choices"][0]["code"])
    kind, got = outcome(services, args, said, s)
    assert kind == "card" and got["customer"] == customer, case


def test_h020_reads_two_before_the_items_own_unit(services):
    s = Session(bindings={"customer": "Acme Corporation"})
    kind, got = outcome(services, {"items": [{"item": "support retainer"}]}, "Acme corp, two months of support retainer",
                        s)
    assert kind == "card" and got["items"] == [{"item": "Support retainer", "quantity": "2"}]


def test_h027_a_name_with_another_legal_form_asks(services):
    kind, code, options = outcome(services, {"customer": "Acme Corp Ltd",
                                             "items": [{"item": "consulting", "quantity": 2}]},
                                  "Invoice Acme Corp Ltd for 2 hours of consulting.")
    assert kind in ("confirm", "question") and code == "customer_ambiguous" and "Acme Corporation" in options


@pytest.mark.parametrize("passed", ["Acme Corporation", "Hooli", None])
def test_h023_two_customers_named_ask_whichever_the_model_passed(services, passed):
    args = {"items": [{"item": "website audit", "quantity": 1}], **({"customer": passed} if passed else {})}
    kind, code, options = outcome(services, args, "Invoice either Acme Corporation or Hooli for one website audit.")
    assert (kind, code, options) == ("question", "customer_ambiguous", ["Acme Corporation", "Hooli"])


def test_one_exact_name_and_a_name_in_another_field_are_unaffected(services):
    kind, got = outcome(services, {"customer": "Hooli", "items": [{"item": "website audit", "quantity": 1}]},
                        "Invoice Hooli for one website audit.")
    assert kind == "card" and got["customer"] == "Hooli"
    kind, got = outcome(services, {"customer": "Hooli", "items": [{"item": "consulting", "quantity": 2}]},
                        "Invoice Hooli for 2 hours of consulting, and the email is payables@hooli.example")
    assert kind == "card" and got["customer"] == "Hooli"  # its own email names the same customer
