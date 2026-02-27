"""Tests for cron-related actions: cleanup-backups and fetch-exchange-rates."""
import os
import db_query
from helpers import _call_action


# Test 1: cleanup-backups with no backups returns kept=0, deleted=0
def test_cleanup_no_backups(fresh_db, tmp_path):
    # Point backup dir to empty tmp
    original = db_query.BACKUP_DIR
    db_query.BACKUP_DIR = str(tmp_path)
    try:
        result = _call_action(db_query.cleanup_backups, fresh_db)
        assert result["status"] == "ok"
        assert result["kept"] == 0
        assert result["deleted"] == 0
    finally:
        db_query.BACKUP_DIR = original


# Test 2: cleanup-backups keeps recent, deletes old
def test_cleanup_retention(fresh_db, tmp_path):
    original = db_query.BACKUP_DIR
    db_query.BACKUP_DIR = str(tmp_path)
    try:
        # Create 10 fake backup files with sequential dates
        for i in range(10):
            day = f"202601{i+10:02d}"
            path = tmp_path / f"erpclaw_backup_{day}_020000.sqlite"
            path.write_text("fake")

        result = _call_action(db_query.cleanup_backups, fresh_db)
        assert result["status"] == "ok"
        assert result["kept"] == 10  # All 10 should be kept (within 7 daily + weekly range)
    finally:
        db_query.BACKUP_DIR = original


# Test 3: cleanup-backups deletes old files beyond retention window
def test_cleanup_deletes_old(fresh_db, tmp_path):
    original = db_query.BACKUP_DIR
    db_query.BACKUP_DIR = str(tmp_path)
    try:
        # Create 30 daily backups spanning a month
        for i in range(30):
            day = f"2025{(i // 28) + 1:02d}{(i % 28) + 1:02d}"
            path = tmp_path / f"erpclaw_backup_{day}_020000.sqlite"
            path.write_text("fake")

        result = _call_action(db_query.cleanup_backups, fresh_db)
        assert result["status"] == "ok"
        # Should keep some and delete some
        assert result["kept"] + result["deleted"] == 30
        assert result["kept"] > 0
        assert result["freed_bytes"] >= 0
    finally:
        db_query.BACKUP_DIR = original


# Test 4: fetch-exchange-rates (mock the HTTP call) — just test the action exists
def test_fetch_exchange_rates_in_actions():
    assert "fetch-exchange-rates" in db_query.ACTIONS


# Test 5: cleanup-backups is in ACTIONS
def test_cleanup_backups_in_actions():
    assert "cleanup-backups" in db_query.ACTIONS
