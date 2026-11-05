"""The numbered format-2 revision follows file storage and installs on empty books."""
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from erpclaw_core.migrate import config, upgrade
from conftest import drop_everything


def test_revision_0011_follows_the_file_store_in_one_core_chain():
    scripts = ScriptDirectory.from_config(config(plugins=False))
    revision = scripts.get_revision("0011")
    assert revision.down_revision == "0010"
    ordered = [s.revision for s in scripts.walk_revisions(head="0011")]
    assert ordered == [f"{n:04d}" for n in range(11, 0, -1)]


def test_revision_0011_installs_and_reapplying_preserves_empty_format2(engine):
    drop_everything(engine)
    with engine.begin() as conn:
        upgrade(conn, "0010")
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0010"
        assert "dimension_set" not in inspect(conn).get_table_names()
        upgrade(conn, "0011")
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0011"
        tables = inspect(conn).get_table_names()
        assert "file_object" in tables and "dimension_set" in tables
        assert conn.execute(text("SELECT count(*) FROM dimension_set")).scalar_one() == 0
        columns = {c["name"]: (str(c["type"]), c["nullable"]) for c in inspect(conn).get_columns("ledger_entry")}
        assert all(n in columns for n in ("rate", "functional_currency", "functional_debit",
                                         "functional_credit", "kind", "dimension_set_id"))
        assert columns["dimension_set_id"][1] is True
        upgrade(conn, "0011")
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0011"
        assert inspect(conn).get_table_names() == tables
        assert {c["name"]: (str(c["type"]), c["nullable"]) for c in inspect(conn).get_columns("ledger_entry")} == columns
        assert conn.execute(text("SELECT count(*) FROM dimension_set")).scalar_one() == 0
