"""Every failing test's report, in every phase, shows no database password: a long traceback prints each frame's
arguments, and captured output is shown as it was written, so both are redacted before they are reported."""
from __future__ import annotations

import os
import re

import pytest

pytest_plugins = ["pytester"]

# a URL's userinfo password: scheme://user:password@
_USERINFO = re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s:/?#@'\"]*:)[^\s@'\"/]+@")
# a password as a URL query parameter or a libpq keyword, up to the next &, whitespace or quote
_KEYWORD = re.compile(r"(\bpassword=)[^\s&'\"]+", re.IGNORECASE)
BARE_MIN = 16  # a shorter password could be an ordinary word, and is never stripped from diagnostic text


def _server_password() -> str | None:
    """The test server's own password, when it is long enough to replace wherever it appears on its own."""
    url = os.environ.get("ERPCLAW_PG_TEST_URL")
    if not url:
        return None
    from sqlalchemy.engine import make_url

    try:
        password = make_url(url).password
    except Exception:  # noqa: BLE001 - an unparsable URL has no password to find
        return None
    return password if password and len(password) >= BARE_MIN else None


def redact(text: str) -> str:
    text = _USERINFO.sub(r"\1***@", text)
    text = _KEYWORD.sub(r"\1***", text)
    password = _server_password()
    return text.replace(password, "***") if password else text


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if report.failed:  # setup, call or teardown; a skip's reason stays as pytest made it
        report.longrepr = redact(str(report.longrepr))
        report.sections = [(title, redact(content)) for title, content in report.sections]
    return report
