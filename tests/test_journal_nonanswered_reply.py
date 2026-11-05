"""Uninformative retry errors cannot prove the earlier attempted approval made no change."""
import httpx
import pytest
from erpclaw_cli import journal, main


@pytest.mark.parametrize("status", [422, 429])
@pytest.mark.parametrize("shape", ["empty-detail", "missing-detail", "non-json"])
def test_unknown_first_attempt_then_uninformative_nonanswered_reply_stays_unknown(tmp_path, monkeypatch, status, shape):
    card = {"approval_id": "synthetic-card", "digest": "d" * 64}
    attempts, published = [], []
    monkeypatch.setattr(main, "con", main.Console(record=True, width=240))
    monkeypatch.setattr(main, "_ask", lambda prompt: "yes")
    with journal.Journal(tmp_path / "synthetic-receipts", "synthetic-books") as owned:
        publish = owned.publish
        def captured(*args):
            entry = publish(*args)
            published.append(entry)
            return entry
        monkeypatch.setattr(owned, "publish", captured)
        class Client:
            def post(self, path, **kwargs):
                assert owned.owns(published[0])
                attempts.append((path, kwargs))
                if len(attempts) == 1:
                    # The first response is lost; the client has no authority to infer whether it committed.
                    raise httpx.ReadTimeout("synthetic unknown first outcome")
                if shape == "non-json":
                    return httpx.Response(status, text="synthetic uninformative proxy response")
                return httpx.Response(status, json={"detail": ""} if shape == "empty-detail" else {})
        main.approve(Client(), owned, "synthetic-conversation", card)
        output = main.con.export_text()
        assert len(attempts) == 2 and attempts[0] == attempts[1]
        assert published[0].path.exists()  # a non-ANSWERED response cannot erase the recovery record
        assert main.NO_ANSWER not in output
        assert "nothing was changed" not in output.lower()
        assert "may or may not have been saved" in output


@pytest.mark.parametrize("status", [422, 429])
def test_parsed_backend_detail_is_reported_without_inventing_a_saved_outcome(tmp_path, monkeypatch, status):
    calls = []
    detail = "Synthetic request refused. Try later."
    monkeypatch.setattr(main, "con", main.Console(record=True, width=240))
    monkeypatch.setattr(main, "_ask", lambda prompt: "yes")
    with journal.Journal(tmp_path / "synthetic-receipts", "synthetic-books") as owned:
        class Client:
            def post(self, path, **kwargs):
                calls.append((path, kwargs))
                if len(calls) == 1:
                    raise httpx.ReadTimeout("synthetic unknown first outcome")
                return httpx.Response(status, json={"detail": detail})
        main.approve(Client(), owned, "synthetic-conversation",
                     {"approval_id": "synthetic-card", "digest": "d" * 64})
        output = main.con.export_text()
        assert detail in output and main.NO_ANSWER not in output
        assert len(calls) == 2 and calls[0] == calls[1]
        assert len(list(owned.folder.glob("*.json"))) == 1


@pytest.mark.parametrize("body", [[], None, "not an object", {"detail": ["unexpected"]}])
def test_non_object_reply_keeps_uncertain_approval(tmp_path, monkeypatch, body):
    monkeypatch.setattr(main, "con", main.Console(record=True, width=240))
    monkeypatch.setattr(main, "_ask", lambda prompt: "yes")
    calls = []
    with journal.Journal(tmp_path / "receipts", "synthetic-books") as owned:
        class Client:
            def post(self, path, **kwargs):
                calls.append((path, kwargs))
                if len(calls) == 1:
                    raise httpx.ReadTimeout("synthetic unknown outcome")
                return httpx.Response(422, json=body)
        main.approve(Client(), owned, "synthetic-conversation",
                     {"approval_id": "synthetic-card", "digest": "d" * 64})
        assert "may or may not have been saved" in main.con.export_text()
        assert len(list(owned.folder.glob("*.json"))) == 1


def test_recovery_nonanswered_response_preserves_uncertainty(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "con", main.Console(record=True, width=240))
    folder = tmp_path / "receipts"
    with journal.Journal(folder, "synthetic-books") as first:
        first.keep(first.publish("synthetic-card", "d" * 64, "synthetic-conversation"))
    with journal.Journal(folder, "synthetic-books") as recovered:
        class Client:
            def post(self, path, **kwargs):
                return httpx.Response(429, json={})
        main.recover(Client(), recovered)
        assert "may or may not have been saved" in main.con.export_text()
        assert main.NO_ANSWER not in main.con.export_text()
        assert len(list(folder.glob("*.json"))) == 1


def test_nonanswered_without_a_timeout_does_not_claim_connection_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "con", main.Console(record=True, width=240))
    with journal.Journal(tmp_path / "receipts", "synthetic-books") as owned:
        class Client:
            def post(self, path, **kwargs):
                return httpx.Response(429, json={})
        main.approve(Client(), owned, "synthetic-conversation",
                     {"approval_id": "synthetic-card", "digest": "d" * 64})
        output = main.con.export_text()
        assert main.UNCONFIRMED in output
        assert main.UNREACHABLE not in output
        assert len(list(owned.folder.glob("*.json"))) == 1


def test_nonanswered_after_record_loss_does_not_promise_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "con", main.Console(record=True, width=240))
    with journal.Journal(tmp_path / "receipts", "synthetic-books") as owned:
        class Client:
            def post(self, path, **kwargs):
                next(owned.folder.glob("*.json")).unlink()
                return httpx.Response(429, json={})
        main.approve(Client(), owned, "synthetic-conversation",
                     {"approval_id": "synthetic-card", "digest": "d" * 64})
        output = main.con.export_text()
        assert main.RECORD_LOST in output
        assert main.UNCONFIRMED not in output
