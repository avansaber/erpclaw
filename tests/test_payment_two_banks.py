"""A reference or memo naming a bank does not name the receiving account."""
import pytest

from test_outgoing_account import snapshot
from test_payment_phrase_misc import receipts
from test_payment_resolve import _add_bank, kinds, lines, pay


@pytest.mark.parametrize("phrase", ("reference Savings", "memo Savings", "into Savings"))
def test_only_an_account_owned_occurrence_selects_the_receiving_bank(receipts, phrase):
    # PostgreSQL books: Hooli has one open invoice, and both banks are eligible.
    _add_bank(receipts, "1110", "Savings")
    before = snapshot(receipts)
    events = pay(receipts, f"Hooli paid 100 {phrase}", customer="Hooli", amount=100,
                 account="Savings", reference="Savings")

    if phrase == "into Savings":
        assert kinds(events) == ["card"], events
        assert lines(events[0])["Account"] == ["1110 Savings"]
        assert snapshot(receipts, cards=False) == before[:4]
    else:
        assert kinds(events) == ["ask"], events
        assert events[0]["code"] == "account_ambiguous"
        assert events[0]["options"] == ["1100 Bank", "1110 Savings"]
        assert snapshot(receipts) == before
