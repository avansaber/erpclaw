"""Qualified payment dates retain the existing question rule and protected fixtures."""
from hashlib import sha256
from pathlib import Path

import pytest

from test_payment_phrase_misc import receipts
from test_payment_resolve import pay, kinds

QUALIFIED = ("next November 3", "next 25 October", "last November 3", "this 25 October",
             "November 3 next", "coming November 3", "previous 25 October")


@pytest.mark.parametrize("phrase", QUALIFIED)
def test_qualified_payment_dates_ask(receipts, phrase):
    for i, text in enumerate((f"{phrase}, Hooli paid 100", f"Hooli paid 100 {phrase}")):
        ev = pay(receipts, text, str(i), amount=100)
        assert kinds(ev) == ["ask"] and ev[0]["code"] == "date_unknown", ev


PROTECTED = {
    "test_payment_resolve.py": "37f42c2421963cce9d34870b2f6b2da376f07925f4b5ea4b8bf00ab2f02b06b9",
    "test_outgoing_account.py": "f4a6b116879d5e3e8b05420e5a1647593b298e42040cc76d1716b73c468209f8",
    "test_date_expressions.py": "898b91e4788003d00d29ce0c24459c83e2a252e57e201a2952c5b5709e39f943",
}


def test_protected_payment_and_date_files_are_byte_identical():
    for name, digest in PROTECTED.items():
        assert sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() == digest, name
