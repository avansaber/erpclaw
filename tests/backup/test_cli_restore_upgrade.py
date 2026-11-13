"""The CLI exposes the existing safe restore-and-upgrade path."""
import zipfile

import pytest
from sqlalchemy.engine import make_url
from typer.testing import CliRunner

from backup_support import nothing_published, open_target, pg_new, snapshot, upgradeable_books  # noqa: F401
from erpclaw_cli.main import app
from erpclaw_core import backup_reflect
from erpclaw_core.plugins import check_installed


@pytest.fixture
def old_backup(engine, tmp_path, monkeypatch):
    upgradeable_books(engine)
    directory = tmp_path / 'backup'
    backup_reflect.backup(engine, directory)
    archive = tmp_path / 'backup.zip'
    with zipfile.ZipFile(archive, 'w') as out:
        for path in directory.rglob('*'):
            if path.is_file():
                out.write(path, path.relative_to(directory))
    monkeypatch.setenv('ERPCLAW_MIGRATION_URL', engine.url.render_as_string(hide_password=False))
    return archive


def _restore(backup, target, upgrade=False):
    args = ['restore', str(backup), '--to-database', target]
    if upgrade:
        args.append('--upgrade')
    return CliRunner().invoke(app, args)


def test_upgrade_opens_older_backup_in_new_database(engine, old_backup, pg_new):
    before = snapshot(engine)
    target = pg_new()
    result = _restore(old_backup, target.name, upgrade=True)
    assert result.exit_code == 0, result.output
    assert 'Restored and proven' in result.output
    restored = open_target(target)
    try:
        check_installed(restored)
    finally:
        restored.dispose()
    assert snapshot(engine) == before
    assert make_url(target.server_url).password not in result.output
    assert target.server_url not in result.output


def test_without_upgrade_the_same_older_backup_is_refused(old_backup, pg_new):
    target = pg_new()
    result = _restore(old_backup, target.name)
    assert result.exit_code == 1
    assert 'Nothing was published' in result.output
    nothing_published(target)


def test_upgrade_refuses_existing_target_without_changing_it(engine, old_backup):
    before = snapshot(engine)
    result = _restore(old_backup, engine.url.database, upgrade=True)
    assert result.exit_code == 1
    assert 'Nothing was published' in result.output
    assert make_url(engine.url).password not in result.output
    assert snapshot(engine) == before
