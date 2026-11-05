"""Nothing the engine or its test suite prints shows a database password: a restore target's repr, and every
failing test's report, in every phase and every captured section. Each pytester run first checks its outcome and a
marker the failing test printed, so a run that broke early cannot pass by printing nothing."""
from __future__ import annotations

import secrets
from pathlib import Path

from sqlalchemy.engine import make_url

from conftest import PG_URL
from erpclaw_core import backup as bk

ROOT_CONFTEST = Path(__file__).resolve().parent.parent / "conftest.py"
MARKER = "diagnostic-marker-kept"


def _run(pytester, capsys, source: str, *args: str):
    pytester.makeconftest(ROOT_CONFTEST.read_text())
    pytester.makepyfile(source)
    result = pytester.runpytest_subprocess("--tb=long", "-p", "no:cacheprovider", *args)
    capsys.readouterr()  # pytester echoes the inner output into this test's capture: discarded, so a failure here never shows it
    return result


def test_a_restore_targets_repr_shows_no_login():
    login, secret = "owner_" + secrets.token_hex(3), "Pw" + secrets.token_hex(12)
    target = bk.NewPostgresDatabase(f"postgresql+psycopg://{login}:{secret}@db.example:5432/postgres", "restored_x")
    shown = repr(target)
    leaked = secret in shown or login in shown  # a boolean, so a failure never prints the secret
    assert not leaked
    assert "db.example" in shown and "restored_x" in shown


def test_every_phase_and_every_captured_section_is_redacted(pytester, capsys):
    secret = "Pw" + secrets.token_hex(12)
    url = f"postgresql+psycopg://someone:{secret}@db.example:5432/x_test"
    result = _run(pytester, capsys, f'''
import logging, sys
import pytest

URL = "{url}"

def show(where):
    print("{MARKER}", where, URL)
    print("{MARKER}", where, URL, file=sys.stderr)
    logging.getLogger("t").warning("%s %s %s", "{MARKER}", where, URL)

@pytest.fixture
def broken_setup():
    show("setup")
    server_url = URL
    raise RuntimeError("setup failed for " + server_url)

@pytest.fixture
def broken_teardown():
    yield
    show("teardown")
    server_url = URL
    raise RuntimeError("teardown failed for " + server_url)

def take(server_url):
    assert server_url is None, server_url

def test_setup(broken_setup):
    pass

def test_call():
    show("call")
    take(URL)

def test_teardown(broken_teardown):
    pass
''')
    result.assert_outcomes(passed=1, failed=1, errors=2)
    out = result.stdout.str()
    for where in ("setup", "call", "teardown"):
        assert f"{MARKER} {where}" in out, where
    assert "***@db.example" in out
    leaked = secret in out
    assert not leaked


def test_the_other_credential_forms_are_redacted(pytester, capsys, monkeypatch):
    server_pw = secrets.token_hex(24)  # the test server's password has pg.sh's form: 48 hexadecimal characters
    monkeypatch.setenv("ERPCLAW_PG_TEST_URL", f"postgresql+psycopg://erpclaw:{server_pw}@127.0.0.1:55432/x_test")
    query_pw, keyword_pw = "Q" + secrets.token_hex(10), "K" + secrets.token_hex(10)
    result = _run(pytester, capsys, f'''
def test_query():
    print("{MARKER}")
    url = "postgresql+psycopg://db.example/x_test?user=u&password={query_pw}&sslmode=disable"
    assert url is None

def test_keyword():
    print("{MARKER}")
    dsn = "host=db.example user=u password={keyword_pw} dbname=x_test"
    assert dsn is None

def test_bare_in_a_repr():
    print("{MARKER}")
    held = {{"secret": "{server_pw}"}}
    assert repr(held) is None
''')
    result.assert_outcomes(failed=3)
    out = result.stdout.str()
    assert out.count(MARKER) >= 3
    leaked = [i for i, pw in enumerate((query_pw, keyword_pw, server_pw)) if pw in out]  # positions, never values
    assert not leaked


def test_a_short_password_is_not_stripped_from_diagnostic_text(pytester, capsys, monkeypatch):
    monkeypatch.setenv("ERPCLAW_PG_TEST_URL", "postgresql+psycopg://erpclaw:ledger@127.0.0.1:55432/x_test")
    result = _run(pytester, capsys, f'''
def test_words():
    print("{MARKER}")
    assert "the ledger balances" is None
''')
    result.assert_outcomes(failed=1)
    out = result.stdout.str()
    assert MARKER in out and "the ledger balances" in out


def test_a_failing_cleanup_on_the_test_server_shows_no_password(pytester, capsys, monkeypatch):
    """The real cleanup on the test server's URL. A server URL with no password, or one too short to tell from a word
    (a CI service's), gets a generated one in its place: the cleanup still runs, and fails at its login or its drop,
    with that password in its frames either way."""
    url = make_url(PG_URL)
    if not url.password or len(url.password) < 16:
        url = url.set(password="Pw" + secrets.token_hex(15))
        monkeypatch.setenv("ERPCLAW_PG_TEST_URL", url.render_as_string(hide_password=False))
    password = url.password
    result = _run(pytester, capsys, f'''
import os
from erpclaw_core import backup as bk

def clean(target):  # the target is an argument, so a long traceback prints its repr
    bk.clean_staging(bk.PG_STAGE_PREFIX + "absent_b", target.server_url)

def test_cleanup():
    print("{MARKER}")
    clean(bk.NewPostgresDatabase(os.environ["ERPCLAW_PG_TEST_URL"], "restored_none_test"))
''')
    result.assert_outcomes(failed=1)
    out = result.stdout.str()
    assert MARKER in out and "target = NewPostgresDatabase(" in out  # the premise: the repr was printed
    leaked = password in out
    assert not leaked
    assert "NewPostgresDatabase(server_url=" not in out
