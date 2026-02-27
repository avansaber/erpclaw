"""Tests for RBAC: users, roles, assignment, permission checking.

Tests: add/list/get user, add/list role, assign/revoke role, seed permissions,
       permission checking (System Manager, Accounts User, denied, no RBAC).
"""
import json
import uuid

import db_query
from helpers import _call_action


# ---------------------------------------------------------------------------
# User CRUD
# ---------------------------------------------------------------------------

def test_add_user(fresh_db):
    """Create a new ERP user."""
    result = _call_action(db_query.add_user, fresh_db,
                          name="tony.stark", email="tony@example.com",
                          full_name="Tony Stark")
    assert result["status"] == "ok"
    assert result["username"] == "tony.stark"
    assert result["user_id"]


def test_add_user_duplicate(fresh_db):
    """Duplicate username should fail."""
    _call_action(db_query.add_user, fresh_db, name="duplicate.user")
    result = _call_action(db_query.add_user, fresh_db, name="duplicate.user")
    assert result["status"] == "error"
    assert "already exists" in result["message"]


def test_list_users(fresh_db):
    """List users with pagination."""
    _call_action(db_query.add_user, fresh_db, name="user_a")
    _call_action(db_query.add_user, fresh_db, name="user_b")
    result = _call_action(db_query.list_users, fresh_db)
    assert result["status"] == "ok"
    assert result["count"] == 2
    assert result["has_more"] is False


def test_get_user_with_roles(fresh_db):
    """Get user details including assigned roles."""
    user = _call_action(db_query.add_user, fresh_db, name="role_test_user")
    user_id = user["user_id"]

    # Assign a role
    _call_action(db_query.assign_role, fresh_db,
                 user_id=user_id, role_name="System Manager")

    result = _call_action(db_query.get_user, fresh_db, user_id=user_id)
    assert result["status"] == "ok"
    assert result["username"] == "role_test_user"
    assert len(result["roles"]) == 1
    assert result["roles"][0]["role_name"] == "System Manager"


def test_update_user(fresh_db):
    """Update user email and status."""
    user = _call_action(db_query.add_user, fresh_db, name="update_me")
    user_id = user["user_id"]

    result = _call_action(db_query.update_user, fresh_db,
                          user_id=user_id, email="new@email.com",
                          user_status="disabled")
    assert result["status"] == "ok"
    assert "email" in result["updated_fields"]
    assert "status" in result["updated_fields"]

    # Verify — note: _ok() overwrites "status" key, so check DB directly
    row = fresh_db.execute(
        "SELECT email, status FROM erp_user WHERE id = ?", (user_id,)
    ).fetchone()
    assert row["email"] == "new@email.com"
    assert row["status"] == "disabled"


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

def test_list_roles_seeded(fresh_db):
    """System roles should be seeded by init_db."""
    result = _call_action(db_query.list_roles, fresh_db)
    assert result["status"] == "ok"
    role_names = [r["name"] for r in result["roles"]]
    assert "System Manager" in role_names
    assert "Accounts Manager" in role_names
    assert "HR Manager" in role_names
    assert result["count"] == 12  # 12 seeded roles


def test_add_custom_role(fresh_db):
    """Create a non-system custom role."""
    result = _call_action(db_query.add_role, fresh_db,
                          name="Intern", description="Limited access intern role")
    assert result["status"] == "ok"
    assert result["name"] == "Intern"


# ---------------------------------------------------------------------------
# Role Assignment
# ---------------------------------------------------------------------------

def test_assign_role(fresh_db):
    """Assign a role to a user."""
    user = _call_action(db_query.add_user, fresh_db, name="assign_user")
    result = _call_action(db_query.assign_role, fresh_db,
                          user_id=user["user_id"],
                          role_name="Accounts User")
    assert result["status"] == "ok"
    assert result["role_name"] == "Accounts User"


def test_assign_role_duplicate(fresh_db):
    """Assigning same role twice should fail."""
    user = _call_action(db_query.add_user, fresh_db, name="dup_assign")
    _call_action(db_query.assign_role, fresh_db,
                 user_id=user["user_id"], role_name="HR User")
    result = _call_action(db_query.assign_role, fresh_db,
                          user_id=user["user_id"], role_name="HR User")
    assert result["status"] == "error"
    assert "already assigned" in result["message"]


def test_revoke_role(fresh_db):
    """Revoke a role from a user."""
    user = _call_action(db_query.add_user, fresh_db, name="revoke_user")
    _call_action(db_query.assign_role, fresh_db,
                 user_id=user["user_id"], role_name="Sales Manager")

    result = _call_action(db_query.revoke_role, fresh_db,
                          user_id=user["user_id"], role_name="Sales Manager")
    assert result["status"] == "ok"
    assert result["revoked"] == "Sales Manager"

    # Verify role is gone
    got = _call_action(db_query.get_user, fresh_db,
                       user_id=user["user_id"])
    assert len(got["roles"]) == 0


# ---------------------------------------------------------------------------
# Permission seeding and checking
# ---------------------------------------------------------------------------

def test_seed_permissions(fresh_db):
    """Seed default role permissions."""
    result = _call_action(db_query.seed_permissions, fresh_db)
    assert result["status"] == "ok"
    assert result["permissions_seeded"] > 0

    # Verify some permissions exist
    perms = fresh_db.execute(
        """SELECT rp.skill, rp.action_pattern
           FROM role_permission rp
           JOIN role r ON r.id = rp.role_id
           WHERE r.name = 'System Manager'"""
    ).fetchall()
    assert len(perms) == 1  # System Manager gets (*,*)
    assert perms[0]["skill"] == "*"
    assert perms[0]["action_pattern"] == "*"


def test_permission_check_no_rbac(fresh_db):
    """When no users exist, RBAC is inactive — all actions allowed."""
    from erpclaw_lib.rbac import check_permission
    assert check_permission(fresh_db, None, "erpclaw-gl", "submit-journal-entry") is True


def test_permission_check_system_manager(fresh_db):
    """System Manager should have access to everything."""
    from erpclaw_lib.rbac import check_permission, seed_role_permissions
    seed_role_permissions(fresh_db)

    user = _call_action(db_query.add_user, fresh_db, name="admin")
    _call_action(db_query.assign_role, fresh_db,
                 user_id=user["user_id"], role_name="System Manager")

    assert check_permission(fresh_db, user["user_id"],
                            "erpclaw-gl", "submit-journal-entry") is True
    assert check_permission(fresh_db, user["user_id"],
                            "erpclaw-payroll", "create-payroll-run") is True


def test_permission_check_accounts_user_allowed(fresh_db):
    """Accounts User should be able to submit journals."""
    from erpclaw_lib.rbac import check_permission, seed_role_permissions
    seed_role_permissions(fresh_db)

    user = _call_action(db_query.add_user, fresh_db, name="accountant")
    _call_action(db_query.assign_role, fresh_db,
                 user_id=user["user_id"], role_name="Accounts User")

    # Accounts User can submit journal entries and payments
    assert check_permission(fresh_db, user["user_id"],
                            "erpclaw-journals", "submit-journal-entry") is True
    assert check_permission(fresh_db, user["user_id"],
                            "erpclaw-reports", "trial-balance") is True


def test_permission_check_denied(fresh_db):
    """Accounts User should NOT be able to modify HR."""
    from erpclaw_lib.rbac import check_permission, seed_role_permissions
    seed_role_permissions(fresh_db)

    user = _call_action(db_query.add_user, fresh_db, name="no_hr_access")
    _call_action(db_query.assign_role, fresh_db,
                 user_id=user["user_id"], role_name="Accounts User")

    # Accounts User has no HR permissions
    assert check_permission(fresh_db, user["user_id"],
                            "erpclaw-hr", "add-employee") is False
    assert check_permission(fresh_db, user["user_id"],
                            "erpclaw-payroll", "create-payroll-run") is False


def test_permission_require_raises(fresh_db):
    """require_permission should raise PermissionError on denial."""
    from erpclaw_lib.rbac import require_permission, seed_role_permissions
    seed_role_permissions(fresh_db)

    user = _call_action(db_query.add_user, fresh_db, name="limited")
    _call_action(db_query.assign_role, fresh_db,
                 user_id=user["user_id"], role_name="Analytics User")

    # Should raise on HR action
    import pytest
    with pytest.raises(PermissionError, match="Permission denied"):
        require_permission(fresh_db, user["user_id"],
                           "erpclaw-hr", "add-employee")


def test_user_no_roles_denied(fresh_db):
    """A user with no roles should be denied everything."""
    from erpclaw_lib.rbac import check_permission

    user = _call_action(db_query.add_user, fresh_db, name="no_roles")
    assert check_permission(fresh_db, user["user_id"],
                            "erpclaw-gl", "list-accounts") is False
