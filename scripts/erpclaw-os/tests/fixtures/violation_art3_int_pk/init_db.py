"""Violation fixture: Article 3 — INTEGER primary key instead of TEXT UUID."""
import sqlite3

DDL = """
    CREATE TABLE IF NOT EXISTS violart3claw_item (
        id          INTEGER PRIMARY KEY,
        name        TEXT NOT NULL,
        price       TEXT NOT NULL DEFAULT '0',
        company_id  TEXT NOT NULL,
        created_at  TEXT DEFAULT (datetime('now'))
    );
"""
