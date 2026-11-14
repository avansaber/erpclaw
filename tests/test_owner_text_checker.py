"""The owner-facing text checker itself: each rule of tests/owner_text_rules.json fires on a
planted example and passes its allowed counterpart, and the allow list is never flagged.

Plain, no database: the registries come from the loaded plugins, the SQLAlchemy metadata and the engine's source."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from owner_text import event_violations, owner_text_violations, registry, rules  # tests/ is on the path (pytest rootdir)

RULES = Path(__file__).with_name("owner_text_rules.json")


def fired(text: str, **kw) -> set[int]:
    """The rule numbers a text breaks."""
    return {int(re.match(r"rule (\d+)", v).group(1)) for v in owner_text_violations(text, **kw)}


def test_resend_message_id_allowance_is_one_exact_card_line():
    value = "<3f2b8c1e-9a4d-4c2b-8e7f-1a2b3c4d5e6f@example.test>"
    def card(title="Send this message again", label="Message-ID", **extra):
        return {"type": "card", "card": {"title": title, "lines": [[label, value]], **extra}}
    assert event_violations([card()]) == []
    assert fired(value) == {5}
    assert event_violations([card(title="Another card")])
    assert event_violations([card(label="Message identity")])
    assert event_violations([card(warnings=["Message-ID " + value])])
    assert event_violations([{"type": "answer", "text": "Message-ID " + value}])
    assert event_violations([card(lines=[["Subject", value], ["Message-ID", value]])])


def test_resend_message_id_allowance_rejects_duplicate_lines():
    values = [
        "<3f2b8c1e-9a4d-4c2b-8e7f-1a2b3c4d5e6f@example.test>",
        "<4f2b8c1e-9a4d-4c2b-8e7f-1a2b3c4d5e6f@example.test>",
    ]
    event = {"type": "card", "card": {
        "title": "Send this message again",
        "lines": [["Message-ID", value] for value in values],
    }}
    violations = event_violations([event])
    assert len(violations) == 2
    for value, violation in zip(values, violations):
        assert value in violation and "rule 5" in violation


# --- the rule file -------------------------------------------------------------------------------------------------

def test_the_rule_file_holds_rules_one_to_seven_and_every_allow_entry_has_a_reason():
    data = json.loads(RULES.read_text())
    assert [r["rule"] for r in data["rules"]] == [1, 2, 3, 4, 5, 6, 7]
    for r in data["rules"]:
        assert r["name"] and r["patterns"], r
        for p in r["patterns"]:
            assert p["id"] and p["flags"] in ("", "i"), p
            re.compile(p["regex"].replace("{libraries}", "x"))
    ids = [a["id"] for a in data["allow"]]
    assert {"document_number", "account_number", "version_number", "allowed_url", "owner_data"} <= set(ids)
    assert all(a.get("reason", "").strip() for a in data["allow"]), "every allow entry needs its reason"


def test_the_registries_are_read_from_the_engine_not_copied():
    reg = registry()
    assert {"create_invoice", "record_payment", "get_metric"} <= reg["contracts"]
    assert {"find_capability", "choose_capability"} <= reg["tools"]
    assert {"sales_invoice", "ledger_entry", "approval"} <= reg["tables"]
    assert {"functional_currency", "voucher_type", "company_id"} <= reg["columns"]
    assert {"oldest_first", "customer", "items"} <= reg["contract_fields"]
    assert {"year_end_close", "sales_invoice", "stock_receipt"} <= reg["voucher_types"]
    assert "stock.issue" in reg["services"]
    assert "uq_posting_voucher" in reg["constraints"]
    # every Refusal code the source constructs, every event code, the API's codes
    assert {"no_company", "not_a_write", "digest", "fenced", "model_unavailable"} <= reg["refusal_codes"]
    assert {"card_set_aside", "card_restored_away", "card_already_ended"} <= reg["event_codes"]
    assert {"internal_error", "no_conversation", "invalid_request"} <= reg["api_error_codes"]
    assert any(p.fullmatch("date_choice_missing") for p in reg["code_patterns"])  # an f-string code, as a pattern


# --- each rule fires on a planted example and passes its allowed counterpart ---------------------------------------

PLANTED = [  # (rule, a text that breaks it, a plain counterpart that does not)
    (1, 'Traceback (most recent call last):', "Something went wrong; nothing was saved."),
    (1, 'File "/srv/app.py", in handler', "The file was stored."),
    (1, "error at line 42", "The invoice has two lines."),
    (1, "failed in approvals.py", "The approval failed; nothing was saved."),
    (1, "could not load Engine.dll", "The engine could not start."),
    (1, "see HostBridge.cs", "See the help page."),
    (2, "ValueError: bad amount", "That amount isn't valid."),
    (2, "a KeyError was raised", "That key was not found."),
    (2, "RuntimeException in the host", "The host stopped."),
    (2, "sqlalchemy could not connect", "The books could not be reached."),
    (2, "psycopg refused the connection", "The books refused the connection."),
    (2, "pydantic rejected the field", "That value was not accepted."),
    (2, "httpx timed out", "The request timed out."),
    (2, "openai returned nothing", "OpenAI's model returned nothing."),
    (2, "System.IO failed", "The system could not read the file."),
    (2, "Microsoft.Web.WebView2 is missing", "Microsoft Edge WebView2 is missing; install it first."),
    (3, "SELECT id FROM approval WHERE x", "Select an account from the list."),
    (3, "INSERT INTO party VALUES", "Insert the card into the reader."),
    (3, 'UPDATE "approval" SET status', "Update the price, then set it again."),
    (3, "DELETE FROM outbox", "Delete it from the list."),
    (3, "CREATE TABLE sales_payment", "Create a new invoice."),
    (3, "ALTER ROLE erpclaw_rt", "Alter the role of the user."),
    (3, "SQLSTATE 23505", "The code 23505 is the owner's own figure."),
    (3, "the sales_invoice_line row", "the invoice line"),
    (3, "violates uq_posting_voucher", "That document was already posted."),
    (4, "refused: not_a_write", "That isn't a change I can draft."),
    (4, "card_set_aside", "Your card was set aside."),
    (4, "error code: internal_error", "The engine hit an internal error."),
    (4, "no_conversation", "That conversation was not found."),
    (4, "customer_unknown", "I can't find that customer."),
    (4, "the date_choice_missing step", "Shall I date it today?"),
    (4, "code: busy", "I'm busy with your last message."),
    (4, "refused (`fenced`)", "That request has ended; ask again."),
    (5, "card 3f2b8c1e-9a4d-4c2b-8e7f-1a2b3c4d5e6f", "card INV-2026-00001"),
    (5, "digest 9f86d081884c7d659a2feaa0c55ad015", "account number 1234567890123456"),
    (5, "approval_id=12", "the approval for 12.00 USD"),
    (5, "id=7", "seven of them"),
    (5, '{"id": 7}', "item 7"),
    (5, "the company_id was wrong", "the company was wrong"),
    (5, "planned.op.record_payment", "Recording a payment is planned for a later release."),
    (5, "call create_invoice", "create an invoice"),
    (5, "oldest_first was not set", "apply it to the oldest invoices first"),
    (5, "functional_currency missing", "the company's currency is missing"),
    (5, "a year_end_close posting", "the year-end close"),
    (5, "invoice.customer is empty", "the invoice's customer is empty"),
    (5, "stock.issue failed", "The stock issue failed."),
    (5, "the field `items`", "the items on the invoice"),
    (6, r"saved to C:\Users\owner\books", "saved to your ERPClaw folder"),
    (6, r"the folder ERPClaw\backups", "the backups folder"),
    (6, "/home/ubuntu/erpclaw", "your home folder"),
    (6, "/tmp/x", "a temporary file"),
    (6, "connect to 127.0.0.1", "connect to the engine"),
    (6, "localhost refused", "the engine refused"),
    (6, "engine:8765 is down", "the meeting is at 10:30"),
    (6, r"\\.\pipe\erpclaw", "the engine's pipe"),
    (6, "see https://example.com/help", "see the help page"),
    (6, "postgresql://erpclaw@db/books", "the books' database"),
    (7, " ".join(("-----BEGIN RSA", "PRIVATE KEY-----")), "a private key"),
    (7, "".join(("sk-", "proj-", "a" * 24)), "a key from your provider"),
    (7, "".join(("AKIA", "ABCDEFGHIJKLMNOP")), "an access key"),
    (7, "".join(("ghp_", "a1" * 16)), "a GitHub token"),
    (7, "".join(("Bearer ", "x" * 24)), "the bearer of this invoice"),
    (7, ": ".join(("password", "hunter2")), "your password is set"),
    (7, "=".join(("api_key", "abc")), "an API key is needed"),
    (7, "".join(("LLM_123456_", "ab" * 16)), "your Muse key"),
    (7, "".join(("erpclaw-planted-secret-", "0a1b2c3d")), "a planted value"),
]


@pytest.mark.parametrize("rule, bad, good", PLANTED, ids=[f"rule{r}-{i}" for i, (r, _, _) in enumerate(PLANTED)])
def test_each_rule_fires_on_its_planted_example_and_passes_its_counterpart(rule, bad, good):
    assert rule in fired(bad), (bad, owner_text_violations(bad))
    assert owner_text_violations(good) == [], (good, owner_text_violations(good))


def test_the_responses_own_code_fails_as_a_whole_word_and_only_as_one():
    assert 4 in fired("Refused: message_overtaken.", code="message_overtaken")
    assert 4 in fired("Not saved [digest].", code="digest")
    assert owner_text_violations("Message overtaken by another.", code="message_overtaken") == []
    assert owner_text_violations("Nothing was saved.", code="nothing_saved_here") == []


def test_a_planted_value_fails_wherever_it_appears():
    secret = "q9-planted-" + "Z" * 12
    assert 7 in fired(f"Your key {secret} was saved.", planted=[secret])
    assert owner_text_violations("Your key was saved.", planted=[secret]) == []


# --- the allow list is never flagged -------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Saved GR-2026-00001.", "SINV-2026-00012 is paid.", "SPAY-2026-00003 was reversed.",
    "Invoice INV-2026-00008 for Acme Corporation: 140.00 USD.",
    "Received into Bank (1100).", "Accounts Receivable (1200) holds 4,870.00 USD.",
    "ERPClaw 3.0.3 is ready to install.", "Updated from 3.0.2 to 3.0.3.",
    "Acme_Trading-2 owes 1,250.00 USD since 26 Oct 2026.",
])
def test_the_allow_list_is_never_flagged(text):
    assert owner_text_violations(text) == [], owner_text_violations(text)


def test_an_allowed_span_hides_only_itself():
    assert 5 in fired("INV-2026-00001 card 3f2b8c1e-9a4d-4c2b-8e7f-1a2b3c4d5e6f")
    assert 6 in fired("3.0.3 from https://example.com/x")


def test_a_url_named_on_the_allow_list_passes(monkeypatch):
    data = rules()
    allowed = next(a for a in data["allow"] if a["id"] == "allowed_url")
    monkeypatch.setitem(allowed, "urls", ["https://help.erpclaw.example/keys"])
    assert owner_text_violations("Get a key at https://help.erpclaw.example/keys today.") == []
    assert 6 in fired("Get a key at https://help.erpclaw.example.evil/keys today.")


def test_every_violation_names_its_rule_its_pattern_and_the_text():
    [v] = owner_text_violations("ValueError")
    assert v.startswith("rule 2 (exception_name): ") and "ValueError" in v


def test_a_document_line_number_is_allowed_but_a_source_line_still_fails():
    text = "Only 2 of Copper Wire on line 3 can still come back into stock."
    assert owner_text_violations(text) == []
    assert 1 in fired("The handler failed on line 3.")
    assert 1 in fired(text + " The handler failed on line 3.")


def test_a_card_preview_line_number_is_allowed_only_at_the_start():
    text = "line 1: 40 x Copper Wire @ 28.00 = 1,120.00"
    assert event_violations([{"type": "card", "card": {"lines": [["Credit", text]]}}]) == []
    assert 1 in fired(text)
    assert 1 in fired("see line 42 of x.py", card_preview_line=True)
    assert 1 in fired("Credit " + text, card_preview_line=True)
    assert 1 in fired(text + " see line 42", card_preview_line=True)


@pytest.mark.parametrize("rule,bad", sorted({rule: bad for rule, bad, _ in PLANTED}.items()))
def test_a_card_prefix_leaves_every_rule_checking_the_rest_of_the_line(rule, bad):
    assert rule in fired("line 1: " + bad, card_preview_line=True)


def test_a_card_line_with_an_exception_still_fails_the_exception_rule():
    assert fired("line 1: KeyError missing item", card_preview_line=True) == {2}
    assert event_violations([{"type": "card", "card": {"lines": [["Credit", "line 1: KeyError"]]}}])


@pytest.mark.parametrize("event", [
    {"type": "answer", "text": "line 1: Copper Wire"},
    {"type": "card", "text": "line 1: Copper Wire"},
    {"type": "card", "card": {"title": "line 1: Copper Wire"}},
    {"type": "card", "card": {"footer": "line 1: Copper Wire"}},
    {"type": "card", "card": {"books": ["line 1: Copper Wire"]}},
    {"type": "card", "card": {"warnings": ["line 1: Copper Wire"]}},
    {"type": "card", "card": {"details": ["line 1: Copper Wire"]}},
])
def test_a_card_line_prefix_in_any_other_owner_text_still_fails_rule_one(event):
    assert any("rule 1 (line_number)" in v for v in event_violations([event]))


def invoice_line_question(*labels):
    return {"type": "ask", "code": "invoice_line_ambiguous",
            "text": "Copper Wire is on 2 lines of INV-2026-00001: " + "; ".join(labels) + ". Which line?",
            "options": list(labels), "choices": [{"label": label} for label in labels],
            "data": {"choices": [{"label": label} for label in labels]}}


def test_invoice_line_question_allows_its_rendered_segments_and_choice_labels():
    event = invoice_line_question("line 1: 5 x Copper Wire @ 28.00", "line 2: 3 x Copper Wire @ 28.00")
    assert event_violations([event]) == []
    assert 1 in fired(event["text"], code=event["code"])


def test_invoice_line_question_still_rejects_a_traceback_after_the_prefix():
    from owner_text import event_text_violations

    event = invoice_line_question("line 3: Traceback (most recent call last):")
    for text, violations in event_text_violations(event):
        assert any("rule 1 (traceback)" in v for v in violations), (text, violations)
        assert not any("rule 1 (line_number)" in v for v in violations)


@pytest.mark.parametrize("rule,bad", sorted({rule: bad for rule, bad, _ in PLANTED}.items()))
@pytest.mark.parametrize("field", ["text", "options", "choices", "data"])
def test_invoice_line_prefix_leaves_every_rule_checking_the_whole_text(rule, bad, field):
    from owner_text import event_text_violations

    event = invoice_line_question("line 3: " + bad)
    # Isolate each surface so another copy cannot conceal a missing check.
    event = {key: value for key, value in event.items() if key in ("type", "code", field)}
    if field == "text":
        event["options"] = ["line 3: " + bad]
    checked = event_text_violations(event)
    if field == "text":
        checked = [(text, found) for text, found in checked if text == event["text"]]
    assert checked
    assert all(any(f"rule {rule} (" in v for v in found) for _, found in checked)


@pytest.mark.parametrize("event_type,code", [
    ("ask", "customer_ambiguous"), ("ask", "invoice_line_unknown"), ("ask", None),
    ("answer", "invoice_line_ambiguous"), ("refused", "invoice_line_ambiguous"),
    (None, "invoice_line_ambiguous"),
])
def test_invoice_line_prefix_requires_both_the_question_type_and_exact_code(event_type, code):
    event = invoice_line_question("line 3: Copper Wire")
    event.update(type=event_type, code=code)
    assert any("rule 1 (line_number)" in v for v in event_violations([event]))


@pytest.mark.parametrize("text", [
    "line 3: Copper Wire", "Credit line 3: Copper Wire",
    "Copper Wire: see line 3: Copper Wire", "Copper Wire: line 3: Copper Wire see line 42",
])
def test_invoice_question_masks_only_the_start_of_a_rendered_label_segment(text):
    event = invoice_line_question("line 3: Copper Wire")
    event["text"] = text
    assert any("rule 1 (line_number)" in v for v in event_violations([event]))


@pytest.mark.parametrize("field", ["title", "footer", "books", "warnings", "details", "lines", "steps"])
def test_invoice_question_prefix_is_not_allowed_in_other_fields(field):
    event = invoice_line_question("line 3: Copper Wire")
    label = event["options"][0]
    if field == "steps":
        event[field] = [{"asked": label, "returned": label}]
    else:
        value = [[label]] if field == "lines" else [label] if field in ("books", "warnings", "details") else label
        event["card"] = {field: value}
    assert any("rule 1 (line_number)" in v for v in event_violations([event]))


def test_inventory_keeps_a_violation_when_the_same_words_are_also_an_allowed_choice():
    from owner_text import check_inventory

    label = "line 3: Copper Wire"
    event = invoice_line_question(label)
    event["text"] = label
    entry = {"text": label, "code": event["code"], "event": event, "file": __file__, "line": 1}
    with pytest.raises(AssertionError, match="unclassified or changed owner text"):
        check_inventory([entry], "repeated-owner-text", expected=[])


def owner_sweep_xfail_marks(request, *, all_collected=False):
    """Inspect collected cases and declarations, even when only this file runs."""
    import importlib
    import inspect

    modules = ("test_owner_text_checker", "test_owner_text_sweep_static",
               "test_owner_text_sweep_turns", "test_owner_text_sweep_routes")
    all_marks = [mark for item in request.session.items if all_collected or item.module.__name__ in modules
                 for mark in item.iter_markers("xfail")]
    for module_name in modules:
        module = importlib.import_module(module_name)
        for name, fn in vars(module).items():
            if not name.startswith("test_") or not inspect.isfunction(fn): continue
            for mark in getattr(fn, "pytestmark", []):
                if mark.name == "xfail": all_marks.append(mark)
                if mark.name == "parametrize":
                    for parameter in mark.args[1]:
                        for parameter_mark in getattr(parameter, "marks", ()):
                            if parameter_mark.name != "xfail": continue
                            all_marks.append(parameter_mark)
                            if parameter_mark.kwargs.get("reason", "").startswith("pending a ruling"):
                                assert parameter.id == parameter_mark.kwargs["case"]
    return all_marks


def test_every_strict_xfail_reason_names_its_fix(request):
    phrases = ("awaiting the model text screen",)
    for mark in owner_sweep_xfail_marks(request):
        if mark.kwargs.get("strict") is True:
            assert mark.kwargs.get("reason", "").startswith(phrases), mark


def test_every_strict_xfail_accepts_only_verified_owner_text(request):
    from owner_text import DeferredOwnerText

    marks = owner_sweep_xfail_marks(request)
    assert marks
    for mark in marks:
        assert mark.kwargs.get("strict") is True, mark
        assert mark.kwargs.get("raises") is DeferredOwnerText, mark


@pytest.mark.parametrize("module,name", [
    ("erpclaw_assistant.resolve", "_say"),
    ("erpclaw_assistant.resolve_text", "_say"),
    ("erpclaw_assistant.resolve_amounts", "_say"),
    ("erpclaw_assistant.resolve_args", "_say"),
    ("erpclaw_assistant.resolve_dates", "_say"),
    ("erpclaw_assistant.resolve_inventory", "_say"),
    ("erpclaw_assistant.resolve_payment", "_say"),
    ("erpclaw_assistant.resolve_policy", "_say"),
    ("erpclaw_assistant.resolve_records", "_say"),
    ("erpclaw_assistant.resolve_state", "_say"),
    ("erpclaw_assistant.graph", "say"),
    ("erpclaw_core.plugins", "say"),
    ("erpclaw_core.files", "say"),
    ("erpclaw_assistant.muse", "say"),
    ("erpclaw_core.core_contracts", "say"),
])
def test_static_say_lookup_follows_the_calling_modules_import(module, name):
    import importlib
    import test_owner_text_sweep_static as static

    caller = importlib.import_module(module)
    provider = importlib.import_module(getattr(caller, name).__module__)
    table = provider.table()["text"] if provider is static.resolve_text else provider.TEXT[provider.LANGUAGE]
    assert static._say_table(Path(caller.__file__), name) is table


def test_every_say_call_resolves_through_its_actual_module_binding():
    import ast
    import importlib
    from owner_text import source_files
    import test_owner_text_sweep_static as static

    calls = {(path, node.func.id) for path in source_files()
             for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id in ("say", "_say")}
    assert calls
    for path, name in calls:
        source = next(parent for parent in path.parents if parent.name == "src")
        parts = path.relative_to(source).with_suffix("").parts
        caller = importlib.import_module(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
        provider = importlib.import_module(getattr(caller, name).__module__)
        assert static._say_table(path, name) is static.LANGUAGE_TABLES[provider.__name__][provider.LANGUAGE]


@pytest.mark.parametrize("key", ["plugin_boundary_failed", "plugin_boundary_currency_unknown"])
def test_core_plugin_sentences_use_the_same_static_owner_text_rules(monkeypatch, key):
    import ast
    import test_owner_text_sweep_static as static
    from erpclaw_core import plugins, plugin_text

    # Mutate the table, then read it through both sweep paths. An internal
    # contract name must fail even in a sentence with currency placeholders.
    table = plugin_text.TEXT[plugin_text.LANGUAGE]
    monkeypatch.setitem(table, key, table[key] + " get_metric")
    entry = f"{plugin_text.__name__}.{plugin_text.LANGUAGE}.{key}"
    monkeypatch.setattr(static, "TABLE", static._entries())
    with pytest.raises(AssertionError, match="rule 5"):
        static.test_every_language_table_entry_reads_plainly_with_its_placeholders_filled(entry)
    texts = static._texts(ast.parse(f"say({key!r})", mode="eval").body, {}, static.FILLS[0],
                          Path(plugins.__file__))
    assert texts and all(5 in fired(text) for text in texts)


@pytest.mark.parametrize("case", ["static-service", "model"])
def test_a_crash_before_text_checking_is_reported_as_failed(request, monkeypatch, case):
    """Use pytest's real report hooks without starting another pytest or database run."""
    from _pytest.runner import CallInfo
    from _pytest.skipping import evaluate_xfail_marks, xfailed_key
    import test_owner_text_sweep_static as static
    import test_owner_text_sweep_turns as turns

    def crash(*args, **kwargs):
        raise RuntimeError("injected crash before the owner text check")

    if case.startswith("static-"):
        key = "plugins.py:service_output#1"
        fn = static.test_every_refusal_text_in_the_source_reads_plainly
        parameter = next(p for p in fn.pytestmark[0].args[1] if p == key or getattr(p, "values", ()) == (key,))
        mark = next((m for m in getattr(parameter, "marks", ()) if m.name == "xfail"), None)
        assert mark is None
        monkeypatch.setattr(static, "checked", crash)
        operation = lambda: fn(key)
    elif case == "model":
        fn = turns.test_a_model_that_misbehaves_never_puts_technical_text_in_front_of_the_owner
        parameter = next(p for p in fn.pytestmark[0].args[1] if getattr(p, "values", ()) ==
                         ("the model's words hold technical text",))
        mark = next(m for m in parameter.marks if m.name == "xfail")
        monkeypatch.setattr(turns, "chat", crash)
        operation = lambda: fn(None, parameter.values[0])
    item = pytest.Function.from_parent(request.node.parent, name="injected_crash", callobj=operation)
    if mark is not None:
        item.add_marker(pytest.mark.xfail(*mark.args, **mark.kwargs))
    item.stash[xfailed_key] = evaluate_xfail_marks(item)
    call = CallInfo.from_call(operation, when="call")
    assert isinstance(call.excinfo.value, RuntimeError)
    report = item.ihook.pytest_runtest_makereport(item=item, call=call)
    assert report.failed and not hasattr(report, "wasxfail"), report


@pytest.mark.parametrize("change", ["text", "rules", "plain"])
def test_deferral_requires_the_exact_enumerated_text_and_rules(monkeypatch, change):
    import owner_text

    text, expected = "KeyError", [("KeyError", [2])]
    if change == "text": text = "TypeError"
    elif change == "rules": monkeypatch.setattr(owner_text, "violation_rules", lambda *args: [5])
    else: text, expected = "All saved.", [("All saved.", [])]
    with pytest.raises(AssertionError) as caught:
        owner_text.defer_owner_text([text], expected)
    assert type(caught.value) is AssertionError


@pytest.mark.parametrize("missing", [False, True])
def test_runtime_coverage_waits_for_producers_and_still_rejects_missing_codes(monkeypatch, missing):
    import inspect
    from types import SimpleNamespace
    import owner_text
    from test_owner_text_sweep_routes import test_every_declared_refusal_code_was_reached_and_checked

    items = [SimpleNamespace(path=Path(__file__).with_name(f"test_owner_text_sweep_{part}.py"))
             for part in ("static", "turns", "routes")]
    session = SimpleNamespace(items=items, config=SimpleNamespace(
        args=[str(item.path) for item in items],
        option=SimpleNamespace(keyword="", markexpr="", deselect=[], lf=False)))
    monkeypatch.setattr(owner_text, "RUNTIME_REFUSALS", {})
    # Simulate the routes file running before the static and turn producers.
    early_check = test_every_declared_refusal_code_was_reached_and_checked
    arguments = {"request": SimpleNamespace(session=session), "refusal_code_report": {}}
    early_check(**{name: arguments[name] for name in inspect.signature(early_check).parameters})
    # The report must start before any producer runs; a missing code fails only at teardown.
    report = owner_text.refusal_code_report.__wrapped__(SimpleNamespace(session=session))
    reached = next(report)
    expected = owner_text.expected_refusal_codes()
    reached.update({code: {"early producer"} for code in expected})
    last = sorted(expected)[-1]
    del reached[last]
    if missing:
        with pytest.raises(AssertionError, match="refusal codes without a runtime case"):
            next(report)
    else:
        reached[last] = {"last producer"}
        with pytest.raises(StopIteration):
            next(report)


@pytest.mark.parametrize("selection", ["single-file", "keyword", "node", "marker", "deselected", "last-failed"])
def test_partial_owner_sweeps_explicitly_skip_runtime_coverage(selection):
    from types import SimpleNamespace
    from owner_text import runtime_coverage_skip_reason

    names = [f"test_owner_text_sweep_{part}.py" for part in ("static", "turns", "routes")]
    option = SimpleNamespace(keyword="", markexpr="", deselect=[], lf=False)
    args = list(names)
    if selection == "single-file": names = names[-1:]
    elif selection == "keyword": option.keyword = "refusal"
    elif selection == "node": args[-1] += "::test_every_declared_refusal_code_was_reached_and_checked"
    elif selection == "marker": option.markexpr = "slow"
    elif selection == "deselected": option.deselect = ["one-case"]
    elif selection == "last-failed": option.lf = True
    session = SimpleNamespace(items=[SimpleNamespace(path=Path(name)) for name in names],
                              config=SimpleNamespace(args=args, option=option))
    reason = runtime_coverage_skip_reason(session)
    assert reason and "partial owner-text selection" in reason, reason


def test_pending_ruling_xfails_equal_the_shared_inventory_exactly(request):
    from owner_text import PENDING_REASON, pending_entries

    entries = pending_entries()
    for entry in entries:
        assert set(entry) == {"text", "rules", "file", "line", "code", "case"}, entry
        assert entry["rules"] == sorted(set(entry["rules"])) and entry["rules"], entry
        assert fired(entry["text"], code=entry["code"]) == set(entry["rules"]), entry
        assert entry["file"] not in {
            "packages/erpclaw-core/src/erpclaw_core/plugins.py",
            "packages/erpclaw-core/src/erpclaw_core/files.py",
        } or not set(entry["rules"]) <= {2, 5}, entry
    keys = [(e["case"], e["file"], e["line"], e["code"], e["text"]) for e in entries]
    assert len(keys) == len(set(keys)), "duplicate pending violation"
    all_marks = owner_sweep_xfail_marks(request, all_collected=True)
    marks = [mark for mark in all_marks if mark.kwargs.get("reason", "").startswith("pending a ruling")]
    assert all(mark.kwargs.get("reason") == PENDING_REASON and mark.kwargs.get("strict") is True
               for mark in marks), marks
    actual = {mark.kwargs["case"] for mark in marks}
    expected = {entry["case"] for entry in entries}
    assert actual == expected, {"extra": actual - expected, "missing": expected - actual}


def test_the_pending_list_is_empty_before_closure():
    from owner_text import pending_entries

    assert pending_entries() == []
