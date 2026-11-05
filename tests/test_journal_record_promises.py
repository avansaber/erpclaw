"""Post-attempt receipt promises: real locked journal, deterministic synthetic client, no DB or user data."""
import fcntl

import httpx
import pytest

from erpclaw_cli import journal, main


@pytest.mark.parametrize("phase", ["retry_send", "decline_prompt", "recovery_send"])
@pytest.mark.parametrize("change", ["intact", "deleted", "replaced-and-locked"])
@pytest.mark.parametrize("failure", ["transport", "server_error"])
def test_future_recovery_is_only_promised_with_an_owned_record(tmp_path, monkeypatch, phase, change, failure):
    folder = tmp_path / "synthetic-receipts"
    card = {"approval_id": "synthetic-card", "digest": "d" * 64}
    sent, prompts, active, replacements = [], [], [], []
    monkeypatch.setattr(main, "con", main.Console(record=True, width=240))

    with journal.Journal(folder, "synthetic-books") as current, journal.Journal(folder, "synthetic-books") as other:
        publish = current.publish
        def published(*args):
            entry = publish(*args)
            active[:] = [entry]
            return entry
        monkeypatch.setattr(current, "publish", published)

        def alter():
            if change == "intact":
                return
            active[0].path.unlink()
            if change == "replaced-and-locked":
                replacement = other.publish(card["approval_id"], card["digest"], "synthetic-conversation")
                replacements.append(replacement)
                assert other.owns(replacement)

        def lost():
            if failure == "transport":
                raise httpx.ReadTimeout("synthetic lost acknowledgement")
            return httpx.Response(503, json={"detail": "synthetic lost acknowledgement"})

        class Client:
            def post(self, path, **kwargs):
                entry = active[0] if phase != "recovery_send" else current.held[0]
                active[:] = [entry]
                assert current.owns(entry)
                with entry.path.open() as independently_opened:
                    with pytest.raises(BlockingIOError):
                        fcntl.flock(independently_opened, fcntl.LOCK_EX | fcntl.LOCK_NB)
                sent.append((path, kwargs))
                if phase == "recovery_send" or (phase == "retry_send" and len(sent) == 2):
                    alter()
                return lost()

        def answer(prompt):
            assert prompt == main.LOST
            prompts.append(prompt)
            if phase == "decline_prompt":
                alter()
                return "no"
            return "yes"
        monkeypatch.setattr(main, "_ask", answer)

        if phase == "recovery_send":
            with journal.Journal(folder, "synthetic-books") as earlier:
                earlier.publish(card["approval_id"], card["digest"], "synthetic-conversation")
            main.recover(Client(), current)
        else:
            main.approve(Client(), current, "synthetic-conversation", card)
        output = main.con.export_text()  # Rich clears by default: capture exactly once.
        assert len(sent) == (2 if phase == "retry_send" else 1)
        assert len(prompts) == (0 if phase == "recovery_send" else 1)
        if len(sent) == 2:
            assert sent[0] == sent[1]  # same card, digest, conversation; never a third attempt
        if change == "intact":
            expected = (main.LATER if phase == "decline_prompt" else
                        main.UNREACHABLE if failure == "transport" else main.UNCONFIRMED)
            assert expected in output
            assert main.RECORD_LOST not in output
            assert active[0].path.exists()
        else:
            assert main.RECORD_LOST in output
            assert main.LATER not in output and main.UNREACHABLE not in output
            assert "next time" not in output
            assert main.UNSAFE not in output  # an attempt happened
            assert active[0].path.exists() is bool(replacements)
        if replacements:
            assert other.owns(replacements[0])  # current window never removes or unlocks another's record
            assert list(current.recover()) == []  # locked foreign entry cannot be resent
