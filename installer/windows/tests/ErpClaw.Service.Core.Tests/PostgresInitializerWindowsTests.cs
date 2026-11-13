// WINDOWS CI ONLY. Exercises the complete initializer against the bundled PostgreSQL, without a mock server.
using System.Diagnostics;
using System.IO.Pipes;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using ErpClaw.Service.Core;
using Xunit;

namespace ErpClaw.Service.Core.Tests;

[Collection(PostgresInitializerCollection.Name)]
public class PostgresInitializerWindowsTests
{
    private sealed class Credentials : ICredentialStore
    {
        public Dictionary<string, Secret> Items { get; } = new();
        public int Writes { get; private set; }
        public void Write(string target, Secret value) { Items[target] = value; Writes++; }
        public Secret? Read(string target) => Items.GetValueOrDefault(target);
        public void Delete(string target) => Items.Remove(target);
    }

    private sealed class Channels(bool fallback) : IOneShotSecretChannelFactory
    {
        public int Created { get; private set; }

        public IOneShotSecretChannel Create()
        {
            if (fallback) throw new IOException("Forced fallback.");
            Created++;
            return new Channel();
        }
    }

    private sealed class Channel : IOneShotSecretChannel
    {
        private readonly NamedPipeServerStream _pipe;
        public string Path { get; }

        public Channel()
        {
            if (!OperatingSystem.IsWindows()) throw new PlatformNotSupportedException();
            var name = "erpclaw-initdb-" + Guid.NewGuid().ToString("N");
            Path = @"\\.\pipe\" + name;
            var security = new PipeSecurity();
            security.SetAccessRuleProtection(true, false);
            security.AddAccessRule(new PipeAccessRule(WindowsIdentity.GetCurrent().User!,
                PipeAccessRights.FullControl, AccessControlType.Allow));
            _pipe = NamedPipeServerStreamAcl.Create(name, PipeDirection.Out, 1, PipeTransmissionMode.Byte,
                PipeOptions.Asynchronous | PipeOptions.FirstPipeInstance, 0, 0, security);
        }

        public async Task ServeOnceAsync(ReadOnlyMemory<byte> content, CancellationToken ct)
        {
            await _pipe.WaitForConnectionAsync(ct);
            await _pipe.WriteAsync(content, ct);
            await _pipe.FlushAsync(ct);
            // Keep the instance alive until initdb has consumed the line.
            if (OperatingSystem.IsWindows()) _pipe.WaitForPipeDrain();
        }

        public ValueTask DisposeAsync() => _pipe.DisposeAsync();
    }

    private sealed class Child : IChildProcess, IDisposable
    {
        private readonly Process _process;
        public int Id { get; }
        public DateTimeOffset StartTime { get; }
        public Task<int> Exited { get; }
        public string StandardOutput { get; private set; } = "";
        public string ErrorDiagnostic { get; private set; } = "";

        public Child(ProcessSpec spec, string? password = null)
        {
            var info = new ProcessStartInfo(spec.Executable)
            {
                UseShellExecute = false,
                CreateNoWindow = true,
                WorkingDirectory = spec.WorkingDirectory!,
                RedirectStandardInput = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            };
            foreach (var arg in spec.Arguments) info.ArgumentList.Add(arg);
            foreach (var key in info.Environment.Keys.Where(key => key.StartsWith("PG", StringComparison.Ordinal)).ToArray())
                info.Environment.Remove(key);
            // Only the throwaway psql client receives this variable; never the parent or another child.
            if (password is not null) info.Environment["PGPASSWORD"] = password;
            _process = Process.Start(info) ?? throw new IOException("PostgreSQL child did not start.");
            Id = _process.Id;
            StartTime = _process.StartTime;
            Exited = CompleteAsync(spec);
        }

        private async Task<int> CompleteAsync(ProcessSpec spec)
        {
            var stdout = _process.StandardOutput.ReadToEndAsync();
            var stderr = new PostgresDiagnosticFilter(spec.StandardInput).ReadAsync(_process.StandardError);
            IOException? inputFailure = null;
            try
            {
                await _process.StandardInput.BaseStream.WriteAsync(spec.StandardInput);
            }
            catch (IOException e)
            {
                // An early child exit can close stdin first. Finish draining before reporting its exit.
                inputFailure = e;
            }
            finally
            {
                _process.StandardInput.Close();
            }
            await _process.WaitForExitAsync();
            StandardOutput = await stdout;
            var diagnostic = await stderr;
            if (spec.CaptureErrorDiagnostic) ErrorDiagnostic = diagnostic;
            if (inputFailure is not null && _process.ExitCode == 0)
                throw new IOException("PostgreSQL stdin could not be written.\n" + ErrorDiagnostic);
            return _process.ExitCode;
        }

        public void Kill() { if (!_process.HasExited) _process.Kill(entireProcessTree: true); }
        public void Dispose() => _process.Dispose();
    }

    private sealed class InterruptedChild : IChildProcess
    {
        private readonly IChildProcess _child;
        public int Id => _child.Id;
        public DateTimeOffset StartTime => _child.StartTime;
        public string StandardOutput => _child.StandardOutput;
        public string ErrorDiagnostic => _child.ErrorDiagnostic;
        public Task<int> Exited { get; }
        public void Kill() => _child.Kill();

        public InterruptedChild(IChildProcess child)
        {
            _child = child;
            Exited = InterruptAsync(child);
        }

        private static async Task<int> InterruptAsync(IChildProcess child)
        {
            var exit = await child.Exited;
            Assert.True(exit == 0, $"PostgreSQL exited {exit}.\n{child.ErrorDiagnostic}");
            throw new IOException("Simulated interruption after the child committed its work.");
        }
    }

    private sealed class Launcher(string pgBin) : IProcessLauncher, IAsyncDisposable
    {
        private readonly List<Child> _children = new();
        public string? InterruptAfter { get; set; }
        public int InitDbStarts { get; private set; }
        public bool InitDbViaPipe { get; private set; }

        public IChildProcess Start(ProcessSpec spec)
        {
            var name = Names.Of(spec);
            if (name == "initdb")
            {
                InitDbStarts++;
                InitDbViaPipe = spec.Arguments.Any(arg => arg.StartsWith("--pwfile=", StringComparison.Ordinal));
            }
            if (name == "postgres-single" && InterruptAfter == "roles")
            {
                // Commit CREATE ROLE but omit its ALTER ROLE: recovery must repair a role with no login/password.
                var prefix = Rig.Utf8(spec.StandardInput).Split('\n')
                    .TakeWhile(line => !line.StartsWith("ALTER ROLE \"erpclaw_runtime\"", StringComparison.Ordinal));
                spec = spec with { StandardInput = Encoding.UTF8.GetBytes(string.Join('\n', prefix) + "\n") };
            }
            var child = StartChild(spec with { Executable = Path.Combine(pgBin, Path.GetFileName(spec.Executable)) });
            var point = name == "postgres-single"
                ? Rig.Utf8(spec.StandardInput).StartsWith("CREATE DATABASE", StringComparison.Ordinal) ? "database" : "roles"
                : name;
            if (InterruptAfter != point) return child;
            InterruptAfter = null;
            return new InterruptedChild(child);
        }

        private Child StartChild(ProcessSpec spec, string? password = null)
        {
            var child = new Child(spec, password);
            _children.Add(child);
            return child;
        }

        public async Task<(int Exit, string Output, string Diagnostic)> SqlAsync(ServicePaths paths, int port, string user, string password,
            string database, string sql, CancellationToken ct)
        {
            var spec = new ProcessSpec(Path.Combine(pgBin, "psql.exe"), new[]
            {
                "--no-psqlrc", "--no-password", "--set=ON_ERROR_STOP=1", "--tuples-only", "--no-align",
                "--host=127.0.0.1", "--port=" + port, "--username=" + user, "--dbname=" + database, "--command=" + sql,
            }) { WorkingDirectory = paths.DataFolder, CaptureErrorDiagnostic = true };
            var child = StartChild(spec, password);
            return (await child.Exited.WaitAsync(ct), child.StandardOutput.Trim(), child.ErrorDiagnostic);
        }

        public async ValueTask DisposeAsync()
        {
            foreach (var child in _children) child.Kill();
            foreach (var child in _children)
            {
                try { await child.Exited.WaitAsync(TimeSpan.FromSeconds(30)); }
                finally { child.Dispose(); }
            }
        }
    }

    [WindowsTheory]
    [InlineData(false, null)]
    [InlineData(true, null)]
    [InlineData(false, "initdb")]
    [InlineData(true, "initdb")]
    [InlineData(false, "roles")]
    [InlineData(true, "roles")]
    [InlineData(false, "database")]
    [InlineData(true, "database")]
    public async Task The_complete_initializer_supports_both_routes_and_recovers_interrupted_setup(bool fallback, string? interruption)
    {
        if (!OperatingSystem.IsWindows()) return;
        var pgBin = Environment.GetEnvironmentVariable("ERPCLAW_TEST_PG_BIN");
        Assert.False(string.IsNullOrWhiteSpace(pgBin), "Set ERPCLAW_TEST_PG_BIN to the bundled PostgreSQL bin folder for Windows CI.");
        foreach (var executable in new[] { "initdb.exe", "postgres.exe", "psql.exe", "pg_ctl.exe" })
            Assert.True(File.Exists(Path.Combine(pgBin!, executable)), "The bundled PostgreSQL executables are required.");
        var data = Directory.CreateTempSubdirectory("erpclaw-data-").FullName;
        try
        {
            await using var launcher = new Launcher(pgBin!) { InterruptAfter = interruption };
            using var timeout = new CancellationTokenSource(TimeSpan.FromMinutes(3));
            var ct = timeout.Token;
            var paths = new ServicePaths(@"C:\P\ERPClaw\3.0.0", data);
            var commands = ServiceCommands.Proposed(paths);
            var credentials = new Credentials();
            var channels = new Channels(fallback);
            var secrets = new SecretRegistry();
            var sink = new MemorySink();
            var log = new ServiceLog(sink, new FakeClock(), secrets);
            var initializer = new ClusterInitializer(commands, launcher, channels, credentials, secrets, log);
            var marker = Path.Combine(paths.Books, ClusterInitializer.SetupCompleteFileName);
            if (interruption is not null)
            {
                var interrupted = false;
                try { await initializer.InitialiseAsync(ct); }
                catch (IOException) { interrupted = true; }
                Assert.True(interrupted, initializer.LastFailure?.ToString() ?? "The requested setup interruption was not reached.");
                Assert.True(File.Exists(Path.Combine(paths.Books, "PG_VERSION")));
                Assert.False(File.Exists(marker));
                var ownerBefore = credentials.Read(CredentialTargets.DatabaseOwner)!.Reveal();
                var runtimeBefore = credentials.Read(CredentialTargets.DatabaseRuntime)!.Reveal();
                // A new service instance must recover using only the durable cluster and credential store.
                secrets = new SecretRegistry();
                log = new ServiceLog(sink, new FakeClock(), secrets);
                initializer = new ClusterInitializer(commands, launcher, channels, credentials, secrets, log);
                Assert.True(await initializer.InitialiseAsync(ct) == ClusterInitResult.Initialised,
                    initializer.LastFailure?.ToString() ?? "PostgreSQL initialization failed without a child diagnostic.");
                Assert.Equal(ownerBefore, credentials.Read(CredentialTargets.DatabaseOwner)!.Reveal());
                Assert.Equal(runtimeBefore, credentials.Read(CredentialTargets.DatabaseRuntime)!.Reveal());
            }
            else
                Assert.True(await initializer.InitialiseAsync(ct) == ClusterInitResult.Initialised,
                    initializer.LastFailure?.ToString() ?? "PostgreSQL initialization failed without a child diagnostic.");

            Assert.True(File.Exists(marker));
            Assert.Equal(2, credentials.Writes);
            Assert.Equal(1, launcher.InitDbStarts);
            Assert.Equal(!fallback, launcher.InitDbViaPipe);
            Assert.Equal(fallback ? 0 : 1, channels.Created);
            Assert.Equal(ClusterInitResult.AlreadyInitialised, await initializer.InitialiseAsync(ct));
            var ownerPassword = credentials.Read(CredentialTargets.DatabaseOwner)!.Reveal();
            var runtimePassword = credentials.Read(CredentialTargets.DatabaseRuntime)!.Reveal();
            Assert.True(secrets.HoldsSecret(ownerPassword));
            Assert.True(secrets.HoldsSecret(runtimePassword));

            var port = new LoopbackPortProbe().DrawFree();
            new PostgresConfigFiles(paths).WriteManagedSettings(port);
            var conf = File.ReadAllText(Path.Combine(paths.Books, "postgresql.conf"));
            Assert.Equal(1, conf.Split(PostgresConfig.IncludeLine).Length - 1);
            Assert.Equal(PostgresConfig.ManagedSettings(port, paths.PostgresLogs),
                File.ReadAllText(Path.Combine(paths.Books, PostgresConfig.ManagedFileName)));
            Assert.Equal(PostgresConfig.Hba(commands.Roles), File.ReadAllText(Path.Combine(paths.Books, "pg_hba.conf")));
            var server = launcher.Start(commands.Postgres());
            try
            {
                while (true)
                {
                    Assert.False(server.Exited.IsCompleted,
                        "The private PostgreSQL exited before accepting logins.\n" + server.ErrorDiagnostic);
                    var ready = await launcher.SqlAsync(paths, port, commands.Roles.Owner, ownerPassword, commands.Roles.Database, "SELECT 1", ct);
                    if (ready.Exit == 0) break;
                    await Task.Delay(200, ct);
                }

                async Task<string> Query(string user, string password, string sql)
                {
                    var result = await launcher.SqlAsync(paths, port, user, password, commands.Roles.Database, sql, ct);
                    Assert.True(result.Exit == 0, $"PostgreSQL query exited {result.Exit}.\n{result.Diagnostic}");
                    return result.Output;
                }
                async Task Denied(string user, string password, string sql)
                {
                    var result = await launcher.SqlAsync(paths, port, user, password, commands.Roles.Database, sql, ct);
                    Assert.NotEqual(0, result.Exit);
                }

                Assert.Equal(commands.Roles.Owner, await Query(commands.Roles.Owner, ownerPassword, "SELECT current_user"));
                Assert.Equal(commands.Roles.Runtime, await Query(commands.Roles.Runtime, runtimePassword, "SELECT current_user"));
                await Denied(commands.Roles.Owner, "wrong-" + ownerPassword, "SELECT 1");
                await Denied(commands.Roles.Runtime, "wrong-" + runtimePassword, "SELECT 1");
                Assert.Equal("t", await Query(commands.Roles.Owner, ownerPassword, "SELECT rolsuper AND rolcanlogin FROM pg_roles WHERE rolname = current_user"));
                Assert.Equal("t", await Query(commands.Roles.Runtime, runtimePassword,
                    "SELECT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls FROM pg_roles WHERE rolname = current_user"));
                Assert.Equal("f", await Query(commands.Roles.Runtime, runtimePassword, "SELECT pg_has_role(current_user, 'erpclaw_owner', 'MEMBER')"));
                Assert.Equal("erpclaw_owner|UTF8", await Query(commands.Roles.Owner, ownerPassword,
                    "SELECT pg_get_userbyid(datdba), pg_encoding_to_char(encoding) FROM pg_database WHERE datname = current_database()"));
                await Query(commands.Roles.Owner, ownerPassword, "CREATE TABLE public.owner_only (value integer); INSERT INTO public.owner_only VALUES (7)");
                await Denied(commands.Roles.Runtime, runtimePassword, "SELECT * FROM public.owner_only");
                await Denied(commands.Roles.Runtime, runtimePassword, "CREATE ROLE forbidden_role");
                await Denied(commands.Roles.Runtime, runtimePassword, "CREATE DATABASE forbidden_database");
                await Denied(commands.Roles.Runtime, runtimePassword, "CREATE TABLE public.forbidden_table (value integer)");
                await Query(commands.Roles.Owner, ownerPassword, "GRANT SELECT ON public.owner_only TO erpclaw_runtime");
                Assert.Equal("7", await Query(commands.Roles.Runtime, runtimePassword, "SELECT * FROM public.owner_only"));
                Assert.Equal("127.0.0.1|" + port + "||scram-sha-256|on|none|panic|warning|t|off",
                    await Query(commands.Roles.Owner, ownerPassword,
                        "SELECT current_setting('listen_addresses'), current_setting('port'), current_setting('unix_socket_directories'), current_setting('password_encryption'), current_setting('logging_collector'), current_setting('log_statement'), current_setting('log_min_error_statement'), current_setting('log_min_messages'), current_setting('log_connections') IN ('off', ''), current_setting('log_disconnections')"));
                Assert.Equal("2|t", await Query(commands.Roles.Owner, ownerPassword,
                    "SELECT count(*), bool_and(type = 'host' AND address = '127.0.0.1' AND netmask = '255.255.255.255' AND auth_method = 'scram-sha-256' AND error IS NULL) FROM pg_hba_file_rules"));
            }
            finally
            {
                var stop = launcher.Start(commands.PgCtlStop() with { CaptureErrorDiagnostic = true });
                Assert.True(await stop.Exited.WaitAsync(ct) == 0, "PostgreSQL stop failed.\n" + stop.ErrorDiagnostic + "\n" + server.ErrorDiagnostic);
                Assert.True(await server.Exited.WaitAsync(ct) == 0, "PostgreSQL server failed.\n" + server.ErrorDiagnostic);
            }
        }
        finally
        {
            Directory.Delete(data, true);
        }
    }
}
