"""The plain-text sweep, statically: every refusal text and every card.

Every refusal text: every entry of the assistant and core language tables,
its placeholders filled with representative values (a name with an underscore and a hyphen, a number, a date); and,
until the last literal moves into the table, every `Refusal(...)` the engine's source constructs, listed one test
each, its text read from the source (a literal, an f-string with its placeholders filled, a module constant, a
planned reply) and checked with its own code. A construction whose text is computed at run time is listed as such.
Beside them, every reply the capability catalogue can give and every operation's planned reply and plain words.

Every card: for each write the registry holds, a card made from a scripted request on the demo books, line
by line (a new write without a case here fails), and every card fixture of the plugin interface (plugin/4).

A text that breaks a rule fails with its source location and the rule."""
from __future__ import annotations

import ast
import importlib
import json
from pathlib import Path

import pytest

from erpclaw_assistant import resolve, resolve_text
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import catalogue as cat
from erpclaw_core import core_contracts, plugin_text
from erpclaw_core.contracts import planned_reply, planned_text
from erpclaw_core.plugins import REGISTRY, load_plugins
from owner_text import ROOT, FIELD, event_violations, fill, owner_strings, owner_texts, owner_text_violations, refusal_calls, refusal_code
from owner_text import DeferredOwnerText, defer_owner_text, observe_refusal_codes, refusal_code_report

FILLS = ("Acme_Trading-2", "1,250.00", "26 Oct 2026")  # a name with an underscore and a hyphen, a number, a date
EXCEPTION_VARS = {"e", "exc", "err", "error", "ex"}


def checked(texts: list[str], code: str | None = None) -> list[str]:
    return [f"{t!r} -> {v}" for t in texts if (v := owner_text_violations(t, code=code))]


def xfail_marked(cases: dict, known: dict) -> list:
    return [pytest.param(k, marks=pytest.mark.xfail(strict=True, raises=DeferredOwnerText, reason=known[k])) if k in known else k
            for k in cases]


# --- the language table ----------------------------------------------------------------------------------

LANGUAGE_TABLES = {
    resolve_text.__name__: {lang: table["text"] for lang, table in resolve_text.TABLES.items()},
    plugin_text.__name__: plugin_text.TEXT,
    core_contracts.__name__: core_contracts.TEXT,
}


def _entries() -> dict[str, str]:
    out = {}
    for module, languages in LANGUAGE_TABLES.items():
        for lang, table in languages.items():
            for key, value in table.items():
                values = value.items() if isinstance(value, dict) else [("", value)]
                for sub, text in values:
                    out[f"{module}.{lang}.{key}{'.' + str(sub) if sub != '' else ''}"] = text
    return out


TABLE = _entries()
TABLE_XFAIL: dict[str, str] = {}


@pytest.mark.parametrize("key", xfail_marked(TABLE, TABLE_XFAIL))
def test_every_language_table_entry_reads_plainly_with_its_placeholders_filled(key):
    template = TABLE[key]
    bad = checked([fill(template, v) for v in FILLS] if FIELD.search(template) else [template])
    assert not bad, bad


# --- every Refusal(...) the source constructs ------------------------------------------------------------

def _module_constants(tree: ast.Module) -> dict[str, str]:
    out = {}
    for n in tree.body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            try:
                value = ast.literal_eval(n.value)
            except ValueError:
                continue
            if isinstance(value, str):
                out[n.targets[0].id] = value
    return out


def _placeholder(node: ast.FormattedValue, value: str) -> str:
    """A placeholder's representative value: an exception's type name where the placeholder names one, a contract's
    registered name where it names one, else the owner's value."""
    expr = ast.unparse(node.value)
    if expr in ("n", "line_no"):
        return "2"  # document line ordinals are integers
    if expr.endswith("__name__") and isinstance(node.value, ast.Attribute) and isinstance(node.value.value, ast.Call) \
            and node.value.value.args and ast.unparse(node.value.value.args[0]) in EXCEPTION_VARS:
        return "TypeError"
    if expr in ("contract.name", "c.name"):
        return next(iter(REGISTRY.contracts))
    return repr(value) if node.conversion == ord("r") else value


def _say_table(path: Path, name: str) -> dict:
    """Follow the calling module's binding to the function that owns its wording."""
    source = next(parent for parent in path.parents if parent.name == "src")
    parts = path.relative_to(source).with_suffix("").parts
    module = importlib.import_module(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
    provider = importlib.import_module(getattr(module, name).__module__)
    return LANGUAGE_TABLES[provider.__name__][provider.LANGUAGE]


def _texts(node: ast.expr, constants: dict[str, str], value: str, path: Path) -> list[str] | None:
    """The texts a Refusal's message expression can give, or None when it is computed at run time."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        return ["".join(v.value if isinstance(v, ast.Constant) else _placeholder(v, value) for v in node.values)]
    if isinstance(node, ast.Name) and node.id in constants:
        return [constants[node.id]]
    if isinstance(node, ast.IfExp):
        a, b = _texts(node.body, constants, value, path), _texts(node.orelse, constants, value, path)
        return None if a is None or b is None else a + b
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = _texts(node.left, constants, value, path), _texts(node.right, constants, value, path)
        return None if a is None or b is None else [x + y for x in a for y in b]
    if isinstance(node, ast.Call) and getattr(node.func, "id", None) in ("_say", "say") and node.args \
            and isinstance(node.args[0], ast.Constant):
        return [fill(_say_table(path, node.func.id)[node.args[0].value], value)]
    if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "planned_text" and node.args \
            and isinstance(node.args[0], ast.Constant):
        return [planned_text(node.args[0].value, "Core"), planned_text(node.args[0].value, "v3.1")]
    return None


def _constructions() -> dict[str, tuple[list[str] | None, str | None, str]]:
    """Every Refusal construction: its id (file, code, ordinal), its texts (None: computed at run time), its code and
    its source location."""
    out: dict = {}
    trees: dict[Path, ast.Module] = {}
    for path, node in refusal_calls():
        tree = trees.setdefault(path, ast.parse(path.read_text(encoding="utf-8")))
        code_node = refusal_code(node)
        code = "refused" if code_node is None else code_node.value if isinstance(code_node, ast.Constant) else None
        msg = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "message"), None)
        texts = None
        if msg is not None:
            per_fill = [_texts(msg, _module_constants(tree), v, path) for v in FILLS]
            texts = None if per_fill[0] is None else list(dict.fromkeys(t for ts in per_fill for t in ts))
        rel = path.relative_to(ROOT)
        base = f"{path.name}:{code or ast.unparse(code_node)}"
        n = sum(1 for k in out if k.rsplit("#", 1)[0] == base)
        out[f"{base}#{n}"] = (texts, code, f"{rel}:{node.lineno}")
    return out


load_plugins()
REFUSALS = _constructions()


def test_the_refusals_are_found_and_listed():
    literal = [k for k, (texts, _, _) in REFUSALS.items() if texts is not None]
    computed = [f"{k} at {where}" for k, (texts, _, where) in REFUSALS.items() if texts is None]
    assert len(REFUSALS) > 150 and len(literal) > 0.9 * len(REFUSALS), (len(REFUSALS), len(literal))
    print(f"\n{len(literal)} Refusal texts read from the source; computed at run time ({len(computed)}):",
          *computed, sep="\n  ")


REFUSAL_XFAIL = {
}

REFUSAL_DEFERRED = {
}


@pytest.mark.parametrize("key", xfail_marked(REFUSALS, REFUSAL_XFAIL))
def test_every_refusal_text_in_the_source_reads_plainly(key):
    texts, code, where = REFUSALS[key]
    if texts is None:
        pytest.skip(f"computed at run time ({where}): reached by the turn sweep, or by its own feature's test")
    bad = checked(texts, code)
    if key in REFUSAL_DEFERRED:
        defer_owner_text(texts, REFUSAL_DEFERRED[key], code=code)
    assert not bad, (where, bad)


def test_every_planned_reply_and_every_operations_plain_words_read_plainly():
    texts = []
    for c in REGISTRY.contracts.values():
        texts += [c.plain] if c.plain else []
        for wave in ("Core", "v3.1"):
            c.planned, before = wave, c.planned
            try:
                texts.append(planned_reply(c))
            finally:
                c.planned = before
    bad = checked(texts)
    assert not bad, bad


def test_every_reply_the_catalogue_gives_reads_plainly(engine):
    with engine.connect() as conn:
        entries, on_now = cat.catalogue(conn), cat.switched_on(conn)
    texts = [on_now]
    for e in entries:
        for pick in (e.id, "none", "unsure"):
            texts.append(cat.decide([e], pick, on_now).text)
    texts.append(cat.decide(entries[:3], "unsure", on_now).text)
    texts.append(cat.decide([], "none", on_now).text)
    bad = checked([t for t in dict.fromkeys(texts) if t])  # outcome a (run it) has no text
    assert not bad, bad


# --- every card --------------------------------------------------------------------------------------------

def _paid_number(engine) -> str:
    from decimal import Decimal

    from sqlalchemy import select

    from erpclaw_core import models as m
    from erpclaw_core.common import company_ctx
    from erpclaw_sales import service as s
    from erpclaw_sales import tables as t

    with engine.begin() as conn:
        ctx = company_ctx(conn)
        cust = dict(conn.execute(select(m.party).where(m.party.c.name == "Hooli")).mappings().one())
        r = s.record_payment(ctx, s.plan_payment(ctx, cust, Decimal("100"), ctx.today, oldest_first=True))
        return conn.execute(select(t.payment.c.number).where(t.payment.c.id == r["id"])).scalar_one()


def _draft_number(engine) -> str:
    from sqlalchemy import select

    from erpclaw_sales import tables as t

    with engine.connect() as conn:
        return conn.execute(select(t.invoice.c.number).where(t.invoice.c.status == "draft")).scalars().first()


def _open_number(engine) -> str:
    from decimal import Decimal

    from sqlalchemy import select

    from erpclaw_sales import tables as t

    with engine.connect() as conn:
        return conn.execute(select(t.invoice.c.number).where(t.invoice.c.status == "submitted",
                                                             t.invoice.c.outstanding > Decimal("0"))
                            .order_by(t.invoice.c.number.desc())).scalars().first()


CARDS = {  # each write the registry holds -> (the owner's words, the model's call), from the demo books
    "create_invoice": lambda e: ("invoice Acme Corporation for 5 copper wire",
                                 call("create_invoice", customer="Acme Corporation",
                                      items=[{"item": "copper wire", "quantity": 5}])),
    "submit_invoice": lambda e: (f"submit {(n := _draft_number(e))}", call("submit_invoice", invoice=n)),
    "record_payment": lambda e: ("Pied Piper paid 100 by check, oldest first",
                                 call("record_payment", customer="Pied Piper", amount=100, oldest_first=True,
                                      method="check")),
    "cancel_invoice": lambda e: (f"cancel {(n := _draft_number(e))}", call("cancel_invoice", invoice=n)),
    "reverse_payment": lambda e: (f"reverse {(n := _paid_number(e))}", call("reverse_payment", payment=n)),
}
CARDS_XFAIL: dict[str, str] = {}


def test_every_write_has_a_card_case():
    writes = {n for n, c in REGISTRY.contracts.items() if c.effect != "read"}
    cases = set(CARDS) | FIXTURE_CARDS
    assert cases == writes, f"a write with no card case: {writes - cases}; a case for no write: {cases - writes}"


@pytest.mark.parametrize("name", xfail_marked(CARDS, CARDS_XFAIL))
def test_every_writes_card_reads_plainly_line_by_line(engine, name):
    said, model_call = CARDS[name](engine)
    events = Assistant(engine, ScriptedModel([Reply("", [model_call])] * 2)).turn("c-" + name, said)
    card = next((e for e in events if e["type"] == "card"), None)
    assert card is not None, events
    bad = event_violations(events)
    assert not bad, bad


V4 = sorted((ROOT / "tests" / "contracts" / "plugin" / "v4").glob("card_*.json"))


@pytest.mark.parametrize("path", V4, ids=lambda p: p.stem)
def test_every_card_fixture_of_the_plugin_interface_reads_plainly(path, engine):
    from erpclaw_core import approvals
    from erpclaw_core.common import company_ctx
    from test_write_contract import _Dated, _Dateless
    from test_plugin_contract_v4 import _built

    fx = json.loads(path.read_text())
    contract = _built(fx["write"])
    with engine.connect() as conn:
        card = approvals._preview(company_ctx(conn), contract, _Dateless() if fx["write"]["dateless"] else _Dated(),
                                  date_source={"not_applicable": True})
    bad = event_violations([{"card": card}])
    assert not bad, bad


FIXTURE_CARDS = {
    "record_opening_position",
    "set_up_company", "add_customer", "add_supplier", "add_cash_account", "add_item", "update_email_sender", "resend_message",
    "create_sales_order", "deliver_order", "create_credit_note",
    "record_opening_stock", "write_off_stock", "record_stock_count", "cancel_stock_movement",
    "reverse_supplier_payment", "reverse_goods_received",
}


def feature_fixture(filename):
    """Load an existing feature's fixture without changing pytest's search path."""
    import importlib.util

    path = ROOT / "tests" / filename
    spec = importlib.util.spec_from_file_location("owner_fixture_" + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", sorted(FIXTURE_CARDS))
def test_each_remaining_contracts_fixture_card_reads_plainly_line_by_line(engine, monkeypatch, request, name):
    import inspect

    from erpclaw_core import approvals

    stock_fixtures = {
        "record_opening_stock": "opening_stock", "write_off_stock": "write_off",
        "record_stock_count": "stock_counts", "cancel_stock_movement": "cancellation",
    }
    contract = REGISTRY.contracts[name]
    if name in stock_fixtures:
        module = feature_fixture("stock/test_" + stock_fixtures[name] + "_unit.py")
        world = module.world.__wrapped__(monkeypatch)
        card = approvals._preview(world.ctx, contract, module.inputs())
    else:
        company = None
        if name == "set_up_company":
            from test_own_books import own_books, SETUP
            own_books(engine)
            args = SETUP
        elif name in ("add_customer", "add_supplier"):
            from core_writes.scenarios import NO_MONEY_SCENARIOS
            args, _ = NO_MONEY_SCENARIOS[name](request, engine)
        elif name == "resend_message":
            from core_writes.scenarios import resend_message

            args, _ = resend_message(request, engine)
        elif name == "add_cash_account":
            args = {}
        elif name == "record_opening_position":
            from core_writes.scenarios import MONEY_SCENARIOS
            args, _ = MONEY_SCENARIOS[name](request, engine)
        elif name == "update_email_sender":
            module = feature_fixture("test_email_sender_settings.py")
            module.configured.__wrapped__(engine)
            args = module.ARGS
            monkeypatch.setattr(module.smtp, "company_password", lambda company: None)
        elif name == "add_item":
            args = {"name": "Consulting", "unit": "hour", "price": "80.00", "kind": "service"}
        elif name == "create_credit_note":
            args = {"invoice": _open_number(engine), "everything": True}
        elif name == "reverse_supplier_payment":
            module = feature_fixture("test_supplier_payment_reversal_contract.py")
            seed = module.seed_supplier_payment(engine)
            args = {"payment": seed["number"]}
        elif name == "reverse_goods_received":
            module = feature_fixture("stock/test_goods_received_reversal_contract.py")
            seed = module.seed_delivery(engine)
            args = {"delivery": seed["number"]}
        else:
            args = {"customer": "Hooli", "items": [{"item": "Copper Wire", "quantity": 5}],
                    "delivery_date": "2026-10-27"}
            if name == "deliver_order":
                first = approvals.propose(engine, REGISTRY.contracts["create_sales_order"], args)
                done = approvals.approve(engine, first.approval_id, first.digest)
                assert done.status == "committed", done
                args = {"order": done.data["number"]}
        card = approvals.propose(engine, contract, args, company_id=company).card
    strings = owner_strings({"card": card})
    assert strings and card["title"] and card["lines"], card
    if name == "resend_message":
        assert not event_violations([{"type": "card", "card": card}])
        return
    preview = inspect.unwrap(contract.preview)
    where = f"{inspect.getsourcefile(preview)}:{inspect.getsourcelines(preview)[1]}"
    for text, preview_line in owner_texts({"card": card}):
        assert not (bad := owner_text_violations(text, card_preview_line=preview_line)), (where, text, bad)
