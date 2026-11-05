"""Revision 0005 on older books: their cards and operations carry over."""
from __future__ import annotations

from sqlalchemy import inspect, text

from conftest import drop_everything
from erpclaw_core import models as m
from erpclaw_core.migrate import upgrade

NOW = "2026-10-26 12:00:00+00:00"


def test_older_cards_and_operations_carry_over_and_the_owner_gets_every_permission(engine):
    drop_everything(engine)
    with engine.begin() as conn:
        upgrade(conn, "0004")
        conn.execute(text("INSERT INTO company (id, name, functional_currency, fiscal_year_start_month, created_at) "
                          "VALUES ('c1', 'Old Co', 'USD', 1, :now)"), {"now": NOW})
        for aid, status, key in (("a1", "committed", "k1"), ("a2", "awaiting", "k2"), ("a3", "rejected", "k3"),
                                 ("a4", "failed", "k4")):
            conn.execute(text(
                "INSERT INTO approval (id, company_id, conversation_id, contract, args_json, digest, card_json, status, "
                "operation_key, created_at, expires_at) VALUES (:id, 'c1', NULL, 'create_invoice', '{}', :d, '{}', :s, "
                ":k, :now, :now)"), {"id": aid, "d": aid * 32, "s": status, "k": key, "now": NOW})
        for key, aid, status, result in (("k1", "a1", "committed", '{"text": "done"}'), ("k2", "a2", "started", None),
                                         ("k4", "a4", "failed", '{"code": "state_changed"}')):  # as the old engine wrote
            conn.execute(text("INSERT INTO operation (key, approval_id, contract, status, result_json, started_at, "
                              "finished_at) VALUES (:k, :a, 'create_invoice', :s, :r, :now, :now)"),
                         {"k": key, "a": aid, "s": status, "r": result, "now": NOW})
    with engine.begin() as conn:
        upgrade(conn, "0005")  # Establish the revision's own effects before later migrations can mask them.
        assert "operation" not in inspect(conn).get_table_names()
        assert dict(conn.execute(text("SELECT id, status FROM approval")).all()) == {
            "a1": "consumed", "a2": "expired", "a3": "declined", "a4": "consumed"}
        assert dict(conn.execute(text("SELECT key, status FROM operation_outcome")).all()) == {
            "k1": "committed", "k4": "refused"}
        owner = conn.execute(text("SELECT id FROM principal WHERE name = 'owner'")).scalar_one()
        assert set(conn.execute(text("SELECT permission FROM principal_grant WHERE principal_id = :p AND "
                                     "company_id = 'c1'"), {"p": owner}).scalars()) == set(m.PERMISSIONS)
        assert set(conn.execute(text("SELECT principal_id FROM approval")).scalars()) == {owner}
    with engine.begin() as conn:
        upgrade(conn, "0010")  # Current read_back needs the pre-format-2 head columns.
        assert "operation" not in inspect(conn).get_table_names()
        cards = dict(conn.execute(text("SELECT id, status FROM approval")).all())
        assert cards == {"a1": "consumed", "a2": "expired", "a3": "declined", "a4": "consumed"}
        outcomes = dict(conn.execute(text("SELECT key, status FROM operation_outcome")).all())
        assert outcomes == {"k1": "committed", "k4": "refused"}  # k2 started and never finished: no effect, none
        owner = conn.execute(text("SELECT id FROM principal WHERE name = 'owner'")).scalar_one()
        perms = set(conn.execute(text("SELECT permission FROM principal_grant WHERE principal_id = :p AND "
                                      "company_id = 'c1'"), {"p": owner}).scalars())
        assert perms == set(m.PERMISSIONS)
        assert set(conn.execute(text("SELECT principal_id FROM approval")).scalars()) == {owner}

    from erpclaw_core import approvals  # the carried-over outcomes read back

    assert approvals.read_back(engine, "k1").text == "done"
    refused = approvals.read_back(engine, "k4")
    assert refused.status == "refused" and refused.data["code"] == "state_changed" and "nothing was saved" in refused.text
    assert approvals.read_back(engine, "k2").status == "unknown"
