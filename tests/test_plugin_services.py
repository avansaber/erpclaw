"""Plugin services: typed operations one plugin calls in another inside the caller's transaction, through core's
call_service; checks a write service registers, run before an approved commit ends; a question raised anywhere
reaching the owner with its data; and manifests without services keeping their digest."""
from __future__ import annotations

import hashlib
import json
import logging
from collections import OrderedDict, UserDict, deque
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import service_plugins as sp  # tests/ is on the path (pytest rootdir)
from bookdiff import assert_changes, snapshot
from erpclaw_api.app import create_app
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import approvals, models as m
from erpclaw_core.catalogue import catalogue
from erpclaw_core.common import Refusal, company_ctx
from erpclaw_core.contracts import Result, Service, ServiceModel
from erpclaw_core.manifest import Manifest, ServiceDecl
from erpclaw_core.plugins import REGISTRY, PluginRefused, Registry, call_service, check_installed

FIXTURES = Path(__file__).parent / "contracts" / "plugin" / "v1"
PROVIDER, CONSUMER = sp.PROVIDER, sp.CONSUMER
QUOTE, RECORD = f"{PROVIDER}.quote", f"{PROVIDER}.record"


def M(value, currency="USD") -> dict:
    """An amount as it crosses a service."""
    return {"value": value, "currency": currency}


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture
def books(engine, monkeypatch):
    """Demo books with the planted provider and consumer installed."""
    sp.RAN.clear()
    sp.install(monkeypatch, engine, sp.provider(), sp.consumer())
    return engine


@pytest.fixture
def private_probe(books, monkeypatch):
    class PrivateProbe(sp.ProbeIn):
        private_note: str

    monkeypatch.setattr(REGISTRY.services[f"{PROVIDER}.probe"], "input_model", PrivateProbe)
    return {"private_note": "private-probe-7c9e"}


def _posted(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(m.posting)
                            .where(m.posting.c.voucher_type == "planted_sale")).scalar_one()


def _call(engine, name: str, inp, caller: str = CONSUMER, commit: bool = False):
    """Call in a real approved commit, or on a fresh context rolled back afterwards."""
    if commit:
        called = []

        def approved_commit(ctx, approved_input, key):
            try:
                out = call_service(ctx, caller, name, inp)
            except Refusal as refusal:
                called.append(refusal)
                raise  # approve must roll back and record the service's refusal
            called.append(out)
            return Result("Checked the service response.")

        consumer = REGISTRY.plugins[CONSUMER]
        consumer = replace(consumer, contracts=[
            replace(c, commit=approved_commit) if c.name == "svc_post" else c
            for c in consumer.contracts])
        with pytest.MonkeyPatch.context() as patch:
            sp.register(patch, REGISTRY.plugins[PROVIDER], consumer)
            card = approvals.propose(engine, REGISTRY.contracts["svc_post"], {"amount": "1"})
            assert card.status == "card"
            outcome = approvals.approve(engine, card.approval_id, card.digest)
        assert len(called) == 1
        value = called[0]
        assert outcome.status == ("refused" if isinstance(value, Refusal) else "committed"), outcome.text
        if isinstance(value, Refusal):
            raise value
        return value

    with engine.connect() as conn:
        tx = conn.begin()
        try:
            return call_service(company_ctx(conn), caller, name, inp)
        finally:
            tx.rollback()


def _refused(engine, name: str, inp, **kw) -> Refusal:
    with pytest.raises(Refusal) as e:
        _call(engine, name, inp, **kw)
    return e.value


def _private_warning(caplog, service, code, reason, *values):
    records = [record for record in caplog.records if record.name == "erpclaw_core.plugins"]
    assert len(records) == 1
    record = records[0]
    assert record.levelname == "WARNING"
    what = f"The input to {service}" if code == "service_input" else f"The result the service {service} returned"
    assert record.getMessage().startswith(f"{what} refused ({code}): ")
    assert reason in record.getMessage()
    assert record.exc_info is None
    for value in values:
        assert value not in record.getMessage()
        assert value not in repr(record.msg)
        assert value not in repr(record.args)
        for captured in caplog.records:
            assert value not in captured.getMessage()
            assert value not in repr(captured.msg)
            assert value not in repr(captured.args)
            assert value not in (captured.exc_text or "")
            if captured.exc_info:
                assert value not in logging.Formatter().formatException(captured.exc_info)


# --- the digest -------------------------------------------------------------------

def _digest_before_services(manifest: Manifest) -> str:
    """The digest as the engine computed it before services existed: sha256 of the canonical JSON of every field
    that existed then."""
    body = {k: v for k, v in manifest.model_dump(mode="json").items() if k not in ("services", "uses")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@pytest.mark.parametrize("name", ["manifest_email_folder_before_services", "manifest_sales_before_services"])
def test_a_manifest_without_services_keeps_the_digest_written_before_services(name):
    fx = _fixture(name)
    assert "services" not in fx["manifest"] and "uses" not in fx["manifest"]
    assert Manifest(**fx["manifest"]).digest() == fx["expect"]["digest"]
    assert Manifest(**fx["manifest"], services=[], uses=[]).digest() == fx["expect"]["digest"]


def test_books_installed_before_services_open(engine):
    """The install records as the engine wrote them before services existed: the books open, through the engine
    and through the API."""
    with engine.begin() as conn:  # sales cannot serve here now: it declares optional uses, so its manifest has
        for name in ("email_folder",):  # no digest from before services
            conn.execute(m.plugin_install.update().where(m.plugin_install.c.name == name).values(
                manifest_digest=_digest_before_services(REGISTRY.plugins[name].manifest)))
    check_installed(engine)
    create_app(engine, ScriptedModel([]))


def test_a_manifest_with_services_or_uses_has_a_different_digest():
    base = Manifest(**_fixture("manifest_email_folder_before_services")["manifest"])
    services = base.model_copy(update={"services": [ServiceDecl(name="deliver", effect="write")]})
    uses = base.model_copy(update={"depends": {"sales": ">=0.0.1"}, "uses": ["sales.quote"]})
    assert services.digest() != base.digest()
    assert uses.digest() != base.model_copy(update={"depends": {"sales": ">=0.0.1"}}).digest()
    fx = _fixture("manifest_with_services")
    assert Manifest(**fx["manifest"]).digest() == fx["expect"]["digest"] != base.digest()


# --- the loader ---------------------------------------------------------------------

def _loads(*plugins) -> None:
    Registry().fill([REGISTRY.plugins[n] for n in REGISTRY.order] + list(plugins))


def _refusal(*plugins) -> str:
    with pytest.raises(PluginRefused) as e:
        _loads(*plugins)
    return str(e.value)


def _consumer_manifest(**over):
    c = sp.consumer()
    c.manifest = c.manifest.model_copy(update=over)
    return c


def test_the_loader_accepts_matching_services_and_uses():
    _loads(sp.provider(), sp.consumer())


def test_the_loader_refuses_uses_without_the_dependency():
    assert f"uses {QUOTE} but does not depend on {PROVIDER}" in _refusal(sp.provider(),
                                                                        _consumer_manifest(depends={}))


def test_the_loader_refuses_a_declared_service_with_no_registration():
    p = sp.provider()
    p.services = [s for s in p.services if s.name != "record"]
    assert "declares service record, which it does not register" in _refusal(p, sp.consumer())


def test_the_loader_refuses_a_registration_not_declared():
    p = sp.provider()
    p.manifest = p.manifest.model_copy(update={"services": [d for d in p.manifest.services if d.name != "probe"]})
    assert "registers service probe, which its manifest does not declare" in _refusal(
        p, sp.consumer([QUOTE, RECORD]))


def test_the_loader_refuses_uses_naming_a_service_the_dependency_does_not_declare():
    assert f"uses {PROVIDER}.refund, which {PROVIDER} does not declare" in _refusal(
        sp.provider(), sp.consumer([QUOTE, f"{PROVIDER}.refund"]))


def test_the_loader_refuses_a_registration_whose_effect_or_models_differ():
    p = sp.provider()
    p.services = [Service("quote", "write", sp.QuoteIn, sp.QuoteOut, sp.quote), *p.services[1:]]
    assert "service quote is registered as 'write', its manifest declares 'read'" in _refusal(p, sp.consumer())

    from pydantic import BaseModel

    class Loose(BaseModel):
        total: str

    p = sp.provider()
    p.services = [Service("quote", "read", sp.QuoteIn, Loose, sp.quote), *p.services[1:]]
    assert "output model Loose accepts unknown fields" in _refusal(p, sp.consumer())


# --- call_service -----------------------------------------------------------------------

def test_an_undeclared_call_is_refused(books, monkeypatch):
    monkeypatch.setitem(REGISTRY.plugins, CONSUMER, sp.consumer([RECORD]))  # uses no longer lists quote
    assert _refused(books, QUOTE, {"amount": M("1"), "rate": "1"}).code == "undeclared_service"
    assert _refused(books, f"{PROVIDER}.nothing", {}).code == "undeclared_service"
    assert _refused(books, QUOTE, {"amount": M("1"), "rate": "1"}, caller="sales").code == "undeclared_service"


def test_a_disabled_callee_is_refused(books):
    with books.begin() as conn:
        conn.execute(m.plugin_install.update().where(m.plugin_install.c.name == PROVIDER).values(status="disabled"))
    assert _refused(books, QUOTE, {"amount": M("1"), "rate": "1"}).code == "plugin_not_enabled"


def test_a_write_service_outside_an_approved_commit_is_refused(books):
    inp = {"voucher": "v", "amount": M("1")}
    assert _refused(books, RECORD, inp).code == "service_outside_commit"  # any non-commit context
    assert _call(books, RECORD, inp, commit=True)["voucher"] == "v"  # the commit's context
    before = snapshot(books)
    with pytest.raises(Refusal) as e:  # in a preview, while the card is proposed: no card
        approvals.propose(books, REGISTRY.contracts["svc_post"], {"amount": "1", "write_in_preview": True})
    assert e.value.code == "service_outside_commit"
    assert_changes(before, snapshot(books), {})


def test_registering_a_check_outside_an_approved_commit_is_refused(books):
    with books.connect() as conn:
        with pytest.raises(Refusal) as e:
            company_ctx(conn).register_check("compare", lambda ctx: None)
    assert e.value.code == "service_outside_commit"


def test_a_read_service_works_in_a_preview(books):
    card = approvals.propose(books, REGISTRY.contracts["svc_post"], {"amount": "12.5"})
    assert card.status == "card" and ["Amount", "12.50"] in card.card["lines"]


@pytest.mark.parametrize("inp", [{"amount": 1.5, "rate": "1"}, {"amount": M("1"), "rate": "1", "discount": "0"},
                                 {"amount": M("1"), "rate": {"value": 0.5}}, {"amount": M("1"), "rate": "-1"}])
def test_an_input_with_a_float_an_unknown_field_or_a_bad_value_is_refused(books, inp):
    assert _refused(books, QUOTE, inp).code == "service_input"


def test_the_input_is_a_dict_of_plain_data_and_exact_decimals_cross(books):
    from decimal import Decimal

    assert _call(books, QUOTE, {"amount": M(Decimal("2.5")), "rate": Decimal("1.1")}) == {"total": M("2.75"),
                                                                                        "quoteRef": ""}


def test_an_alias_keyed_dict_crosses_in_and_out(books):
    """A field with an alias is given by its alias, and comes back under it (model_dump by_alias)."""
    assert _call(books, QUOTE, {"amount": M("2"), "rate": "1.5", "quoteRef": "Q-7"}) == {"total": M("3.00"),
                                                                                        "quoteRef": "Q-7"}
    assert _refused(books, QUOTE, {"amount": M("2"), "rate": "1.5", "quote_ref": "Q-7"}).code == "service_input"


INPUT_NOT_PLAIN = {
    "float": ({"amount": 0.1 + 0.2, "rate": "1"}, "floating-point"),
    "nested_float": ({"amount": "1", "rate": ["1", {"x": 0.5}]}, "floating-point"),
    "generator": ({"amount": (x for x in [0.1 + 0.2]), "rate": "1"}, "is a generator"),
    "iterator": ({"amount": iter([0.1 + 0.2]), "rate": "1"}, "is a list_iterator"),
    "deque": ({"amount": deque([0.1 + 0.2]), "rate": "1"}, "is a deque"),
    "userdict_value": ({"amount": UserDict({"value": 0.1 + 0.2}), "rate": "1"}, "is a UserDict"),
    "set": ({"amount": {0.1 + 0.2}, "rate": "1"}, "is a set"),
    "tuple": ({"amount": ("1",), "rate": "1"}, "is a tuple"),
    "bytes": ({"amount": b"1", "rate": "1"}, "is a bytes"),
    "object": ({"amount": object(), "rate": "1"}, "is a object"),
    "str_enum": ({"amount": M("1"), "rate": "1", "quoteRef": sp.Colour.RED}, "is a Colour"),
    "int_enum": ({"amount": sp.Level.HIGH, "rate": "1"}, "is a Level"),
    "str_subclass": ({"amount": sp.Text("1"), "rate": "1"}, "is a Text"),
    "non_str_key": ({"amount": M("1"), "rate": "1", 1: "x"}, "key that is not plain text"),
    "nested_instance": ({"amount": M("1"), "rate": sp.QuoteOut(total=M("1"))}, "is a QuoteOut"),
    "nested_money": ({"amount": sp.Money(value="1", currency="USD"), "rate": "1"}, "is a Money"),
    "nested_constructed": ({"amount": M("1"), "rate": sp.QuoteIn.model_construct(amount=0.1 + 0.2, rate="1")},
                           "is a QuoteIn"),
}


@pytest.mark.parametrize("case", INPUT_NOT_PLAIN)
def test_an_input_that_is_not_plain_data_is_refused_before_validation(books, case, caplog):
    """Floats, containers other than an exact dict or list, subclasses (enums, a str subclass) and model instances,
    nested anywhere: refused by exact type before the model sees them."""
    inp, why = INPUT_NOT_PLAIN[case]
    private = "private-input-7c9e"
    with caplog.at_level("WARNING", logger="erpclaw_core.plugins"):
        r = _refused(books, QUOTE, {**inp, "private_note": private})
    assert r.code == "service_input"
    assert r.message == "Something went wrong inside your books' software, so nothing was changed."
    _private_warning(caplog, QUOTE, r.code, why, private, "0.30000000000000004", "0.5", "USD")


@pytest.mark.parametrize("inp", [sp.QuoteIn(amount=M("1"), rate="1"), UserDict({"amount": M("1"), "rate": "1"}),
                                 OrderedDict(amount="1", rate="1"), [("amount", "1")]])
def test_the_input_itself_is_an_exact_dict(books, inp):
    """A model instance, valid or not, is refused as the input: plain data only crosses."""
    assert _refused(books, QUOTE, inp).code == "service_input"


OUTPUT_NOT_PLAIN = {
    # floats, raw: in a dict, in a nested field
    "float_total": "floating-point", "float_nested": "floating-point", "float": "floating-point",
    # floats in containers a model would read into its Decimal fields
    "float_generator": "is a generator", "float_iterator": "is a list_iterator", "float_deque": "is a deque",
    "float_userdict": "is a UserDict", "float_set": "is a set",
    # model instances, at the top level and nested, valid, constructed or changed since
    "instance_valid": "is a ProbeOut", "instance_nested": "is a Part", "instance": "is a ProbeOut",
    "float_constructed": "is a ProbeOut", "float_nested_constructed": "is a ProbeOut",
    "float_assigned": "is a ProbeOut", "nested_constructed_invalid_decimal": "is a Part",
    "nested_mutated_negative": "is a Part",
    # a float source a Decimal-to-text serializer would turn into "0.30000000000000004"
    "serializer_text_from_float": "is a ProbeOut",
    # subclasses, other containers and objects
    "str_enum": "is a Colour", "int_enum": "is a Level", "tuple": "is a tuple", "ordered_dict": "is a OrderedDict",
    "str_subclass": "is a Text", "non_str_key": "key that is not plain text", "custom_object": "is a object",
}


@pytest.mark.parametrize("give", OUTPUT_NOT_PLAIN)
def test_an_output_that_is_not_plain_data_is_refused_before_validation(books, give, caplog, private_probe):
    with caplog.at_level("WARNING", logger="erpclaw_core.plugins"):
        r = _refused(books, f"{PROVIDER}.probe", {"give": give, **private_probe})
    assert r.code == "service_output"
    assert r.message == "Something went wrong inside your books' software, so nothing was changed."
    # A selector such as "tuple" can also be a diagnostic type; its quoted value must stay private.
    _private_warning(caplog, f"{PROVIDER}.probe", r.code, OUTPUT_NOT_PLAIN[give],
                     repr(give), private_probe["private_note"])


def test_a_serializer_that_emits_a_float_is_refused_after_the_dump(books, caplog, private_probe):
    """The caller's dict is the model's model_dump(mode="json"), which runs the model's own serializers; the dump
    is checked again, so a serializer cannot hand the caller a float."""
    with caplog.at_level("WARNING", logger="erpclaw_core.plugins"):
        r = _refused(books, f"{PROVIDER}.probe", {"give": "serializer_float", **private_probe})
    assert r.code == "service_output"
    assert r.message == "Something went wrong inside your books' software, so nothing was changed."
    _private_warning(caplog, f"{PROVIDER}.probe", r.code, "note is a floating-point number",
                     "serializer_float", private_probe["private_note"])


def test_a_bool_is_not_taken_as_a_number(books, caplog, private_probe):
    with caplog.at_level("WARNING", logger="erpclaw_core.plugins"):
        r = _refused(books, f"{PROVIDER}.probe", {"give": "bool_as_number", **private_probe})
    assert r.code == "service_output"
    assert r.message == "Something went wrong inside your books' software, so nothing was changed."
    _private_warning(caplog, f"{PROVIDER}.probe", r.code,
                     "true or false where a number is required", "bool_as_number", private_probe["private_note"])


def test_exact_decimals_in_the_output_cross_as_canonical_strings(books):
    assert _call(books, f"{PROVIDER}.probe", {"give": "decimals"}) == {
        "total": "0.30", "note": None, "count": 0, "parts": [{"amount": "0.10"}, {"amount": "0.20"}],
        "amounts": ["0.30"], "by_name": {"copper": "0.30"}}


@pytest.mark.parametrize("give", ["missing", "unknown_field", "float", "not_a_dict", "instance"])
def test_the_output_is_validated_against_its_model(books, give):
    assert _refused(books, f"{PROVIDER}.probe", {"give": give}).code == "service_output"


def test_input_and_output_cross_as_dicts_matching_the_fixtures(books):
    for name in ("service_read_returns_json", "service_output_good"):
        fx = _fixture(name)
        out = _call(books, fx["service"], fx["input"])
        assert type(out) is dict and out == fx["expect"]["output"]
    # the output is the model's model_dump(mode="json", by_alias=True): decimals as canonical strings
    assert _call(books, QUOTE, {"amount": M("2.5"), "rate": "1.1"}) == sp.QuoteOut(total=M("2.75")).model_dump(
        mode="json", by_alias=True)


def test_a_services_refusal_reaches_the_caller_unchanged(books):
    r = _refused(books, RECORD, {"voucher": "v", "amount": M("1"), "ask": True}, commit=True)
    q = sp.QUESTION
    assert (r.code, r.message, r.options, r.data) == (q["code"], q["message"], q["options"], q["data"])


def test_a_service_runs_on_the_callers_connection_and_transaction(books, monkeypatch):
    seen = []

    def spy(ctx, inp):
        seen.append(ctx)
        return {"total": M("1")}

    monkeypatch.setattr(REGISTRY.services[QUOTE], "fn", spy)
    with books.connect() as conn:
        tx = conn.begin()
        ctx = company_ctx(conn)
        call_service(ctx, CONSUMER, QUOTE, {"amount": M("1"), "rate": "1"})
        tx.rollback()
    assert seen == [ctx]


# --- Money: every amount crosses with its currency ------------------------------------------

@pytest.mark.parametrize("amount, code", [
    ("2", "service_input"),                              # a bare amount where Money is declared
    ({"value": 2.5, "currency": "USD"}, "service_input"),  # a float
    (M("2.001"), "service_input"),                       # beyond the currency's scale
    (M("2", "usd"), "service_input"),                    # not a currency code
    (M("2", "XYZ"), "unknown_currency"),                 # not one of the books' currencies
])
def test_a_money_input_is_refused_unless_it_is_an_exact_value_in_a_currency_of_the_books(books, amount, code):
    assert _refused(books, QUOTE, {"amount": amount, "rate": "1"}).code == code
    assert _refused(books, RECORD, {"voucher": "v", "amount": amount}, commit=True).code == code


def test_a_money_input_is_accepted_in_any_currency_of_the_books_at_its_scale(books):
    for amount, total in ((M("2"), M("3.00")), (M("2.125", "KWD"), M("3.190", "KWD")), (M("2", "JPY"), M("3", "JPY"))):
        assert _call(books, QUOTE, {"amount": amount, "rate": "1.5"})["total"] == total


@pytest.mark.parametrize("line, code", [
    ({"debit": "1.00", "credit": M("0.00")}, "service_output"),                  # a bare amount
    ({"debit": {"value": 1.0, "currency": "USD"}, "credit": M("0.00")}, "service_output"),  # a float
    ({"debit": M("1.001"), "credit": M("0.00")}, "service_output"),              # beyond scale
    ({"debit": M("1.00"), "credit": M("0", "XYZ")}, "unknown_currency"),         # an unknown currency, nested
])
def test_a_money_in_the_write_services_nested_output_lines_is_checked_like_any_other(books, monkeypatch, line, code):
    monkeypatch.setattr(REGISTRY.services[RECORD], "fn", lambda ctx, inp: {
        "voucher": inp.voucher, "ledger_lines": [{"account": "a", **line}]})
    assert _refused(books, RECORD, {"voucher": "v", "amount": M("1")}, commit=True).code == code


def test_without_the_currency_check_an_unknown_currency_would_cross(books, monkeypatch):
    """Mutation: the Money currency check removed, an unknown currency crosses (so the check above is what refuses)."""
    from erpclaw_core import plugins

    monkeypatch.setattr(plugins, "_known_currencies", lambda ctx, model, what: None)
    assert _call(books, QUOTE, {"amount": M("2", "XYZ"), "rate": "1"})["total"] == M("2.00", "XYZ")


# --- registered checks ----------------------------------------------------------------------

def _approve(engine, args: dict):
    card = approvals.propose(engine, REGISTRY.contracts["svc_post"], args)
    return card, approvals.approve(engine, card.approval_id, card.digest)


def test_a_refusing_check_after_the_callers_posting_rolls_the_whole_commit_back(books):
    card = approvals.propose(books, REGISTRY.contracts["svc_post"], {"amount": "5.00", "checks": ["refuse_compare"]})
    before = snapshot(books)
    out = approvals.approve(books, card.approval_id, card.digest)
    assert out.status == "refused" and out.data["code"] == "provider_mismatch" and sp.RAN == ["refuse_compare"]
    after = snapshot(books)
    row = after["approval"][(card.approval_id,)]
    assert_changes(before, after, {  # only the claim and the refused outcome: no posting, no audit row
        "approval": {"changed": [({"id": card.approval_id}, {k: row[k] for k in (
            "status", "decided_at", "approved_by", "approver_grants_version")})]},
        "operation_outcome": {"added": [{"key": card.operation_key, "status": "refused"}]}})
    assert _posted(books) == 0


def test_a_passing_check_commits(books):
    _card, out = _approve(books, {"amount": "5.00", "checks": ["compare"]})
    assert out.status == "committed" and sp.RAN == ["compare"] and _posted(books) == 1


def test_the_same_key_registered_twice_runs_once(books):
    _card, out = _approve(books, {"amount": "5.00", "checks": ["compare", "compare"]})
    assert out.status == "committed" and sp.RAN == ["compare"]


def test_checks_run_in_registration_order(books):
    _card, out = _approve(books, {"amount": "5.00", "checks": ["b_second", "a_first", "c_third"]})
    assert out.status == "committed" and sp.RAN == ["b_second", "a_first", "c_third"]


def test_checks_run_after_the_commit_returns_with_the_posting_visible(books, monkeypatch):
    seen = []

    def record(ctx, inp):
        ctx.register_check("count", lambda c: seen.append(c.conn.execute(
            select(func.count()).select_from(m.posting).where(m.posting.c.voucher_type == "planted_sale")).scalar_one()))
        return sp.record(ctx, inp.model_copy(update={"checks": []}))

    monkeypatch.setattr(REGISTRY.services[RECORD], "fn", record)
    _card, out = _approve(books, {"amount": "5.00"})
    assert out.status == "committed" and seen == [1]  # the caller's posting, made after the service returned


# --- a question's data on every path to the owner ---------------------------------------------

QUESTION_EVENT = {"type": "ask", "code": sp.QUESTION["code"], "options": sp.QUESTION["options"],
                  "data": sp.PUBLIC_DATA}  # what an event shows: each choice by code and label
INTERNAL = {"code": sp.QUESTION["code"], "options": sp.QUESTION["options"], "data": sp.QUESTION["data"]}


def _no_ref(event: dict) -> bool:
    """The event carries no ref anywhere: the question's choices are code and label only."""
    return '"ref"' not in json.dumps(event) and all(set(c) == {"code", "label"} for c in event["data"]["choices"])


def _client(engine, *replies):
    return TestClient(create_app(engine, ScriptedModel(list(replies)), embedded_owner=True))


def _ask_in_quote(monkeypatch) -> None:
    def ask(ctx, inp):
        q = sp.QUESTION
        raise Refusal(q["message"], q["code"], q["options"], **q["data"])

    monkeypatch.setattr(REGISTRY.services[QUOTE], "fn", ask)


def _pick(event: dict) -> dict:
    return {k: event.get(k) for k in QUESTION_EVENT}


def test_a_question_raised_in_a_service_at_proposal_is_an_ask_with_its_data(books, monkeypatch):
    _ask_in_quote(monkeypatch)
    c = _client(books, Reply("", [call("svc_post", amount="5.00")]))
    cid = c.post("/v1/conversations").json()["id"]
    [ev] = c.post(f"/v1/conversations/{cid}/messages", json={"text": "record 5"}).json()["events"]
    assert _pick(ev) == QUESTION_EVENT and _no_ref(ev)


def test_a_question_raised_at_an_approvals_recomputation_keeps_its_data(books, monkeypatch):
    c = _client(books, Reply("", [call("svc_post", amount="5.00")]))
    cid = c.post("/v1/conversations").json()["id"]
    [card] = c.post(f"/v1/conversations/{cid}/messages", json={"text": "record 5"}).json()["events"]
    _ask_in_quote(monkeypatch)  # the preview, recomputed under the locks, now asks
    [ev] = c.post(f"/v1/cards/{card['approval_id']}/approve", json={"digest": card["digest"]}).json()["events"]
    assert _pick(ev) == QUESTION_EVENT and ev["status"] == "superseded" and _no_ref(ev)
    assert _noted(c, cid) == _note_text(ev)  # in the card's own conversation, with its options and data, refs kept


def _noted(client, cid: str) -> str:
    from erpclaw_assistant import store

    return store.events(store.open_request(cid)["id"])[-1]["text"]


def _note_text(ev: dict) -> str:
    """The conversation note: the engine's question with its complete data, refs included."""
    return (f"(engine question, code {ev['code']}) {ev['text']} Options: " + "; ".join(ev["options"]) + ". Data: "
            + json.dumps(sp.QUESTION["data"], sort_keys=True) + ".")


def test_a_question_raised_by_a_write_service_inside_the_effect_is_stored_replayed_and_asked(books):
    c = _client(books, Reply("", [call("svc_post", amount="5.00", ask=True)]))
    cid = c.post("/v1/conversations").json()["id"]
    [card] = c.post(f"/v1/conversations/{cid}/messages", json={"text": "record 5"}).json()["events"]
    before = snapshot(books)
    [ev] = c.post(f"/v1/cards/{card['approval_id']}/approve", json={"digest": card["digest"]}).json()["events"]
    assert _pick(ev) == QUESTION_EVENT and ev["status"] == "refused" and _no_ref(ev)
    # rolled back: the caller's posting made before the question is gone; the refused outcome is stored
    after = snapshot(books)
    row = after["approval"][(card["approval_id"],)]
    assert_changes(before, after, {
        "approval": {"changed": [({"id": card["approval_id"]}, {k: row[k] for k in (
            "status", "decided_at", "approved_by", "approver_grants_version")})]},
        "operation_outcome": {"added": [{"status": "refused"}]}})
    assert _posted(books) == 0
    with books.connect() as conn:
        o = conn.execute(select(m.operation_outcome).where(
            m.operation_outcome.c.approval_id == card["approval_id"])).mappings().one()
    stored = json.loads(o["result_json"])
    assert {k: stored[k] for k in ("code", "options", "data")} == INTERNAL  # the stored outcome keeps the refs
    # replayed: by read_back (inside the engine, refs kept) and by a repeated approval (the event, none)
    back = approvals.read_back(books, o["key"], ownership_check=lambda conversation: conversation == cid)
    assert back.status == "refused" and back.data == INTERNAL
    [again] = c.post(f"/v1/cards/{card['approval_id']}/approve", json={"digest": card["digest"]}).json()["events"]
    assert _pick(again) == QUESTION_EVENT and _no_ref(again)
    # noted in the card's own conversation as the engine's question, with its options and data
    assert _noted(c, cid) == _note_text(again)


def test_a_refusal_inside_the_effect_that_is_not_a_question_stays_a_result(books):
    c = _client(books, Reply("", [call("svc_post", amount="5.00", checks=["refuse_compare"])]))
    cid = c.post("/v1/conversations").json()["id"]
    [card] = c.post(f"/v1/conversations/{cid}/messages", json={"text": "record 5"}).json()["events"]
    [ev] = c.post(f"/v1/cards/{card['approval_id']}/approve", json={"digest": card["digest"]}).json()["events"]
    assert ev["type"] == "result" and ev["status"] == "refused"


# --- services never reach the model, the catalogue or the API ---------------------------------

SERVICE_NAMES = {"quote", "record", "probe", QUOTE, RECORD, f"{PROVIDER}.probe"}


def test_services_never_reach_the_models_tools_the_catalogue_or_the_api(books):
    # the model's tool schema: the consumer's contracts are offered, no service is
    model = ScriptedModel([Reply("", [call("quote", amount="1", rate="1")]), Reply("", [call(QUOTE, amount="1")]),
                           Reply("Nothing to do.")])
    app = create_app(books, model, embedded_owner=True)
    c = TestClient(app)
    cid = c.post("/v1/conversations").json()["id"]
    events = c.post(f"/v1/conversations/{cid}/messages", json={"text": "quote 1"}).json()["events"]
    offered = set().union(*map(set, model.tools_seen))
    assert {"svc_post", "svc_read"} <= offered and not offered & SERVICE_NAMES
    # a call to a service by name is not a tool: it goes back to the model as unknown, and nothing runs
    tool_replies = [msg["content"] for msg in model.seen[-1] if msg.get("role") == "tool"]
    assert all("There is no tool called" in t for t in tool_replies) and len(tool_replies) == 2
    assert events == [{"type": "answer", "text": "Nothing to do.", "checked": "passed", "currency": "USD"}]
    # the catalogue
    with books.connect() as conn:
        entries = catalogue(conn)
    named = {e.id for e in entries} | {e.name for e in entries} | {e.target for e in entries if e.target}
    assert "op.svc_post" in {e.id for e in entries} and not named & SERVICE_NAMES
    assert not any(n in e.id for e in entries for n in (QUOTE, RECORD))
    # the API: no route names a service
    paths = {r.path for r in app.routes}
    assert not any("service" in p or any(n in p for n in SERVICE_NAMES) for p in paths)


def test_services_are_not_contracts_or_metrics_in_the_registry(books):
    assert set(REGISTRY.services) >= {QUOTE, RECORD}
    assert not set(REGISTRY.contracts) & SERVICE_NAMES and not set(REGISTRY.metrics) & SERVICE_NAMES


def test_service_models_based_on_service_model_refuse_unknown_fields():
    class Thing(ServiceModel):
        total: str

    with pytest.raises(Exception):
        Thing(total="1", extra="x")
