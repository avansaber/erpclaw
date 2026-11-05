"""Independent journal send-boundary honesty probes; no engine, DB or user data."""
import fcntl
import os

import httpx
import pytest

from erpclaw_cli import journal, main


def scenario(tmp_path, monkeypatch, *, boundary, replacement=False, failure="transport"):
    folder = tmp_path / "synthetic-approvals"
    sent, ownership, prompts, published, replacement_fds = [], [], [], [], []
    monkeypatch.setattr(main, "con", main.Console(record=True, width=200))

    def change():
        path = published[0].path
        if replacement:
            temporary = folder / "synthetic-replacement"
            body = path.read_bytes()
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            replacement_fds.append(fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.write(fd, body)
            os.replace(temporary, path)
        else:
            path.unlink()

    with journal.Journal(folder, "synthetic-books") as owned:
        real_publish = owned.publish

        def publish(*args):
            entry = real_publish(*args)
            published.append(entry)
            if boundary == "before_first_send":
                change()
            return entry

        monkeypatch.setattr(owned, "publish", publish)

        class Client:
            def post(self, path, **kwargs):
                sent.append((path, kwargs))
                ownership.append(owned.owns(published[0]))
                assert ownership[-1], "an HTTP attempt must retain its own locked inode"
                with open(published[0].path) as other:
                    with pytest.raises(BlockingIOError):
                        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if len(sent) == 1:
                    # Synthetic unknown outcome: no real posting is made or claimed.
                    if boundary == "after_first_send":
                        change()
                    if failure == "transport":
                        raise httpx.ReadTimeout("synthetic lost acknowledgement")
                    return httpx.Response(500, json={"detail": "synthetic lost acknowledgement"})
                return httpx.Response(409, json={"detail": "synthetic known outcome"})

        def answer(prompt):
            prompts.append(prompt)
            assert prompt == main.LOST
            if boundary == "after_retry_prompt":
                change()
            return "yes"

        monkeypatch.setattr(main, "_ask", answer)
        try:
            main.approve(Client(), owned, "synthetic-conversation",
                         {"approval_id": "synthetic-card", "digest": "d" * 64})
            output = main.con.export_text()
            remaining = published[0].path.exists()
        finally:
            for fd in replacement_fds:
                os.close(fd)
    return sent, ownership, prompts, output, remaining


@pytest.mark.parametrize("boundary", ["after_first_send", "after_retry_prompt"])
@pytest.mark.parametrize("replacement", [False, True], ids=["deleted", "replaced-and-locked"])
def test_an_attempted_approval_never_becomes_a_not_sent_claim(tmp_path, monkeypatch, boundary, replacement):
    sent, owned, prompts, output, remaining = scenario(
        tmp_path, monkeypatch, boundary=boundary, replacement=replacement)
    assert len(sent) == 1 and owned == [True]
    assert len(prompts) == (1 if boundary == "after_retry_prompt" else 0)
    assert main.UNSAFE not in output, "the first send happened; its lost reply cannot become 'did not send'"
    assert "I sent your approval" in output and "may or may not have been saved" in output
    assert "next time" not in output, "a missing or foreign record cannot promise automatic recovery"
    assert remaining is replacement


@pytest.mark.parametrize("replacement", [False, True], ids=["deleted", "replaced-and-locked"])
def test_loss_before_the_first_attempt_still_reports_not_sent(tmp_path, monkeypatch, replacement):
    sent, owned, prompts, output, remaining = scenario(
        tmp_path, monkeypatch, boundary="before_first_send", replacement=replacement)
    assert sent == owned == prompts == []
    assert main.UNSAFE in output
    assert "I sent your approval" not in output
    assert remaining is replacement


@pytest.mark.parametrize("failure", ["transport", "server_error"])
def test_intact_owned_entry_still_allows_the_explicit_idempotent_retry(tmp_path, monkeypatch, failure):
    sent, owned, prompts, output, remaining = scenario(
        tmp_path, monkeypatch, boundary="intact", failure=failure)
    assert len(sent) == 2 and owned == [True, True]
    assert len(prompts) == 1
    assert sent[0] == sent[1]
    assert main.UNSAFE not in output and "couldn't keep its record safely" not in output
    # A generic 409 does not establish whether the first lost attempt committed.
    assert remaining and main.UNCONFIRMED in output
