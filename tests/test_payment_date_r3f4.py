"""Repeat the qualified-date protection at the phrase-reading boundary."""
from datetime import date
from hashlib import sha256
from pathlib import Path

import pytest

from erpclaw_assistant import resolve as rv
from erpclaw_core.common import Refusal
import test_payment_date_r1f4 as first


@pytest.mark.parametrize("phrase", first.QUALIFIED)
def test_qualified_date_resolver_never_returns_a_day(phrase):
    session = rv.Session()
    with pytest.raises(Refusal) as raised:
        rv.resolve_date(phrase, date(2026, 10, 26), session, "date")
    assert raised.value.code == "date_unknown"
    assert raised.value.message == "Which date do you mean?"


def test_protected_files_keep_their_original_bytes():
    for name, digest in first.PROTECTED.items():
        assert sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() == digest, name
