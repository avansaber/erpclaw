#!/usr/bin/env python3
"""Compare two PostgreSQL books in independent, read-only snapshots."""
from __future__ import annotations

import argparse
import os
import re
import sys
from contextlib import ExitStack
from itertools import zip_longest

import psycopg
from psycopg import sql

from erpclaw_core.restore_auth import AUTH_INVALIDATIONS, matches_restored_authentication


def _open(url: str):
    # psycopg understands PostgreSQL URLs, including SQLAlchemy's driver spelling
    # after removing that driver qualifier. Configure BEGIN before the first SQL.
    url = url.replace("postgresql+psycopg://", "postgresql://", 1)
    conn = psycopg.connect(url)
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    conn.read_only = True
    return conn


def _schema(conn):
    tables = {}
    for schema, table, column in conn.execute("""
        SELECT n.nspname, c.relname, a.attname
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_catalog.pg_attribute a
          ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
        WHERE c.relkind IN ('r', 'p') AND n.nspname <> 'information_schema'
          AND n.nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
        ORDER BY n.nspname, c.relname, a.attname
    """):
        tables.setdefault((schema, table), set())
        if column is not None:
            tables[(schema, table)].add(column)
    return tables


def _primary_key(conn, table):
    return tuple(row[0] for row in conn.execute("""
        SELECT a.attname
        FROM pg_catalog.pg_index i
        JOIN pg_catalog.pg_class c ON c.oid = i.indrelid
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, position)
        JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
        WHERE i.indisprimary AND n.nspname = %s AND c.relname = %s
        ORDER BY k.position
    """, table))


def _checksum(conn, table, columns, primary_key):
    values = sql.SQL(', ').join(sql.SQL('{}::text').format(sql.Identifier(c)) for c in columns)
    row_text = sql.SQL('json_build_array({})::text').format(values)
    order = (sql.SQL(', ').join(sql.Identifier(c) for c in primary_key)
             if primary_key else row_text)
    query = sql.SQL("SELECT count(*), md5(coalesce(string_agg(md5({}), '' ORDER BY {}), '')) FROM {}.{}").format(
        row_text, order, sql.Identifier(table[0]), sql.Identifier(table[1]))
    return conn.execute(query).fetchone()



def _auth_matches(left, right, table, columns, key):
    query = sql.SQL('SELECT {} FROM {}.{} ORDER BY {}').format(
        sql.SQL(', ').join(map(sql.Identifier, columns)), sql.Identifier(table[0]),
        sql.Identifier(table[1]), sql.SQL(', ').join(map(sql.Identifier, key)))
    left_rows, right_rows = left.execute(query), right.execute(query)
    for old, new in zip_longest(left_rows, right_rows):
        if old is None or new is None or not matches_restored_authentication(
                table[1], dict(zip(columns, old)), dict(zip(columns, new))):
            return False
    return True

def _name(table):
    return table[1] if table[0] == 'public' else '.'.join(table)


def compare(left_url, right_url, expected=(), added_tables=(), added_columns=()):
    """Print only schema names, counts, and comparison results; return success."""
    def table_name(name):
        parts = name.split('.')
        if len(parts) == 1:
            parts.insert(0, 'public')
        if len(parts) != 2 or not all(re.fullmatch(r'[a-z_][a-z0-9_]*', part) for part in parts):
            raise ValueError('Invalid table name')
        return tuple(parts)

    expected = {table_name(name) for name in expected}
    added_tables = {table_name(name) for name in added_tables}
    additions = set()
    for name in added_columns:
        parts = name.split('.')
        if len(parts) != 3:
            raise ValueError('Invalid column name')
        table = table_name('.'.join(parts[:2]))
        if not re.fullmatch(r'[a-z_][a-z0-9_]*', parts[2]):
            raise ValueError('Invalid column name')
        additions.add((*table, parts[2]))
    with ExitStack() as stack:
        left = _open(left_url)
        stack.callback(left.close)  # close rolls back; never commit either snapshot
        right = _open(right_url)
        stack.callback(right.close)
        schemas = (_schema(left), _schema(right))
        success = True
        for table in sorted(schemas[0].keys() | schemas[1].keys()):
            name = _name(table)
            ignored = table in expected
            if table not in schemas[0] or table not in schemas[1]:
                side = 'left' if table in schemas[0] else 'right'
                print(f'{name}: differs (table only on {side})')
                success &= ignored or (side == 'right' and table in added_tables)
                continue
            columns = schemas[0][table] & schemas[1][table]
            left_key, right_key = _primary_key(left, table), _primary_key(right, table)
            key = left_key if left_key == right_key else ()
            left_count, left_hash = _checksum(left, table, sorted(columns), key)
            right_count, right_hash = _checksum(right, table, sorted(columns), key)
            unexpected_left = schemas[0][table] - schemas[1][table]
            unexpected_right = {c for c in schemas[1][table] - schemas[0][table] if (*table, c) not in additions}
            rows_match = (left_count, left_hash) == (right_count, right_hash)
            if table[0] == 'public' and table[1] in AUTH_INVALIDATIONS and key:
                open_column, replacements = AUTH_INVALIDATIONS[table[1]]
                if {open_column, *replacements} <= columns:
                    rows_match = _auth_matches(left, right, table, sorted(columns), key)
            same = not unexpected_left and not unexpected_right and rows_match
            ordering = '; row order depends on the server collation' if not key else ''
            print(f'{name}: {"same" if same else "differs"} (left {left_count}, right {right_count}{ordering})')
            for index, side in enumerate(('left', 'right')):
                for column in sorted(schemas[index][table] - schemas[1 - index][table]):
                    print(f'{name}: column {column} only on {side}')
            success &= same or ignored
        return success


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # An invalid argument can itself be a URL, so never repeat it.
        self.print_usage(sys.stderr)
        self.exit(2, 'Invalid arguments.\n')


def main(argv=None):
    parser = _Parser(description='Compare two PostgreSQL books without changing them.')
    parser.add_argument('--expect-differ', action='append', default=[], metavar='TABLE')
    parser.add_argument('--expect-added-table', action='append', default=[], metavar='SCHEMA.TABLE')
    parser.add_argument('--expect-added-column', action='append', default=[], metavar='SCHEMA.TABLE.COLUMN')
    args = parser.parse_args(argv)
    try:
        left, right = os.environ['OLD_DATABASE_URL'], os.environ['NEW_DATABASE_URL']
        return 0 if compare(left, right, args.expect_differ, args.expect_added_table, args.expect_added_column) else 1
    except Exception:
        # Driver errors may contain connection URLs, SQL text or row values.
        print('Comparison could not be completed.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
