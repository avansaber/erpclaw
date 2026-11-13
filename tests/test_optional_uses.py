"""Optional service dependencies: a plugin may name another plugin's service as optional, an entry of `uses` ending in
`?`; it is checked only when that plugin is loaded, the lifecycle never requires that plugin, and a call while it is
absent or disabled is the typed plugin_not_enabled refusal, never a silent no-op. One core function parses an entry,
and what "declares" means is answered by core alone."""
from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

import service_plugins as sp  # tests/ is on the path (pytest rootdir)
from erpclaw_core.common import Refusal, company_ctx
from erpclaw_core.manifest import Manifest
from erpclaw_core.plugins import (REGISTRY, Plugin, PluginRefused, Registry, call_service, check_installed,
                                  declares_service, disable_plugin, parse_use)

ENGINE = Path(__file__).resolve().parents[1]
QUOTE = f"{sp.PROVIDER}.quote"
RECORD = f"{sp.PROVIDER}.record"
EVERY = [f"{sp.PROVIDER}.{s.name}?" for s in sp.PROVIDER_SERVICES]
QUOTE_IN = {"amount": {"value": "10.00", "currency": "USD"}, "rate": "1.5"}


def optional_consumer(uses: list[str] | None = None, depends: dict | None = None) -> Plugin:
    """The planted consumer, naming the provider's services as optional instead of depending on it."""
    return Plugin(manifest=sp.manifest(sp.CONSUMER, depends=depends or {}, voucher_types=["planted_sale"],
                                       contracts={c.name: c.effect for c in sp.CONSUMER_CONTRACTS},
                                       uses=EVERY if uses is None else uses),
                  kind="business-module", contracts=list(sp.CONSUMER_CONTRACTS), artifact_digest="0" * 64)


def loaded() -> list[Plugin]:
    return [REGISTRY.plugins[n] for n in REGISTRY.order]


def call(engine, name: str, inp: dict) -> dict:
    with engine.connect() as conn:
        tx = conn.begin()
        try:
            return call_service(company_ctx(conn), sp.CONSUMER, name, inp)
        finally:
            tx.rollback()


def refused(engine, name: str, inp: dict) -> Refusal:
    with pytest.raises(Refusal) as e:
        call(engine, name, inp)
    return e.value


def assert_value_absent_from_record(record: logging.LogRecord, value: str) -> None:
    assert value not in record.getMessage()
    assert value not in repr(record.args)
    assert value not in repr({name: attr for name, attr in record.__dict__.items() if name not in {
        "created", "msecs", "relativeCreated", "process", "thread", "processName", "threadName", "taskName"
    }})


def test_record_value_check_ignores_timestamps():
    record = logging.makeLogRecord({"msg": "Service refused", "created": 1.5})
    assert "1.5" in repr(record.__dict__)
    assert_value_absent_from_record(record, "1.5")


def test_record_value_check_rejects_value_in_message():
    record = logging.makeLogRecord({"msg": "Service refused: 1.5"})
    with pytest.raises(AssertionError):
        assert_value_absent_from_record(record, "1.5")


# --- one parser, a strict shape --------------------------------------------------------------------------------------

def test_one_parser_splits_an_entry_into_its_plain_name_and_whether_it_is_optional():
    assert parse_use("stock.issue_stock") == ("stock.issue_stock", False)
    assert parse_use("stock.issue_stock?") == ("stock.issue_stock", True)


@pytest.mark.parametrize("entry", ["stock.issue_stock??", "stock.issue?_stock", "?", "stock?.issue", "stock.?"])
def test_any_other_place_or_number_of_question_marks_is_refused(entry):
    with pytest.raises(ValidationError):
        sp.manifest("shapes", uses=[entry])


def test_the_same_service_listed_plain_and_optional_is_refused():
    with pytest.raises(ValidationError):
        sp.manifest("both", depends={sp.PROVIDER: ">=1"}, uses=[QUOTE, f"{QUOTE}?"])


READER = re.compile(r"\.uses\b")  # a read of a manifest's uses list
THROUGH_PARSER = re.compile(r"\bparse_use\b")  # the entries pass through the parser on that same line


def readers_outside_the_parser(root: Path) -> list[str]:
    """Every line of engine code under `root` (tests excluded) that reads `.uses` without passing its entries
    through parse_use: the one place that knows what an entry means."""
    found = []
    for f in sorted(root.rglob("*.py")):
        if "tests" in f.relative_to(root).parts:
            continue
        for n, line in enumerate(f.read_text().splitlines(), 1):
            if READER.search(line) and not THROUGH_PARSER.search(line):
                found.append(f"{f.relative_to(root)}:{n}: {line.strip()}")
    return found


def test_no_engine_code_reads_uses_except_through_the_parser():
    assert readers_outside_the_parser(ENGINE / "packages") + readers_outside_the_parser(ENGINE / "plugins") == []


def test_the_scan_catches_a_planted_reader(tmp_path):
    """Mutation: a reader elsewhere that looks at the question mark itself is found."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "planted.py").write_text("ok = plugin.manifest.uses[0].endswith('?')\n"
                                                 "fine = [parse_use(u) for u in plugin.manifest.uses]\n")
    assert readers_outside_the_parser(tmp_path) == ["pkg/planted.py:1: ok = plugin.manifest.uses[0].endswith('?')"]


def test_a_manifest_with_no_optional_entry_digests_as_before():
    plain = sp.manifest("plain", uses=[])
    assert plain.digest() == Manifest(**plain.model_dump()).digest()


# --- the loader ------------------------------------------------------------------------------------------------------

def test_an_optional_use_of_a_loaded_plugins_declared_service_loads():
    Registry().fill(loaded() + [sp.provider(), optional_consumer()])


def test_an_optional_use_of_a_plugin_that_is_not_loaded_loads():
    Registry().fill(loaded() + [optional_consumer()])


def test_an_optional_use_of_a_service_the_loaded_plugin_does_not_declare_is_refused():
    with pytest.raises(PluginRefused, match=f"optionally uses {sp.PROVIDER}.nosuch, which {sp.PROVIDER} does not "
                                            "declare"):
        Registry().fill(loaded() + [sp.provider(), optional_consumer(uses=[f"{sp.PROVIDER}.nosuch?"])])


def test_a_plugin_named_both_as_a_dependency_and_an_optional_use_is_refused():
    with pytest.raises(PluginRefused, match=f"{sp.PROVIDER} is both a dependency and an optional use"):
        Registry().fill(loaded() + [sp.provider(), optional_consumer(depends={sp.PROVIDER: ">=1"})])


def test_a_plain_use_still_needs_its_dependency():
    with pytest.raises(PluginRefused, match="but does not depend on"):
        Registry().fill(loaded() + [sp.provider(), optional_consumer(uses=[QUOTE])])


# --- calls -----------------------------------------------------------------------------------------------------------

def test_an_optional_service_of_an_enabled_plugin_runs(engine, monkeypatch):
    sp.install(monkeypatch, engine, sp.provider(), optional_consumer())
    assert call(engine, QUOTE, QUOTE_IN)


def test_an_optional_service_of_a_disabled_plugin_is_the_typed_refusal_naming_the_plain_service(engine, monkeypatch):
    sp.install(monkeypatch, engine, sp.provider(), optional_consumer())
    with engine.begin() as conn:
        disable_plugin(conn, sp.PROVIDER)  # never refused for an optional use
    e = refused(engine, QUOTE, QUOTE_IN)
    assert e.code == "plugin_not_enabled" and str(e) == f"The {sp.PROVIDER} plugin is not enabled in these books."
    assert "?" not in str(e)
    check_installed(engine)  # the books still open with the optional plugin disabled


def test_an_optional_service_of_a_plugin_that_is_not_loaded_is_the_typed_refusal(engine, monkeypatch):
    sp.install(monkeypatch, engine, optional_consumer())
    e = refused(engine, QUOTE, QUOTE_IN)
    assert e.code == "plugin_not_enabled" and "?" not in str(e)


def test_an_undeclared_service_is_refused_naming_the_plain_service(engine, monkeypatch, caplog):
    sp.install(monkeypatch, engine, sp.provider(), optional_consumer(uses=[]))
    private = "private-quote-7c9e"
    with caplog.at_level("WARNING", logger="erpclaw_core.plugins"):
        e = refused(engine, QUOTE, {**QUOTE_IN, "quoteRef": private})
    assert e.code == "undeclared_service" and "?" not in str(e)
    assert str(e) == "Something went wrong inside your books' software, so nothing was changed."
    assert caplog.record_tuples == [("erpclaw_core.plugins", 30,
        f"Service {QUOTE} refused (undeclared_service): caller={sp.CONSUMER}, owner=None")]
    for value in (private, "10.00", "USD", "1.5"):
        assert value not in caplog.text
        assert_value_absent_from_record(caplog.records[0], value)
    assert caplog.records[0].exc_info is None


def test_a_write_through_an_optional_service_still_runs_only_inside_a_commit(engine, monkeypatch):
    sp.install(monkeypatch, engine, sp.provider(), optional_consumer())
    assert refused(engine, RECORD, {}).code == "service_outside_commit"


# --- what "declares" means -------------------------------------------------------------------------------------------

def test_declares_service_reads_plain_and_optional_entries_by_the_plain_name(monkeypatch):
    sp.register(monkeypatch, sp.provider(), optional_consumer(uses=[f"{QUOTE}?"]))
    assert declares_service(sp.CONSUMER, QUOTE) is True
    assert declares_service(sp.CONSUMER, RECORD) is False
    assert declares_service("no_such_plugin", QUOTE) is False


def test_declares_service_reads_a_plain_use_too(monkeypatch):
    sp.register(monkeypatch, sp.provider(), sp.consumer())
    assert declares_service(sp.CONSUMER, QUOTE) is True
