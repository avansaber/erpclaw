# ERPClaw

An open-source ERP you run by chat. GPL-3.0-only (see `LICENSE`; dependencies in `THIRD_PARTY_NOTICES.md` and `DEPENDENCY_NOTES.md`).

ERPClaw v3 combines a CLI, PostgreSQL books and first-party business plugins. The engine calculates financial effects and requires approval for proposed writes. Operation availability is declared by each plugin; a planned operation returns a planned response.

## Layout

| Path | What |
|---|---|
| `packages/erpclaw-core` | Ledger (the only ledger writer), money, periods, metrics, contracts, approvals and operation keys, outbox, plugin loader |
| `packages/erpclaw-assistant` | The assistant steps (LangGraph) and the model client |
| `packages/erpclaw-api` | The engine API (FastAPI) |
| `packages/erpclaw-cli` | `erpclaw` command: `init`, `chat`, `serve`, `eval`, `demo` |
| `plugins/erpclaw-sales` | Customers, invoices, payments; demo books |
| `plugins/erpclaw-stock` | Stock operations and warehouse setup |
| `plugins/erpclaw-email` | Email connector |
| `plugins/erpclaw-email-folder` | Email connector that writes `.eml` files instead of sending |
| `tools/` | Push scanner, dependency licence gate |
| `tests/` | Engine tests, on the PostgreSQL `*_test` database named by `ERPCLAW_PG_TEST_URL` |

## Try it

Demo books of their own, on a PostgreSQL server of their own and a local model, in three commands. You need
Docker, and a local model behind an OpenAI-compatible server, for example LM Studio serving Qwen 3.5 9B at
`http://127.0.0.1:1234/v1` (change it with `ERPCLAW_MODEL_BASE_URL` and `ERPCLAW_MODEL`).

The demo's server is a container of its own on port 55433, so it never meets another PostgreSQL on this machine
(5432 is often taken). If `docker run` says the port is already in use, pick another and change it in both lines.
Give the server a few seconds to start before `demo up`. The password lives only in this shell: a new terminal needs
the `ERPCLAW_DEMO_PG_URL` line again with the same value, which the container still holds
(`export DEMO_PG_PASSWORD="$(docker exec erpclaw-demo-pg printenv POSTGRES_PASSWORD)"`).

```bash
export DEMO_PG_PASSWORD="$(openssl rand -hex 16)"
docker run -d --name erpclaw-demo-pg -p 127.0.0.1:55433:5432 \
  -e POSTGRES_USER=erpclaw -e POSTGRES_PASSWORD="$DEMO_PG_PASSWORD" -e POSTGRES_DB=erpclaw postgres:18
export ERPCLAW_DEMO_PG_URL="postgresql+psycopg://erpclaw:${DEMO_PG_PASSWORD}@127.0.0.1:55433/erpclaw"   # its superuser
uv run erpclaw demo up      # the demo books (database erpclaw_demo), their own login and folder
uv run erpclaw demo chat    # chat with them: "invoice Acme Corporation for 5 copper wire"
uv run erpclaw demo reset   # start again; `reset --down` removes the demo
```

The demo touches only what it made: the database `erpclaw_demo`, the login `erpclaw_demo_rt` (a fresh password each
time, never stored) and the folder `~/.erpclaw-demo/erpclaw_demo` (change the parent with `ERPCLAW_DEMO_HOME`).
Before it writes or drops anything it checks that the database carries the engine's demo mark, that nothing outside
the demo uses the login, and that it is not the database your own `ERPCLAW_DATABASE_URL` names; anything else is
refused with nothing changed. `--name other_demo` runs a second demo beside the first. A server it cannot reach or
log in to is reported in one line, naming the host, port and user it tried (never the password).

## Install PostgreSQL

ERPClaw keeps its books in PostgreSQL and needs it to run: any other database URL is refused at startup with "ERPClaw v3 needs PostgreSQL; see the install notes." Any supported PostgreSQL release works; the tests run on 18.

With Docker (any system), listening on this machine only:

```bash
docker run -d --name erpclaw-pg -p 127.0.0.1:5432:5432 \
  -e POSTGRES_USER=erpclaw -e POSTGRES_PASSWORD=change-me -e POSTGRES_DB=erpclaw postgres:18
```

Or natively, then create the owner role and the books database as shown below:

- Ubuntu: `sudo apt install postgresql`, then run `psql` as `sudo -u postgres psql`.
- macOS (Homebrew): `brew install postgresql@18 && brew services start postgresql@18`.
- Windows: the installer from <https://www.postgresql.org/download/windows/>, then `psql` from its Start menu entry.

```sql
CREATE ROLE erpclaw LOGIN CREATEDB PASSWORD 'change-me';   -- the owner: creates and migrates the books
CREATE DATABASE erpclaw OWNER erpclaw;
```

The engine runs as a separate runtime role that holds only the privileges it needs; the owner grants them. Create it (in Docker: `docker exec -it erpclaw-pg psql -U erpclaw`), point ERPClaw at both, and make demo books:

```sql
CREATE ROLE erpclaw_rt LOGIN PASSWORD 'change-me-too';      -- the runtime
```

```bash
export OWNER_PASSWORD=change-me RUNTIME_PASSWORD=change-me-too   # the passwords you set above
export ERPCLAW_MIGRATION_URL="postgresql+psycopg://erpclaw:${OWNER_PASSWORD}@127.0.0.1:5432/erpclaw"
export ERPCLAW_DATABASE_URL="postgresql+psycopg://erpclaw_rt:${RUNTIME_PASSWORD}@127.0.0.1:5432/erpclaw"
uv run erpclaw init     # as the owner: the tables, the demo books and the runtime role's grants
unset ERPCLAW_MIGRATION_URL
uv run erpclaw chat     # as the runtime role
```

## Develop

The tests need a database whose name ends in `_test` (they empty it), for example `CREATE DATABASE erpclaw_test OWNER erpclaw;`:

```bash
uv sync
export ERPCLAW_PG_TEST_URL="postgresql+psycopg://erpclaw:${OWNER_PASSWORD}@127.0.0.1:5432/erpclaw_test"
uv run pytest -q
uv run python tools/check_lock.py uv.lock --licenses
```

`erpclaw eval` runs every trial on its own disposable database, made and dropped on the server named by `ERPCLAW_EVAL_PG_URL` (else `ERPCLAW_PG_TEST_URL`); that role needs `CREATEDB` and `CREATEROLE`.

Local mail, the assistant's own store and eval reports go to `$ERPCLAW_HOME` (default `./var`). The engine refuses to open books it does not own.
