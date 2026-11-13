# Release rules

The tests here enforce a release rule: **every migration in a release is transactional and has no escape**. An update runs core's
migrations and each first-party plugin's in one transaction and commits only if all of them and the checks pass;
that is only safe if no migration can end the transaction, step outside it or write anywhere but the books.
The rule is enforced two ways, not inferred from a successful run.

## `test_migration_lint.py`: the lint

Walks `packages/` and `plugins/` for every `migrations/versions` folder, so a new migration (or a new first-party
plugin's branch) is linted the day it is added, and refuses a migration that contains any of:

| rule | refused |
| --- | --- |
| `populated_books_refusal` | an immediate SQL `DO` block with an unfiltered table read and `RAISE EXCEPTION`/`RAISE SQLSTATE`, or a Python upgrade/helper with an unfiltered table read and a raise/assert; requires a reviewed revision and reason in `POPULATED_BOOKS_REFUSALS` |
| `commit` | SQL `COMMIT`, `END`/`COMMIT [WORK\|TRANSACTION]` as a statement, `PREPARE TRANSACTION`; a `.commit()` call |
| `rollback` | SQL `ROLLBACK`, `ABORT`; a `.rollback()` call |
| `autocommit_block` | `autocommit_block`, `isolation_level=`, assignments to `.autocommit`/`.isolation_level`, SQL `AUTOCOMMIT` |
| `concurrently` | SQL `CONCURRENTLY`; `postgresql_concurrently=` |
| `vacuum` | SQL `VACUUM` |
| `alter_system` | SQL `ALTER SYSTEM` |
| `alter_type_add_value` | SQL `ALTER TYPE ... ADD VALUE` (refused unless proven transactional on the supported majors) |
| `dblink` | SQL `dblink...`, `postgres_fdw` |
| `second_connection` | `create_engine` and its kin, any `connect(...)`, `raw_connection`, `.engine`, a driver import |
| `file_write` | `open(...)` in a write mode (or a mode the source cannot show), path and `os` writes, `shutil`, `tempfile`; SQL `COPY ... TO`/`PROGRAM`, `lo_export`, `pg_file_write` |
| `network` | `requests`, `urllib`, `socket`, `http`, `httpx` and the like |
| `process` | `subprocess`, `os.system` and the like |

Calls, names and imports are matched on the parsed Python (`ast`), including ordinary import and callable aliases,
so a comment never counts. SQL keywords are matched,
case-insensitively, on string constants with SQL comments, quoted literals and quoted identifiers blanked first
(`'committed'`, `-- COMMIT` and `"commit"` never count; `ON COMMIT DROP` is a clause and is allowed); a dollar-quoted
body is read, since a `COMMIT` in a `DO` block is an escape too. Literal string concatenations are read together;
f-string replacement fields stand in as placeholders. Docstrings are not read. A string that ends inside an
open quote or comment is read as it is (fail closed). Each rule is proven to fire on a planted migration (a string, or
a file under pytest's `tmp_path`; never in the repo's migration folders). A real migration that fails a rule fails
the release suite with its file, line, rule and explanation. There are no expected-failure marks. The only exception
registry is `POPULATED_BOOKS_REFUSALS` in `migration_rules.py`, keyed by revision id with the predecessor, exact refusal
message and reviewed reason. It exempts only the population-refusal rule; transaction and escape rules always apply.
Planted SQL and Python guards, and a copy of the actual reviewed migration with a new revision id, prove a new
population refusal fails lint. Filtered checks for invalid historical rows, stored trigger functions, and backfills
that read populated tables without refusing them are allowed. Adding an exception requires a reviewed change to the
registry. Report other findings for a migration fix; the migration itself is never edited here. This source lint is a release
check, not a sandbox for arbitrary Python or dynamically generated SQL; rollback tests provide independent evidence.

The engine lives in `migration_rules.py`.

## `test_migration_rollback.py`: rollback at every boundary

For each migration of core's chain and of each first-party plugin's chain (a plugin's after core's head, as the update
runs them), through the engine's own runner (`erpclaw_core.migrate.upgrade`) on a connection already inside one
transaction:

1. from empty books, the whole chain up to and including that migration, a failure injected right after it, rolled
   back;
2. from books committed at the previous revision (core's head for a plugin's base), populated with a company,
   customer and account using that historical schema, that migration, a failure injected right after it, rolled back
   (the reviewed refusal boundary instead asserts its exact reason and unchanged books);
3. from the same populated predecessor, commit the migration, reopen on another connection, prove its revision is
   recorded, and retry it without any book changes. Data backfills for principals, identity, account normal side,
   city keys and company status have explicit assertions.

The deliberate format-2 boundary is the sole reviewed exception: populated books at `0010` must be refused by `0011`
with the registry's exact message, and the before/after dumps must remain byte-identical, including the revision row.
Ledger format 2 was added on 4 Nov before any v3.0 release, and preview 2 already ships it. Empty books committed at
that predecessor must upgrade, commit, reopen and retry successfully; the empty-book rollback test also remains.
Every boundary starting from `0011` and every first-party plugin boundary still requires successful upgrades of
populated books and rollback after an injected failure. An unregistered refusal fails those tests as well as lint
for the source guard patterns described above. The registry also has to name real revisions and their actual predecessors.

The complete core and plugin chains also run in one transaction, followed by the engine's book-opening and ledger
integrity checks, both through commit and through an injected failure after the checks. These are migration tests;
they do not implement the service's update, backup, install-record refresh or cluster-switch orchestration.

Each time a dump of the books taken before and after is equal byte for byte: every non-system schema's tables,
columns, defaults, constraints, indexes, triggers, functions, types, views, sequence definitions and ownership, rules,
policies, comments, privileges, extensions, event triggers, database settings, large objects and every table's rows. **PostgreSQL's
sequence counters (`last_value`, `is_called`) are excluded**: `nextval` is not transactional, so a rolled-back insert
still moves a counter that existed before; the comparison excludes them and says so. Planted residue tests cover DDL,
data, grants, rules, policies, sequence ownership and large-object content; a moved counter is ignored, and a
migration made to `COMMIT` mid-chain is caught.

The rollback tests use the suite's PostgreSQL test database (`ERPCLAW_PG_TEST_URL`, `tests/conftest.py`). Use a private
disposable database, or hold the suite lock for a shared one. Never run database-mutating suites concurrently on it.
Set `PG_SOCKET_DIR` to the socket directory of your own PostgreSQL server.

```sh
ERPCLAW_PG_TEST_URL=postgresql+psycopg://postgres@/build_test \
PGHOST="$PG_SOCKET_DIR" ERPCLAW_REQUIRE_PG=1 ERPCLAW_TEST_DB_PRIVATE=1 \
.venv/bin/python -m pytest -q tests/release/ --tb=line
```
