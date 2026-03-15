"""Violation fixture: Article 12 — valid tables (violation is in SKILL.md action names)."""
import sqlite3

DDL = """
    CREATE TABLE IF NOT EXISTS violart12claw_widget (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        price       TEXT DEFAULT '0',
        company_id  TEXT NOT NULL,
        created_at  TEXT DEFAULT (datetime('now'))
    );
"""
