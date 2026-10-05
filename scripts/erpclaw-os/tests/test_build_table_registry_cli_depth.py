"""Source ownership evidence through the build-table-registry CLI."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


OS_DIR = Path(__file__).resolve().parents[1]
SETUP_DIR = OS_DIR.parent / "erpclaw-setup"


def _run(*flags):
    environment = dict(os.environ)
    environment["ERPCLAW_HOME"] = str(SETUP_DIR)
    environment["PYTHONPATH"] = str(SETUP_DIR / "lib")
    result = subprocess.run(
        [sys.executable, str(OS_DIR / "db_query.py"),
         "--action", "build-table-registry", *flags],
        env=environment, capture_output=True, text=True, timeout=30,
    )
    return result, json.loads(result.stdout)


def _schema_file(path, names):
    path.parent.mkdir(parents=True, exist_ok=True)
    declarations = "\n".join(
        f"Table({name!r}, metadata, Column('id', Text, primary_key=True))"
        for name in names
    )
    path.write_text(
        "from erpclaw_lib.seam import MetaData, Table, Column, Text\n"
        "metadata = MetaData()\n"
        + declarations
        + "\nraise RuntimeError('Registry scanning must not execute source files')\n",
        encoding="utf-8",
    )


def test_registry_identifies_core_and_nested_module_ownership_without_execution(tmp_path):
    source = tmp_path / "source"
    _schema_file(source / "erpclaw/scripts/erpclaw-setup/init_schema.py", ["company"])
    _schema_file(source / "erpclaw-addons/billingclaw/init_db.py",
                 ["billingclaw_invoice", "billingclaw_charge"])
    _schema_file(source / "erpclaw-addons/billingclaw/vendor/foreign/init_db.py",
                 ["foreign_vendor_table"])
    before = {path.relative_to(source): path.read_bytes()
              for path in source.rglob("*") if path.is_file()}

    process, result = _run("--src-root", str(source))

    assert process.returncode == 0, process.stderr
    assert result == {
        "status": "ok",
        "total_tables": 3,
        "total_modules": 2,
        "registry": {
            "company": "erpclaw",
            "billingclaw_invoice": "billingclaw",
            "billingclaw_charge": "billingclaw",
        },
        "by_module": {
            "billingclaw": ["billingclaw_charge", "billingclaw_invoice"],
            "erpclaw": ["company"],
        },
    }
    assert {path.relative_to(source): path.read_bytes()
            for path in source.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("kind", ["absent", "file"])
def test_registry_refuses_a_source_root_that_is_not_a_directory(tmp_path, kind):
    source = tmp_path / "invalid-source"
    if kind == "file":
        source.write_text("A file cannot be a source root.\n", encoding="utf-8")
    before = source.read_bytes() if source.exists() else None

    process, result = _run("--src-root", str(source))

    assert process.returncode == 1
    assert result["status"] == "error"
    assert str(source) in result["message"]
    assert "not a directory" in result["message"]
    assert (source.read_bytes() if source.exists() else None) == before


def test_registry_of_an_empty_source_tree_is_explicitly_empty(tmp_path):
    source = tmp_path / "empty-source"
    source.mkdir()

    process, result = _run("--src-root", str(source))

    assert process.returncode == 0, process.stderr
    assert result == {
        "status": "ok", "total_tables": 0, "total_modules": 0,
        "registry": {}, "by_module": {},
    }
    assert list(source.iterdir()) == []
