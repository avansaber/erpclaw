"""Tests for erpclaw-setup utility actions.

Actions tested:
  - seed-defaults
  - get-audit-log
  - get-schema-version
  - status
"""
import pytest
from setup_helpers import call_action, ns, seed_company, is_error, is_ok, load_db_query
from erpclaw_lib.audit import audit

mod = load_db_query()


class TestSeedDefaults:
    def test_seed_creates_currencies_and_uoms(self, conn):
        cid = seed_company(conn)
        result = call_action(mod.seed_defaults, conn, ns(company_id=cid))
        assert is_ok(result)

        # Should have seeded currencies
        cur_count = conn.execute("SELECT COUNT(*) as cnt FROM currency").fetchone()["cnt"]
        assert cur_count >= 1  # At least USD

        # Should have seeded UoMs
        uom_count = conn.execute("SELECT COUNT(*) as cnt FROM uom").fetchone()["cnt"]
        assert uom_count >= 1

    def test_seed_idempotent(self, conn):
        """Running seed-defaults twice should not error (INSERT OR IGNORE)."""
        cid = seed_company(conn)
        call_action(mod.seed_defaults, conn, ns(company_id=cid))
        result = call_action(mod.seed_defaults, conn, ns(company_id=cid))
        assert is_ok(result)


class TestGetAuditLog:
    def test_empty_log(self, conn):
        company_id = seed_company(conn)
        result = call_action(mod.get_audit_log, conn, ns(
            company_id=company_id,
            entity_type=None, entity_id=None, audit_action=None,
            from_date=None, to_date=None, limit=None, offset=None,
        ))
        assert result["entries"] == []

    def test_filter_by_entity_type(self, conn):
        company_id = seed_company(conn)
        audit(conn, "erpclaw-setup", "update", "company", company_id,
              new_values={"name": "Audit Log Test Co"},
              scope_company_ids=[company_id], scope_status="in_scope")
        conn.commit()
        result = call_action(mod.get_audit_log, conn, ns(
            company_id=company_id,
            entity_type="company", entity_id=None, audit_action=None,
            from_date=None, to_date=None, limit=None, offset=None,
        ))
        assert len(result["entries"]) >= 1
        for entry in result["entries"]:
            assert entry["entity_type"] == "company"


class TestGetSchemaVersion:
    def test_returns_versions(self, conn):
        result = call_action(mod.get_schema_version, conn, ns(module=None))
        assert "versions" in result or "version" in result or "error" not in result


class TestStatus:
    def test_status_empty_db(self, conn):
        result = call_action(mod.status, conn, ns())
        assert is_ok(result)

    def test_status_with_company(self, conn):
        call_action(mod.setup_company, conn, ns(
            name="Status Test Co",
            abbr=None, currency=None, country=None,
            fiscal_year_start_month=None,
        ))
        result = call_action(mod.status, conn, ns())
        assert is_ok(result)
