"""Unknown approval outcomes retain the owned recovery record."""
import httpx
import pytest

from erpclaw_cli import journal, main


@pytest.mark.parametrize("status,body", [
    (200,b"{"),(200,b"[]"),(200,b'{"events":[]}'),
    (200,b'{"events":[{"type":"result"}]}'),
    (200,b'{"events":[{"type":"result","status":[],"text":"Bad"}]}'),
    (200,b'{"events":[{"type":"unknown","text":"nothing confirmed"}]}'),
    (200,b'{"events":[{"type":"result","status":"committed","text":"Saved.","operation_key":"op"},{"type":"email"}]}'),
    (403,b'{"detail":"Permission denied."}'),(403,b"[]"),
    (409,b'{"detail":"Digest mismatch. Nothing was saved."}'),(409,b"[]"),
])
def test_unknown_response_never_deletes_owned_receipt(tmp_path,monkeypatch,status,body):
    monkeypatch.setattr(main,"con",main.Console(record=True,width=240))
    folder=tmp_path/"receipts"
    with journal.Journal(folder,"synthetic-books") as j:
        entry=j.publish("synthetic-card","d"*64,"synthetic-conversation")
        try:
            main._answered(None,j,entry,httpx.Response(status,content=body))
        except Exception as exc:
            pytest.fail(f"unconfirmed response should be handled: {type(exc).__name__}")
        assert entry.path.exists()
        output=main.con.export_text()
        assert "may or may not have been saved" in output
        assert main.NO_ANSWER not in output


def test_well_formed_committed_outcome_can_finish_receipt(tmp_path,monkeypatch):
    monkeypatch.setattr(main,"con",main.Console(record=True,width=240))
    with journal.Journal(tmp_path/"receipts","synthetic-books") as j:
        entry=j.publish("synthetic-card","d"*64,"synthetic-conversation")
        main._answered(None,j,entry,httpx.Response(200,json={"events":[
            {"type":"result","status":"committed","text":"Saved.","operation_key":"synthetic-operation"}
        ]}))
        assert not entry.path.exists()
        assert "Saved." in main.con.export_text()


def test_first_nonanswered_attempt_is_not_called_a_retry(tmp_path,monkeypatch):
    monkeypatch.setattr(main,"con",main.Console(record=True,width=240))
    with journal.Journal(tmp_path/"receipts","synthetic-books") as j:
        entry=j.publish("synthetic-card","d"*64,"synthetic-conversation")
        main._answered(None,j,entry,httpx.Response(429,json={"detail":"Try later."}))
        assert "Latest retry response" not in main.con.export_text()
        assert entry.path.exists()


def test_received_server_error_is_not_reported_as_unreachable(tmp_path,monkeypatch):
    monkeypatch.setattr(main,"con",main.Console(record=True,width=240))
    monkeypatch.setattr(main,"_ask",lambda prompt:"yes")
    class Client:
        def post(self,*args,**kwargs): return httpx.Response(500,json={"detail":"Try later."})
    with journal.Journal(tmp_path/"receipts","synthetic-books") as j:
        main.approve(Client(),j,"synthetic-conversation",
                     {"approval_id":"synthetic-card","digest":"d"*64})
        output=main.con.export_text()
        assert main.UNREACHABLE not in output
        assert "may or may not have been saved" in output
        assert len(list(j.folder.glob("*.json")))==1


def test_render_failure_retains_receipt_without_contradicting_shown_result(tmp_path,monkeypatch):
    monkeypatch.setattr(main,"con",main.Console(record=True,width=240))
    def fail_after_show(*args):
        main.con.print("Saved.")
        raise RuntimeError("synthetic display failure")
    monkeypatch.setattr(main,"show_events",fail_after_show)
    with journal.Journal(tmp_path/"receipts","synthetic-books") as j:
        entry=j.publish("synthetic-card","d"*64,"synthetic-conversation")
        with pytest.raises(RuntimeError,match="display failure"):
            main._answered(None,j,entry,httpx.Response(200,json={"events":[
                {"type":"result","status":"committed","text":"Saved.","operation_key":"op"}]}))
        assert entry.path.exists()
        output=main.con.export_text()
        assert "Saved." in output and main.UNCONFIRMED not in output


@pytest.mark.parametrize("kind", ["result", "ask"])
@pytest.mark.parametrize("position", ["before", "after"])
def test_unknown_additive_events_and_fields_do_not_change_the_recognized_outcome(tmp_path, monkeypatch, kind, position):
    monkeypatch.setattr(main, "con", main.Console(record=True, width=240))
    if kind == "result":
        outcome = {"type": "result", "status": "committed", "text": "Saved.",
                   "operation_key": "synthetic-operation", "future_field": {"anything": True}}
    else:
        outcome = {"type": "ask", "status": "superseded", "text": "Which customer?",
                   "code": "customer_ambiguous", "question_id": "q1", "kind": "field",
                   "choices": [{"code": "q1-1", "label": "Acme", "control": False, "future_field": "ignored"}],
                   "future_field": {"anything": True}}
    unknown = {"type": "future_event", "text": "DO NOT DISPLAY FUTURE EVENT", "future_field": [1, 2]}
    events = [unknown, outcome] if position == "before" else [outcome, unknown]
    with journal.Journal(tmp_path / "receipts", "synthetic-books") as owned:
        entry = owned.publish("synthetic-card", "d" * 64, "synthetic-conversation")
        continuation = main._answered(None, owned, entry, httpx.Response(200, json={"events": events, "future": 1}))
        assert continuation == (outcome if kind == "ask" else None)
        assert "DO NOT DISPLAY FUTURE EVENT" not in main.con.export_text()
        assert not entry.path.exists()
