import os, sys, sqlite3
import importlib.util
if importlib.util.find_spec("erpclaw_lib") is None:
    sys.path.insert(0, os.path.join(os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))
from erpclaw_lib.response import ok, err

def post_journal_entry(conn):
    """Violation: direct INSERT into gl_entry instead of using erpclaw_lib.gl_posting."""
    conn.execute("INSERT INTO gl_entry (id, account_id, debit, credit) VALUES (?, ?, ?, ?)",
                 ("gl-1", "acc-1", "100.00", "0"))
    conn.execute("INSERT INTO stock_ledger_entry (id, item_id, qty) VALUES (?, ?, ?)",
                 ("sle-1", "item-1", "5"))
    ok({"message": "posted"})

if __name__ == "__main__":
    post_journal_entry(None)
