"""Pytest fixtures for ERPClaw OS module validation tests."""
import os
import sys
import textwrap

import pytest

# Make the erpclaw-os package importable
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
OS_DIR = os.path.dirname(TESTS_DIR)
if OS_DIR not in sys.path:
    sys.path.insert(0, OS_DIR)


@pytest.fixture
def temp_module_dir(tmp_path):
    """Create a temp directory with a valid minimal module.

    Contains:
        - init_db.py with a properly prefixed table
        - scripts/db_query.py with ok/err imports
        - SKILL.md with valid YAML frontmatter
        - scripts/tests/test_basic.py with a minimal test
    """
    module_dir = tmp_path / "testclaw"
    module_dir.mkdir()

    # init_db.py
    (module_dir / "init_db.py").write_text(textwrap.dedent("""\
        import os, sqlite3, sys

        DEFAULT_DB_PATH = os.path.expanduser("~/.openclaw/erpclaw/data.sqlite")

        def init_schema(db_path=None):
            db_path = db_path or DEFAULT_DB_PATH
            conn = sqlite3.connect(db_path)
            conn.executescript(\"\"\"
                CREATE TABLE IF NOT EXISTS testclaw_item (
                    id          TEXT PRIMARY KEY,
                    name        TEXT NOT NULL,
                    price       TEXT NOT NULL DEFAULT '0',
                    amount      TEXT NOT NULL DEFAULT '0',
                    company_id  TEXT NOT NULL,
                    created_at  TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS testclaw_order (
                    id          TEXT PRIMARY KEY,
                    item_id     TEXT NOT NULL REFERENCES testclaw_item(id),
                    quantity    INTEGER NOT NULL DEFAULT 1,
                    total       TEXT NOT NULL DEFAULT '0',
                    company_id  TEXT NOT NULL,
                    created_at  TEXT DEFAULT (datetime('now'))
                );
            \"\"\")
            conn.commit()
            conn.close()

        if __name__ == "__main__":
            path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB_PATH
            init_schema(path)
    """))

    # scripts/db_query.py
    scripts_dir = module_dir / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "db_query.py").write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import os, sys, json
        sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
        from erpclaw_lib.response import ok, err
        from erpclaw_lib.args import SafeArgumentParser

        def handle_status(args):
            ok({"message": "TestClaw is running"})

        def handle_add_item(args):
            ok({"message": "Item added", "id": "test-id"})

        def main():
            parser = SafeArgumentParser()
            parser.add_argument("--action", required=True)
            parser.add_argument("--name", default=None)
            args, unknown = parser.parse_known_args()

            if args.action == "status":
                handle_status(args)
            elif args.action == "test-add-item":
                handle_add_item(args)
            else:
                err(f"Unknown action: {args.action}")

        if __name__ == "__main__":
            main()
    """))

    # SKILL.md
    (module_dir / "SKILL.md").write_text(textwrap.dedent("""\
        ---
        name: testclaw
        version: 1.0.0
        description: Test module for validation
        author: TestAuthor
        scripts:
          - scripts/db_query.py
        ---

        # testclaw

        Test module.

        ## Actions

        | Action | Description |
        |--------|-------------|
        | `test-add-item` | Add a test item |
        | `status` | Check status |
    """))

    # scripts/tests/
    tests_dir = scripts_dir / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").write_text("")
    (tests_dir / "test_basic.py").write_text(textwrap.dedent("""\
        def test_add_item():
            assert True

        def test_status():
            assert True
    """))

    return module_dir


@pytest.fixture
def violation_module(tmp_path):
    """Factory fixture that creates a temp module with a specific article violation.

    Usage:
        module_dir = violation_module(article=2)  # Creates module violating Article 2
    """
    def _create(article: int) -> str:
        module_dir = tmp_path / f"violclaw_art{article}"
        module_dir.mkdir(exist_ok=True)

        scripts_dir = module_dir / "scripts"
        scripts_dir.mkdir(exist_ok=True)
        tests_dir = scripts_dir / "tests"
        tests_dir.mkdir(exist_ok=True)
        (tests_dir / "__init__.py").write_text("")

        # Base SKILL.md (valid)
        skill_md = textwrap.dedent("""\
            ---
            name: violclaw
            version: 1.0.0
            description: Violation test module
            author: TestAuthor
            scripts:
              - scripts/db_query.py
            ---

            # violclaw

            Test module with intentional violations.

            ## Actions

            | Action | Description |
            |--------|-------------|
            | `viol-add-thing` | Add a thing |
            | `status` | Check status |
        """)

        # Base init_db.py (valid)
        init_db = textwrap.dedent("""\
            import sqlite3
            conn = None
            DDL = \"\"\"
                CREATE TABLE IF NOT EXISTS violclaw_item (
                    id      TEXT PRIMARY KEY,
                    name    TEXT NOT NULL,
                    price   TEXT NOT NULL DEFAULT '0',
                    company_id TEXT NOT NULL
                );
            \"\"\"
        """)

        # Base db_query.py (valid)
        db_query = textwrap.dedent("""\
            import os, sys, json
            sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
            from erpclaw_lib.response import ok, err

            def main():
                ok({"message": "ok"})

            if __name__ == "__main__":
                main()
        """)

        # Base test
        test_py = textwrap.dedent("""\
            def test_add_thing():
                assert True

            def test_status():
                assert True
        """)

        # Apply violation per article
        if article == 1:
            # Table without module prefix
            init_db = textwrap.dedent("""\
                import sqlite3
                DDL = \"\"\"
                    CREATE TABLE IF NOT EXISTS appointment (
                        id      TEXT PRIMARY KEY,
                        name    TEXT NOT NULL,
                        price   TEXT NOT NULL DEFAULT '0'
                    );
                \"\"\"
            """)

        elif article == 2:
            # REAL/FLOAT column for money field
            init_db = textwrap.dedent("""\
                import sqlite3
                DDL = \"\"\"
                    CREATE TABLE IF NOT EXISTS violclaw_item (
                        id      TEXT PRIMARY KEY,
                        name    TEXT NOT NULL,
                        amount  REAL NOT NULL DEFAULT 0.0,
                        price   FLOAT NOT NULL DEFAULT 0.0,
                        company_id TEXT NOT NULL
                    );
                \"\"\"
            """)

        elif article == 3:
            # INTEGER primary key instead of TEXT UUID
            init_db = textwrap.dedent("""\
                import sqlite3
                DDL = \"\"\"
                    CREATE TABLE IF NOT EXISTS violclaw_item (
                        id      INTEGER PRIMARY KEY,
                        name    TEXT NOT NULL,
                        price   TEXT NOT NULL DEFAULT '0'
                    );
                \"\"\"
            """)

        elif article == 4:
            # Foreign key referencing nonexistent table
            init_db = textwrap.dedent("""\
                import sqlite3
                DDL = \"\"\"
                    CREATE TABLE IF NOT EXISTS violclaw_item (
                        id          TEXT PRIMARY KEY,
                        name        TEXT NOT NULL,
                        category_id TEXT REFERENCES nonexistent_table(id),
                        price       TEXT NOT NULL DEFAULT '0'
                    );
                \"\"\"
            """)

        elif article == 5:
            # INSERT/UPDATE/DELETE targeting a table not owned by the module
            db_query = textwrap.dedent("""\
                import os, sys, json, sqlite3
                sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
                from erpclaw_lib.response import ok, err

                def add_thing(conn):
                    conn.execute("INSERT INTO customer (id, name) VALUES (?, ?)", ("1", "test"))
                    conn.execute("UPDATE account SET balance = '100' WHERE id = ?", ("1",))
                    ok({"message": "done"})

                if __name__ == "__main__":
                    add_thing(None)
            """)

        elif article == 6:
            # Direct INSERT INTO gl_entry
            db_query = textwrap.dedent("""\
                import os, sys, json, sqlite3
                sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
                from erpclaw_lib.response import ok, err

                def post_entries(conn):
                    conn.execute("INSERT INTO gl_entry (id, account_id) VALUES (?, ?)", ("1", "a1"))
                    conn.execute("INSERT INTO stock_ledger_entry (id, item_id) VALUES (?, ?)", ("2", "i1"))
                    ok({"message": "posted"})

                if __name__ == "__main__":
                    post_entries(None)
            """)

        elif article == 7:
            # Action returning raw print() instead of ok()/err()
            db_query = textwrap.dedent("""\
                import json

                def main():
                    print(json.dumps({"result": "success"}))

                if __name__ == "__main__":
                    main()
            """)

        elif article == 8:
            # Action in SKILL.md with no corresponding test
            skill_md = textwrap.dedent("""\
                ---
                name: violclaw
                version: 1.0.0
                description: Violation test module
                author: TestAuthor
                scripts:
                  - scripts/db_query.py
                ---

                # violclaw

                ## Actions

                | Action | Description |
                |--------|-------------|
                | `viol-add-thing` | Add a thing |
                | `viol-update-thing` | Update a thing |
                | `viol-delete-thing` | Delete a thing |
                | `viol-list-things` | List things |
                | `status` | Check status |
            """)
            # Only test for add_thing — missing update, delete, list
            test_py = textwrap.dedent("""\
                def test_add_thing():
                    assert True
            """)

        elif article == 10:
            # Hardcoded password/API key in code
            db_query = textwrap.dedent("""\
                import os, sys, json
                sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
                from erpclaw_lib.response import ok, err

                password = "SuperSecret123!"
                api_key = "sk-1234567890abcdef"

                def main():
                    ok({"message": "ok"})

                if __name__ == "__main__":
                    main()
            """)

        elif article == 11:
            # SKILL.md exceeding 300 lines or missing YAML frontmatter
            skill_md = "# No YAML Frontmatter\n\nJust a regular markdown file.\n" + ("Line\n" * 301)

        elif article == 12:
            # Action name not using kebab-case or missing namespace prefix
            skill_md = textwrap.dedent("""\
                ---
                name: violclaw
                version: 1.0.0
                description: Violation test module
                author: TestAuthor
                scripts:
                  - scripts/db_query.py
                ---

                # violclaw

                ## Actions

                | Action | Description |
                |--------|-------------|
                | `add-thing` | Add without prefix |
                | `update_thing` | Underscore not kebab |
                | `status` | Check status |
            """)

        (module_dir / "SKILL.md").write_text(skill_md)
        (module_dir / "init_db.py").write_text(init_db)
        (scripts_dir / "db_query.py").write_text(db_query)
        (tests_dir / "test_basic.py").write_text(test_py)

        return str(module_dir)

    return _create


@pytest.fixture
def src_root():
    """Return the path to the real src/ directory for cross-module testing."""
    # Navigate from this test file up to src/
    this_file = os.path.abspath(__file__)
    # tests/ -> erpclaw-os/ -> scripts/ -> erpclaw/ -> src/
    path = os.path.dirname(this_file)
    for _ in range(4):
        path = os.path.dirname(path)
    src_path = os.path.join(path, "src")
    if os.path.isdir(src_path):
        return src_path
    # Fallback: try standard project structure
    return os.path.join(os.path.dirname(path), "src")
