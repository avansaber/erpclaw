"""Approval questions may offer labels without numbered choice codes."""
import httpx
import pytest

from erpclaw_cli import journal, main


def approval_question(status="superseded", kind=None, choices=None):
    return {
        "type": "ask",
        "status": status,
        "code": "invoice_ambiguous",
        "text": "Which invoice should it go to, or oldest first?",
        "question_id": None,
        "kind": kind,
        "options": ["INV-001", "oldest first"],
        "choices": choices if choices is not None else [
            {"label": "INV-001"}, {"label": "oldest first"},
        ],
    }


@pytest.mark.parametrize("status", ["superseded", "refused"])
def test_validator_accepts_label_only_authoritative_question(status):
    question = approval_question(status=status)
    response = httpx.Response(200, json={"events": [question]})

    assert main._approval_outcome_events(response) == [question], (
        "Label-only authoritative ask must be accepted"
    )


@pytest.mark.parametrize("status", ["superseded", "refused"])
def test_answered_returns_label_only_continuation_and_finishes_receipt(tmp_path, monkeypatch, status):
    monkeypatch.setattr(main, "con", main.Console(record=True, width=240))
    question = approval_question(status=status)
    response = httpx.Response(200, json={"events": [question]})

    with journal.Journal(tmp_path / "receipts", "synthetic-books") as owned:
        entry = owned.publish("synthetic-card", "d" * 64, "synthetic-conversation")
        continuation = main._answered(None, owned, entry, response)

        assert continuation == question, "Label-only authoritative ask must return its continuation"
        assert not entry.path.exists()
        assert entry not in owned.held
        assert main.message_for(continuation, "oldest first") == {"text": "oldest first"}
        assert main.message_for(continuation, "1") == {"text": "1"}
        output = main.con.export_text()
        assert question["text"] in output
        assert "1. INV-001" not in output
        assert main.UNCONFIRMED not in output


@pytest.mark.parametrize("kind", [None, "free"])
def test_unnumbered_question_accepts_optional_string_codes(kind):
    question = approval_question(kind=kind, choices=[{"label": "oldest first", "code": "oldest"}])
    response = httpx.Response(200, json={"events": [question]})

    assert main._approval_outcome_events(response) == [question]
    assert main.message_for(question, "1") == {"text": "1"}


@pytest.mark.parametrize("choice", [
    pytest.param(None, id="null-choice"),
    pytest.param("oldest first", id="non-dictionary-choice"),
    pytest.param({"code": "oldest"}, id="missing-label"),
    pytest.param({"label": None, "code": "oldest"}, id="null-label"),
    pytest.param({"label": 1, "code": "oldest"}, id="numeric-label"),
    pytest.param({"label": [], "code": "oldest"}, id="list-label"),
    pytest.param({"label": "oldest first", "code": None}, id="null-code"),
    pytest.param({"label": "oldest first", "code": 1}, id="numeric-code"),
    pytest.param({"label": "oldest first", "code": []}, id="list-code"),
    pytest.param({"label": "oldest first", "code": {}}, id="dictionary-code"),
])
def test_malformed_choice_retains_receipt(tmp_path, monkeypatch, choice):
    monkeypatch.setattr(main, "con", main.Console(record=True, width=240))
    question = approval_question(choices=[choice])
    response = httpx.Response(200, json={"events": [question]})

    assert main._approval_outcome_events(response) is None
    with journal.Journal(tmp_path / "receipts", "synthetic-books") as owned:
        entry = owned.publish("synthetic-card", "d" * 64, "synthetic-conversation")
        assert main._answered(None, owned, entry, response) is None
        assert entry.path.exists()
        assert main.UNCONFIRMED in main.con.export_text()


@pytest.mark.parametrize("kind", ["field", "approval", "capability"])
def test_numbered_question_without_choice_codes_retains_receipt(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(main, "con", main.Console(record=True, width=240))
    question = approval_question(kind=kind)
    response = httpx.Response(200, json={"events": [question]})

    assert main._approval_outcome_events(response) is None
    with journal.Journal(tmp_path / "receipts", "synthetic-books") as owned:
        entry = owned.publish("synthetic-card", "d" * 64, "synthetic-conversation")
        assert main._answered(None, owned, entry, response) is None
        assert entry.path.exists()
        assert main.UNCONFIRMED in main.con.export_text()


@pytest.mark.parametrize("status", [None, "pending", "committed"])
def test_label_only_question_without_authoritative_status_retains_receipt(tmp_path, monkeypatch, status):
    monkeypatch.setattr(main, "con", main.Console(record=True, width=240))
    question = approval_question(status=status)
    response = httpx.Response(200, json={"events": [question]})

    assert main._approval_outcome_events(response) is None
    with journal.Journal(tmp_path / "receipts", "synthetic-books") as owned:
        entry = owned.publish("synthetic-card", "d" * 64, "synthetic-conversation")
        assert main._answered(None, owned, entry, response) is None
        assert entry.path.exists()
        assert main.UNCONFIRMED in main.con.export_text()
