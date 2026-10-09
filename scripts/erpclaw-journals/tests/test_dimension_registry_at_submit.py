"""Journal submission uses the current dimension registry on its connection."""
import json
import uuid
from decimal import Decimal

import pytest

from erpclaw_lib.db import get_connection
from erpclaw_lib.query import P, Q, Table
from journals_helpers import build_journals_env, call_action, is_error, is_ok
from test_journal_dimensions import _add, _entry_lines, _jns, _submit, mod


@pytest.fixture
def tagged_books(db_path):
    connection = get_connection(db_path)
    environment = build_journals_env(connection)
    registry = Table("dimension_registry")
    for key, allowed in (("fund", ["general", "restricted"]),
                         ("function", ["program", "administration"]),
                         ("award", ["award-a", "award-b"])):
        statement = (Q.into(registry)
                     .columns("id", "key", "label", "data_type",
                              "allowed_values_json",
                              "is_required_on_account_types_json", "is_active")
                     .insert(*[P() for _ in range(7)]).get_sql())
        connection.execute(statement, (
            str(uuid.uuid4()), key, key.title(), "enum", json.dumps(allowed),
            json.dumps(["expense"]), 1))
    connection.commit()
    yield connection, environment
    connection.close()


def _tags(**changes):
    tags = {"fund": "general", "function": "program", "award": "award-a"}
    tags.update(changes)
    return tags


def _draft(connection, environment, header=None, expense_tags=None):
    header = _tags() if header is None else header
    lines = (_entry_lines(environment) if expense_tags is None
             else _entry_lines(environment, expense_tags))
    result = _add(connection, environment, lines, dimensions=json.dumps(header))
    assert is_ok(result), result
    return result["journal_entry_id"]


def _registry_update(connection, key, field, value):
    registry = Table("dimension_registry")
    statement = (Q.update(registry).set(registry[field], P())
                 .where(registry.key == P()).get_sql())
    connection.execute(statement, (value, key))
    connection.commit()


def _posting_state(connection):
    state = {}
    for name in ("journal_entry", "journal_entry_line", "gl_entry",
                 "gl_chain_head", "audit_log", "naming_series"):
        table = Table(name)
        rows = connection.execute(Q.from_(table).select(table.star).get_sql())
        state[name] = sorted((tuple(row) for row in rows.fetchall()), key=repr)
    return state


def _ledger_rows(connection, journal_id):
    ledger = Table("gl_entry")
    query = (Q.from_(ledger)
             .select(ledger.account_id, ledger.debit, ledger.credit,
                     ledger.dimensions_json)
             .where(ledger.voucher_id == P()))
    return connection.execute(query.get_sql(), (journal_id,)).fetchall()


def test_required_axes_post_effective_line_tags_and_reverse_exactly(tagged_books):
    connection, environment = tagged_books
    journal_id = _draft(connection, environment,
                        expense_tags={"fund": "restricted", "award": "award-b"})
    result = _submit(connection, journal_id)
    assert is_ok(result), result
    rows = _ledger_rows(connection, journal_id)
    assert len(rows) == 2
    for row in rows:
        tags = json.loads(row["dimensions_json"])
        if row["account_id"] == environment["expense"]:
            assert tags == _tags(fund="restricted", award="award-b")
            assert Decimal(row["debit"]) == Decimal("100.00")
        else:
            assert tags == _tags()
            assert Decimal(row["credit"]) == Decimal("100.00")
    cancelled = call_action(mod.cancel_journal_entry, connection,
                            _jns(journal_entry_id=journal_id))
    assert is_ok(cancelled), cancelled
    rows = _ledger_rows(connection, journal_id)
    assert len(rows) == 4
    net_by_tag = {}
    for row in rows:
        key = (row["account_id"], row["dimensions_json"])
        net_by_tag[key] = (net_by_tag.get(key, Decimal("0"))
                           + Decimal(row["debit"]) - Decimal(row["credit"]))
    assert len(net_by_tag) == 2
    assert all(net == Decimal("0.00") for net in net_by_tag.values())


@pytest.mark.parametrize("key", ["fund", "function", "award"])
def test_submit_refuses_retired_dimension_without_posting(tagged_books, key):
    connection, environment = tagged_books
    journal_id = _draft(connection, environment)
    _registry_update(connection, key, "is_active", 0)
    before = _posting_state(connection)
    result = _submit(connection, journal_id)
    assert is_error(result), result
    assert f"Unknown or inactive dimension '{key}'" in result["message"]
    assert _posting_state(connection) == before


@pytest.mark.parametrize("key,replacement", [
    ("fund", "restricted"), ("function", "administration"), ("award", "award-b")])
def test_submit_refuses_changed_enum_then_accepts_updated_draft(
        tagged_books, key, replacement):
    connection, environment = tagged_books
    journal_id = _draft(connection, environment)
    _registry_update(connection, key, "allowed_values_json", json.dumps([replacement]))
    before = _posting_state(connection)
    result = _submit(connection, journal_id)
    assert is_error(result), result
    assert f"Dimension '{key}' value" in result["message"]
    assert _posting_state(connection) == before
    updated = call_action(mod.update_journal_entry, connection, _jns(
        journal_entry_id=journal_id, dimensions=json.dumps(_tags(**{key: replacement}))))
    assert is_ok(updated), updated
    assert is_ok(_submit(connection, journal_id))
    rows = _ledger_rows(connection, journal_id)
    assert len(rows) == 2
    assert all(json.loads(row["dimensions_json"])[key] == replacement for row in rows)


def test_new_required_key_refuses_an_existing_untagged_draft(tagged_books):
    connection, environment = tagged_books
    _registry_update(connection, "fund", "is_required_on_account_types_json", "[]")
    _registry_update(connection, "function", "is_required_on_account_types_json", "[]")
    _registry_update(connection, "award", "is_required_on_account_types_json", "[]")
    journal_id = _draft(connection, environment, header={})
    _registry_update(connection, "fund", "is_required_on_account_types_json",
                     json.dumps(["expense"]))
    before = _posting_state(connection)
    result = _submit(connection, journal_id)
    assert is_error(result), result
    assert "Dimension 'fund' is required" in result["message"]
    assert _posting_state(connection) == before


def test_required_keys_refuse_at_draft_without_writes(tagged_books):
    connection, environment = tagged_books
    before = _posting_state(connection)
    result = _add(connection, environment, _entry_lines(environment),
                  dimensions=json.dumps({"fund": "general"}))
    assert is_error(result), result
    assert "is required" in result["message"]
    assert _posting_state(connection) == before


def test_untagged_draft_still_posts_when_no_key_is_required(tagged_books):
    connection, environment = tagged_books
    for key in ("fund", "function", "award"):
        _registry_update(connection, key, "is_required_on_account_types_json", "[]")
    journal_id = _draft(connection, environment, header={})
    assert is_ok(_submit(connection, journal_id))
    rows = _ledger_rows(connection, journal_id)
    assert len(rows) == 2
    assert all(json.loads(row["dimensions_json"]) == {} for row in rows)
    assert sum((Decimal(row["debit"]) for row in rows), Decimal("0")) == Decimal("100.00")
    assert sum((Decimal(row["credit"]) for row in rows), Decimal("0")) == Decimal("100.00")
