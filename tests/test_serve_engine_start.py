"""Starting the engine ends earlier sign-in sessions and voids earlier codes."""
import errno
import os
import secrets
import socket
from datetime import timedelta
from types import SimpleNamespace

import pytest
import uvicorn
from sqlalchemy import create_engine, select
from typer.testing import CliRunner

from erpclaw_assistant.model import ScriptedModel
from erpclaw_cli import main as cli


@pytest.fixture
def started(engine, runtime_login, monkeypatch):
    runtime = create_engine(runtime_login)
    monkeypatch.setenv("ERPCLAW_DATABASE_URL", runtime_login)
    monkeypatch.setattr("erpclaw_core.db.make_engine", lambda url: runtime)
    monkeypatch.setattr("erpclaw_assistant.muse.model_from_env", lambda: ScriptedModel([]))
    yield SimpleNamespace(owner=engine, runtime=runtime)
    runtime.dispose()


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as free:
        free.bind(("127.0.0.1", 0))
        return free.getsockname()[1]


def _fd_port(options):
    with socket.socket(fileno=os.dup(options["fd"])) as bound:
        host, port = bound.getsockname()
        assert host == "127.0.0.1" and port != 0
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as other:
            with pytest.raises(OSError) as collision:
                other.bind((host, port))
            assert collision.value.errno == errno.EADDRINUSE
        return port


def _dummy_value(col, at):
    try:
        py = col.type.python_type
    except Exception:
        py = None
    import datetime
    if py is str:
        length = getattr(col.type, "length", None)
        base = "test-" + col.name
        return base[:length] if length else base
    if py is int:
        return 1
    if py is bool:
        return False
    if py is datetime.datetime:
        return at
    if py is datetime.date:
        return at.date() if hasattr(at, "date") else at
    name = type(col.type).__name__.lower()
    if "json" in name:
        return {}
    if "bool" in name:
        return False
    if "int" in name:
        return 1
    if "date" in name or "time" in name:
        return at
    if "binary" in name or "large" in name:
        return b"test"
    return "test"


def _open_session(owner, name="owner", *, created_at=None):
    from erpclaw_core import models as m
    from erpclaw_core import principals as pr
    from erpclaw_core.common import new_id, now
    with owner.begin() as conn:
        pid = conn.execute(select(m.principal.c.id).where(m.principal.c.name == name)).scalar_one_or_none()
        if pid is None:
            from erpclaw_core import administration as admin
            cid = conn.execute(select(m.company.c.id).order_by(m.company.c.id).limit(1)).scalar_one()
            admin.add_principal(conn, name, ["view", "draft"], [cid])
            pid = conn.execute(select(m.principal.c.id).where(m.principal.c.name == name)).scalar_one()
        at = created_at if created_at is not None else now()
        cols = {c.name: c for c in m.principal_session.c}
        secret = secrets.token_urlsafe(32)
        key = secrets.token_urlsafe(32)
        values = {}
        if "principal_id" in cols:
            values["principal_id"] = pid
        if "created_at" in cols:
            values["created_at"] = at
        if "ended_at" in cols:
            values["ended_at"] = None
        if "ended_why" in cols:
            values["ended_why"] = None
        if "secret_sha256" in cols:
            try:
                values["secret_sha256"] = pr.session_digest(secret)
            except Exception:
                import hashlib
                values["secret_sha256"] = hashlib.sha256(secret.encode()).hexdigest()
        if "key_sha256" in cols:
            try:
                values["key_sha256"] = pr.session_digest(key)
            except Exception:
                try:
                    values["key_sha256"] = pr.code_digest(key)
                except Exception:
                    import hashlib
                    values["key_sha256"] = hashlib.sha256(key.encode()).hexdigest()
        if "last_seen_at" in cols:
            values["last_seen_at"] = at
        if "idle_expires_at" in cols:
            values["idle_expires_at"] = at + timedelta(hours=12)
        if "expires_at" in cols:
            values["expires_at"] = at + timedelta(days=7)
        if "user_agent" in cols:
            values["user_agent"] = "test-agent"
        if "id" in cols and cols["id"].default is None and cols["id"].server_default is None:
            if not cols["id"].nullable:
                try:
                    values["id"] = new_id()
                except Exception:
                    values["id"] = secrets.token_hex(16)
        for cname, col in cols.items():
            if cname in values:
                continue
            if col.nullable:
                continue
            if col.default is not None or col.server_default is not None:
                continue
            values[cname] = _dummy_value(col, at)
        conn.execute(m.principal_session.insert().values(**values))
        secret_hash = values.get("secret_sha256")
        row_id = values.get("id")
        if row_id is None:
            row = conn.execute(select(m.principal_session.c.id).where(
                m.principal_session.c.secret_sha256 == secret_hash)).first()
            row_id = row[0] if row else None
        return secret_hash, row_id


def _valid_code(owner, name="owner"):
    from erpclaw_core import administration as admin
    from erpclaw_core import principals as pr
    with owner.begin() as conn:
        code = admin.mint_sign_in_code(conn, name, "owner", run_id="old-run")
    return code, pr.code_digest(code)


def _session_by_id(owner, row_id):
    from erpclaw_core import models as m
    with owner.connect() as conn:
        return conn.execute(select(m.principal_session).where(
            m.principal_session.c.id == row_id)).mappings().one()


def _code_by_hash(owner, code_hash):
    from erpclaw_core import models as m
    with owner.connect() as conn:
        return conn.execute(select(m.principal_sign_in).where(
            m.principal_sign_in.c.code_sha256 == code_hash)).mappings().one()


def test_port_start_ends_old_and_keeps_new(started, monkeypatch, tmp_path):
    """A port start ends the earlier session and code but keeps a later session open.

    Catches removal of the cutoff call: without it the earlier rows stay open.
    """
    from erpclaw_core.common import now
    owner = started.owner
    old_hash, old_id = _open_session(owner, "owner", created_at=now() - timedelta(minutes=5))
    code, code_hash = _valid_code(owner, "owner")
    made = {}

    def fake_run(app, **options):
        _fd_port(options)
        new_hash, new_id = _open_session(owner, "owner", created_at=now() + timedelta(minutes=5))
        made["id"] = new_id
        made["hash"] = new_hash

    monkeypatch.setattr(uvicorn, "run", fake_run)
    monkeypatch.chdir(tmp_path)
    port = _free_port()
    result = CliRunner().invoke(cli.app, ["serve", "--port", str(port)])
    assert result.exit_code == 0, result.output
    old = _session_by_id(owner, old_id)
    assert old["ended_at"] is not None
    assert old["ended_why"] == "engine_start"
    voided = _code_by_hash(owner, code_hash)
    assert voided["used_at"] is not None
    assert made["id"] is not None
    fresh = _session_by_id(owner, made["id"])
    assert fresh["ended_at"] is None
    assert fresh["created_at"] > old["ended_at"]


def test_run_file_start_ends_old_and_keeps_new(started, monkeypatch, tmp_path):
    """A run-file start ends the earlier session and code but keeps a later session open.

    Catches removal of the cutoff call on the run-file path.
    """
    import json
    from erpclaw_core.common import now
    owner = started.owner
    path = tmp_path / "run.json"
    old_hash, old_id = _open_session(owner, "owner", created_at=now() - timedelta(minutes=5))
    code, code_hash = _valid_code(owner, "owner")
    made = {}

    def fake_run(app, **options):
        port = _fd_port(options)
        assert json.loads(path.read_bytes()) == {"port": port}
        new_hash, new_id = _open_session(owner, "owner", created_at=now() + timedelta(minutes=5))
        made["id"] = new_id

    monkeypatch.setattr(uvicorn, "run", fake_run)
    result = CliRunner().invoke(cli.app, ["serve", "--run-file", str(path), "--tcp"])
    assert result.exit_code == 0, result.output
    assert not path.exists()
    old = _session_by_id(owner, old_id)
    assert old["ended_at"] is not None
    assert old["ended_why"] == "engine_start"
    voided = _code_by_hash(owner, code_hash)
    assert voided["used_at"] is not None
    fresh = _session_by_id(owner, made["id"])
    assert fresh["ended_at"] is None


@pytest.mark.parametrize("case", ["bad-host", "neither", "tcp-alone", "both", "both-tcp",
                                  "file-without-tcp", "port-zero", "port-huge",
                                  "bad-path-symlink", "bad-path-missing"])
def test_refused_starts_leave_all_unchanged(engine, monkeypatch, tmp_path, case):
    """Every refused start leaves earlier sessions and codes exactly as they were.

    Catches calling the cutoff before the host, mode, port or path checks:
    such a placement would end rows even though the start is refused.
    """
    from erpclaw_core.common import now
    monkeypatch.chdir(tmp_path)
    folder = tmp_path / "real"
    folder.mkdir(exist_ok=True)
    old_hash, old_id = _open_session(engine, "owner", created_at=now() - timedelta(minutes=5))
    code, code_hash = _valid_code(engine, "owner")

    def forbidden(*args, **kwargs):
        raise AssertionError("A refused start opened a resource")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(uvicorn, "run", forbidden)
    path = tmp_path / "run.json"
    if case == "bad-host":
        args = ["serve", "--host", "example.com", "--port", "54321"]
    elif case == "neither":
        args = ["serve"]
    elif case == "tcp-alone":
        args = ["serve", "--tcp"]
    elif case == "both":
        args = ["serve", "--port", "54321", "--run-file", str(path)]
    elif case == "both-tcp":
        args = ["serve", "--port", "54321", "--run-file", str(path), "--tcp"]
    elif case == "file-without-tcp":
        args = ["serve", "--run-file", str(path)]
    elif case == "port-zero":
        args = ["serve", "--port", "0"]
    elif case == "port-huge":
        args = ["serve", "--port", "70000"]
    elif case == "bad-path-symlink":
        (tmp_path / "link").symlink_to("real", target_is_directory=True)
        args = ["serve", "--run-file", str(tmp_path / "link/run.json"), "--tcp"]
    else:
        args = ["serve", "--run-file", str(tmp_path / "missing/run.json"), "--tcp"]
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 2, result.output
    assert len(result.output.strip().splitlines()) == 1
    assert not path.exists()
    kept = _session_by_id(engine, old_id)
    assert kept["ended_at"] is None
    assert kept["ended_why"] is None
    kept_code = _code_by_hash(engine, code_hash)
    assert kept_code["used_at"] is None


def test_cutoff_runs_before_bind(started, monkeypatch, tmp_path):
    """The cutoff still ends rows when the bind itself fails.

    A bind failure is used instead of recording call order because it proves
    through observable behaviour that no socket work precedes the cutoff,
    without coupling to helper names. Catches moving the cutoff after the bind:
    then a failed bind would leave the earlier rows open.
    """
    import socket as real_socket
    from erpclaw_core.common import now
    owner = started.owner
    old_hash, old_id = _open_session(owner, "owner", created_at=now() - timedelta(minutes=5))
    code, code_hash = _valid_code(owner, "owner")
    real_type = real_socket.socket
    port = _free_port()  # probe before the socket patch, which would fail the probe's own bind

    class FailingSocket(real_type):
        def bind(self, address):
            raise OSError(errno.EADDRINUSE, "taken")

    monkeypatch.setattr(socket, "socket", FailingSocket)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: pytest.fail("A failed bind started the server"))
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli.app, ["serve", "--port", str(port)])
    assert result.exit_code == 1, result.output
    old = _session_by_id(owner, old_id)
    assert old["ended_at"] is not None
    assert old["ended_why"] == "engine_start"
    voided = _code_by_hash(owner, code_hash)
    assert voided["used_at"] is not None


def test_start_writes_no_audit_or_business_rows(started, monkeypatch, tmp_path):
    """The start changes only session end markers and code use markers.

    Catches an implementation that ends sessions through audited helpers or
    writes business rows: those add audit or business changes.
    """
    from host_client import books, business_changes, session_writes
    from erpclaw_core.common import now
    owner = started.owner
    _open_session(owner, "owner", created_at=now() - timedelta(minutes=5))
    _valid_code(owner, "owner")
    before = books(owner)
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: _fd_port(options))
    monkeypatch.chdir(tmp_path)
    port = _free_port()
    result = CliRunner().invoke(cli.app, ["serve", "--port", str(port)])
    assert result.exit_code == 0, result.output
    after = books(owner)
    assert business_changes(before, after) == {}
    assert before.get("audit_log") == after.get("audit_log")
    diff = session_writes(before, after)
    assert diff != {}
    for table, change in diff.items():
        assert change["added"] == [] and change["removed"] == []
    for old, new in diff.get("principal_session", {}).get("changed", []):
        assert old["ended_at"] is None and new["ended_at"] is not None
        assert new["ended_why"] == "engine_start"
        assert {k for k in old if old[k] != new[k]} == {"ended_at", "ended_why"}
    for old, new in diff.get("principal_sign_in", {}).get("changed", []):
        assert old["used_at"] is None and new["used_at"] is not None
        assert {k for k in old if old[k] != new[k]} == {"used_at"}


def test_cutoff_failure_stops_start_without_binding(started, monkeypatch, tmp_path):
    """A failing cutoff stops the start with exit 1 and binds nothing.

    Catches missing failure handling around the cutoff call.
    """
    import erpclaw_core.administration as admin
    from erpclaw_core.common import now
    owner = started.owner
    old_hash, old_id = _open_session(owner, "owner", created_at=now() - timedelta(minutes=5))
    code, code_hash = _valid_code(owner, "owner")

    def broken(engine):
        raise RuntimeError("books unavailable")

    monkeypatch.setattr(admin, "end_all_sessions_at_start", broken)
    bound = []
    real_socket = socket.socket
    port = _free_port()  # probe before the socket patch, so its own bind is not recorded

    class RecordingSocket(real_socket):
        def bind(self, address):
            bound.append(address)
            return super().bind(address)

    monkeypatch.setattr(socket, "socket", RecordingSocket)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: pytest.fail("A failed cutoff started the server"))
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli.app, ["serve", "--port", str(port)])
    assert result.exit_code == 1, result.output
    assert len(result.output.strip().splitlines()) == 1
    assert bound == []
    kept = _session_by_id(owner, old_id)
    assert kept["ended_at"] is None
    kept_code = _code_by_hash(owner, code_hash)
    assert kept_code["used_at"] is None
