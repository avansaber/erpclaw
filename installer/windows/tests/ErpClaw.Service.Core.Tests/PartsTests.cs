// The smaller parts: the log rule, secrets, the launcher guard, command lines, ports, the run-folder record,
// leftovers, and the commands' exact argv (SERVICE_COMMANDS r1b section 2).
using System.Net;
using System.Net.Sockets;
using System.Text.Json.Nodes;
using ErpClaw.Service.Core;
using Xunit;

namespace ErpClaw.Service.Core.Tests;

public class ServiceLogTests
{
    private static (ServiceLog log, MemorySink sink, SecretRegistry secrets) Log()
    {
        var sink = new MemorySink();
        var secrets = new SecretRegistry();
        return (new ServiceLog(sink, new FakeClock(), secrets), sink, secrets);
    }

    [Fact]
    public void A_plain_line_is_written_with_a_timestamp()
    {
        var (log, sink, _) = Log();
        Assert.True(log.Write("PostgreSQL accepted the owner's login; starting the engine."));
        var line = Assert.Single(sink.Lines);
        Assert.StartsWith("2026-11-02T09:00:00", line);
        Assert.EndsWith("PostgreSQL accepted the owner's login; starting the engine.", line);
    }

    [Fact]
    public void A_line_holding_a_registered_secret_is_withheld_and_the_notice_names_nothing_of_it()
    {
        var (log, sink, secrets) = Log();
        secrets.Add("hunter2-planted-password-value");
        Assert.False(log.Write("connecting with hunter2-planted-password-value now"));
        var line = Assert.Single(sink.Lines);
        Assert.Contains(ServiceLog.WithheldNotice, line);
        Assert.DoesNotContain("hunter2", line);
        Assert.DoesNotContain("connecting", line);
    }

    [Fact]
    public void A_registered_secret_object_is_withheld_too()
    {
        var (log, _, secrets) = Log();
        var secret = new Secret("another-planted-value-123");
        secrets.Add(secret);
        Assert.True(log.WouldRefuse("x another-planted-value-123 y"));
    }

    [Theory]
    [InlineData("database restarted on 50123")]
    [InlineData("50123")]
    [InlineData("(50123)")]
    public void A_registered_port_standing_alone_is_withheld(string message)
    {
        var (log, _, secrets) = Log();
        secrets.AddPort(50123);
        Assert.True(log.WouldRefuse(message));
    }

    [Fact]
    public void A_registered_port_inside_a_longer_number_is_not_a_port()
    {
        var (log, _, secrets) = Log();
        secrets.AddPort(50123);
        Assert.False(log.WouldRefuse("backup size 1501234 bytes"));
    }

    [Theory]
    [InlineData("listening on 127.0.0.1:5432")]
    [InlineData("http://localhost:8080/v1")]
    [InlineData("[::1]:6000")]
    [InlineData("port 5432")]
    [InlineData("Port=5432")]
    [InlineData("port: 61000")]
    public void Any_port_shape_is_withheld_even_unregistered(string message)
    {
        Assert.True(Log().log.WouldRefuse(message));
    }

    [Theory]
    [InlineData("code ABCDEFGHIJKLMNOPQRSTUVWXYZ")]
    [InlineData("abcdefghijklmnopqrstuvwxyz")]
    [InlineData("k=MZXW6YTBOI234567234567ABCD.")]
    public void A_sign_in_code_shape_is_withheld(string message)
    {
        Assert.True(Log().log.WouldRefuse(message));
    }

    [Fact]
    public void A_session_key_shape_is_withheld()
    {
        Assert.True(Log().log.WouldRefuse("key " + new string('A', 20) + "-_" + new string('b', 21)));
    }

    [Theory]
    [InlineData("The engine stopped unexpectedly; trying again in 4 seconds.")]
    [InlineData("Five failures in ten minutes; stopped trying.")]
    [InlineData("Backup finished in 2026.")]
    [InlineData("The database did not start.")]
    public void Ordinary_sentences_pass(string message)
    {
        Assert.False(Log().log.WouldRefuse(message));
    }

    [Fact]
    public void A_line_cannot_forge_another_line()
    {
        var (log, sink, _) = Log();
        log.Write("one\r\n2026-01-01T00:00:00Z forged");
        Assert.Single(sink.Lines);
        Assert.DoesNotContain('\n', sink.Lines[0]);
        Assert.DoesNotContain('\r', sink.Lines[0]);
    }
}

public class SecretTests
{
    [Fact]
    public void A_secret_never_prints_itself()
    {
        var secret = new Secret("planted-secret-value");
        Assert.DoesNotContain("planted", secret.ToString());
        Assert.DoesNotContain("planted", $"{secret}");
        Assert.Equal("planted-secret-value", secret.Reveal());
    }

    [Fact]
    public void An_empty_secret_is_refused()
    {
        Assert.Throws<ArgumentException>(() => new Secret(""));
    }

    [Fact]
    public void The_registry_finds_secrets_and_ignores_empty_ones()
    {
        var registry = new SecretRegistry();
        registry.Add("");
        Assert.False(registry.HoldsSecret("anything"));
        registry.Add("s3cr3t-value");
        Assert.True(registry.HoldsSecret("x s3cr3t-value y"));
        Assert.False(registry.HoldsSecret("s3cr3t"));
    }

    [Fact]
    public void The_guard_refuses_a_command_line_holding_a_secret_and_says_nothing_of_it()
    {
        var journal = new Journal();
        var inner = new FakeLauncher(journal, new FakeClock());
        var secrets = new SecretRegistry();
        secrets.Add("planted-password-xyz");
        var guard = new GuardedLauncher(inner, secrets);

        var e = Assert.Throws<InvalidOperationException>(() =>
            guard.Start(new ProcessSpec(@"C:\x\psql.exe", new[] { "-c", "ALTER ROLE o PASSWORD 'planted-password-xyz'" })));
        Assert.DoesNotContain("planted", e.Message);
        Assert.Empty(inner.Started);

        Assert.Throws<InvalidOperationException>(() =>
            guard.Start(new ProcessSpec(@"C:\planted-password-xyz\a.exe", Array.Empty<string>())));

        guard.Start(new ProcessSpec(@"C:\x\a.exe", new[] { "ok" }));
        Assert.Single(inner.Started);
    }

    [Fact]
    public void A_spec_prints_its_command_but_never_its_stdin()
    {
        var spec = new ProcessSpec(@"C:\pg\postgres.exe", new[] { "--single" }) { StandardInput = "CREATE ROLE x PASSWORD 'SCRAM-SHA-256$planted'"u8.ToArray() };
        var text = spec.ToString();
        Assert.Contains("postgres.exe", text);
        Assert.DoesNotContain("planted", text);
    }
}

public class WindowsCommandLineTests
{
    [Theory]
    [InlineData("plain", "plain")]
    [InlineData("", "\"\"")]
    [InlineData("has space", "\"has space\"")]
    [InlineData(@"C:\Program Files\x\", "\"C:\\Program Files\\x\\\\\"")]
    [InlineData("say \"hi\"", "\"say \\\"hi\\\"\"")]
    [InlineData(@"a\\b", @"a\\b")]
    [InlineData("a\\\"b", "\"a\\\\\\\"b\"")]
    [InlineData("tab\there", "\"tab\there\"")]
    public void Arguments_are_quoted_as_CommandLineToArgvW_reads_them(string argument, string expected)
    {
        Assert.Equal(expected, WindowsCommandLine.Quote(argument));
    }

    [Fact]
    public void A_command_line_is_the_quoted_executable_and_its_arguments()
    {
        Assert.Equal("\"C:\\P F\\python.exe\" -I -m erpclaw_cli.main \"a b\"",
            WindowsCommandLine.Build(@"C:\P F\python.exe", new[] { "-I", "-m", "erpclaw_cli.main", "a b" }));
    }
}

public class PortTests
{
    [Fact]
    public void A_drawn_port_is_on_loopback_and_bindable()
    {
        var probe = new LoopbackPortProbe();
        int port = probe.DrawFree();
        Assert.InRange(port, 1024, 65535);
        Assert.True(probe.IsFree(port));
    }

    [Fact]
    public void A_port_held_by_a_listener_is_not_free()
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        try
        {
            int port = ((IPEndPoint)listener.LocalEndpoint).Port;
            Assert.False(new LoopbackPortProbe().IsFree(port));
        }
        finally
        {
            listener.Stop();
        }
    }

    [Fact]
    public void The_old_port_is_kept_when_free_else_a_new_one_is_drawn()
    {
        var ports = new FakePorts();
        Assert.Equal(50001, PortChoice.Choose(null, ports));
        Assert.Equal(50001, PortChoice.Choose(50001, ports));
        ports.Taken.Add(50001);
        Assert.Equal(50002, PortChoice.Choose(50001, ports));
    }
}

public class RunRecordTests
{
    private static ServiceRecord Sample() => new()
    {
        Postgres = new ChildRecord(4321, new DateTimeOffset(2026, 11, 2, 9, 0, 0, TimeSpan.Zero).AddTicks(1234567)),
        Engine = new ChildRecord(4322, new DateTimeOffset(2026, 11, 2, 9, 0, 1, TimeSpan.Zero)),
        DatabasePort = 50123,
        State = ServiceState.GaveUp,
        Reason = "ERPClaw did not start.",
        UpdatedAt = new DateTimeOffset(2026, 11, 2, 9, 0, 2, TimeSpan.Zero),
    };

    [Fact]
    public void A_record_round_trips_with_exact_start_times()
    {
        var record = Sample();
        Assert.Equal(record, ServiceRecordJson.Parse(ServiceRecordJson.Serialize(record)));
    }

    [Fact]
    public void The_json_is_small_snake_case_and_holds_only_known_fields()
    {
        var node = JsonNode.Parse(ServiceRecordJson.Serialize(Sample()))!.AsObject();
        Assert.Equal(new[] { "database_port", "engine", "postgres", "reason", "state", "updated_at", "version" },
            node.Select(p => p.Key).OrderBy(k => k, StringComparer.Ordinal));
        Assert.Equal("gave_up", (string?)node["state"]);
        Assert.Equal(4321, (int)node["postgres"]!["pid"]!);
    }

    [Theory]
    [InlineData("")]
    [InlineData("{")]
    [InlineData("[]")]
    [InlineData("""{"version":2}""")]
    public void An_unreadable_record_is_no_record(string json)
    {
        Assert.Null(ServiceRecordJson.Parse(json));
    }

    [Fact]
    public void The_state_record_has_its_own_file_apart_from_the_service_file()
    {
        Assert.Equal("service-state.json", ServiceRecordJson.FileName);
        Assert.NotEqual(ServiceFileJson.FileName, ServiceRecordJson.FileName);
    }

    [Fact]
    public void The_record_never_takes_the_engines_run_folder_names()
    {
        Assert.DoesNotContain(ServiceRecordJson.FileName, ServiceRecordJson.ReservedNames);
        Assert.Contains("books.json", ServiceRecordJson.ReservedNames);
        Assert.Contains("books.json.prev", ServiceRecordJson.ReservedNames);
        Assert.Contains("installed.json", ServiceRecordJson.ReservedNames);
    }

    [Fact]
    public void The_file_store_writes_by_replacing_and_leaves_no_temporary_file()
    {
        var run = Directory.CreateTempSubdirectory("erpclaw-run-").FullName;
        try
        {
            var store = new FileRunRecordStore(run);
            Assert.Null(store.Read());
            store.Write(Sample());
            store.Write(Sample() with { State = ServiceState.Running, Reason = null });
            Assert.Equal(ServiceState.Running, store.Read()!.State);
            Assert.Equal(new[] { ServiceRecordJson.FileName }, Directory.GetFiles(run).Select(Path.GetFileName));
            Assert.Equal(Path.Combine(run, ServiceRecordJson.FileName), store.FilePath);
        }
        finally
        {
            Directory.Delete(run, true);
        }
    }
}

public class LeftoverTests
{
    private static readonly DateTimeOffset T = new(2026, 11, 1, 8, 0, 0, TimeSpan.Zero);

    [Fact]
    public void Live_children_with_matching_start_times_are_stopped_engine_first()
    {
        var inspector = new FakeInspector();
        inspector.Alive[10] = T;
        inspector.Alive[11] = T.AddSeconds(1);
        var stopped = new LeftoverSweeper(inspector).Sweep(new ServiceRecord
        {
            Postgres = new ChildRecord(10, T),
            Engine = new ChildRecord(11, T.AddSeconds(1)),
        });
        Assert.Equal(new[] { 11, 10 }, stopped);
        Assert.Equal(new[] { 11, 10 }, inspector.Killed);
    }

    [Fact]
    public void A_reused_pid_or_a_dead_one_is_left_alone()
    {
        var inspector = new FakeInspector();
        inspector.Alive[10] = T.AddMilliseconds(1);
        var stopped = new LeftoverSweeper(inspector).Sweep(new ServiceRecord
        {
            Postgres = new ChildRecord(10, T),
            Engine = new ChildRecord(99, T),
        });
        Assert.Empty(stopped);
        Assert.Empty(inspector.Killed);
    }

    [Fact]
    public void No_record_nothing_to_do()
    {
        Assert.Empty(new LeftoverSweeper(new FakeInspector()).Sweep(null));
    }
}

public class ServiceCommandsTests
{
    private const string Data = @"C:\Users\owner\ERPClaw";
    private static readonly ServicePaths Paths = new(@"C:\Users\owner\AppData\Local\Programs\ERPClaw\3.0.0", Data);
    private static readonly ServiceCommands Commands = ServiceCommands.Proposed(Paths);
    private static readonly string[] Prefix = { "-I", "-m", "erpclaw_cli.main" };

    private static void Engine(ProcessSpec spec, params string[] rest)
    {
        Assert.Equal(Paths.Python, spec.Executable);
        Assert.Equal(Prefix.Concat(rest), spec.Arguments);
        Assert.True(spec.StandardInput.IsEmpty);
    }

    [Fact]
    public void The_engine_entry_is_the_bundled_python_isolated()
    {
        Assert.Equal(new[] { Paths.Python }.Concat(Prefix), Commands.EnginePrefix);
    }

    [Theory]
    [InlineData(BackupKind.Daily, "daily")]
    [InlineData(BackupKind.Manual, "manual")]
    [InlineData(BackupKind.BeforeUpdate, "before-update")]
    [InlineData(BackupKind.BeforeRestore, "before-restore")]
    public void Backup_is_the_contract_argv(BackupKind kind, string name)
    {
        Engine(Commands.Backup(kind), "service", "backup", "--data-folder", Data, "--kind", name);
        Engine(Commands.Backup(kind, @"E:\copies"), "service", "backup", "--data-folder", Data, "--kind", name, "--copy-to", @"E:\copies");
    }

    [Fact]
    public void The_other_contract_commands_are_exact()
    {
        Engine(Commands.ListBackups(), "service", "list-backups", "--data-folder", Data);
        Engine(Commands.RestoreStage("erpclaw-2026-11-01.zip"), "service", "restore-stage", "--data-folder", Data, "--backup", "erpclaw-2026-11-01.zip");
        Engine(Commands.Switch("erpclaw_r2"), "service", "switch", "--data-folder", Data, "--to", "erpclaw_r2");
        Engine(Commands.RemoveReplaced("erpclaw_old"), "service", "remove-replaced", "--data-folder", Data, "--name", "erpclaw_old");
        Engine(Commands.UpdateStep(), "service", "update-step", "--data-folder", Data);
        Engine(Commands.Pointer(), "service", "pointer", "--data-folder", Data);
    }

    [Fact]
    public void The_proposed_engine_commands()
    {
        Engine(Commands.Serve(), "serve", "--release", "--data-folder", Data);
        Engine(Commands.Bootstrap(), "init", "--own-books", "--data-folder", Data);
        Engine(Commands.DbCheck(), "service", "db-check", "--data-folder", Data);
        Engine(Commands.MintCode("owner", "0123456789abcdef0123456789abcdef"),
            "service", "mint-code", "--data-folder", Data, "--principal", "owner", "--run-id", "0123456789abcdef0123456789abcdef");
    }

    [Fact]
    public void Commands_capture_stdout_and_serve_does_not()
    {
        Assert.True(Commands.Pointer().CaptureOutput);
        Assert.True(Commands.Backup(BackupKind.Daily).CaptureOutput);
        Assert.False(Commands.Serve().CaptureOutput);
        Assert.False(Commands.Postgres().CaptureOutput);
    }

    [Theory]
    [InlineData("")]
    [InlineData("-x")]
    [InlineData("--data-folder")]
    [InlineData("a\nb")]
    [InlineData("a\0b")]
    public void A_value_that_could_be_read_as_an_option_or_breaks_a_line_is_refused(string value)
    {
        Assert.Throws<ArgumentException>(() => Commands.RestoreStage(value));
        Assert.Throws<ArgumentException>(() => Commands.Switch(value));
        Assert.Throws<ArgumentException>(() => Commands.RemoveReplaced(value));
        Assert.Throws<ArgumentException>(() => Commands.MintCode(value, "ab"));
        Assert.Throws<ArgumentException>(() => Commands.MintCode("owner", value));
    }

    [Fact]
    public void Postgres_runs_directly_never_through_pg_ctl_start()
    {
        var pg = Commands.Postgres();
        Assert.Equal(Path.Combine(Paths.PostgresBin, "postgres.exe"), pg.Executable);
        Assert.Equal(new[] { "-D", Paths.Books }, pg.Arguments);

        var stop = Commands.PgCtlStop();
        Assert.Equal(Path.Combine(Paths.PostgresBin, "pg_ctl.exe"), stop.Executable);
        Assert.Equal(new[] { "stop", "-D", Paths.Books, "-m", "fast" }, stop.Arguments);
    }

    [Fact]
    public void Single_user_mode_takes_its_statements_on_stdin()
    {
        var spec = Commands.SingleUser("SELECT 1;\n"u8.ToArray());
        Assert.Equal(Path.Combine(Paths.PostgresBin, "postgres.exe"), spec.Executable);
        Assert.Equal(new[] { "--single", "-D", Paths.Books, "-F", "-c", "exit_on_error=on", "postgres" }, spec.Arguments);
        Assert.Equal("SELECT 1;\n", Rig.Utf8(spec.StandardInput));
    }

    [Fact]
    public void Every_child_runs_in_the_data_folder()
    {
        foreach (var spec in new[] { Commands.Postgres(), Commands.Serve(), Commands.Pointer(), Commands.PgCtlStop(), Commands.InitDb(@"\\.\pipe\x") })
            Assert.Equal(Data, spec.WorkingDirectory);
    }
}
