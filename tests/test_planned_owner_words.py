"""Planned answers use owner-facing words without release schedules."""
from erpclaw_assistant.resolve_text import TABLES
from erpclaw_core import catalogue as cat
from erpclaw_core.contracts import figures_planned_text, planned_reply, planned_text
from erpclaw_core.core_contracts import TEXT
from erpclaw_core.plugins import REGISTRY


def test_planned_words_cover_waves_and_figures(as_released):
    for wave in ("Core", "v3.0", "v3.1", "later"):
        assert planned_text("tax calculation", wave) == "Tax calculation is not in ERPClaw yet."
        assert figures_planned_text(wave) == "Figures and reports like revenue are not in ERPClaw yet."
    assert planned_reply(REGISTRY.contracts["get_metric"]) == "Figures and reports like revenue are not in ERPClaw yet."
    assert planned_reply(REGISTRY.contracts["list_invoices"]) == "The invoice list is not in ERPClaw yet."


def test_planned_catalogue_replies(engine, as_released):
    with engine.connect() as conn:
        entries = {e.id: e for e in cat.catalogue(conn)}
    for entry_id, expected in (
        ("planned.payroll", "Payroll is not in ERPClaw yet."),
        ("planned.tax_engine", "Tax calculation is not in ERPClaw yet."),
        ("planned.users", "These books have a named owner and message authors. "
                          "Second-person screens and roles are not in ERPClaw yet."),
    ):
        result = cat.decide([entries[entry_id]], entry_id)
        assert (result.outcome, result.capability, result.text) == ("c", entry_id, expected)
    for entry in entries.values():
        if entry.state == "planned":
            result = cat.decide([entry], entry.id)
            assert "planned for" not in result.text


def test_core_owner_text_matches_assistant_table():
    for language, sentences in TEXT.items():
        assert {key: TABLES[language]["text"][key] for key in sentences} == sentences
