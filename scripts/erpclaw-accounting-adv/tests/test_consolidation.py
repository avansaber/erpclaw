"""Tests for erpclaw-accounting-adv Multi-Entity Consolidation actions.

Actions tested: add-consolidation-group, list-consolidation-groups, add-group-entity,
                run-consolidation, generate-elimination-entries,
                add-currency-translation, consolidation-trial-balance-report,
                consolidation-summary
"""
import json
import pytest
from decimal import Decimal
from advacct_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
)

mod = load_db_query()


def _add_group(conn, env, name="Global Holdings Group"):
    return call_action(mod.add_consolidation_group, conn, ns(
        company_id=env["company_id"], name=name,
        parent_company_id=env["company_id"],
        consolidation_currency="USD",
    ))


def _add_entity(conn, env, group_id, entity_company_id=None, entity_name="Subsidiary A",
                ownership_pct="100"):
    return call_action(mod.add_group_entity, conn, ns(
        group_id=group_id, company_id=env["company_id"],
        entity_company_id=entity_company_id or env["company_id"],
        entity_name=entity_name, ownership_pct=ownership_pct,
        functional_currency="USD", consolidation_method="full",
    ))


def _setup_group_with_entities(conn, env):
    """Create a group with two entities."""
    grp = _add_group(conn, env)
    _add_entity(conn, env, grp["id"], env["company_id"], "Parent Corp", "100")
    _add_entity(conn, env, grp["id"], env["company2_id"], "Subsidiary Inc", "80")
    return grp


# ──────────────────────────────────────────────────────────────────────────────
# Consolidation Groups
# ──────────────────────────────────────────────────────────────────────────────

class TestAddConsolidationGroup:
    def test_basic_create(self, conn, env):
        result = _add_group(conn, env)
        assert is_ok(result)
        assert result["name"] == "Global Holdings Group"
        assert result["group_status"] == "active"

    def test_missing_name_fails(self, conn, env):
        result = call_action(mod.add_consolidation_group, conn, ns(
            company_id=env["company_id"], name=None,
            parent_company_id=None, consolidation_currency=None,
        ))
        assert is_error(result)

    def test_missing_company_fails(self, conn, env):
        result = call_action(mod.add_consolidation_group, conn, ns(
            company_id=None, name="Test Group",
            parent_company_id=None, consolidation_currency=None,
        ))
        assert is_error(result)


class TestListConsolidationGroups:
    def test_list(self, conn, env):
        _add_group(conn, env)
        result = call_action(mod.list_consolidation_groups, conn, ns(
            company_id=env["company_id"], group_status=None,
            search=None, limit=50, offset=0,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1


# ──────────────────────────────────────────────────────────────────────────────
# Group Entities
# ──────────────────────────────────────────────────────────────────────────────

class TestAddGroupEntity:
    def test_basic_create(self, conn, env):
        grp = _add_group(conn, env)
        result = _add_entity(conn, env, grp["id"], env["company2_id"],
                             "Subsidiary Inc", "80")
        assert is_ok(result)
        assert result["entity_name"] == "Subsidiary Inc"
        assert result["ownership_pct"] == "80"
        assert result["consolidation_method"] == "full"

    def test_missing_entity_name_fails(self, conn, env):
        grp = _add_group(conn, env)
        result = call_action(mod.add_group_entity, conn, ns(
            group_id=grp["id"], company_id=env["company_id"],
            entity_company_id=env["company2_id"],
            entity_name=None, ownership_pct="100",
            functional_currency=None, consolidation_method=None,
        ))
        assert is_error(result)

    def test_invalid_consolidation_method_fails(self, conn, env):
        grp = _add_group(conn, env)
        result = call_action(mod.add_group_entity, conn, ns(
            group_id=grp["id"], company_id=env["company_id"],
            entity_company_id=env["company2_id"],
            entity_name="Test Entity", ownership_pct="100",
            functional_currency=None, consolidation_method="invalid",
        ))
        assert is_error(result)


# ──────────────────────────────────────────────────────────────────────────────
# Consolidation Operations
# ──────────────────────────────────────────────────────────────────────────────

class TestRunConsolidation:
    def test_run(self, conn, env):
        grp = _setup_group_with_entities(conn, env)
        result = call_action(mod.run_consolidation, conn, ns(
            group_id=grp["id"], period_date="2026-06-30",
        ))
        assert is_ok(result)
        assert result["entity_count"] == 2
        assert result["consolidation_run"] == "completed"

    def test_no_entities_fails(self, conn, env):
        grp = _add_group(conn, env)
        result = call_action(mod.run_consolidation, conn, ns(
            group_id=grp["id"], period_date="2026-06-30",
        ))
        assert is_error(result)

    def test_missing_period_fails(self, conn, env):
        grp = _setup_group_with_entities(conn, env)
        result = call_action(mod.run_consolidation, conn, ns(
            group_id=grp["id"], period_date=None,
        ))
        assert is_error(result)


def _post_ic(conn, env, amount="25000.00", description="IC sale"):
    """Add + approve + post one IC transaction; return its id.

    All three steps matter: `generate-elimination-entries` only eliminates
    transactions whose ic_status is 'posted', so a helper that stops at "add"
    would make every test below pass by eliminating nothing.
    """
    ic = call_action(mod.add_ic_transaction, conn, ns(
        company_id=env["company_id"],
        from_company_id=env["company_id"],
        to_company_id=env["company2_id"],
        transaction_type="sale", amount=amount,
        description=description, currency="USD",
        transfer_price_method=None,
    ))
    call_action(mod.approve_ic_transaction, conn, ns(id=ic["id"]))
    call_action(mod.post_ic_transaction, conn, ns(id=ic["id"]))
    return ic["id"]


def _generate(conn, env, group_id, period_date="2026-06-30"):
    return call_action(mod.generate_elimination_entries, conn, ns(
        group_id=group_id, period_date=period_date,
        company_id=env["company_id"],
    ))


def _elimination_rows(conn, group_id):
    return conn.execute(
        "SELECT id, period_date, amount, entry_type, source_ic_transaction_id "
        "FROM advacct_elimination_entry WHERE group_id = ? ORDER BY created_at, id",
        (group_id,)
    ).fetchall()


class TestGenerateEliminationEntries:
    def test_generate_with_posted_ic(self, conn, env):
        grp = _setup_group_with_entities(conn, env)
        ic_id = _post_ic(conn, env)

        result = _generate(conn, env, grp["id"])
        assert is_ok(result)
        assert result["entries_created"] >= 1
        assert result["outcome"] == "created"
        assert result["eliminated_ic_transaction_ids"] == [ic_id]
        # The row records WHERE it came from — the whole basis of re-run safety.
        rows = _elimination_rows(conn, grp["id"])
        assert len(rows) == 1
        assert rows[0]["source_ic_transaction_id"] == ic_id

    def test_too_few_entities_fails(self, conn, env):
        grp = _add_group(conn, env)
        _add_entity(conn, env, grp["id"], env["company_id"], "Only One", "100")
        result = call_action(mod.generate_elimination_entries, conn, ns(
            group_id=grp["id"], period_date="2026-06-30",
            company_id=env["company_id"],
        ))
        assert is_error(result)


# ──────────────────────────────────────────────────────────────────────────────
# M95 — generation is safe to re-run
#
# The engine this flow replaced (the retired `run-elimination`) skipped when an
# entry already existed. Until M95 the replacement did not: a second run for the
# same group and period returned entries_created: 1 again and left a duplicate
# row, which the consolidation trial balance then double-counted (measured:
# 50,000.00 of eliminations against one 25,000.00 transaction). M63-C's steer
# sends every user of the old flow straight here by name, so the duplicate was
# reachable by following our own instructions.
#
# Plan home: planning/pending_items.md row M95.
# SIM: planning/simlogs/m95_SIM_2026-08-12.md
# ──────────────────────────────────────────────────────────────────────────────

class TestGenerateEliminationEntriesIsIdempotent:
    def test_second_run_creates_nothing_and_says_so(self, conn, env):
        grp = _setup_group_with_entities(conn, env)
        ic_id = _post_ic(conn, env)

        first = _generate(conn, env, grp["id"])
        assert first["entries_created"] == 1
        assert first["outcome"] == "created"

        second = _generate(conn, env, grp["id"])
        assert is_ok(second)
        assert second["entries_created"] == 0
        assert second["entries_skipped"] == 1
        assert second["outcome"] == "already_eliminated"
        assert second["skipped_ic_transaction_ids"] == [ic_id]
        assert second["eliminated_ic_transaction_ids"] == []

        # The database, not just the response.
        rows = _elimination_rows(conn, grp["id"])
        assert len(rows) == 1, "second run left a duplicate elimination row"

    def test_report_does_not_double_count_after_a_re_run(self, conn, env):
        """The user-visible harm: the consolidated trial balance."""
        grp = _setup_group_with_entities(conn, env)
        _post_ic(conn, env, amount="25000.00")
        _generate(conn, env, grp["id"])
        _generate(conn, env, grp["id"])

        report = call_action(mod.consolidation_trial_balance_report, conn, ns(
            group_id=grp["id"], period_date="2026-06-30",
        ))
        assert len(report["elimination_entries"]) == 1
        assert Decimal(report["total_eliminations"]) == Decimal("25000.00")

    def test_re_run_after_new_activity_eliminates_only_the_new(self, conn, env):
        """The legitimate re-run: refusing or no-op'ing here would be a worse bug.

        A user posts more intercompany activity into the same period and runs
        generation again. The correct outcome is that the NEW transaction gets
        eliminated, the old one does not get eliminated twice.
        """
        grp = _setup_group_with_entities(conn, env)
        first_ic = _post_ic(conn, env, amount="25000.00", description="January sale")

        first = _generate(conn, env, grp["id"])
        assert first["entries_created"] == 1

        second_ic = _post_ic(conn, env, amount="4000.00", description="February sale")
        second = _generate(conn, env, grp["id"])
        assert second["entries_created"] == 1
        assert second["entries_skipped"] == 1
        assert second["outcome"] == "created"
        assert second["eliminated_ic_transaction_ids"] == [second_ic]
        assert second["skipped_ic_transaction_ids"] == [first_ic]

        rows = _elimination_rows(conn, grp["id"])
        assert len(rows) == 2
        assert sorted(r["source_ic_transaction_id"] for r in rows) == \
            sorted([first_ic, second_ic])
        assert sum(Decimal(r["amount"]) for r in rows) == Decimal("29000.00")

    def test_two_indistinguishable_transactions_both_get_eliminated(self, conn, env):
        """Identical shape is not identity.

        Two separate posted sales with the same from/to/type/amount produce
        elimination rows that are byte-identical apart from the row id. A key
        derived from the row's CONTENT would collapse them and silently
        under-eliminate; the key is the source transaction id.
        """
        grp = _setup_group_with_entities(conn, env)
        a = _post_ic(conn, env, amount="1000.00", description="January shipment")
        b = _post_ic(conn, env, amount="1000.00", description="February shipment")

        first = _generate(conn, env, grp["id"])
        assert first["entries_created"] == 2
        assert sorted(first["eliminated_ic_transaction_ids"]) == sorted([a, b])

        again = _generate(conn, env, grp["id"])
        assert again["entries_created"] == 0
        assert again["entries_skipped"] == 2
        rows = _elimination_rows(conn, grp["id"])
        assert len(rows) == 2
        assert sum(Decimal(r["amount"]) for r in rows) == Decimal("2000.00")

    def test_nothing_to_eliminate_is_not_the_same_as_already_done(self, conn, env):
        """The two kinds of "nothing happened" must be tellable apart.

        An agent that reads entries_created: 0 and reports "already done" to a
        user who never posted their transaction repeats M63-C's round-1 steer
        failure. `outcome` is the distinction.
        """
        grp = _setup_group_with_entities(conn, env)

        empty = _generate(conn, env, grp["id"])
        assert is_ok(empty)
        assert empty["entries_created"] == 0
        assert empty["entries_skipped"] == 0
        assert empty["outcome"] == "nothing_to_eliminate"
        assert "posted" in empty["message"].lower()

        _post_ic(conn, env)
        _generate(conn, env, grp["id"])
        done = _generate(conn, env, grp["id"])
        assert done["outcome"] == "already_eliminated"
        assert done["outcome"] != empty["outcome"]

    def test_a_different_period_keeps_its_own_entries(self, conn, env):
        """The key includes the period; it is not a group-wide "done" flag."""
        grp = _setup_group_with_entities(conn, env)
        ic_id = _post_ic(conn, env)

        _generate(conn, env, grp["id"], period_date="2026-06-30")
        other = _generate(conn, env, grp["id"], period_date="2026-09-30")
        assert other["entries_created"] == 1
        assert other["outcome"] == "created"

        rows = _elimination_rows(conn, grp["id"])
        assert len(rows) == 2
        assert {r["period_date"] for r in rows} == {"2026-06-30", "2026-09-30"}
        assert {r["source_ic_transaction_id"] for r in rows} == {ic_id}

    def test_a_second_group_eliminates_the_same_transaction_for_itself(self, conn, env):
        """One transaction, two groups, two eliminations — one per group.

        Nothing stops the same two companies being entities of two consolidation
        groups (a sub-group plus a top-level group is ordinary). Each group must
        eliminate the transaction for itself, which is why the key is per group
        and why a single 'eliminated' flag on the transaction would not do.
        """
        grp_a = _setup_group_with_entities(conn, env)
        grp_b = _add_group(conn, env, name="Second Group")
        _add_entity(conn, env, grp_b["id"], env["company_id"], "Parent Corp", "100")
        _add_entity(conn, env, grp_b["id"], env["company2_id"], "Subsidiary Inc", "80")
        ic_id = _post_ic(conn, env)

        a = _generate(conn, env, grp_a["id"])
        b = _generate(conn, env, grp_b["id"])
        assert a["entries_created"] == 1
        assert b["entries_created"] == 1
        assert b["eliminated_ic_transaction_ids"] == [ic_id]
        assert len(_elimination_rows(conn, grp_a["id"])) == 1
        assert len(_elimination_rows(conn, grp_b["id"])) == 1

    def test_manual_currency_translations_stay_repeatable(self, conn, env):
        """A CTA entry is authored, not derived: it carries no source and the
        uniqueness constraint must not reach it."""
        grp = _setup_group_with_entities(conn, env)
        for _ in range(2):
            result = call_action(mod.add_currency_translation, conn, ns(
                group_id=grp["id"], company_id=env["company_id"],
                period_date="2026-06-30", amount="5000.00",
                debit_account=None, credit_account=None, description=None,
            ))
            assert is_ok(result)

        rows = _elimination_rows(conn, grp["id"])
        assert len(rows) == 2
        assert all(r["source_ic_transaction_id"] is None for r in rows)

    def test_duplicate_source_link_is_refused_by_the_database(self, conn, env):
        """The application check is not the only guard.

        Two generators running at once could both see "not yet eliminated". The
        partial unique index is the backstop, so the loser writes nothing rather
        than a duplicate.
        """
        import sqlite3 as _sqlite3
        grp = _setup_group_with_entities(conn, env)
        ic_id = _post_ic(conn, env)
        _generate(conn, env, grp["id"])

        with pytest.raises(_sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO advacct_elimination_entry "
                "(id, group_id, period_date, debit_account, credit_account, amount, "
                " description, entry_type, source_ic_transaction_id, company_id, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("forced-duplicate", grp["id"], "2026-06-30", "IC Revenue",
                 "IC Expense", "25000.00", "second writer", "ic_elimination",
                 ic_id, env["company_id"], "2026-06-30T00:00:00Z"))
        conn.rollback()


# ──────────────────────────────────────────────────────────────────────────────
# M95 merge-QA riders R3 + R4 — the two paths where the guard itself can fail.
#
# Both are driven through connections that misbehave in ONE specific way, so the
# interleaving being tested is deterministic rather than raced. Everything else
# is the real connection and the real action.
# ──────────────────────────────────────────────────────────────────────────────

_ALREADY_ELIMINATED_QUERY = "SELECT source_ic_transaction_id"


class _ConnProxy:
    """Delegates everything to a real connection; subclasses bend one query."""

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_conn"), name)


class _AlreadyEliminatedReadFails(_ConnProxy):
    """The "what is already eliminated" read raises; every other query works.

    A swallowed failure here does not stop the run — it hands generation an
    empty set, which MEANS "nothing is eliminated yet", and the run proceeds to
    re-create what is already there. That is why the re-raise is behaviour and
    not tidiness.
    """

    def __init__(self, conn, error):
        super().__init__(conn)
        self._error = error

    def execute(self, sql, params=()):
        if _ALREADY_ELIMINATED_QUERY in sql:
            raise self._error
        return self._conn.execute(sql, params)


class _SeesNoEliminationsYet(_ConnProxy):
    """The same read answers "nothing", after another writer has committed.

    This is the concurrency window replayed deterministically: a generator's
    check ran before a second generator's commit, so it believes the transaction
    is unclaimed and tries to write it. Four generators racing produce this state
    sometimes; one connection produces it every time.
    """

    def execute(self, sql, params=()):
        if _ALREADY_ELIMINATED_QUERY in sql:
            return self._conn.execute(
                "SELECT source_ic_transaction_id FROM advacct_elimination_entry "
                "WHERE 1 = 0")
        return self._conn.execute(sql, params)


class TestGenerationSurvivesItsOwnGuardsFailing:
    def test_a_database_failure_that_is_not_the_column_is_never_swallowed(
            self, conn, env):
        """R3. A missing column is a directed refusal; everything else propagates.

        Turning any other database failure into "nothing is eliminated yet" is
        not a degraded read — it is the duplicate this row exists to remove,
        reinstated by an except block.
        """
        import sqlite3 as _sqlite3
        grp = _setup_group_with_entities(conn, env)
        _post_ic(conn, env)
        _generate(conn, env, grp["id"])
        assert len(_elimination_rows(conn, grp["id"])) == 1

        locked = _sqlite3.OperationalError("database is locked")
        with pytest.raises(_sqlite3.OperationalError):
            call_action(mod.generate_elimination_entries,
                        _AlreadyEliminatedReadFails(conn, locked),
                        ns(group_id=grp["id"], period_date="2026-06-30",
                           company_id=env["company_id"]))

        conn.rollback()
        assert len(_elimination_rows(conn, grp["id"])) == 1, \
            "a swallowed database error re-created the elimination"

    def test_losing_the_race_reads_as_an_instruction_not_a_driver_string(
            self, conn, env):
        """R4. The loser's data is correct; its message has to be too.

        An agent reads this output. "UNIQUE constraint failed:
        advacct_elimination_entry.group_id, ..." states nothing it can act on,
        and the correct next step (re-run, which will skip what the winner
        wrote) is exactly what the missing-column path already spells out.
        """
        grp = _setup_group_with_entities(conn, env)
        ic_id = _post_ic(conn, env)
        _generate(conn, env, grp["id"])          # the writer that won

        result = call_action(mod.generate_elimination_entries,
                             _SeesNoEliminationsYet(conn),
                             ns(group_id=grp["id"], period_date="2026-06-30",
                                company_id=env["company_id"]))

        assert is_error(result)
        assert ic_id in result["message"]
        assert "another writer" in result["message"]
        assert "wrote nothing" in result["message"]
        assert "generate-elimination-entries" in result["suggestion"]
        # The raw driver text, in either dialect's phrasing, must not be it.
        assert "UNIQUE constraint failed" not in result["message"]
        assert "uq_advacct_ee_source" not in result["message"]
        assert "duplicate key value" not in result["message"]

        conn.rollback()
        rows = _elimination_rows(conn, grp["id"])
        assert len(rows) == 1, "the loser wrote a row"
        assert rows[0]["source_ic_transaction_id"] == ic_id

    def test_the_loser_writes_none_of_its_batch(self, conn, env):
        """Not just the colliding row: the whole run, or nothing.

        Two transactions, one of them already eliminated by the winner. The
        run's other insert must not survive as a half-done batch, because a
        half-done batch plus an error is the state nobody reconciles.
        """
        grp = _setup_group_with_entities(conn, env)
        first = _post_ic(conn, env, amount="1000.00", description="January")
        _generate(conn, env, grp["id"])          # the winner takes `first`
        second = _post_ic(conn, env, amount="2000.00", description="February")

        result = call_action(mod.generate_elimination_entries,
                             _SeesNoEliminationsYet(conn),
                             ns(group_id=grp["id"], period_date="2026-06-30",
                                company_id=env["company_id"]))
        assert is_error(result)

        conn.rollback()
        rows = _elimination_rows(conn, grp["id"])
        assert [r["source_ic_transaction_id"] for r in rows] == [first]

        # And the re-run the message instructs finishes the job.
        again = _generate(conn, env, grp["id"])
        assert again["entries_created"] == 1
        assert again["eliminated_ic_transaction_ids"] == [second]
        assert sorted(r["source_ic_transaction_id"]
                      for r in _elimination_rows(conn, grp["id"])) == \
            sorted([first, second])

    def test_a_write_failure_that_is_not_the_backstop_is_never_swallowed(
            self, conn, env):
        """The sibling re-raise, pinned for the same reason as R3's.

        `_is_duplicate_source` decides which failures become an instruction. Any
        other insert failure must reach the router, which rolls back and reports
        it, rather than being read as a lost race.
        """
        import sqlite3 as _sqlite3
        grp = _setup_group_with_entities(conn, env)
        _post_ic(conn, env)

        class _InsertFails(_ConnProxy):
            def execute(self, sql, params=()):
                if "INSERT INTO advacct_elimination_entry" in sql:
                    raise _sqlite3.OperationalError("disk I/O error")
                return self._conn.execute(sql, params)

        with pytest.raises(_sqlite3.OperationalError):
            call_action(mod.generate_elimination_entries, _InsertFails(conn),
                        ns(group_id=grp["id"], period_date="2026-06-30",
                           company_id=env["company_id"]))

        conn.rollback()
        assert _elimination_rows(conn, grp["id"]) == []


class TestAddCurrencyTranslation:
    def test_add(self, conn, env):
        grp = _add_group(conn, env)
        result = call_action(mod.add_currency_translation, conn, ns(
            group_id=grp["id"], company_id=env["company_id"],
            period_date="2026-06-30", amount="5000.00",
            debit_account="CTA Debit", credit_account="CTA Credit",
            description="EUR translation adjustment",
        ))
        assert is_ok(result)
        assert result["entry_type"] == "currency_translation"
        assert result["amount"] == "5000.00"

    def test_missing_amount_fails(self, conn, env):
        grp = _add_group(conn, env)
        result = call_action(mod.add_currency_translation, conn, ns(
            group_id=grp["id"], company_id=env["company_id"],
            period_date="2026-06-30", amount=None,
            debit_account=None, credit_account=None,
            description=None,
        ))
        assert is_error(result)


# ──────────────────────────────────────────────────────────────────────────────
# Reports
# ──────────────────────────────────────────────────────────────────────────────

class TestConsolidationTrialBalanceReport:
    def test_report(self, conn, env):
        grp = _setup_group_with_entities(conn, env)
        result = call_action(mod.consolidation_trial_balance_report, conn, ns(
            group_id=grp["id"], period_date="2026-06-30",
        ))
        assert is_ok(result)
        assert result["entity_count"] == 2
        assert result["group_name"] == "Global Holdings Group"


class TestConsolidationSummary:
    def test_summary(self, conn, env):
        grp = _setup_group_with_entities(conn, env)
        # Add a currency translation entry so we have elimination data
        call_action(mod.add_currency_translation, conn, ns(
            group_id=grp["id"], company_id=env["company_id"],
            period_date="2026-06-30", amount="3000.00",
            debit_account=None, credit_account=None,
            description=None,
        ))
        result = call_action(mod.consolidation_summary, conn, ns(
            group_id=grp["id"],
        ))
        assert is_ok(result)
        assert result["entity_count"] == 2
        assert result["elimination_count"] >= 1
        assert "eliminations_by_type" in result
