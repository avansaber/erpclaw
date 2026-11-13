// The private PostgreSQL's configuration and one-time initialisation (INSTALLER 2.2).
using System.Text;
using ErpClaw.Service.Core;
using Xunit;

namespace ErpClaw.Service.Core.Tests;

public class PostgresConfigTests
{
    private static readonly DatabaseRoles Roles = new();

    private static string[] Lines(string text) =>
        text.Split('\n').Select(l => l.Trim()).Where(l => l.Length > 0 && !l.StartsWith('#')).ToArray();

    [Fact]
    public void The_managed_settings_listen_on_loopback_with_scram_and_log_nothing_secret()
    {
        var conf = PostgresConfig.ManagedSettings(50123, @"D:\Data\o'neil\ERPClaw\logs\postgresql");
        var lines = Lines(conf);

        Assert.Contains("listen_addresses = '127.0.0.1'", lines);
        Assert.Contains("port = 50123", lines);
        Assert.Contains("unix_socket_directories = ''", lines);
        Assert.Contains("password_encryption = 'scram-sha-256'", lines);
        Assert.Contains("logging_collector = on", lines);
        Assert.Contains("log_directory = 'D:/Data/o''neil/ERPClaw/logs/postgresql'", lines);
        Assert.Contains("log_statement = 'none'", lines);
        Assert.Contains("log_min_error_statement = panic", lines); // a failed statement's text (a password) is never logged
        Assert.Contains("log_min_messages = warning", lines);      // keeps "listening on ... port N" out of the log
        Assert.Contains("log_connections = off", lines);
        Assert.Contains("log_hostname = off", lines);
        Assert.DoesNotContain(lines, l => l.StartsWith("log_line_prefix") && (l.Contains("%r") || l.Contains("%h")));
        Assert.DoesNotContain('\\', conf);
    }

    [Fact]
    public void Pg_hba_allows_only_host_lines_from_127_0_0_1_with_scram_for_the_two_roles()
    {
        var lines = Lines(PostgresConfig.Hba(Roles)).Select(l => string.Join(' ', l.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries))).ToArray();
        Assert.Equal(new[]
        {
            "host all erpclaw_owner 127.0.0.1/32 scram-sha-256",
            "host all erpclaw_runtime 127.0.0.1/32 scram-sha-256",
        }, lines);
    }

    [Fact]
    public void Initdb_gets_utf8_the_builtin_c_utf8_locale_scram_and_the_owner_as_superuser_with_no_password()
    {
        var args = PostgresConfig.InitDbArguments(@"C:\Users\owner\ERPClaw\books", Roles, @"\\.\pipe\erpclaw-initdb-0123");
        Assert.Equal(new[]
        {
            "-D", @"C:\Users\owner\ERPClaw\books",
            "-U", "erpclaw_owner",
            "-E", "UTF8",
            "--locale-provider=builtin",
            "--builtin-locale=C.UTF-8",
            "--locale=C",
            "-A", "scram-sha-256",
            @"--pwfile=\\.\pipe\erpclaw-initdb-0123",
            "--no-instructions",
        }, args);
    }

    [Fact]
    public void Single_user_setup_creates_or_updates_the_runtime_login_by_verifier()
    {
        var verifier = ScramVerifier.Compute("runtime-password", new byte[16]);
        var lines = PostgresConfig.SingleUserSetup(Roles, verifier).Split('\n', StringSplitOptions.RemoveEmptyEntries);
        Assert.Equal(new[]
        {
            "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'erpclaw_runtime') THEN CREATE ROLE \"erpclaw_runtime\"; END IF; END $$;",
            $"ALTER ROLE \"erpclaw_runtime\" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD '{verifier}';",
        }, lines);
    }

    [Theory]
    [InlineData("runtime-password")]
    [InlineData("md5abc")]
    [InlineData("SCRAM-SHA-256$4096:x$y:z' ; DROP ROLE x; --")]
    public void Single_user_setup_takes_only_a_verifier(string notAVerifier)
    {
        Assert.Throws<ArgumentException>(() => PostgresConfig.SingleUserSetup(Roles, notAVerifier));
    }

    [Theory]
    [InlineData("erpclaw owner")]
    [InlineData("Erpclaw")]
    [InlineData("x\"y")]
    [InlineData("")]
    public void Role_and_database_names_are_plain_identifiers(string bad)
    {
        Assert.Throws<ArgumentException>(() => PostgresConfig.Hba(new DatabaseRoles(Owner: bad)));
        Assert.Throws<ArgumentException>(() => PostgresConfig.SingleUserSetup(new DatabaseRoles(Database: bad), ScramVerifier.Create("p")));
    }

    [Fact]
    public void Conf_literals_use_forward_slashes_and_double_single_quotes()
    {
        Assert.Equal("'C:/a/it''s'", PostgresConfig.Literal(@"C:\a\it's"));
    }
}

public class ScramVerifierTests
{
    [Fact]
    public void The_verifier_matches_an_independent_computation()
    {
        // Computed with Python's hashlib (pbkdf2_hmac sha256, 4096) and hmac, PostgreSQL's format.
        Assert.Equal(
            "SCRAM-SHA-256$4096:AAECAwQFBgcICQoLDA0ODw==$ONYbSJBXtKl6bP6PVqw8pm9e7EiacprLnoUQPFS80Hw=:IPOtHuGJ2HifEQg74W2XXqqCrCyQG55GbPRHa6g6n9w=",
            ScramVerifier.Compute("correct horse battery staple", Enumerable.Range(0, 16).Select(i => (byte)i).ToArray()));
    }

    [Fact]
    public void Create_uses_a_fresh_salt_and_never_holds_the_password()
    {
        var a = ScramVerifier.Create("same-password");
        var b = ScramVerifier.Create("same-password");
        Assert.NotEqual(a, b);
        Assert.StartsWith("SCRAM-SHA-256$4096:", a);
        Assert.DoesNotContain("same-password", a);
    }

    [Fact]
    public void A_new_password_is_32_random_bytes_base64url()
    {
        var a = ScramVerifier.NewPassword().Reveal();
        var b = ScramVerifier.NewPassword().Reveal();
        Assert.NotEqual(a, b);
        Assert.Matches("^[A-Za-z0-9_-]{43}$", a);
    }
}

public class ClusterInitializerTests
{
    private sealed class FakeChannel(Journal journal) : IOneShotSecretChannel
    {
        public string Path => @"\\.\pipe\erpclaw-initdb-test";
        public string? Served { get; private set; }
        public bool Disposed { get; private set; }

        public Task ServeOnceAsync(ReadOnlyMemory<byte> content, CancellationToken ct)
        {
            journal.Add("serve pwfile");
            Served = Encoding.UTF8.GetString(content.Span);
            return Task.CompletedTask;
        }

        public ValueTask DisposeAsync()
        {
            Disposed = true;
            return ValueTask.CompletedTask;
        }
    }

    private sealed class FakeChannels(Journal journal) : IOneShotSecretChannelFactory
    {
        public List<FakeChannel> Made { get; } = new();

        /// <summary>When set, no pipe can be made (the pipe server was refused, for instance).</summary>
        public Exception? Failure { get; set; }

        public IOneShotSecretChannel Create()
        {
            if (Failure is not null) throw Failure;
            var c = new FakeChannel(journal);
            Made.Add(c);
            return c;
        }
    }

    private sealed class FakeCredentials(Journal journal) : ICredentialStore
    {
        public Dictionary<string, Secret> Items { get; } = new();

        public void Write(string target, Secret value)
        {
            journal.Add("credential " + target);
            Items[target] = value;
        }

        public Secret? Read(string target) => Items.GetValueOrDefault(target);

        public void Delete(string target) => Items.Remove(target);
    }

    private sealed class Setup : IDisposable
    {
        public Journal Journal { get; } = new();
        public FakeLauncher Launcher { get; }
        public FakeChannels Channels { get; }
        public FakeCredentials Credentials { get; }
        public SecretRegistry Secrets { get; } = new();
        public ServiceCommands Commands { get; }
        public int InitDbExit { get; set; }
        public int SingleUserExit { get; set; }
        public string ErrorText { get; set; } = "";
        public MemorySink Sink { get; } = new();
        public int DatabaseCreateExit { get; set; }
        public bool InterruptAfterInitDb { get; set; }
        public bool InterruptAfterRuntimeRole { get; set; }
        public bool RuntimeRoleExists { get; private set; }
        public bool DatabaseExists { get; private set; }
        public bool MissingRequiredInclude { get; private set; }

        public Setup()
        {
            var data = Directory.CreateTempSubdirectory("erpclaw-data-").FullName;
            Commands = ServiceCommands.Proposed(new ServicePaths("/program", data));
            Channels = new FakeChannels(Journal);
            Credentials = new FakeCredentials(Journal);
            Launcher = new FakeLauncher(Journal, new FakeClock())
            {
                OnStart = child =>
                {
                    if (child.Name is "initdb" or "postgres-single")
                        child.CaptureStandardError(ErrorText);
                    if (child.Name == "initdb")
                    {
                        if (InitDbExit == 0)
                        {
                            Directory.CreateDirectory(Commands.Paths.Books);
                            File.WriteAllText(Path.Combine(Commands.Paths.Books, "PG_VERSION"), "18\n");
                            File.WriteAllText(Path.Combine(Commands.Paths.Books, "postgresql.conf"), "max_connections = 100\n");
                            File.WriteAllText(Path.Combine(Commands.Paths.Books, "pg_hba.conf"), "local all all trust\n");
                        }
                        child.Exit(InitDbExit);
                        if (InterruptAfterInitDb) throw new IOException("interrupted after initdb");
                    }
                    else if (child.Name == "postgres-single")
                    {
                        // PostgreSQL loads mandatory includes even in single-user mode, before reading stdin.
                        var includes = File.ReadAllLines(Path.Combine(Commands.Paths.Books, "postgresql.conf"))
                            .Where(line => line.TrimStart().StartsWith("include = '", StringComparison.Ordinal));
                        MissingRequiredInclude = includes.Any(line =>
                            !File.Exists(Path.Combine(Commands.Paths.Books, line.Split('\'')[1])));
                        if (MissingRequiredInclude || SingleUserExit != 0)
                        {
                            child.Exit(MissingRequiredInclude ? 1 : SingleUserExit);
                            return;
                        }
                        var sql = Rig.Utf8(child.Spec.StandardInput);
                        if (sql.Contains("ALTER ROLE \"erpclaw_runtime\"", StringComparison.Ordinal))
                        {
                            RuntimeRoleExists = true;
                            if (InterruptAfterRuntimeRole)
                            {
                                child.Exit(1);
                                throw new IOException("interrupted during single-user setup");
                            }
                        }
                        if (sql.StartsWith("CREATE DATABASE", StringComparison.Ordinal))
                        {
                            if (DatabaseCreateExit != 0) { child.Exit(DatabaseCreateExit); return; }
                            if (DatabaseExists) { child.Exit(1); return; }
                            DatabaseExists = true;
                        }
                        child.Exit(sql.Contains("FROM pg_database", StringComparison.Ordinal) && !DatabaseExists ? 1 : 0);
                    }
                },
            };
        }

        public ClusterInitializer Initializer() => new(Commands, Launcher, Channels, Credentials, Secrets,
            new ServiceLog(Sink, new FakeClock(), Secrets));

        public string Books(string file) => File.ReadAllText(Path.Combine(Commands.Paths.Books, file));

        public void Dispose() => Directory.Delete(Commands.Paths.DataFolder, true);
    }

    private static bool Verifies(string verifier, string password)
    {
        // SCRAM-SHA-256$<iter>:<salt>$<stored>:<server>
        var parts = verifier.Split('$');
        var head = parts[1].Split(':');
        var salt = Convert.FromBase64String(head[1]);
        return ScramVerifier.Compute(password, salt, int.Parse(head[0])) == verifier;
    }

    [Fact]
    public async Task A_fresh_cluster_gets_both_passwords_stored_first_and_never_on_a_command_line_or_in_a_file()
    {
        using var s = new Setup();
        var result = await s.Initializer().InitialiseAsync(CancellationToken.None);
        Assert.Equal(ClusterInitResult.Initialised, result);

        var owner = s.Credentials.Items[CredentialTargets.DatabaseOwner].Reveal();
        var runtime = s.Credentials.Items[CredentialTargets.DatabaseRuntime].Reveal();
        Assert.NotEqual(owner, runtime);
        Assert.True(s.Secrets.HoldsSecret(owner));
        Assert.True(s.Secrets.HoldsSecret(runtime));

        // Stored before initdb runs, so a crash after it never loses a password.
        Assert.True(s.Journal.IndexOf("credential " + CredentialTargets.DatabaseRuntime) < s.Journal.IndexOf("start initdb"));
        Assert.True(s.Journal.IndexOf("credential " + CredentialTargets.DatabaseOwner) < s.Journal.IndexOf("start initdb"));

        // initdb read the owner's verifier, never the password, from the one-shot channel.
        var channel = Assert.Single(s.Channels.Made);
        Assert.True(channel.Disposed);
        Assert.EndsWith("\n", channel.Served);
        var served = channel.Served!.TrimEnd('\n');
        Assert.DoesNotContain(owner, served);
        Assert.True(Verifies(served, owner));
        Assert.True(s.Journal.IndexOf("serve pwfile") < s.Journal.IndexOf("start initdb"));
        var initdb = s.Launcher.Latest("initdb")!;
        Assert.Equal(PostgresConfig.InitDbArguments(s.Commands.Paths.Books, s.Commands.Roles, channel.Path), initdb.Spec.Arguments);

        // Single-user mode got the runtime role's verifier on stdin, never the password.
        var single = s.Launcher.Named("postgres-single").First();
        var stdin = Rig.Utf8(single.Spec.StandardInput);
        Assert.DoesNotContain(runtime, stdin);
        var verifier = stdin.Split('\n').Single(line => line.StartsWith("ALTER ROLE", StringComparison.Ordinal)).Split('\'')[1];
        Assert.True(Verifies(verifier, runtime));

        // No password or verifier on any command line; no password in any file under the data folder.
        foreach (var child in s.Launcher.Started)
        {
            var line = WindowsCommandLine.Build(child.Spec.Executable, child.Spec.Arguments);
            Assert.DoesNotContain(owner, line);
            Assert.DoesNotContain(runtime, line);
            Assert.DoesNotContain("SCRAM-SHA-256", line);
        }
        foreach (var file in Directory.EnumerateFiles(s.Commands.Paths.DataFolder, "*", SearchOption.AllDirectories))
        {
            var text = File.ReadAllText(file);
            Assert.DoesNotContain(owner, text);
            Assert.DoesNotContain(runtime, text);
        }
    }

    [Fact]
    public async Task With_no_pipe_possible_initdb_rejects_all_logins_and_the_verifier_is_set_through_single_user_stdin()
    {
        using var s = new Setup();
        s.Channels.Failure = new IOException("no pipe");
        var result = await s.Initializer().InitialiseAsync(CancellationToken.None);
        Assert.Equal(ClusterInitResult.Initialised, result);

        var owner = s.Credentials.Items[CredentialTargets.DatabaseOwner].Reveal();
        var runtime = s.Credentials.Items[CredentialTargets.DatabaseRuntime].Reveal();
        var initdb = s.Launcher.Latest("initdb")!;
        Assert.Equal(PostgresConfig.InitDbArguments(s.Commands.Paths.Books, s.Commands.Roles, null), initdb.Spec.Arguments);
        Assert.Contains("--auth=reject", initdb.Spec.Arguments);
        Assert.DoesNotContain(initdb.Spec.Arguments, a => a.StartsWith("--pwfile", StringComparison.Ordinal));
        Assert.DoesNotContain("-A", initdb.Spec.Arguments);
        Assert.DoesNotContain("scram-sha-256", initdb.Spec.Arguments);
        Assert.Empty(s.Channels.Made);

        var lines = Rig.Utf8(s.Launcher.Named("postgres-single").First().Spec.StandardInput).Split('\n', StringSplitOptions.RemoveEmptyEntries);
        Assert.Equal(3, lines.Length);
        Assert.StartsWith("ALTER ROLE \"erpclaw_owner\" PASSWORD 'SCRAM-SHA-256$", lines[0]);
        Assert.Contains("CREATE ROLE \"erpclaw_runtime\"", lines[1]);
        Assert.Contains("ALTER ROLE \"erpclaw_runtime\"", lines[2]);
        Assert.True(Verifies(lines[0].Split('\'')[1], owner));
        Assert.True(Verifies(lines[2].Split('\'')[1], runtime));
        foreach (var line in lines)
        {
            Assert.DoesNotContain(owner, line);
            Assert.DoesNotContain(runtime, line);
        }
        foreach (var child in s.Launcher.Started)
        {
            var commandLine = WindowsCommandLine.Build(child.Spec.Executable, child.Spec.Arguments);
            Assert.DoesNotContain(owner, commandLine);
            Assert.DoesNotContain("SCRAM-SHA-256", commandLine);
        }
    }

    [Fact]
    public void The_fallback_initdb_arguments_reject_logins_and_name_no_password_file()
    {
        var args = PostgresConfig.InitDbArguments(@"C:\b", new DatabaseRoles(), null);
        Assert.Equal(new[]
        {
            "-D", @"C:\b",
            "-U", "erpclaw_owner",
            "-E", "UTF8",
            "--locale-provider=builtin",
            "--builtin-locale=C.UTF-8",
            "--locale=C",
            "--auth=reject",
            "--no-instructions",
        }, args);
    }

    [Fact]
    public void Single_user_setup_with_an_owner_verifier_sets_the_owner_first()
    {
        var owner = ScramVerifier.Compute("owner-password", new byte[16]);
        var runtime = ScramVerifier.Compute("runtime-password", new byte[16]);
        var lines = PostgresConfig.SingleUserSetup(new DatabaseRoles(), runtime, owner).Split('\n', StringSplitOptions.RemoveEmptyEntries);
        Assert.Equal($"ALTER ROLE \"erpclaw_owner\" PASSWORD '{owner}';", lines[0]);
        Assert.Equal(3, lines.Length);
        Assert.Throws<ArgumentException>(() => PostgresConfig.SingleUserSetup(new DatabaseRoles(), runtime, "plaintext"));
    }

    [Fact]
    public async Task A_missing_required_include_prevents_even_single_user_startup()
    {
        using var s = new Setup();
        Directory.CreateDirectory(s.Commands.Paths.Books);
        File.WriteAllText(Path.Combine(s.Commands.Paths.Books, "postgresql.conf"), PostgresConfig.IncludeLine + "\n");
        var child = s.Launcher.Start(s.Commands.SingleUser("SELECT 1;\n"u8.ToArray()));
        Assert.NotEqual(0, await child.Exited);
        Assert.True(s.MissingRequiredInclude);
    }

    [Fact]
    public async Task The_cluster_gets_the_include_and_pg_hba_before_its_first_start()
    {
        using var s = new Setup();
        Assert.Equal(ClusterInitResult.Initialised, await s.Initializer().InitialiseAsync(CancellationToken.None));
        Assert.False(s.MissingRequiredInclude);
        Assert.Equal(PostgresConfig.ManagedSettings(5432, s.Commands.Paths.PostgresLogs), s.Books(PostgresConfig.ManagedFileName));

        Assert.EndsWith(PostgresConfig.IncludeLine + "\n", s.Books("postgresql.conf"));
        Assert.StartsWith("max_connections = 100\n", s.Books("postgresql.conf"));
        Assert.Equal(PostgresConfig.Hba(s.Commands.Roles), s.Books("pg_hba.conf"));
        Assert.True(s.Journal.IndexOf("start postgres-single") > s.Journal.IndexOf("start initdb"));
        Assert.Empty(s.Launcher.Named("postgres"));
    }

    [Fact]
    public async Task An_initialised_cluster_is_left_alone()
    {
        using var s = new Setup();
        Directory.CreateDirectory(s.Commands.Paths.Books);
        File.WriteAllText(Path.Combine(s.Commands.Paths.Books, "PG_VERSION"), "18\n");
        File.WriteAllText(Path.Combine(s.Commands.Paths.Books, ClusterInitializer.SetupCompleteFileName), "1\n");

        Assert.Equal(ClusterInitResult.AlreadyInitialised, await s.Initializer().InitialiseAsync(CancellationToken.None));
        Assert.Empty(s.Launcher.Started);
        Assert.Empty(s.Credentials.Items);
    }

    [Fact]
    public async Task A_failed_initdb_stops_there_and_its_channel_is_closed()
    {
        using var s = new Setup { InitDbExit = 1 };
        Assert.Equal(ClusterInitResult.Failed, await s.Initializer().InitialiseAsync(CancellationToken.None));
        Assert.Empty(s.Launcher.Named("postgres-single"));
        Assert.True(s.Channels.Made.Single().Disposed);
    }

    [Fact]
    public async Task A_failed_single_user_step_is_a_failure()
    {
        using var s = new Setup { SingleUserExit = 1 };
        Assert.Equal(ClusterInitResult.Failed, await s.Initializer().InitialiseAsync(CancellationToken.None));
        Assert.False(File.Exists(Path.Combine(s.Commands.Paths.Books, ClusterInitializer.SetupCompleteFileName)));
    }

    [Fact]
    public async Task Mutation_failed_single_user_surfaces_filtered_stderr_in_failure_and_log()
    {
        using var s = new Setup { SingleUserExit = 1, ErrorText = "LOG: noise\nERROR: syntax error\nFATAL: PASSWORD <placeholder>" };
        var initializer = s.Initializer();
        Assert.Equal(ClusterInitResult.Failed, await initializer.InitialiseAsync(CancellationToken.None));
        var failure = Assert.IsType<PostgresStepFailure>(initializer.LastFailure);
        Assert.Equal("single-user", failure.Step);
        Assert.Equal(1, failure.ExitCode);
        Assert.Equal("ERROR: syntax error", failure.Diagnostic);
        Assert.Contains(s.Sink.Lines, line => line.EndsWith("ERROR: syntax error", StringComparison.Ordinal));
        Assert.DoesNotContain(s.Sink.Lines, line => line.Contains("PASSWORD", StringComparison.Ordinal));
        Assert.True(s.Launcher.Named("postgres-single").First().Spec.CaptureErrorDiagnostic);
    }

    [Fact]
    public async Task Mutation_failed_initdb_surfaces_filtered_stderr_in_failure_and_log()
    {
        using var s = new Setup { InitDbExit = 1, ErrorText = "initdb: could not open file" };
        var initializer = s.Initializer();
        Assert.Equal(ClusterInitResult.Failed, await initializer.InitialiseAsync(CancellationToken.None));
        var failure = Assert.IsType<PostgresStepFailure>(initializer.LastFailure);
        Assert.Equal("initdb", failure.Step);
        Assert.Equal(s.ErrorText, failure.Diagnostic);
        Assert.Contains(s.Sink.Lines, line => line.EndsWith(s.ErrorText, StringComparison.Ordinal));
    }

    [Fact]
    public async Task Mutation_child_echoes_stdin_but_failure_and_log_never_include_statements()
    {
        using var s = new Setup();
        var start = s.Launcher.OnStart!;
        s.Launcher.OnStart = child =>
        {
            if (child.Name == "postgres-single")
            {
                var sql = Rig.Utf8(child.Spec.StandardInput);
                child.CaptureStandardError("ERROR: syntax error\nSTATEMENT: " + sql + "\n" + sql);
                child.Exit(1);
            }
            else start(child);
        };
        var initializer = s.Initializer();
        Assert.Equal(ClusterInitResult.Failed, await initializer.InitialiseAsync(CancellationToken.None));
        Assert.Equal("ERROR: syntax error", initializer.LastFailure!.Diagnostic);
        Assert.DoesNotContain(s.Sink.Lines, line => line.Contains("ALTER ROLE", StringComparison.Ordinal)
            || line.Contains("DO $$", StringComparison.Ordinal) || line.Contains("SCRAM-SHA-256$", StringComparison.Ordinal));
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task Interrupted_setup_resumes_with_the_stored_credentials(bool duringSingleUser)
    {
        using var s = new Setup
        {
            InterruptAfterInitDb = !duringSingleUser,
            InterruptAfterRuntimeRole = duringSingleUser,
        };
        await Assert.ThrowsAsync<IOException>(() => s.Initializer().InitialiseAsync(CancellationToken.None));
        Assert.False(File.Exists(Path.Combine(s.Commands.Paths.Books, ClusterInitializer.SetupCompleteFileName)));
        var stored = s.Credentials.Items.ToDictionary(pair => pair.Key, pair => pair.Value.Reveal());
        s.InterruptAfterInitDb = false;
        s.InterruptAfterRuntimeRole = false;
        Assert.Equal(ClusterInitResult.Initialised, await s.Initializer().InitialiseAsync(CancellationToken.None));
        Assert.Single(s.Launcher.Named("initdb"));
        Assert.Equal(2, s.Journal.Entries.Count(e => e.StartsWith("credential ", StringComparison.Ordinal)));
        foreach (var pair in stored) Assert.Equal(pair.Value, s.Credentials.Items[pair.Key].Reveal());
        var sql = Rig.Utf8(s.Launcher.Named("postgres-single").First(c =>
            Rig.Utf8(c.Spec.StandardInput).StartsWith("ALTER ROLE", StringComparison.Ordinal)).Spec.StandardInput);
        Assert.True(Verifies(sql.Split('\'')[1], stored[CredentialTargets.DatabaseOwner]));
        Assert.True(s.RuntimeRoleExists);
        Assert.True(s.DatabaseExists);
        Assert.Equal(1, s.Books("postgresql.conf").Split(PostgresConfig.IncludeLine).Length - 1);
        Assert.True(File.Exists(Path.Combine(s.Commands.Paths.Books, ClusterInitializer.SetupCompleteFileName)));
        Assert.Equal(ClusterInitResult.AlreadyInitialised, await s.Initializer().InitialiseAsync(CancellationToken.None));
    }

    [Fact]
    public async Task Recovery_after_database_creation_does_not_create_it_again()
    {
        using var s = new Setup();
        Assert.Equal(ClusterInitResult.Initialised, await s.Initializer().InitialiseAsync(CancellationToken.None));
        File.Delete(Path.Combine(s.Commands.Paths.Books, ClusterInitializer.SetupCompleteFileName));
        Assert.Equal(ClusterInitResult.Initialised, await s.Initializer().InitialiseAsync(CancellationToken.None));
        Assert.Single(s.Launcher.Named("postgres-single"), c =>
            Rig.Utf8(c.Spec.StandardInput).StartsWith("CREATE DATABASE", StringComparison.Ordinal));
    }

    [Fact]
    public async Task Failed_database_creation_does_not_publish_completion_and_can_be_retried()
    {
        using var s = new Setup { DatabaseCreateExit = 1 };
        Assert.Equal(ClusterInitResult.Failed, await s.Initializer().InitialiseAsync(CancellationToken.None));
        Assert.True(s.RuntimeRoleExists);
        Assert.False(s.DatabaseExists);
        Assert.False(File.Exists(Path.Combine(s.Commands.Paths.Books, ClusterInitializer.SetupCompleteFileName)));
        s.DatabaseCreateExit = 0;
        Assert.Equal(ClusterInitResult.Initialised, await s.Initializer().InitialiseAsync(CancellationToken.None));
        Assert.Single(s.Launcher.Named("initdb"));
    }

    [Fact]
    public async Task An_incomplete_cluster_without_both_credentials_fails_without_changing_them()
    {
        using var s = new Setup { InterruptAfterInitDb = true };
        await Assert.ThrowsAsync<IOException>(() => s.Initializer().InitialiseAsync(CancellationToken.None));
        s.Credentials.Delete(CredentialTargets.DatabaseRuntime);
        Assert.Equal(ClusterInitResult.Failed, await s.Initializer().InitialiseAsync(CancellationToken.None));
        Assert.Empty(s.Launcher.Named("postgres-single"));
        Assert.Equal(2, s.Journal.Entries.Count(e => e.StartsWith("credential ", StringComparison.Ordinal)));
    }

    [Fact]
    public void The_managed_settings_file_is_written_for_each_start()
    {
        var data = Directory.CreateTempSubdirectory("erpclaw-data-").FullName;
        try
        {
            var paths = new ServicePaths("/program", data);
            new PostgresConfigFiles(paths).WriteManagedSettings(50123);
            var text = File.ReadAllText(Path.Combine(paths.Books, PostgresConfig.ManagedFileName));
            Assert.Equal(PostgresConfig.ManagedSettings(50123, paths.PostgresLogs), text);
            Assert.True(Directory.Exists(paths.PostgresLogs));
        }
        finally
        {
            Directory.Delete(data, true);
        }
    }
}
