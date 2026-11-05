"""Two planted plugins for the plugin services tests and the plugin/1 contract fixtures: a provider exposing a read
and a write service, and a consumer that declares them in `uses` and calls them from its own contracts. Nothing
here ships: they are registered for one test and installed only into the test books."""
from __future__ import annotations

from collections import OrderedDict, UserDict, deque
from decimal import Decimal
from enum import IntEnum, StrEnum
from typing import Any, Literal

from pydantic import ConfigDict, Field, field_serializer

from erpclaw_core import ledger
from erpclaw_core.common import Ctx, Refusal, new_id
from erpclaw_core.contracts import Card, Contract, Input, Money, Preview, Result, Service, ServiceModel
from erpclaw_core.manifest import PUBLISHER, Capabilities, Events, Manifest, ServiceDecl, Source
from erpclaw_core.plugins import REGISTRY, Plugin, Registry, call_service

PROVIDER, CONSUMER = "svc_provider", "svc_consumer"
RAN: list[str] = []  # the keys of the registered checks, in the order they ran

# the question a service raises, as a resolver raises one: options in the owner's words, and its data, each choice
# with its canonical reference (`ref`), which stays inside the engine
QUESTION = {"message": "Which warehouse should the copper wire come from?", "code": "warehouse_ambiguous",
            "options": ["Main Warehouse", "Back Store"],
            "data": {"field": "warehouse", "question_id": "q1",
                     "choices": [{"code": "q1-1", "label": "Main Warehouse", "ref": "Main Warehouse"},
                                 {"code": "q1-2", "label": "Back Store", "ref": "Back Store"}]}}
# the same data as an event shows it: choices by code and label only
PUBLIC_DATA = {**QUESTION["data"], "choices": [{"code": c["code"], "label": c["label"]}
                                               for c in QUESTION["data"]["choices"]]}


def manifest(name: str, **over) -> Manifest:
    base = dict(name=name, version="1.0.0", publisher=PUBLISHER, distribution=f"erpclaw-{name}",
                licence="GPL-3.0-only", source=Source(repository="workspace", commit="workspace"),
                engine=">=0.0.1", after_core_revision="0004", depends={}, conflicts=[], owned_tables=[],
                contracts={}, calls=[], events=Events(emits=[], consumes=[]), slots={}, voucher_types=[],
                capabilities=Capabilities(network=[], files=[]), metrics=[])
    base.update(over)
    return Manifest(**base)


# --- the provider's services --------------------------------------------------

class QuoteIn(ServiceModel):
    amount: Money
    rate: Decimal = Field(ge=0, max_digits=9, decimal_places=4)
    quote_ref: str = Field(default="", alias="quoteRef", max_length=20)  # an aliased field


class QuoteOut(ServiceModel):
    total: Money
    quote_ref: str = Field(default="", alias="quoteRef")


def quote(ctx: Ctx, inp: QuoteIn) -> dict:
    """A service returns plain data: here a dict keyed by the output model's alias."""
    total = (inp.amount.value * inp.rate).quantize(Decimal("0.01"))
    return {"total": {"value": str(total), "currency": inp.amount.currency}, "quoteRef": inp.quote_ref}


class LineOut(ServiceModel):
    account: str
    debit: Money
    credit: Money


class RecordIn(ServiceModel):
    voucher: str
    amount: Money
    checks: list[str] = []  # keys of the checks to register; a key starting "refuse" refuses
    ask: bool = False  # raise the question instead of returning


class RecordOut(ServiceModel):
    voucher: str
    ledger_lines: list[LineOut]


def _check(key: str):
    def check(ctx: Ctx) -> None:
        RAN.append(key)
        if key.startswith("refuse"):
            raise Refusal(f"The posting does not match what the provider returned ({key}).", "provider_mismatch")
    return check


def record(ctx: Ctx, inp: RecordIn) -> dict:
    """A write service: its lines for the caller's single posting, and the checks it registers."""
    for key in inp.checks:
        ctx.register_check(key, _check(key))
    if inp.ask:
        q = QUESTION
        raise Refusal(q["message"], q["code"], q["options"], **q["data"])
    bank, sales = ledger.account_by_number(ctx, "1100"), ledger.account_by_number(ctx, "4100")
    zero = Money(value=Decimal("0"), currency=inp.amount.currency)
    return RecordOut(voucher=inp.voucher, ledger_lines=[  # the author dumps their own model, by alias
        LineOut(account=bank["id"], debit=inp.amount, credit=zero),
        LineOut(account=sales["id"], debit=zero, credit=inp.amount)]).model_dump(mode="json", by_alias=True)


PROBE_GIVES = ("good", "decimals", "missing", "unknown_field", "float", "not_a_dict", "instance", "float_total",
               "float_nested", "float_constructed", "float_nested_constructed", "float_assigned", "float_generator",
               "float_iterator", "float_deque", "float_userdict", "float_set", "nested_constructed_invalid_decimal",
               "nested_mutated_negative", "bool_as_number", "custom_object", "instance_valid", "instance_nested",
               "serializer_float", "serializer_text_from_float", "str_enum", "int_enum", "tuple", "non_str_key",
               "ordered_dict", "str_subclass")


class ProbeIn(ServiceModel):
    give: Literal[PROBE_GIVES]


class Colour(StrEnum):
    RED = "red"


class Level(IntEnum):
    HIGH = 3


class Text(str):
    pass


class Part(ServiceModel):
    model_config = ConfigDict(extra="forbid", frozen=False)  # so a field can be assigned without validation
    amount: Decimal = Field(gt=0)


class ProbeOut(ServiceModel):
    model_config = ConfigDict(extra="forbid", frozen=False)
    total: Decimal
    note: Any = None
    count: int = 0
    parts: list[Part] = []
    amounts: list[Decimal] = []  # a typed list and dict, which a model would fill from other containers
    by_name: dict[str, Decimal] = {}

    @field_serializer("total")
    def _total(self, v):  # an ordinary Decimal-to-text serializer
        return str(v)

    @field_serializer("note")
    def _note(self, v):  # a serializer that emits a float when asked to
        return 0.1 + 0.2 if v == "emit a float" else v


def _mutated_part() -> Part:
    part = Part(amount=Decimal("1"))
    part.amount = Decimal("-1")  # no validation on assignment
    return part


def probe(ctx: Ctx, inp: ProbeIn):
    """Returns what it is told to, so the boundary can be seen refusing each kind of wrong result."""
    give = inp.give
    if give == "decimals":  # exact Decimal values in plain dicts and lists: accepted
        return {"total": Decimal("0.30"), "parts": [{"amount": Decimal("0.10")}, {"amount": Decimal("0.20")}],
                "amounts": [Decimal("0.30")], "by_name": {"copper": Decimal("0.30")}}
    if give == "float_constructed":
        return ProbeOut.model_construct(total=0.1 + 0.2)
    if give == "float_nested_constructed":
        return ProbeOut(total=Decimal("1"), parts=[Part.model_construct(amount=0.1 + 0.2)])
    if give == "float_assigned":
        out = ProbeOut(total=Decimal("1"))
        out.total = 0.1 + 0.2  # no validation on assignment
        return out
    # floats in containers a model would read into its Decimal fields, turning 0.1 + 0.2 into a Decimal
    if give == "float_generator":
        return {"total": "1", "amounts": (x for x in [0.1 + 0.2])}
    if give == "float_iterator":
        return {"total": "1", "amounts": iter([0.1 + 0.2])}
    if give == "float_deque":
        return {"total": "1", "amounts": deque([0.1 + 0.2])}
    if give == "float_userdict":
        return {"total": "1", "by_name": UserDict({"copper": 0.1 + 0.2})}
    if give == "float_set":
        return {"total": "1", "amounts": {0.1 + 0.2}}
    # model instances, which never cross: at the top level and nested, valid or not
    if give == "nested_constructed_invalid_decimal":
        return {"total": "1", "parts": [Part.model_construct(amount="not a number")]}
    if give == "nested_mutated_negative":
        return {"total": "1", "parts": [_mutated_part()]}
    if give == "instance_valid":
        return ProbeOut(total=Decimal("1"))
    if give == "instance_nested":
        return {"total": "1", "parts": [Part(amount=Decimal("1"))]}
    # the model's own serializers: one emitting a float; one turning a constructed float into text
    if give == "serializer_float":
        return {"total": "1", "note": "emit a float"}
    if give == "serializer_text_from_float":
        return ProbeOut.model_construct(total=0.1 + 0.2)
    return {"good": {"total": "1.50"}, "missing": {}, "unknown_field": {"total": "1", "extra": "x"},
            "float": {"total": "1", "note": 1.5}, "not_a_dict": ["1.50"],
            "instance": ProbeOut.model_construct(total="not a number"), "float_total": {"total": 0.1 + 0.2},
            "float_nested": {"total": "1", "parts": [{"amount": "0.10"}, {"amount": 0.1 + 0.2}]},
            "bool_as_number": {"total": "1", "count": True}, "custom_object": {"total": "1", "note": object()},
            "str_enum": {"total": "1", "note": Colour.RED}, "int_enum": {"total": "1", "count": Level.HIGH},
            "tuple": {"total": "1", "amounts": ("1",)}, "non_str_key": {"total": "1", "by_name": {1: "2"}},
            "ordered_dict": {"total": "1", "by_name": OrderedDict(copper="2")},
            "str_subclass": {"total": Text("1")}}[give]


PROVIDER_SERVICES = [Service("quote", "read", QuoteIn, QuoteOut, quote),
                     Service("record", "write", RecordIn, RecordOut, record),
                     Service("probe", "read", ProbeIn, ProbeOut, probe)]


def provider() -> Plugin:
    return Plugin(manifest=manifest(PROVIDER, services=[ServiceDecl(name=s.name, effect=s.effect)
                                                       for s in PROVIDER_SERVICES]),
                  kind="business-module", services=list(PROVIDER_SERVICES), artifact_digest="0" * 64)


# --- the consumer's contracts ---------------------------------------------------

class PostIn(Input):
    amount: str = Field(max_length=20)
    checks: list[str] = []
    ask: bool = False
    write_in_preview: bool = False


def _quote(ctx: Ctx, amount: str) -> dict:
    return call_service(ctx, CONSUMER, f"{PROVIDER}.quote", {"amount": {"value": amount, "currency": ctx.currency},
                                                              "rate": "1"})


def preview_post(ctx: Ctx, inp: PostIn) -> Preview:
    total = _quote(ctx, inp.amount)["total"]["value"]  # a read service in a preview
    if inp.write_in_preview:
        call_service(ctx, CONSUMER, f"{PROVIDER}.record",
                     {"voucher": "preview", "amount": {"value": inp.amount, "currency": ctx.currency}})
    return Preview(Card("Record a planted sale", [("Amount", total)], ["planted"]), total)


def commit_post(ctx: Ctx, inp: PostIn, key: str) -> Result:
    """The caller's writes first, then the provider's write service; its lines go into the caller's one posting."""
    voucher = new_id()
    if inp.ask:  # a write before the question, so the rollback is seen
        bank, sales = ledger.account_by_number(ctx, "1100"), ledger.account_by_number(ctx, "4100")
        ledger.post(ctx, ctx.today, "planted_sale", new_id(), [ledger.Line(bank["id"], debit=Decimal("1")),
                                                              ledger.Line(sales["id"], credit=Decimal("1"))])
    out = call_service(ctx, CONSUMER, f"{PROVIDER}.record", {"voucher": voucher, "checks": inp.checks, "ask": inp.ask,
                                                              "amount": {"value": inp.amount, "currency": ctx.currency}})
    lines = [ledger.Line(ln["account"], debit=Decimal(ln["debit"]["value"]), credit=Decimal(ln["credit"]["value"]))
             for ln in out["ledger_lines"]]
    posted = ledger.post(ctx, ctx.today, "planted_sale", voucher, lines, operation_key=key)
    return Result(f"Recorded {out['ledger_lines'][0]['debit']['value']}.",
                  {"voucher": voucher, "posting_seq": posted.seq})


def post_by_key(ctx: Ctx, key: str) -> Result | None:
    return None


class ReadIn(Input):
    amount: str = Field(max_length=20)


def run_read(ctx: Ctx, inp: ReadIn) -> Result:
    total = _quote(ctx, inp.amount)["total"]["value"]
    return Result(f"The total is {total}.", {"total": total}, [total])


CONSUMER_CONTRACTS = [
    Contract("svc_post", CONSUMER, "write", "Record a planted sale through the provider's services.", PostIn,
             preview=preview_post, commit=commit_post, find_by_operation_key=post_by_key,
             read_set=lambda ctx, inp: [("company", ctx.company_id)]),
    Contract("svc_read", CONSUMER, "read", "Read a planted quote through the provider's read service.", ReadIn,
             run=run_read),
]


def consumer(uses: list[str] | None = None) -> Plugin:
    return Plugin(manifest=manifest(CONSUMER, depends={PROVIDER: ">=1"}, voucher_types=["planted_sale"],
                                    contracts={c.name: c.effect for c in CONSUMER_CONTRACTS},
                                    uses=uses if uses is not None else [f"{PROVIDER}.{s.name}"
                                                                        for s in PROVIDER_SERVICES]),
                  kind="business-module", contracts=list(CONSUMER_CONTRACTS), artifact_digest="0" * 64)


def register(monkeypatch, *plugins: Plugin) -> None:
    """Validate the plugins with the real loaded set, as the loader would, then add them to the engine's registry
    for one test (monkeypatch undoes it)."""
    Registry().fill([REGISTRY.plugins[n] for n in REGISTRY.order] + list(plugins))
    for p in plugins:
        monkeypatch.setitem(REGISTRY.plugins, p.name, p)
        for c in p.contracts:
            monkeypatch.setitem(REGISTRY.contracts, c.name, c)
            monkeypatch.setitem(REGISTRY.contract_plugin, c.name, p.name)
        for s in p.services:
            monkeypatch.setitem(REGISTRY.services, f"{p.name}.{s.name}", s)


def install(monkeypatch, engine, *plugins: Plugin) -> None:
    """Register the plugins and install them into the test books."""
    from erpclaw_core.plugins import install_record

    register(monkeypatch, *plugins)
    for p in plugins:
        with engine.begin() as conn:
            install_record(conn, p)
