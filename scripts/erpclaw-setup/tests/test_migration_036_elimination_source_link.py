"""Part A — migration 036: elimination entries record their source transaction (M95).

Plan home: `planning/pending_items.md` row M95. SIM:
`planning/simlogs/m95_SIM_2026-08-12.md` §4.3, which states the matching rule and every
failure mode this file pins.

The migration runs over an operator's existing consolidation numbers, and the install
that most needs it is the one that already ran the defect — so the pins are weighted
toward what it must NOT do:

  * it must not delete a row, including the surplus duplicates the defect produced;
  * it must not guess a source: an unparseable description or an unmatched row stays
    NULL and is reported;
  * it must not let two elimination rows claim the same transaction inside one group and
    period, and must let two DIFFERENT groups claim the same one;
  * it must write nothing at all in `--report-only`;
  * it must reach the same end state when run twice, and after a crash.

Every pin runs the REAL migration module against a real database initialized by
`init_schema` and then rewound to its genuine pre-M95 shape (index dropped, column
dropped), with rows planted exactly as the pre-M95 generator wrote them.
"""
import importlib.util
import io
import os
import sqlite3
import sys
import uuid
from contextlib import redirect_stdout

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SETUP_DIR = os.path.dirname(_TESTS_DIR)
_MIGRATION = os.path.join(_SETUP_DIR, "migrations",
                          "036_elimination_entry_source_link.py")

if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mig = _load("migration_036", _MIGRATION)

from setup_helpers import init_all_tables  # noqa: E402  (binds erpclaw_lib to this tree)
from erpclaw_lib import seam  # noqa: E402  (after the lib binding in setup_helpers)


# ── fixtures: a database rewound to its genuine pre-M95 shape ────────────────

def _rewind_to_pre036(conn):
    """Drop the M95 index and column, so the table is shaped as it shipped before.

    A fresh install already has M95 applied (init_schema creates both), so every
    pin here has to put the database back before the migration can do anything.
    """
    conn.execute(f"DROP INDEX IF EXISTS {mig.INDEX}")
    conn.execute(f"ALTER TABLE {mig.TABLE} DROP COLUMN {mig.COLUMN}")
    conn.commit()


@pytest.fixture
def pre036(conn, db_path):
    _rewind_to_pre036(conn)
    assert mig.COLUMN not in seam.column_names(mig.TABLE, db_path)
    return db_path


def _company(conn, name):
    cid = str(uuid.uuid4())
    conn.execute("INSERT INTO company (id, name, abbr, default_currency) "
                 "VALUES (?, ?, ?, 'USD')", (cid, name, name[:4] + cid[:4]))
    conn.commit()
    return cid


def _group(conn, company_id, name="Group"):
    gid = str(uuid.uuid4())
    conn.execute("INSERT INTO advacct_consolidation_group "
                 "(id, name, parent_company_id, company_id) VALUES (?, ?, ?, ?)",
                 (gid, name, company_id, company_id))
    conn.commit()
    return gid


def _ic(conn, company_id, from_id, to_id, amount, status="posted", ttype="sale"):
    ic_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO advacct_ic_transaction (id, from_company_id, to_company_id, "
        " transaction_type, amount, ic_status, company_id) VALUES (?,?,?,?,?,?,?)",
        (ic_id, from_id, to_id, ttype, amount, status, company_id))
    conn.commit()
    return ic_id


def _plant_pre_m95_entry(conn, group_id, company_id, from_id, to_id, amount,
                         period="2026-06-30", ttype="sale", description=None,
                         entry_type="ic_elimination"):
    """Write a row exactly as the pre-M95 generator wrote it.

    Same columns, same description sentence, no source link — the generator that
    produced these rows is the code M95 replaced, so its output is planted rather
    than called.
    """
    eid = str(uuid.uuid4())
    conn.execute(
        f"INSERT INTO {mig.TABLE} (id, group_id, period_date, debit_account, "
        " credit_account, amount, description, entry_type, company_id) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (eid, group_id, period, "IC Revenue", "IC Expense", amount,
         description if description is not None
         else f"Elimination: {ttype} from {from_id} to {to_id}",
         entry_type, company_id))
    conn.commit()
    return eid


def _run(db_path, report_only=False):
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = mig.run_migration(db_path, report_only=report_only)
    return result, buf.getvalue()


def _source_of(conn, entry_id):
    return conn.execute(f"SELECT {mig.COLUMN} FROM {mig.TABLE} WHERE id = ?",
                        (entry_id,)).fetchone()[0]


def _row_count(conn):
    return conn.execute(f"SELECT COUNT(*) FROM {mig.TABLE}").fetchone()[0]


# ── the description rule, exercised without a database ───────────────────────

class TestParseSourceShape:
    def test_reads_the_generators_sentence(self):
        assert mig.parse_source_shape(
            "Elimination: sale from AAA to BBB") == ("sale", "AAA", "BBB")

    @pytest.mark.parametrize("description", [
        None,
        "",
        "Manual elimination for Q2",
        "Elimination: sale from AAA",
        "Currency translation adjustment",
        "elimination: sale from AAA to BBB",     # not the generator's capitalisation
    ])
    def test_refuses_anything_else(self, description):
        assert mig.parse_source_shape(description) is None


# ── the pairing rule, exercised without a database ───────────────────────────

class TestPlanLinks:
    def _row(self, rid, amount="100.00", group="G", period="P",
             description="Elimination: sale from A to B"):
        return {"id": rid, "group_id": group, "period_date": period,
                "amount": amount, "description": description}

    def _txn(self, tid, amount="100.00"):
        return {"id": tid, "from_company_id": "A", "to_company_id": "B",
                "transaction_type": "sale", "amount": amount}

    def test_one_transaction_is_claimed_once_per_group_and_period(self):
        rows = [self._row("e1"), self._row("e2")]     # the duplicate residue
        links, unmatched = mig.plan_links(rows, [self._txn("t1")], {})
        assert links == [("e1", "t1")]
        assert [r["id"] for r, _ in unmatched] == ["e2"]

    def test_two_groups_may_each_claim_the_same_transaction(self):
        rows = [self._row("e1", group="G1"), self._row("e2", group="G2")]
        links, unmatched = mig.plan_links(rows, [self._txn("t1")], {})
        assert links == [("e1", "t1"), ("e2", "t1")]
        assert unmatched == []

    def test_amount_must_match_exactly(self):
        links, unmatched = mig.plan_links(
            [self._row("e1", amount="100.00")], [self._txn("t1", amount="100.0")], {})
        assert links == []
        assert len(unmatched) == 1

    def test_already_claimed_transactions_are_not_reused(self):
        claimed = {("G", "P"): {"t1"}}
        links, unmatched = mig.plan_links(
            [self._row("e1")], [self._txn("t1")], claimed)
        assert links == []
        assert "no unclaimed posted transaction" in unmatched[0][1]


# ── the migration, against a real database ───────────────────────────────────

class TestMigration036:
    def test_report_only_writes_nothing(self, conn, pre036):
        c1 = _company(conn, "Parent")
        c2 = _company(conn, "Sub")
        gid = _group(conn, c1)
        txn = _ic(conn, c1, c1, c2, "25000.00")
        entry = _plant_pre_m95_entry(conn, gid, c1, c1, c2, "25000.00")

        result, out = _run(pre036, report_only=True)

        assert result["report_only"] is True
        assert result["links"] == [(entry, txn)]
        assert mig.COLUMN not in seam.column_names(mig.TABLE, pre036)
        assert mig.INDEX not in seam.index_names(mig.TABLE, pre036)
        assert "would link" in out
        assert "nothing was written" in out

    def test_real_run_links_the_row_and_creates_the_index(self, conn, pre036):
        c1 = _company(conn, "Parent")
        c2 = _company(conn, "Sub")
        gid = _group(conn, c1)
        txn = _ic(conn, c1, c1, c2, "25000.00")
        entry = _plant_pre_m95_entry(conn, gid, c1, c1, c2, "25000.00")

        result, out = _run(pre036)

        assert result["column_added"] is True
        assert result["index_created"] is True
        assert _source_of(conn, entry) == txn
        assert mig.INDEX in seam.index_names(mig.TABLE, pre036)
        assert "linked" in out

    def test_duplicate_residue_is_reported_and_never_deleted(self, conn, pre036):
        """The rows the M95 defect produced: one is linked, the surplus stays put.

        WHICH of the two is linked is deliberately not asserted. The two rows are
        byte-identical apart from their id, and `created_at` defaults to
        CURRENT_TIMESTAMP at one-second granularity, so a database holding this
        residue does not record which run wrote which row — the migration's
        (created_at, id) order is deterministic for a given database, and that is
        the whole of what can honestly be claimed. Either pairing is equally
        correct; what must hold is that exactly one is linked and neither is
        deleted.
        """
        c1 = _company(conn, "Parent")
        c2 = _company(conn, "Sub")
        gid = _group(conn, c1)
        txn = _ic(conn, c1, c1, c2, "25000.00")
        planted = {_plant_pre_m95_entry(conn, gid, c1, c1, c2, "25000.00"),
                   _plant_pre_m95_entry(conn, gid, c1, c1, c2, "25000.00")}

        result, out = _run(pre036)

        assert len(result["links"]) == 1
        linked_id, linked_txn = result["links"][0]
        assert linked_txn == txn
        assert linked_id in planted
        assert result["unmatched"] == list(planted - {linked_id})
        assert _row_count(conn) == 2, "a row was deleted"
        assert _source_of(conn, linked_id) == txn
        assert _source_of(conn, *(planted - {linked_id})) is None
        assert "NOT linked" in out
        assert "Nothing is deleted here" in out

    def test_two_indistinguishable_transactions_get_one_row_each(self, conn, pre036):
        c1 = _company(conn, "Parent")
        c2 = _company(conn, "Sub")
        gid = _group(conn, c1)
        t1 = _ic(conn, c1, c1, c2, "1000.00")
        t2 = _ic(conn, c1, c1, c2, "1000.00")
        e1 = _plant_pre_m95_entry(conn, gid, c1, c1, c2, "1000.00")
        e2 = _plant_pre_m95_entry(conn, gid, c1, c1, c2, "1000.00")

        _run(pre036)

        linked = {_source_of(conn, e1), _source_of(conn, e2)}
        assert linked == {t1, t2}

    def test_two_groups_eliminating_the_same_transaction_both_link(self, conn, pre036):
        c1 = _company(conn, "Parent")
        c2 = _company(conn, "Sub")
        g1 = _group(conn, c1, "Sub-group")
        g2 = _group(conn, c1, "Top group")
        txn = _ic(conn, c1, c1, c2, "500.00")
        e1 = _plant_pre_m95_entry(conn, g1, c1, c1, c2, "500.00")
        e2 = _plant_pre_m95_entry(conn, g2, c1, c1, c2, "500.00")

        _run(pre036)

        assert _source_of(conn, e1) == txn
        assert _source_of(conn, e2) == txn

    def test_unparseable_description_is_left_alone_and_reported(self, conn, pre036):
        c1 = _company(conn, "Parent")
        c2 = _company(conn, "Sub")
        gid = _group(conn, c1)
        _ic(conn, c1, c1, c2, "300.00")
        entry = _plant_pre_m95_entry(conn, gid, c1, c1, c2, "300.00",
                                     description="Manual elimination, Q2 review")

        result, out = _run(pre036)

        assert result["links"] == []
        assert result["unmatched"] == [entry]
        assert _source_of(conn, entry) is None
        assert "not the generator's sentence" in out

    def test_unposted_transactions_are_not_a_match(self, conn, pre036):
        """Only posted transactions were ever eliminated, so only they can be a source."""
        c1 = _company(conn, "Parent")
        c2 = _company(conn, "Sub")
        gid = _group(conn, c1)
        _ic(conn, c1, c1, c2, "700.00", status="approved")
        entry = _plant_pre_m95_entry(conn, gid, c1, c1, c2, "700.00")

        result, _ = _run(pre036)

        assert result["unmatched"] == [entry]
        assert _source_of(conn, entry) is None

    def test_currency_translation_rows_are_untouched(self, conn, pre036):
        c1 = _company(conn, "Parent")
        gid = _group(conn, c1)
        cta = _plant_pre_m95_entry(
            conn, gid, c1, c1, c1, "5000.00",
            description="Currency translation adjustment",
            entry_type="currency_translation")

        result, _ = _run(pre036)

        assert result["links"] == []
        assert result["unmatched"] == []          # not even considered
        assert _source_of(conn, cta) is None
        assert _row_count(conn) == 1

    def test_running_twice_changes_nothing_the_second_time(self, conn, pre036):
        c1 = _company(conn, "Parent")
        c2 = _company(conn, "Sub")
        gid = _group(conn, c1)
        txn = _ic(conn, c1, c1, c2, "900.00")
        entry = _plant_pre_m95_entry(conn, gid, c1, c1, c2, "900.00")

        first, _ = _run(pre036)
        second, out = _run(pre036)

        assert first["links"] == [(entry, txn)]
        assert second["links"] == []
        assert second["column_added"] is False
        assert second["index_created"] is False
        assert _source_of(conn, entry) == txn
        assert _row_count(conn) == 1
        assert "no ic_elimination row needed linking" in out

    def test_a_failure_in_phase_two_leaves_no_half_linked_table(self, conn, pre036,
                                                               monkeypatch):
        """Crash safety, as the docstring states it: links and index are atomic.

        The column may survive (SQLite self-commits DDL outside a transaction, which
        is why the migration commits it deliberately), but no row may claim a source
        without the index that enforces uniqueness having been created too.
        """
        c1 = _company(conn, "Parent")
        c2 = _company(conn, "Sub")
        gid = _group(conn, c1)
        txn = _ic(conn, c1, c1, c2, "1200.00")
        entry = _plant_pre_m95_entry(conn, gid, c1, c1, c2, "1200.00")

        monkeypatch.setattr(mig, "_CREATE_INDEX",
                            "CREATE UNIQUE INDEX no_such_index ON no_such_table(x)")
        with pytest.raises(Exception):
            mig.run_migration(pre036)

        assert _source_of(conn, entry) is None, "a link survived a failed run"
        assert mig.INDEX not in seam.index_names(mig.TABLE, pre036)

        # And the re-run finishes the job, which is what the runner instructs.
        monkeypatch.undo()
        result, _ = _run(pre036)
        assert result["links"] == [(entry, txn)]
        assert _source_of(conn, entry) == txn
        assert mig.INDEX in seam.index_names(mig.TABLE, pre036)

    def test_absent_table_is_not_an_error(self, conn, pre036):
        conn.execute(f"DROP TABLE {mig.TABLE}")
        conn.commit()
        result, out = _run(pre036)
        assert result["reason"] == "table absent"
        assert "Nothing to do" in out


# ── what the migrated database ENFORCES, not just what it is named ───────────
#
# Merge-QA rider R2. Every pin above asked `mig.INDEX in seam.index_names(...)`
# — existence by name. Two mutations of this migration passed that and passed
# the whole setup and accounting-adv suites with it: creating a NON-UNIQUE index
# of the same name, and dropping `period_date` out of the key. Both are caught
# on a FRESH install by the accounting-adv tests; neither was caught here, and
# the migrated path is the one every existing install takes.
#
# So the constraint is read off the migrated database as the database itself
# describes it, and then exercised.

KEY_COLUMNS = ["group_id", "period_date", mig.COLUMN]


def _reflected_index(db_path, name=None):
    """The index as the database describes it: uniqueness and key columns.

    `seam.index_names` answers only "is something with this name here", which is
    exactly the question the two surviving mutations both answered yes to. The
    seam has no public "describe this index", so its inspector is used directly
    — the same thing `testing/unit/L0/test_seam_index_visibility.py` does, and
    for the same reason: nothing else can see the property being asserted.
    Dialect-neutral either way; SQLAlchemy reflects a partial index on both
    backends.
    """
    for index in seam._inspector(db_path).get_indexes(mig.TABLE):
        if index.get("name") == (name or mig.INDEX):
            return index
    return None


def _insert_entry(conn, group_id, company_id, source, period="2026-06-30",
                  amount="25000.00"):
    """Write an elimination row claiming `source`, the way generation does."""
    eid = str(uuid.uuid4())
    conn.execute(
        f"INSERT INTO {mig.TABLE} (id, group_id, period_date, debit_account, "
        f" credit_account, amount, description, entry_type, {mig.COLUMN}, "
        " company_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (eid, group_id, period, "IC Revenue", "IC Expense", amount,
         "Elimination: sale from X to Y", "ic_elimination", source, company_id,
         "2026-06-30T00:00:00Z"))
    conn.commit()
    return eid


class TestMigratedDatabaseEnforcesTheKey:
    def _migrated(self, conn, pre036):
        """A migrated database plus the ids a second write needs."""
        c1 = _company(conn, "Parent")
        c2 = _company(conn, "Sub")
        gid = _group(conn, c1)
        txn = _ic(conn, c1, c1, c2, "25000.00")
        _plant_pre_m95_entry(conn, gid, c1, c1, c2, "25000.00")
        result, _ = _run(pre036)
        assert result["index_created"] is True
        return c1, gid, txn

    def test_the_index_is_unique_over_the_whole_key(self, conn, pre036):
        """Named, unique, and over the three columns — read off the database."""
        self._migrated(conn, pre036)

        index = _reflected_index(pre036)
        assert index is not None, f"{mig.INDEX} is absent after the migration"
        assert index["unique"], (
            "the migrated index is NOT unique: two generators racing would both "
            "write, which is the defect this migration exists to close")
        assert index["column_names"] == KEY_COLUMNS, (
            f"migrated key is {index['column_names']}, not {KEY_COLUMNS}")

    def test_a_second_row_for_the_same_source_is_refused(self, conn, pre036):
        """Uniqueness, exercised rather than reflected."""
        c1, gid, txn = self._migrated(conn, pre036)

        with pytest.raises(sqlite3.IntegrityError):
            _insert_entry(conn, gid, c1, txn)
        conn.rollback()
        assert _row_count(conn) == 1

    def test_the_key_carries_the_period(self, conn, pre036):
        """Same group, same source, a DIFFERENT period: allowed.

        Dropping `period_date` from the migrated key would refuse this — a
        second period could never be consolidated on a migrated install, while
        a fresh one consolidated it fine.
        """
        c1, gid, txn = self._migrated(conn, pre036)

        _insert_entry(conn, gid, c1, txn, period="2026-09-30")
        assert _row_count(conn) == 2

    def test_the_key_carries_the_group(self, conn, pre036):
        """Same source, same period, a different group: allowed.

        Two consolidation groups may each eliminate the same transaction for
        themselves; the migrated key must scope to the group like the fresh one.
        """
        c1, gid, txn = self._migrated(conn, pre036)
        other = _group(conn, c1, "Top group")

        _insert_entry(conn, other, c1, txn)
        assert _row_count(conn) == 2

    def test_rows_with_no_source_stay_repeatable(self, conn, pre036):
        """The predicate survived the migration: NULL sources sit outside it.

        A hand-authored currency translation carries no source transaction, and
        an unmatched row left NULL by this migration is the duplicate residue.
        Neither may be caught by a uniqueness rule about sources.
        """
        c1, gid, _txn = self._migrated(conn, pre036)

        for _ in range(2):
            _plant_pre_m95_entry(conn, gid, c1, c1, c1, "5000.00",
                                 description="Currency translation adjustment",
                                 entry_type="currency_translation")
        assert _row_count(conn) == 3

    def test_the_migrated_and_fresh_constraints_agree(self, conn, pre036, tmp_path):
        """The claim the docstring makes, checked instead of asserted.

        Same name, same uniqueness, same key columns as a fresh install. Column
        ORDER and index DDL TEXT are NOT claimed and do not match — ALTER TABLE
        ADD COLUMN appends — which is stated in migration 036's docstring and in
        SIM 4.2 rather than papered over here.
        """
        self._migrated(conn, pre036)
        fresh = str(tmp_path / "fresh.sqlite")
        init_all_tables(fresh)

        migrated_index = _reflected_index(pre036)
        fresh_index = _reflected_index(fresh)
        assert fresh_index["unique"] == migrated_index["unique"]
        assert fresh_index["column_names"] == migrated_index["column_names"]
        assert (sorted(seam.column_names(mig.TABLE, fresh))
                == sorted(seam.column_names(mig.TABLE, pre036)))
        # And the part that legitimately differs, pinned so nobody re-asserts
        # sameness later: the fresh install declares the column mid-table.
        assert (seam.column_names(mig.TABLE, fresh)
                != seam.column_names(mig.TABLE, pre036))
        assert seam.column_names(mig.TABLE, pre036)[-1] == mig.COLUMN


class TestMigration036MakesGenerationIdempotent:
    """The point of the migration, end to end: after it, a re-run is a no-op."""

    def _consolidation(self):
        return _load("consolidation_m95", os.path.join(
            os.path.dirname(_SETUP_DIR), "erpclaw-accounting-adv", "consolidation.py"))

    def _group_with_two_entities(self, conn):
        c1 = _company(conn, "Parent")
        c2 = _company(conn, "Sub")
        gid = _group(conn, c1)
        for entity in (c1, c2):
            conn.execute("INSERT INTO advacct_group_entity (id, group_id, "
                         " entity_company_id, entity_name, company_id) "
                         "VALUES (?,?,?,?,?)",
                         (str(uuid.uuid4()), gid, entity, f"E-{entity[:4]}", c1))
        conn.commit()
        return c1, c2, gid

    def test_generation_refuses_on_an_unmigrated_install(self, conn, pre036):
        """New code, old schema: refuse with an instruction, never duplicate.

        An operator who updates the foundation without running its migrations
        would otherwise get the defect back — silently, because the pre-M95 INSERT
        would still succeed. A raw "no such column" is the M70 failure shape; this
        names the command that fixes it.
        """
        import argparse
        consolidation = self._consolidation()
        c1, c2, gid = self._group_with_two_entities(conn)
        _ic(conn, c1, c1, c2, "25000.00")

        buf = io.StringIO()
        with redirect_stdout(buf), pytest.raises(SystemExit) as excinfo:
            consolidation.generate_elimination_entries(
                conn, argparse.Namespace(group_id=gid, period_date="2026-06-30",
                                         company_id=c1))
        import json
        response = json.loads(buf.getvalue().strip())
        assert excinfo.value.code == 1
        assert response["status"] == "error"
        assert mig.COLUMN in response["message"]
        assert "--action migrate" in response["suggestion"]
        assert _row_count(conn) == 0, "the refused run wrote an entry anyway"

    def test_generation_skips_a_backfilled_entry(self, conn, pre036):
        import argparse
        consolidation = _load("consolidation_m95", os.path.join(
            os.path.dirname(_SETUP_DIR), "erpclaw-accounting-adv", "consolidation.py"))

        c1 = _company(conn, "Parent")
        c2 = _company(conn, "Sub")
        gid = _group(conn, c1)
        for entity in (c1, c2):
            conn.execute("INSERT INTO advacct_group_entity (id, group_id, "
                         " entity_company_id, entity_name, company_id) "
                         "VALUES (?,?,?,?,?)",
                         (str(uuid.uuid4()), gid, entity, f"E-{entity[:4]}", c1))
        conn.commit()
        txn = _ic(conn, c1, c1, c2, "25000.00")
        entry = _plant_pre_m95_entry(conn, gid, c1, c1, c2, "25000.00")

        _run(pre036)
        assert _source_of(conn, entry) == txn

        buf = io.StringIO()
        args = argparse.Namespace(group_id=gid, period_date="2026-06-30",
                                  company_id=c1)
        with redirect_stdout(buf), pytest.raises(SystemExit):
            consolidation.generate_elimination_entries(conn, args)
        import json
        response = json.loads(buf.getvalue().strip())
        assert response["entries_created"] == 0
        assert response["outcome"] == "already_eliminated"
        assert response["skipped_ic_transaction_ids"] == [txn]
        assert _row_count(conn) == 1
