"""The engine opens only PostgreSQL books, and refuses a database it does not own."""
import pytest
from typer.testing import CliRunner

from conftest import drop_everything, empty_books
from erpclaw_core.db import NEEDS_POSTGRESQL, ForeignBooks, NotPostgreSQL, create_all, make_engine

NOT_POSTGRESQL = ["sqlite:///books.sqlite", "sqlite://", "".join(('mysql+pymysql', '://', 'u:p@127.0.0.1/books')), "not a url"]


@pytest.mark.parametrize("url", NOT_POSTGRESQL)
def test_a_url_that_is_not_postgresql_is_refused_with_the_plain_message(url):
    with pytest.raises(NotPostgreSQL) as e:
        make_engine(url)
    assert str(e.value) == NEEDS_POSTGRESQL == "ERPClaw v3 needs PostgreSQL; see the install notes."


def test_a_sqlite_books_url_in_the_environment_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("ERPCLAW_DATABASE_URL", f"sqlite:///{tmp_path / 'books.sqlite'}")
    with pytest.raises(NotPostgreSQL, match="needs PostgreSQL"):
        make_engine()
    assert list(tmp_path.iterdir()) == []  # no books file was made


def test_no_books_url_is_refused_and_names_the_setting():
    with pytest.raises(NotPostgreSQL) as e:
        make_engine()
    assert str(e.value).startswith(NEEDS_POSTGRESQL) and "ERPCLAW_DATABASE_URL" in str(e.value)


@pytest.mark.parametrize("command", [["init"], ["chat"], ["serve"], ["grants"], ["plugins", "list"], ["backup", "b"],
                                     ["users", "disable", "x"]])
def test_the_cli_gives_the_plain_message_for_sqlite_books(command, monkeypatch, tmp_path):
    from erpclaw_cli.main import app

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ERPCLAW_DATABASE_URL", f"sqlite:///{tmp_path / 'books.sqlite'}")
    out = CliRunner().invoke(app, command)
    assert out.exit_code == 1, out.output
    assert NEEDS_POSTGRESQL in " ".join(out.output.split())
    assert list(tmp_path.iterdir()) == []  # nothing was created


def test_the_cli_gives_the_plain_message_with_no_books_url(monkeypatch, tmp_path):
    from erpclaw_cli.main import app

    monkeypatch.chdir(tmp_path)
    out = CliRunner().invoke(app, ["init"])
    assert out.exit_code == 1 and NEEDS_POSTGRESQL in " ".join(out.output.split())
    assert list(tmp_path.iterdir()) == []


def test_existing_foreign_database_is_refused():
    eng = empty_books()
    try:
        with eng.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE gl_entry (id text)")
        with pytest.raises(ForeignBooks):
            make_engine(eng.url.render_as_string(hide_password=False))
    finally:
        drop_everything(eng)
        eng.dispose()


def test_empty_database_becomes_ours_and_reopens():
    eng = empty_books()
    url = eng.url.render_as_string(hide_password=False)
    try:
        create_all(eng)
        make_engine(url).dispose()
    finally:
        drop_everything(eng)
        eng.dispose()
