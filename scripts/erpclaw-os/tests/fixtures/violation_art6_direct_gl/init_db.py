"""Violation fixture: Article 6 — valid tables (violation is in db_query.py)."""
import sqlite3

DDL = """
    CREATE TABLE IF NOT EXISTS violart6claw_record (
        id          TEXT PRIMARY KEY,
        description TEXT,
        amount      TEXT DEFAULT '0',
        company_id  TEXT NOT NULL,
        created_at  TEXT DEFAULT (datetime('now'))
    );
"""
