"""Cross-skill integration test fixtures."""
import os
import sys
import sqlite3
import pytest

# Add all skill script directories to path
SKILLS_DIR = os.path.dirname(os.path.dirname(__file__))
for skill_name in ("erpclaw-setup", "erpclaw-gl", "erpclaw-journals",
                    "erpclaw-payments", "erpclaw-tax", "erpclaw-reports",
                    "erpclaw-inventory", "erpclaw-selling", "erpclaw-buying",
                    "erpclaw-manufacturing", "erpclaw-hr", "erpclaw-payroll",
                    "erpclaw-crm", "erpclaw-support", "erpclaw-billing",
                    "erpclaw-ai-engine", "erpclaw-analytics",
                    "erpclaw-projects", "erpclaw-assets", "erpclaw-quality"):
    scripts_dir = os.path.join(SKILLS_DIR, skill_name, "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

# Add shared lib
LIB_DIR = os.path.expanduser("~/.openclaw/erpclaw/lib")
if LIB_DIR not in sys.path:
    sys.path.insert(0, LIB_DIR)

# Add tests dir for helpers
TESTS_DIR = os.path.dirname(__file__)
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

from helpers import _run_init_db  # noqa: E402
from erpclaw_lib.db import _DecimalSum  # noqa: E402

_LOCAL_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../"))
_SERVER_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "erpclaw-setup", "scripts")
if os.path.exists(os.path.join(_LOCAL_ROOT, "init_db.py")):
    PROJECT_ROOT = _LOCAL_ROOT
elif os.path.exists(os.path.join(_SERVER_ROOT, "init_db.py")):
    PROJECT_ROOT = _SERVER_ROOT
else:
    PROJECT_ROOT = _LOCAL_ROOT


@pytest.fixture
def db_dir(tmp_path):
    """Temporary directory for SQLite stress test databases."""
    d = str(tmp_path / "stress")
    os.makedirs(d, exist_ok=True)
    return d


@pytest.fixture
def fresh_db(tmp_path):
    """Fresh database with all tables but no data."""
    db_path = str(tmp_path / "test.sqlite")
    _run_init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.create_aggregate("decimal_sum", 1, _DecimalSum)
    yield conn
    conn.close()
