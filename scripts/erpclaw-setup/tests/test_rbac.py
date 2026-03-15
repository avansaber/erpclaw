"""Tests for erpclaw-setup RBAC (Role-Based Access Control) actions.

Actions tested:
  - add-user, get-user, list-users, update-user
  - add-role, list-roles
  - assign-role, revoke-role
  - set-password
"""
import pytest
from setup_helpers import call_action, ns, seed_company, is_error, is_ok, load_db_query

mod = load_db_query()


# ──────────────────────────────────────────────────────────────────────────────
# User Management
# ──────────────────────────────────────────────────────────────────────────────

class TestAddUser:
    def test_basic_create(self, conn):
        result = call_action(mod.add_user, conn, ns(
            name="john_doe", email="john@example.com",
            full_name="John Doe", company_id=None,
        ))
        assert result["username"] == "john_doe"
        assert "user_id" in result

    def test_with_company(self, conn):
        cid = seed_company(conn)
        result = call_action(mod.add_user, conn, ns(
            name="jane_admin", email="jane@example.com",
            full_name="Jane Admin", company_id=cid,
        ))
        assert "user_id" in result
        row = conn.execute("SELECT company_ids FROM erp_user WHERE id=?",
                           (result["user_id"],)).fetchone()
        assert cid in row["company_ids"]

    def test_duplicate_username_fails(self, conn):
        call_action(mod.add_user, conn, ns(
            name="dup_user", email="a@a.com",
            full_name=None, company_id=None,
        ))
        result = call_action(mod.add_user, conn, ns(
            name="dup_user", email="b@b.com",
            full_name=None, company_id=None,
        ))
        assert is_error(result)

    def test_missing_name_fails(self, conn):
        result = call_action(mod.add_user, conn, ns(
            name=None, email="x@x.com",
            full_name=None, company_id=None,
        ))
        assert is_error(result)

    def test_invalid_email_fails(self, conn):
        result = call_action(mod.add_user, conn, ns(
            name="bad_email", email="not-an-email",
            full_name=None, company_id=None,
        ))
        assert is_error(result)


class TestGetUser:
    def test_get_by_id(self, conn):
        create = call_action(mod.add_user, conn, ns(
            name="get_test", email="get@test.com",
            full_name="Get Test", company_id=None,
        ))
        result = call_action(mod.get_user, conn, ns(user_id=create["user_id"]))
        assert result["username"] == "get_test"
        assert "roles" in result

    def test_get_nonexistent_fails(self, conn):
        result = call_action(mod.get_user, conn, ns(user_id="fake-id"))
        assert is_error(result)

    def test_get_missing_id_fails(self, conn):
        result = call_action(mod.get_user, conn, ns(user_id=None))
        assert is_error(result)


class TestListUsers:
    def test_list_empty(self, conn):
        result = call_action(mod.list_users, conn, ns(limit=None, offset=None))
        assert result["users"] == []

    def test_list_returns_created(self, conn):
        call_action(mod.add_user, conn, ns(
            name="list_user1", email="l1@t.com",
            full_name=None, company_id=None,
        ))
        call_action(mod.add_user, conn, ns(
            name="list_user2", email="l2@t.com",
            full_name=None, company_id=None,
        ))
        result = call_action(mod.list_users, conn, ns(limit=None, offset=None))
        assert result["count"] == 2


class TestUpdateUser:
    def test_update_status(self, conn):
        create = call_action(mod.add_user, conn, ns(
            name="status_user", email="s@t.com",
            full_name=None, company_id=None,
        ))
        result = call_action(mod.update_user, conn, ns(
            user_id=create["user_id"], name=None, email=None,
            full_name=None, user_status="disabled", company_id=None,
        ))
        assert "updated_fields" in result
        row = conn.execute("SELECT status FROM erp_user WHERE id=?",
                           (create["user_id"],)).fetchone()
        assert row["status"] == "disabled"

    def test_update_no_fields_fails(self, conn):
        create = call_action(mod.add_user, conn, ns(
            name="nochange", email="nc@t.com",
            full_name=None, company_id=None,
        ))
        result = call_action(mod.update_user, conn, ns(
            user_id=create["user_id"], name=None, email=None,
            full_name=None, user_status=None, company_id=None,
        ))
        assert is_error(result)


# ──────────────────────────────────────────────────────────────────────────────
# Roles
# ──────────────────────────────────────────────────────────────────────────────

class TestAddRole:
    def test_basic_create(self, conn):
        result = call_action(mod.add_role, conn, ns(
            name="Custom Accountant", description="Custom role for accountants",
        ))
        assert result["name"] == "Custom Accountant"
        assert "role_id" in result

    def test_duplicate_fails(self, conn):
        call_action(mod.add_role, conn, ns(name="Dup Role", description=None))
        result = call_action(mod.add_role, conn, ns(name="Dup Role", description=None))
        assert is_error(result)

    def test_system_roles_exist(self, conn):
        """init_db seeds default system roles."""
        result = call_action(mod.list_roles, conn, ns())
        role_names = [r["name"] for r in result["roles"]]
        assert "System Manager" in role_names
        assert "Accounts Manager" in role_names
        assert "Stock Manager" in role_names


class TestListRoles:
    def test_list_includes_system(self, conn):
        result = call_action(mod.list_roles, conn, ns())
        assert result["count"] >= 12  # 12 default system roles
        system_roles = [r for r in result["roles"] if r["is_system"] == 1]
        assert len(system_roles) >= 12


# ──────────────────────────────────────────────────────────────────────────────
# Role Assignment
# ──────────────────────────────────────────────────────────────────────────────

class TestAssignRole:
    def test_assign_global(self, conn):
        user = call_action(mod.add_user, conn, ns(
            name="assign_user", email="ar@t.com",
            full_name=None, company_id=None,
        ))
        result = call_action(mod.assign_role, conn, ns(
            user_id=user["user_id"],
            role_name="System Manager",
            company_id=None,
        ))
        assert is_ok(result)

        # Verify via get-user
        get = call_action(mod.get_user, conn, ns(user_id=user["user_id"]))
        role_names = [r["role_name"] for r in get["roles"]]
        assert "System Manager" in role_names

    def test_assign_company_scoped(self, conn):
        cid = seed_company(conn)
        user = call_action(mod.add_user, conn, ns(
            name="scoped_user", email="sc@t.com",
            full_name=None, company_id=None,
        ))
        result = call_action(mod.assign_role, conn, ns(
            user_id=user["user_id"],
            role_name="Accounts User",
            company_id=cid,
        ))
        assert is_ok(result)

    def test_duplicate_assignment_fails(self, conn):
        user = call_action(mod.add_user, conn, ns(
            name="dup_assign", email="da@t.com",
            full_name=None, company_id=None,
        ))
        call_action(mod.assign_role, conn, ns(
            user_id=user["user_id"],
            role_name="Stock User",
            company_id=None,
        ))
        result = call_action(mod.assign_role, conn, ns(
            user_id=user["user_id"],
            role_name="Stock User",
            company_id=None,
        ))
        assert is_error(result)

    def test_assign_nonexistent_role_fails(self, conn):
        user = call_action(mod.add_user, conn, ns(
            name="norole_user", email="nr@t.com",
            full_name=None, company_id=None,
        ))
        result = call_action(mod.assign_role, conn, ns(
            user_id=user["user_id"],
            role_name="Nonexistent Role",
            company_id=None,
        ))
        assert is_error(result)


class TestRevokeRole:
    def test_revoke_assigned(self, conn):
        user = call_action(mod.add_user, conn, ns(
            name="revoke_user", email="rv@t.com",
            full_name=None, company_id=None,
        ))
        call_action(mod.assign_role, conn, ns(
            user_id=user["user_id"],
            role_name="HR User",
            company_id=None,
        ))
        result = call_action(mod.revoke_role, conn, ns(
            user_id=user["user_id"],
            role_name="HR User",
            company_id=None,
        ))
        assert is_ok(result)

        # Verify role removed
        get = call_action(mod.get_user, conn, ns(user_id=user["user_id"]))
        role_names = [r["role_name"] for r in get["roles"]]
        assert "HR User" not in role_names


class TestSetPassword:
    @pytest.mark.xfail(reason="BUG-005: erp_user table missing password_hash column")
    def test_set_password(self, conn):
        user = call_action(mod.add_user, conn, ns(
            name="pw_user", email="pw@t.com",
            full_name=None, company_id=None,
        ))
        result = call_action(mod.set_password, conn, ns(
            user_id=user["user_id"],
            password="SecureP@ss123",
        ))
        assert is_ok(result)

    def test_set_password_missing_user_fails(self, conn):
        result = call_action(mod.set_password, conn, ns(
            user_id=None,
            password="test",
        ))
        assert is_error(result)
