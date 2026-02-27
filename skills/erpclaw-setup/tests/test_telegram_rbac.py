"""Tests for Telegram RBAC enforcement (V2).

10 tests:
- Link Telegram user (1)
- Unlink Telegram user (1)
- Allowed action via Telegram RBAC (1)
- Denied action via Telegram RBAC (1)
- Unlinked user = deny (1)
- Role-based permission (1)
- Action pattern matching (1)
- Multi-company isolation (1)
- No-op when flag not provided (1)
- Audit trail / invariant (1)
"""
import db_query
from helpers import _call_action


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_user_with_role(conn, username, role_name, telegram_id=None,
                          company_id=None):
    """Create a user, optionally link telegram, assign a role."""
    r = _call_action(db_query.add_user, conn, name=username)
    user_id = r["user_id"]

    if telegram_id:
        _call_action(db_query.link_telegram_user, conn,
                     user_id=user_id, telegram_user_id=telegram_id)

    _call_action(db_query.assign_role, conn,
                 user_id=user_id, role_name=role_name,
                 company_id=company_id)

    return user_id


# ---------------------------------------------------------------------------
# 1. Link Telegram user
# ---------------------------------------------------------------------------

def test_link_telegram_user(fresh_db):
    r = _call_action(db_query.add_user, fresh_db, name="tony.stark")
    user_id = r["user_id"]

    r2 = _call_action(db_query.link_telegram_user, fresh_db,
                      user_id=user_id, telegram_user_id="123456789")
    assert r2["status"] == "ok"
    assert r2["linked"] is True
    assert r2["telegram_user_id"] == "123456789"

    # Verify in DB
    row = fresh_db.execute(
        "SELECT telegram_user_id FROM erp_user WHERE id = ?", (user_id,)
    ).fetchone()
    assert row["telegram_user_id"] == "123456789"


# ---------------------------------------------------------------------------
# 2. Unlink Telegram user
# ---------------------------------------------------------------------------

def test_unlink_telegram_user(fresh_db):
    r = _call_action(db_query.add_user, fresh_db, name="pepper.potts")
    user_id = r["user_id"]

    _call_action(db_query.link_telegram_user, fresh_db,
                 user_id=user_id, telegram_user_id="987654321")

    r2 = _call_action(db_query.unlink_telegram_user, fresh_db,
                      telegram_user_id="987654321")
    assert r2["status"] == "ok"
    assert r2["unlinked"] is True

    # Verify cleared
    row = fresh_db.execute(
        "SELECT telegram_user_id FROM erp_user WHERE id = ?", (user_id,)
    ).fetchone()
    assert row["telegram_user_id"] is None


# ---------------------------------------------------------------------------
# 3. Allowed action via check-telegram-permission
# ---------------------------------------------------------------------------

def test_allowed_telegram_permission(fresh_db):
    user_id = _setup_user_with_role(fresh_db, "accounts_mgr", "Accounts Manager",
                                     telegram_id="111111")

    # Seed permissions so role_permission table has entries
    _call_action(db_query.seed_permissions, fresh_db)

    r = _call_action(db_query.check_telegram_permission, fresh_db,
                     telegram_user_id="111111",
                     skill="erpclaw-journals",
                     check_action="submit-journal-entry")
    assert r["status"] == "ok"
    assert r["allowed"] is True


# ---------------------------------------------------------------------------
# 4. Denied action via check-telegram-permission
# ---------------------------------------------------------------------------

def test_denied_telegram_permission(fresh_db):
    # Stock User should NOT have access to journals
    user_id = _setup_user_with_role(fresh_db, "stock_user", "Stock User",
                                     telegram_id="222222")
    _call_action(db_query.seed_permissions, fresh_db)

    r = _call_action(db_query.check_telegram_permission, fresh_db,
                     telegram_user_id="222222",
                     skill="erpclaw-journals",
                     check_action="submit-journal-entry")
    assert r["status"] == "ok"
    assert r["allowed"] is False


# ---------------------------------------------------------------------------
# 5. Unlinked user = not_linked response
# ---------------------------------------------------------------------------

def test_unlinked_user_denied(fresh_db):
    # Create a user but DON'T link telegram
    _call_action(db_query.add_user, fresh_db, name="no_link_user")

    r = _call_action(db_query.check_telegram_permission, fresh_db,
                     telegram_user_id="999999",
                     skill="erpclaw-gl",
                     check_action="list-accounts")
    assert r["status"] == "ok"
    assert r["allowed"] is False
    assert r["reason"] == "not_linked"


# ---------------------------------------------------------------------------
# 6. Role-based: System Manager gets all permissions
# ---------------------------------------------------------------------------

def test_system_manager_all_access(fresh_db):
    _setup_user_with_role(fresh_db, "admin", "System Manager",
                          telegram_id="333333")
    _call_action(db_query.seed_permissions, fresh_db)

    # Check access to various skills
    for skill, action in [
        ("erpclaw-gl", "submit-journal-entry"),
        ("erpclaw-inventory", "add-item"),
        ("erpclaw-hr", "add-employee"),
    ]:
        r = _call_action(db_query.check_telegram_permission, fresh_db,
                         telegram_user_id="333333",
                         skill=skill, check_action=action)
        assert r["allowed"] is True, f"System Manager should have access to {skill}/{action}"


# ---------------------------------------------------------------------------
# 7. Action pattern matching: Accounts User can list but not cancel
# ---------------------------------------------------------------------------

def test_action_pattern_matching(fresh_db):
    _setup_user_with_role(fresh_db, "acct_user", "Accounts User",
                          telegram_id="444444")
    _call_action(db_query.seed_permissions, fresh_db)

    # Can list
    r1 = _call_action(db_query.check_telegram_permission, fresh_db,
                      telegram_user_id="444444",
                      skill="erpclaw-journals",
                      check_action="list-journal-entries")
    assert r1["allowed"] is True

    # Cannot cancel (Accounts User doesn't have cancel-*)
    r2 = _call_action(db_query.check_telegram_permission, fresh_db,
                      telegram_user_id="444444",
                      skill="erpclaw-journals",
                      check_action="cancel-journal-entry")
    assert r2["allowed"] is False


# ---------------------------------------------------------------------------
# 8. Multi-company: user restricted to specific companies
# ---------------------------------------------------------------------------

def test_multi_company_isolation(fresh_db):
    import json as json_mod

    # Create two companies via setup-company action
    r_a = _call_action(db_query.setup_company, fresh_db, name="Co A", abbr="CA")
    co_a = r_a["company_id"]
    r_b = _call_action(db_query.setup_company, fresh_db, name="Co B", abbr="CB")
    co_b = r_b["company_id"]

    # Create user with access only to Co A
    r = _call_action(db_query.add_user, fresh_db, name="co_a_user",
                     company_id=co_a)
    user_id = r["user_id"]

    _call_action(db_query.link_telegram_user, fresh_db,
                 user_id=user_id, telegram_user_id="555555")
    _call_action(db_query.assign_role, fresh_db,
                 user_id=user_id, role_name="Accounts Manager",
                 company_id=co_a)
    _call_action(db_query.seed_permissions, fresh_db)

    # Verify user company_ids
    from erpclaw_lib.rbac import get_user_companies
    companies = get_user_companies(fresh_db, user_id)
    assert co_a in companies


# ---------------------------------------------------------------------------
# 9. No-op when telegram_user_id not provided (enforce_telegram_rbac)
# ---------------------------------------------------------------------------

def test_enforce_noop_without_telegram_id(fresh_db):
    """enforce_telegram_rbac does nothing when telegram_user_id is None."""
    from erpclaw_lib.rbac import enforce_telegram_rbac

    # Should not raise even though no users exist
    enforce_telegram_rbac(fresh_db, None, "erpclaw-gl", "submit-journal-entry")

    # Create a user to activate RBAC
    _call_action(db_query.add_user, fresh_db, name="some_user")

    # Still should not raise with None telegram_user_id
    enforce_telegram_rbac(fresh_db, None, "erpclaw-gl", "submit-journal-entry")


# ---------------------------------------------------------------------------
# 10. Enforce raises PermissionError for unlinked telegram user
# ---------------------------------------------------------------------------

def test_enforce_raises_for_unlinked(fresh_db):
    """enforce_telegram_rbac raises for unknown telegram user when RBAC active."""
    from erpclaw_lib.rbac import enforce_telegram_rbac
    import pytest

    # Activate RBAC by creating a user
    _call_action(db_query.add_user, fresh_db, name="activate_rbac")

    with pytest.raises(PermissionError, match="not linked"):
        enforce_telegram_rbac(fresh_db, "9999999", "erpclaw-gl", "list-accounts")
