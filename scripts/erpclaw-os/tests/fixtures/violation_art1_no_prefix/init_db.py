"""Violation fixture: Article 1 — table without module prefix."""
import sqlite3

DDL = """
    CREATE TABLE IF NOT EXISTS appointment (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        date        TEXT NOT NULL,
        company_id  TEXT NOT NULL,
        created_at  TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS service_record (
        id              TEXT PRIMARY KEY,
        appointment_id  TEXT NOT NULL REFERENCES appointment(id),
        description     TEXT,
        amount          TEXT DEFAULT '0',
        created_at      TEXT DEFAULT (datetime('now'))
    );
"""
