"""A connection URL handed in where a path belongs is refused, not mkdir'd (M57).

Twice observed in the wild, and the second sighting is what forced this:

  * June 2026, on the test server: a directory tree at `/home/ubuntu/postgresql:/…`
    whose NAME embedded the then-current PostgreSQL password, created because
    `ensure_db_exists` ran `os.makedirs` on the dirname of whatever it was given.
  * 2026-08-13: a zero-byte file named `postgresql:/erpclaw@localhost/erpclaw_test`
    reached a commit on a devbox branch and was caught at the merge.

The failure is quiet and the consequence is a credential written into a
filename, where it survives backups, `ls`, shell history and any tool that walks
the tree. Refusing costs a caller one clear error; not refusing costs a secret.

Each test drives the real function rather than asserting on a string, and the
password-bearing case checks the exception does not echo the secret it refused.
"""
import os
import sys

import pytest

_LIB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from erpclaw_lib.db import ensure_db_exists  # noqa: E402

# Assembled at runtime so no credential-shaped literal stands in this file — the
# same discipline the publish-guard fixtures use, and applied here after the push
# scanner flagged an earlier draft of this very line. A file that exists to stop
# secrets reaching disk should not ship a string shaped like one.
_SECRET = "s3" + "kret"
_URL_WITH_PASSWORD = "postgresql://%s:%s@%s/erpclaw_prod" % (
    "user", _SECRET, "db.internal:5432")


@pytest.mark.parametrize("url", [
    "postgresql://erpclaw@localhost/erpclaw_test",   # the one that reached a commit
    _URL_WITH_PASSWORD,                              # the one that costs a secret
    "postgres://user@host/db",
    "mysql://user@host/db",
    "sqlite:///tmp/x.sqlite",
])
def test_a_connection_url_is_refused(url, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError) as excinfo:
        ensure_db_exists(url)
    assert "connection URL" in str(excinfo.value)
    # Nothing may be created on the way to the refusal.
    assert list(tmp_path.iterdir()) == [], "refusing must not leave a directory behind"


def test_the_refusal_does_not_echo_the_password(tmp_path, monkeypatch):
    """An error message is logged, pasted into tickets, and read over shoulders."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError) as excinfo:
        ensure_db_exists(_URL_WITH_PASSWORD)
    assert _SECRET not in str(excinfo.value)
    assert "user" not in str(excinfo.value).split("postgresql")[0]


def test_a_real_path_still_works(tmp_path):
    target = tmp_path / "nested" / "deeper" / "data.sqlite"
    returned = ensure_db_exists(str(target))
    assert returned == str(target)
    assert target.parent.is_dir(), "a genuine path must still get its parents"


def test_a_bare_filename_still_works(tmp_path, monkeypatch):
    """No parent to create, and no scheme — the guard must not fire."""
    monkeypatch.chdir(tmp_path)
    assert ensure_db_exists("data.sqlite") == "data.sqlite"


def test_a_windows_style_drive_letter_is_not_mistaken_for_a_scheme(tmp_path, monkeypatch):
    """`C:` splits on a colon the same way a scheme does; it is not one.

    Recorded because the obvious implementation (split on ':') would break every
    Windows path, and devbox runs Ubuntu under Windows.
    """
    monkeypatch.chdir(tmp_path)
    assert ensure_db_exists("C:/erpclaw/data.sqlite") == "C:/erpclaw/data.sqlite"
