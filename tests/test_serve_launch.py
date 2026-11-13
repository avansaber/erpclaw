"""Launch modes and publication of a private, atomic run file."""
import errno
import importlib
import json
import logging
import os
from pathlib import Path
import random
import secrets
import socket
import stat
import threading
from types import SimpleNamespace

import pytest
import uvicorn
from sqlalchemy import create_engine
from typer.testing import CliRunner

from erpclaw_assistant.model import ScriptedModel
from erpclaw_cli import main as cli

MODE_ERROR = "Choose --port or --run-file with --tcp to launch the engine."
NO_PIPE = "This build has no pipe listener yet; add --tcp for a loopback listener."


def runfile_module():
    return importlib.import_module("erpclaw_api.runfile")


@pytest.fixture
def unopened(monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("A refused launch opened a resource")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr("erpclaw_core.db.make_engine", forbidden)
    monkeypatch.setattr("erpclaw_assistant.muse.model_from_env", forbidden)
    monkeypatch.setattr(uvicorn, "run", forbidden)
    return calls


@pytest.mark.parametrize("mode", ["none", "tcp", "port-and-file", "port-file-tcp", "file"])
def test_refused_launch_modes(mode, unopened, tmp_path):
    path = tmp_path / "run.json"
    options = {
        "none": [], "tcp": ["--tcp"],
        "port-and-file": ["--port", "54321", "--run-file", str(path)],
        "port-file-tcp": ["--port", "54321", "--run-file", str(path), "--tcp"],
        "file": ["--run-file", str(path)],
    }
    result = CliRunner().invoke(cli.app, ["serve", *options[mode]])
    assert result.exit_code == 2, result.output
    if mode == "file":
        assert result.output == NO_PIPE + "\n"
    else:
        assert result.output == MODE_ERROR + "\n"
    assert not unopened and not path.exists()


@pytest.mark.parametrize("problem", ["first", "middle", "last", "missing"])
def test_bad_run_file_path_is_refused_before_build_or_bind(problem, unopened, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    folder = tmp_path / "real"
    folder.mkdir()
    if problem == "first":
        Path("link").symlink_to("real", target_is_directory=True)
        path = Path("link/run.json")
    elif problem == "middle":
        (folder / "link").symlink_to(".", target_is_directory=True)
        path = Path("real/link/run.json")
    elif problem == "last":
        (folder / "run.json").symlink_to("target.json")
        path = Path("real/run.json")
    else:
        path = Path("missing/run.json")
    result = CliRunner().invoke(cli.app, ["serve", "--run-file", str(path), "--tcp"])
    assert result.exit_code == 2, result.output
    expected = "folder" if problem == "missing" else "symbolic link"
    assert expected in result.output.lower()
    assert len(result.output.splitlines()) == 1
    assert not unopened and not (folder / "target.json").exists()
    assert not path.exists()


@pytest.fixture
def launch(engine, runtime_login, monkeypatch):
    runtime = create_engine(runtime_login)
    monkeypatch.setenv("ERPCLAW_DATABASE_URL", runtime_login)
    monkeypatch.setattr("erpclaw_core.db.make_engine", lambda url: runtime)
    monkeypatch.setattr("erpclaw_assistant.muse.model_from_env", lambda: ScriptedModel([]))
    yield SimpleNamespace(runtime=runtime)
    runtime.dispose()


def fd_port(options):
    with socket.socket(fileno=os.dup(options["fd"])) as bound:
        host, port = bound.getsockname()
        assert host == "127.0.0.1" and port != 0
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as other:
            with pytest.raises(OSError) as collision:
                other.bind((host, port))
            assert collision.value.errno == errno.EADDRINUSE
        return port


@pytest.mark.parametrize("tcp", [False, True])
def test_explicit_port_prints_exactly_one_stderr_address(launch, monkeypatch, tmp_path, tcp):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    starts = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: starts.append(fd_port(options)))
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli.app, ["serve", "--port", str(port), *(["--tcp"] if tcp else [])])
    assert result.exit_code == 0, result.output
    assert starts == [port]
    assert result.stdout == "" and result.stderr == f"http://127.0.0.1:{port}\n"
    assert list(tmp_path.iterdir()) == []


def test_run_file_is_private_matches_bound_fd_and_is_removed_after_run(launch, monkeypatch, tmp_path):
    path = tmp_path / "run.json"
    seen = []

    def run(app, **options):
        port = fd_port(options)
        assert json.loads(path.read_bytes()) == {"port": port}
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert options == {"fd": options["fd"], "log_level": "warning", "ws": "none"}
        seen.append(port)

    monkeypatch.setattr(uvicorn, "run", run)
    result = CliRunner().invoke(cli.app, ["serve", "--run-file", str(path), "--tcp"])
    assert result.exit_code == 0, result.output
    assert len(seen) == 1 and not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_launch_orders_path_build_bind_write_and_run(launch, monkeypatch, tmp_path):
    runfile = runfile_module()
    events = []
    path = tmp_path / "run.json"
    check, write, socket_type = runfile.check_run_file_path, runfile.write_run_file, socket.socket

    def checked(path):
        events.append("check")
        return check(path)

    def build(url):
        events.append("build")
        return launch.runtime

    def model():
        events.append("model")
        return ScriptedModel([])

    class RecordingSocket(socket_type):
        def bind(self, address):
            result = super().bind(address)
            events.append("bind")
            return result

    def written(path, fields):
        events.append("write")
        assert set(fields) == {"port"}
        return write(path, fields)

    def run(app, **options):
        events.append("run")
        assert json.loads(path.read_bytes()) == {"port": fd_port(options)}

    monkeypatch.setattr(runfile, "check_run_file_path", checked)
    monkeypatch.setattr(runfile, "write_run_file", written)
    monkeypatch.setattr("erpclaw_core.db.make_engine", build)
    monkeypatch.setattr("erpclaw_assistant.muse.model_from_env", model)
    monkeypatch.setattr(socket, "socket", RecordingSocket)
    monkeypatch.setattr(uvicorn, "run", run)
    cli.serve(run_file=path, tcp=True)
    assert events == ["check", "build", "model", "bind", "write", "run"]


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_preexisting_temporary_name_stops_start_without_writing_through(launch, monkeypatch, tmp_path, kind):
    runfile = runfile_module()
    path = tmp_path / "run.json"
    target = tmp_path / "target"
    target.write_bytes(b"keep target")
    temporary = path.with_name(".run.json.fixed.tmp")
    if kind == "file":
        temporary.write_bytes(b"keep temporary")
    else:
        temporary.symlink_to(target.name)
    monkeypatch.setattr(runfile.secrets, "token_hex", lambda size: "fixed")
    bound, ports = [], []
    real_socket = socket.socket

    class RecordingSocket(real_socket):
        def bind(self, address):
            super().bind(address)
            bound.append(self)
            ports.append(self.getsockname()[1])

    monkeypatch.setattr(socket, "socket", RecordingSocket)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: pytest.fail("A failed write started the server"))
    result = CliRunner().invoke(cli.app, ["serve", "--run-file", str(path), "--tcp"])
    assert result.exit_code == 1, result.output
    assert len(bound) == 1 and bound[0].fileno() == -1
    assert not path.exists() and target.read_bytes() == b"keep target"
    assert all(str(port) not in result.output for port in ports)
    assert temporary.read_bytes() == (b"keep temporary" if kind == "file" else b"keep target")


def test_writer_creates_exclusively_with_private_mode_and_syncs_before_replace(monkeypatch, tmp_path):
    runfile = runfile_module()
    real_open, real_fsync, real_replace = os.open, os.fsync, os.replace
    events = []
    path = tmp_path / "run.json"

    def opened(name, flags, mode=0o777, **kwargs):
        if flags & os.O_CREAT:
            assert flags & os.O_EXCL and flags & os.O_NOFOLLOW
            assert mode == 0o600 and Path(name).parent == Path(".")
            assert os.fstat(kwargs["dir_fd"]).st_ino == path.parent.stat().st_ino
            events.append("create")
        return real_open(name, flags, mode, **kwargs)

    def synced(fd):
        events.append("fsync")
        return real_fsync(fd)

    def replaced(source, destination, **kwargs):
        events.append("replace")
        assert (path.parent / source).read_bytes() == b'{"port":54321}\n'
        assert destination == path.name
        assert kwargs["src_dir_fd"] == kwargs["dst_dir_fd"]
        assert os.fstat(kwargs["src_dir_fd"]).st_ino == path.parent.stat().st_ino
        return real_replace(source, destination, **kwargs)

    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr(os, "fsync", synced)
    monkeypatch.setattr(os, "replace", replaced)
    written = runfile.write_run_file(path, {"port": 54321})
    assert written == path.read_bytes() == b'{"port":54321}\n'
    assert events == ["create", "fsync", "replace"]


def test_reader_sees_only_complete_old_or_new_file_during_write(monkeypatch, tmp_path):
    runfile = runfile_module()
    path = tmp_path / "run.json"
    old = b'{"port":49152}\n'
    path.write_bytes(old)
    observed = []
    ready, partial, saw_partial, stop = (threading.Event() for _ in range(4))
    real_write = os.write

    def read():
        while not stop.is_set():
            observed.append(path.read_bytes())
            ready.set()
            if partial.is_set():
                saw_partial.set()
                if observed[-1] != old:
                    stop.set()

    def short_write(fd, data):
        if not partial.is_set():
            count = real_write(fd, data[:1])
            partial.set()
            assert saw_partial.wait(5)
            return count
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", short_write)
    reader = threading.Thread(target=read)
    reader.start()
    try:
        assert ready.wait(5)
        new = runfile.write_run_file(path, {"port": 65471})
        assert stop.wait(5)
    finally:
        stop.set()
        reader.join(5)
    assert not reader.is_alive()
    assert saw_partial.is_set() and set(observed) == {old, new}


def test_second_engines_replacement_survives_first_engines_exit(launch, monkeypatch, tmp_path):
    path = tmp_path / "run.json"
    runfile = runfile_module()
    replacements = []

    def run(app, **options):
        port = fd_port(options)
        assert json.loads(path.read_bytes()) == {"port": port}
        replacements.append(runfile.write_run_file(path, {"port": port - 1}))

    monkeypatch.setattr(uvicorn, "run", run)
    cli.serve(run_file=path, tcp=True)
    assert path.read_bytes() == replacements[0]


def test_run_file_port_never_appears_in_real_server_logs_or_output(launch, monkeypatch, tmp_path, caplog, capsys):
    path = tmp_path / "run.json"
    ports = []
    real_run = uvicorn.run

    async def stop_after_start(server):
        server.should_exit = True

    def run(app, **options):
        port = fd_port(options)
        assert json.loads(path.read_bytes()) == {"port": port}
        ports.append(port)
        real_run(app, **options)

    monkeypatch.setattr(uvicorn.Server, "main_loop", stop_after_start)
    monkeypatch.setattr(uvicorn, "run", run)
    caplog.set_level(logging.DEBUG)
    for seed in [31, 57]:
        monkeypatch.setattr(secrets, "SystemRandom", lambda: random.Random(seed))
        cli.serve(run_file=path, tcp=True)
        assert not path.exists()
    assert ports == [random.Random(seed).randint(49152, 65471) for seed in [31, 57]]
    assert ports[0] != ports[1]
    output = capsys.readouterr()
    for port in ports:
        assert str(port) not in output.out + output.err
        assert all(str(port) not in record.getMessage() for record in caplog.records)


def test_cleanup_preserves_a_replacement_with_equal_fields_but_different_bytes(tmp_path):
    runfile = runfile_module()
    path = tmp_path / "run.json"
    written = runfile.write_run_file(path, {"port": 54321})
    replacement = b'{ "port": 54321 }\n'
    path.write_bytes(replacement)
    runfile.remove_if_unchanged(path, written)
    assert path.read_bytes() == replacement


@pytest.mark.parametrize("component", ["parent", "middle"])
def test_directory_swap_after_validation_publishes_in_pinned_folder(launch, monkeypatch, tmp_path, component):
    runfile = runfile_module()
    folder = tmp_path / "checked"
    folder.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    if component == "middle":
        (folder / "nested").mkdir()
        (other / "nested").mkdir()
    suffix = Path("nested/run.json") if component == "middle" else Path("run.json")
    path = folder / suffix
    protected = other / suffix
    protected.write_bytes(b"another engine")
    moved = tmp_path / "original"
    published = []

    def swap():
        folder.rename(moved)
        folder.symlink_to(other, target_is_directory=True)

    def run(app, **options):
        assert protected.read_bytes() == b"another engine"
        published.append((moved / suffix).read_bytes())
        assert json.loads(published[-1]) == {"port": fd_port(options)}

    monkeypatch.setattr(runfile, "_after_check", swap)
    monkeypatch.setattr(uvicorn, "run", run)
    result = CliRunner().invoke(cli.app, ["serve", "--run-file", str(path), "--tcp"])
    assert result.exit_code == 0, (result.output, result.exception)
    assert len(published) == 1
    assert protected.read_bytes() == b"another engine"
    # Cleanup refuses the substituted link, leaving the pinned publication intact.
    assert (moved / suffix).read_bytes() == published[0]
    assert list((moved / suffix).parent.iterdir()) == [moved / suffix]


def test_cleanup_locks_out_a_writer_until_after_unlink(monkeypatch, tmp_path):
    import fcntl

    runfile = runfile_module()
    path = tmp_path / "run.json"
    first = runfile.write_run_file(path, {"port": 54321})
    progress, blocked, finished = (threading.Event() for _ in range(3))
    replacements, failures = [], []
    real_flock = fcntl.flock

    def publish():
        try:
            replacements.append(runfile.write_run_file(path, {"port": 54322}))
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()
            progress.set()

    writer = threading.Thread(target=publish, daemon=True)

    def flock(fd, operation):
        if threading.current_thread() is writer and operation == fcntl.LOCK_EX:
            try:
                real_flock(fd, operation | fcntl.LOCK_NB)
            except BlockingIOError:
                blocked.set()
                progress.set()
        return real_flock(fd, operation)

    def interleave():
        writer.start()
        assert progress.wait(5), "The second writer made no progress"
        if blocked.is_set():
            assert not finished.is_set()

    monkeypatch.setattr(fcntl, "flock", flock)
    monkeypatch.setattr(runfile, "_before_unlink", interleave)
    try:
        runfile.remove_if_unchanged(path, first)
    finally:
        if writer.ident is not None:
            writer.join(5)
    assert not writer.is_alive()
    assert not failures
    assert blocked.is_set(), "The second writer published during cleanup"
    assert finished.is_set() and len(replacements) == 1
    assert path.read_bytes() == replacements[0] == b'{"port":54322}\n'


def test_sequential_writers_preserve_second_publication_on_first_cleanup(tmp_path):
    runfile = runfile_module()
    path = tmp_path / "run.json"
    first = runfile.write_run_file(path, {"port": 54321})
    second = runfile.write_run_file(path, {"port": 54322})
    assert path.read_bytes() == second != first
    runfile.remove_if_unchanged(path, first)
    assert path.read_bytes() == second
    runfile.remove_if_unchanged(path, second)
    assert not path.exists()


@pytest.mark.parametrize("component", ["parent", "middle"])
def test_path_check_refuses_directory_swapped_at_open(monkeypatch, tmp_path, component):
    runfile = runfile_module()
    folder = tmp_path / "checked"
    folder.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    if component == "middle":
        (folder / "nested").mkdir()
        (other / "nested").mkdir()
    path = folder / ("nested/run.json" if component == "middle" else "run.json")
    real_open = os.open
    swapped = []

    def opened(name, flags, mode=0o777, **kwargs):
        if name == folder.name and not swapped:
            folder.rename(tmp_path / "original")
            folder.symlink_to(other, target_is_directory=True)
            swapped.append(True)
        return real_open(name, flags, mode, **kwargs)

    monkeypatch.setattr(os, "open", opened)
    with pytest.raises(ValueError, match="symbolic link"):
        runfile.check_run_file_path(path)
    assert swapped == [True]


@pytest.mark.parametrize("name", ["", ".", "..", "folder/", "folder/.", "folder/..", "folder"])
def test_path_check_refuses_non_file_names(monkeypatch, tmp_path, name):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "folder").mkdir()
    with pytest.raises(ValueError):
        runfile_module().check_run_file_path(name)


@pytest.mark.parametrize("component", ["parent", "middle"])
def test_cleanup_uses_pinned_folder_after_compare(monkeypatch, tmp_path, component):
    runfile = runfile_module()
    folder, other, moved = (tmp_path / name for name in ("checked", "other", "original"))
    folder.mkdir()
    other.mkdir()
    if component == "middle":
        (folder / "nested").mkdir()
        (other / "nested").mkdir()
    suffix = Path("nested/run.json") if component == "middle" else Path("run.json")
    path = folder / suffix
    written = runfile.write_run_file(path, {"port": 54321})
    protected = other / suffix
    protected.write_bytes(b"another engine")

    def swap():
        folder.rename(moved)
        folder.symlink_to(other, target_is_directory=True)

    monkeypatch.setattr(runfile, "_before_unlink", swap)
    runfile.remove_if_unchanged(path, written)
    assert protected.read_bytes() == b"another engine"
    assert not (moved / suffix).exists()
