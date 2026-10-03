"""Ledger-write classifier golden table, cache pin and constant pins."""

import os
import sys

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import setup_helpers  # noqa: E402  (binds erpclaw_lib to this tree)

from erpclaw_lib import authority_gate  # noqa: E402
from erpclaw_lib import authority_sink  # noqa: E402
from erpclaw_lib.query import Field, P, Q, Table  # noqa: E402
from erpclaw_lib.query import insert_row as _insert_row  # noqa: E402
from erpclaw_lib.query import update_row as _update_row  # noqa: E402

SINKS = (
    "gl_entry",
    "gl_chain_head",
    "stock_ledger_entry",
    "stock_fifo_layer",
    "payment_ledger_entry",
)

ALL = frozenset(SINKS)

CHAIN_HEAD_UPSERT = (
    "\n        INSERT INTO gl_chain_head "
    "(company_id, last_sequence, last_checksum, updated_at)\n"
    "        VALUES (?, 0, ?, ?)\n"
    "        ON CONFLICT(company_id) DO UPDATE SET "
    "updated_at = excluded.updated_at\n        "
)

INSTALL_PHASE_SELECT = (
    Q.from_(Table("authority_install"))
    .select(Field("phase"), Field("install_id"))
    .get_sql()
)

CASES = [
    ("INSERT INTO gl_entry (id) VALUES (?)", frozenset({"gl_entry"})),
    ("insert into gl_entry (id) values (?)", frozenset({"gl_entry"})),
    (
        "INSERT OR IGNORE INTO gl_entry (id) VALUES (?)",
        frozenset({"gl_entry"}),
    ),
    ("REPLACE INTO gl_entry (id) VALUES (?)", frozenset({"gl_entry"})),
    ('INSERT INTO "gl_entry" (id) VALUES (?)', frozenset({"gl_entry"})),
    (
        "INSERT INTO public.gl_entry (id) VALUES (?)",
        frozenset({"gl_entry"}),
    ),
    (
        "UPDATE OR IGNORE gl_entry SET remarks = ?",
        frozenset({"gl_entry"}),
    ),
    (
        "WITH x AS (SELECT 1) INSERT INTO payment_ledger_entry (id) VALUES (?)",
        frozenset({"payment_ledger_entry"}),
    ),
    (
        "WITH x AS (INSERT INTO gl_entry (id) VALUES (?) RETURNING id)"
        " SELECT * FROM x",
        frozenset({"gl_entry"}),
    ),
    (
        "WITH x AS (DELETE FROM gl_entry RETURNING *)"
        " INSERT INTO other SELECT * FROM x",
        frozenset({"gl_entry"}),
    ),
    (CHAIN_HEAD_UPSERT, frozenset({"gl_chain_head"})),
    (
        "INSERT INTO gl_entry (id) VALUES (?) ON CONFLICT DO UPDATE SET"
        " remarks = excluded.remarks RETURNING id",
        frozenset({"gl_entry"}),
    ),
    ("UPDATE stock_fifo_layer SET qty = ?", frozenset({"stock_fifo_layer"})),
    (
        "DELETE FROM stock_ledger_entry WHERE id = ?",
        frozenset({"stock_ledger_entry"}),
    ),
    ("ALTER TABLE gl_entry RENAME TO x", frozenset({"gl_entry"})),
    ("DROP TABLE stock_fifo_layer", frozenset({"stock_fifo_layer"})),
    ("TRUNCATE payment_ledger_entry", frozenset({"payment_ledger_entry"})),
    ("TRUNCATE gl_entry", frozenset({"gl_entry"})),
    ("COPY gl_entry FROM STDIN", frozenset({"gl_entry"})),
    (
        "MERGE INTO gl_entry g USING src s ON g.id = s.id"
        " WHEN MATCHED THEN UPDATE SET x = 1",
        frozenset({"gl_entry"}),
    ),
    (
        "MERGE INTO payment_ledger_entry USING src s"
        " ON payment_ledger_entry.id = s.id"
        " WHEN MATCHED THEN UPDATE SET x = 1",
        frozenset({"payment_ledger_entry"}),
    ),
    (
        "CREATE TRIGGER t AFTER INSERT ON other BEGIN"
        " INSERT INTO gl_entry (id) VALUES (new.id); END",
        frozenset({"gl_entry"}),
    ),
    (
        "CREATE FUNCTION f() RETURNS void AS $$"
        " INSERT INTO gl_entry (id) VALUES ('x') $$ LANGUAGE sql",
        frozenset({"gl_entry"}),
    ),
    ("SELECT 1; DELETE FROM gl_entry", frozenset({"gl_entry"})),
    ("EXPLAIN SELECT * FROM gl_entry", frozenset({"gl_entry"})),
    (
        "INSERT INTO /* c */ gl_entry (id) VALUES (?)",
        frozenset({"gl_entry"}),
    ),
    ('INSERT INTO U&"gl_entry" (id) VALUES (?)', ALL),
    ('INSERT INTO U&"other" (id) VALUES (?)', ALL),
    ("SELECT * FROM gl_entry", frozenset()),
    ("SELECT * FROM gl_entry FOR UPDATE", frozenset()),
    ("INSERT INTO gl_entry_m0_old (id) VALUES (?)", frozenset()),
    ("INSERT INTO other (a) VALUES ('gl_entry')", frozenset()),
    ("SAVEPOINT ERPCLAW_INSTALL_PROBE", frozenset()),
    ("RELEASE SAVEPOINT ERPCLAW_INSTALL_PROBE", frozenset()),
    ("INSERT INTO other (a) SELECT x FROM gl_entry", frozenset()),
    ("INSERT INTO report_snapshot SELECT * FROM gl_entry", frozenset()),
    ("UPDATE other SET x = (SELECT max(id) FROM gl_entry)", frozenset()),
    (
        "DELETE FROM other WHERE id IN (SELECT id FROM gl_entry)",
        frozenset(),
    ),
    (
        "UPDATE other SET x = 1 FROM gl_entry"
        " WHERE other.id = gl_entry.id",
        frozenset(),
    ),
    (
        "UPDATE other SET x = g.id FROM gl_entry g"
        " WHERE other.id = g.id",
        frozenset(),
    ),
    (
        "DELETE FROM other USING gl_entry WHERE other.id = gl_entry.id",
        frozenset(),
    ),
    (
        "DELETE FROM other USING gl_entry g WHERE other.id = g.id",
        frozenset(),
    ),
    ("(SELECT * FROM gl_entry)", frozenset()),
    (
        "WITH RECURSIVE t(n) AS (SELECT 1) SELECT * FROM gl_entry",
        frozenset(),
    ),
    ("INSERT INTO other (a) VALUES (E'it\\'s gl_entry')", frozenset()),
    (
        "SELECT REPLACE(remarks, 'a', 'b') FROM gl_entry",
        frozenset(),
    ),
    ("-- INSERT INTO gl_entry\nSELECT 1", frozenset()),
    (
        "INSERT INTO other (id) VALUES (?) ON CONFLICT (id) DO UPDATE"
        " SET x = excluded.x",
        frozenset(),
    ),
    (INSTALL_PHASE_SELECT, frozenset()),
    ("INSERT INTO ? SELECT * FROM gl_entry", frozenset({"gl_entry"})),
    ("CREATE TRIGGER t BEFORE INSERT ON gl_entry BEGIN DELETE FROM other WHERE 0;"
     " SELECT RAISE(IGNORE); END", frozenset({"gl_entry"})),
    ("CREATE RULE r AS ON INSERT TO gl_entry DO INSTEAD INSERT INTO other VALUES (NEW.id)",
     frozenset({"gl_entry"})),
    ("CREATE TRIGGER t AFTER UPDATE ON stock_fifo_layer BEGIN UPDATE other SET x = 1; END",
     frozenset({"stock_fifo_layer"})),
]

for _t in SINKS:
    CASES.append(
        (
            Q.into(Table(_t)).columns("id").insert(P()).get_sql(),
            frozenset({_t}),
        )
    )
    CASES.append(
        (
            Q.update(Table(_t)).set(Field("x"), P()).get_sql(),
            frozenset({_t}),
        )
    )
    CASES.append(
        (Q.from_(Table(_t)).delete().get_sql(), frozenset({_t}))
    )
    CASES.append((_insert_row(_t, {"id": P()})[0], frozenset({_t})))
    CASES.append(
        (_update_row(_t, {"x": P()}, {"id": P()}), frozenset({_t}))
    )


@pytest.mark.parametrize(("sql", "expected"), CASES)
def test_golden_table(sql, expected):
    assert authority_sink.classify(sql) == expected


def test_classify_is_cached_and_pure():
    authority_sink.classify.cache_clear()
    first = authority_sink.classify("INSERT INTO gl_entry (id) VALUES (?)")
    info = authority_sink.classify.cache_info()
    assert info.misses >= 1
    second = authority_sink.classify("INSERT INTO gl_entry (id) VALUES (?)")
    assert second is first
    assert authority_sink.classify.cache_info().hits >= 1


def test_constants():
    assert authority_sink.ENFORCED_FAMILIES == frozenset({"gl"})
    assert authority_sink.LEDGER_WRITE_UNAUTHORIZED == "LEDGER_WRITE_UNAUTHORIZED"
    assert authority_sink.SUGGESTIONS == {
        "LEDGER_WRITE_UNAUTHORIZED": (
            "This change writes to the ledger. Once authority is active,"
            " a ledger write needs a single-use authorization issued for this"
            " exact call and consumed in the same transaction; pass its id as"
            " --authorization-id. Nothing was written."
        )
    }
    err = authority_sink.LedgerWriteRefused("X")
    assert err.args == ("X",)
    assert isinstance(err, authority_gate.AuthorityRefusal)
