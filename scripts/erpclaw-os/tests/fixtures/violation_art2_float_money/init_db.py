"""Violation fixture: Article 2 — money columns using REAL/FLOAT instead of TEXT."""
import sqlite3

DDL = """
    CREATE TABLE IF NOT EXISTS violart2claw_invoice (
        id          TEXT PRIMARY KEY,
        customer_id TEXT NOT NULL,
        amount      REAL NOT NULL DEFAULT 0.0,
        tax         FLOAT NOT NULL DEFAULT 0.0,
        total       REAL DEFAULT 0.0,
        company_id  TEXT NOT NULL,
        created_at  TEXT DEFAULT (datetime('now'))
    );
"""
