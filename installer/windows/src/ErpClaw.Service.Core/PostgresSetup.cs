// The private PostgreSQL (INSTALLER 2.2): its configuration, pg_hba.conf, initdb's arguments, the SCRAM verifiers
// and the one-time initialisation. No password is ever on a command line or in a file.
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;

namespace ErpClaw.Service.Core;

/// <summary>The two roles and the first database. The names are proposed, still to be confirmed.</summary>
public sealed record DatabaseRoles(string Owner = "erpclaw_owner", string Runtime = "erpclaw_runtime", string Database = "erpclaw");

/// <summary>The Windows Credential Manager under the owner's account (SECRETS_ENTRY 2.4's store).</summary>
public interface ICredentialStore
{
    void Write(string target, Secret value);

    Secret? Read(string target);

    void Delete(string target);
}

public static class CredentialTargets
{
    public const string DatabaseOwner = "erpclaw-v3-db-owner";
    public const string DatabaseRuntime = "erpclaw-v3-db-runtime";
}

public static partial class PostgresConfig
{
    /// <summary>The service-managed settings file in books\, included from postgresql.conf.</summary>
    public const string ManagedFileName = "erpclaw.conf";

    public const string IncludeLine = "include = 'erpclaw.conf'";

    /// <summary>The managed settings: loopback only, the port, SCRAM, logging to logs\ with nothing secret.</summary>
    public static string ManagedSettings(int port, string logDirectory)
    {
        if (port is < 1 or > 65535) throw new ArgumentOutOfRangeException(nameof(port));
        return string.Join('\n',
            "# Written by the ERPClaw service before each start; changes here are replaced.",
            "listen_addresses = '127.0.0.1'",
            $"port = {port}",
            "unix_socket_directories = ''",
            "password_encryption = 'scram-sha-256'",
            "logging_collector = on",
            "log_destination = 'stderr'",
            $"log_directory = {Literal(logDirectory)}",
            "log_filename = 'postgresql-%Y-%m-%d.log'",
            "log_rotation_age = 1d",
            "log_rotation_size = 0",
            "# Nothing secret and no port in the log: no statement text (a failed ALTER ROLE would carry a password),",
            "# no connection lines, no client address, and no start-up messages below WARNING (they name the port).",
            "log_min_messages = warning",
            "log_min_error_statement = panic",
            "log_statement = 'none'",
            "log_connections = off",
            "log_disconnections = off",
            "log_hostname = off",
            "log_line_prefix = '%m [%p] '",
            "");
    }

    /// <summary>pg_hba.conf: only host lines from 127.0.0.1/32, scram-sha-256, the two roles.</summary>
    public static string Hba(DatabaseRoles roles)
    {
        Check(roles);
        return string.Join('\n',
            "# Written by the ERPClaw installer: loopback, SCRAM, ERPClaw's two roles, nothing else.",
            "# TYPE  DATABASE  USER  ADDRESS  METHOD",
            $"host    all       {roles.Owner}  127.0.0.1/32  scram-sha-256",
            $"host    all       {roles.Runtime}  127.0.0.1/32  scram-sha-256",
            "");
    }

    /// <summary>
    /// initdb's arguments; the owner's password reaches it only through <paramref name="pwFilePath"/>. When no
    /// password file is possible (null), initdb rejects all logins and the verifier is set afterwards.
    /// </summary>
    public static IReadOnlyList<string> InitDbArguments(string booksFolder, DatabaseRoles roles, string? pwFilePath)
    {
        Check(roles);
        var common = new List<string>
        {
            "-D", booksFolder,
            "-U", roles.Owner,
            "-E", "UTF8",
            "--locale-provider=builtin",
            "--builtin-locale=C.UTF-8",
            "--locale=C",
        };
        // With no password file every login is rejected until single-user mode sets the owner's verifier.
        if (pwFilePath is null) common.Add("--auth=reject");
        else common.AddRange(new[] { "-A", "scram-sha-256", "--pwfile=" + pwFilePath });
        common.Add("--no-instructions");
        return common;
    }

    /// <summary>
    /// The statements for PostgreSQL's single-user mode, on its stdin: the runtime login role with its SCRAM verifier
    /// (never the password), preceded by the owner's verifier when needed. Safe to repeat after interruption.
    /// One statement per line; database creation runs separately because it cannot run inside a DO block.
    /// </summary>
    public static string SingleUserSetup(DatabaseRoles roles, string runtimeVerifier, string? ownerVerifier = null)
    {
        Check(roles);
        if (!VerifierShape().IsMatch(runtimeVerifier))
            throw new ArgumentException("Only a SCRAM-SHA-256 verifier is accepted here.", nameof(runtimeVerifier));
        if (ownerVerifier is not null && !VerifierShape().IsMatch(ownerVerifier))
            throw new ArgumentException("Only a SCRAM-SHA-256 verifier is accepted here.", nameof(ownerVerifier));
        var owner = ownerVerifier is null ? "" : $"ALTER ROLE \"{roles.Owner}\" PASSWORD '{ownerVerifier}';\n";
        return owner
             + $"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{roles.Runtime}') THEN CREATE ROLE \"{roles.Runtime}\"; END IF; END $$;\n"
             + $"ALTER ROLE \"{roles.Runtime}\" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD '{runtimeVerifier}';\n";
    }

    /// <summary>Exit successfully only when the database exists with the intended owner and encoding.</summary>
    public static string SingleUserDatabaseCheck(DatabaseRoles roles)
    {
        Check(roles);
        return $"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_database WHERE datname = '{roles.Database}' AND datdba = (SELECT oid FROM pg_roles WHERE rolname = '{roles.Owner}') AND encoding = pg_char_to_encoding('UTF8')) THEN RAISE EXCEPTION 'Setup database is absent or incompatible'; END IF; END $$;\n";
    }

    public static string SingleUserCreateDatabase(DatabaseRoles roles)
    {
        Check(roles);
        return $"CREATE DATABASE \"{roles.Database}\" OWNER \"{roles.Owner}\" ENCODING 'UTF8';\n";
    }

    /// <summary>A conf-file string literal: forward slashes, single quotes doubled.</summary>
    public static string Literal(string value) => "'" + value.Replace('\\', '/').Replace("'", "''") + "'";

    private static void Check(DatabaseRoles roles)
    {
        foreach (var name in new[] { roles.Owner, roles.Runtime, roles.Database })
            if (!Identifier().IsMatch(name))
                throw new ArgumentException("Role and database names are lower-case identifiers.", nameof(roles));
    }

    [GeneratedRegex("^[a-z_][a-z0-9_]{0,62}$")]
    private static partial Regex Identifier();

    [GeneratedRegex(@"^SCRAM-SHA-256\$[0-9]+:[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+:[A-Za-z0-9+/=]+$")]
    private static partial Regex VerifierShape();
}

/// <summary>PostgreSQL's SCRAM-SHA-256 verifier (what pg_authid stores), so the plaintext never leaves the service.</summary>
public static class ScramVerifier
{
    public const int DefaultIterations = 4096;

    /// <summary>
    /// SCRAM-SHA-256$&lt;iterations&gt;:&lt;salt&gt;$&lt;StoredKey&gt;:&lt;ServerKey&gt; (RFC 7677; PostgreSQL's
    /// scram_build_secret). The password is used as UTF-8; ERPClaw's passwords are base64url, which SASLprep leaves as is.
    /// </summary>
    public static string Compute(string password, byte[] salt, int iterations = DefaultIterations)
    {
        var salted = Rfc2898DeriveBytes.Pbkdf2(Encoding.UTF8.GetBytes(password), salt, iterations, HashAlgorithmName.SHA256, 32);
        var clientKey = HMACSHA256.HashData(salted, "Client Key"u8);
        var serverKey = HMACSHA256.HashData(salted, "Server Key"u8);
        var storedKey = SHA256.HashData(clientKey);
        CryptographicOperations.ZeroMemory(salted);
        return $"SCRAM-SHA-256${iterations}:{Convert.ToBase64String(salt)}${Convert.ToBase64String(storedKey)}:{Convert.ToBase64String(serverKey)}";
    }

    /// <summary>With a fresh random 16-byte salt.</summary>
    public static string Create(string password) => Compute(password, RandomNumberGenerator.GetBytes(16));

    /// <summary>A new random password: 32 bytes from the secure source, base64url.</summary>
    public static Secret NewPassword() =>
        new(Convert.ToBase64String(RandomNumberGenerator.GetBytes(32)).TrimEnd('=').Replace('+', '-').Replace('/', '_'));
}

/// <summary>
/// A path a child opens once to read a secret, served once, leaving no file. Windows (still to write): a named pipe
/// <c>\\.\pipe\erpclaw-initdb-&lt;128 random bits&gt;</c> created once with FILE_FLAG_FIRST_PIPE_INSTANCE,
/// PIPE_REJECT_REMOTE_CLIENTS and a protected owner-only DACL, outbound only, one instance, one client, closed after
/// one write.
/// </summary>
public interface IOneShotSecretChannel : IAsyncDisposable
{
    string Path { get; }

    /// <summary>Waits for one reader, writes <paramref name="content"/>, closes. Cancelled when the reader never comes.</summary>
    Task ServeOnceAsync(ReadOnlyMemory<byte> content, CancellationToken ct);
}

public interface IOneShotSecretChannelFactory
{
    IOneShotSecretChannel Create();
}

public enum ClusterInitResult
{
    Initialised,
    AlreadyInitialised,
    Failed,
}

/// <summary>
/// The once-only set-up of books\ (2.2): two random passwords to Credential Manager, initdb with the owner's SCRAM
/// verifier on a one-shot channel as its --pwfile, the managed settings and pg_hba.conf, then single-user mode for the
/// runtime role and the first database.
/// </summary>
public sealed class ClusterInitializer
{
    /// <summary>Proposed completion marker, distinct from initdb's PG_VERSION.</summary>
    public const string SetupCompleteFileName = "erpclaw-setup-complete";

    private readonly ServiceCommands _commands;
    private readonly IProcessLauncher _launcher;
    private readonly IOneShotSecretChannelFactory _channels;
    private readonly ICredentialStore _credentials;
    private readonly SecretRegistry _secrets;
    private readonly ServiceLog _log;

    /// <summary>The last failed child step for assertions and the host; cleared at the start of each attempt.</summary>
    public PostgresStepFailure? LastFailure { get; private set; }

    public ClusterInitializer(ServiceCommands commands, IProcessLauncher launcher, IOneShotSecretChannelFactory channels,
        ICredentialStore credentials, SecretRegistry secrets, ServiceLog log)
    {
        _commands = commands;
        _launcher = launcher;
        _channels = channels;
        _credentials = credentials;
        _secrets = secrets;
        _log = log;
    }

    /// <summary>The one-shot channel, or null when no pipe can be made (the fallback then sets the verifier in single-user mode).</summary>
    private IOneShotSecretChannel? TryCreateChannel()
    {
        try
        {
            return _channels.Create();
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException or NotSupportedException or PlatformNotSupportedException)
        {
            return null;
        }
    }

    public async Task<ClusterInitResult> InitialiseAsync(CancellationToken ct)
    {
        LastFailure = null;
        var books = _commands.Paths.Books;
        var hasCluster = File.Exists(Path.Combine(books, "PG_VERSION"));
        var complete = Path.Combine(books, SetupCompleteFileName);
        if (hasCluster && File.Exists(complete)) return ClusterInitResult.AlreadyInitialised;

        // Never rotate credentials on a partial cluster: initdb or an earlier single-user step may have used them.
        var owner = hasCluster ? _credentials.Read(CredentialTargets.DatabaseOwner) : ScramVerifier.NewPassword();
        var runtime = hasCluster ? _credentials.Read(CredentialTargets.DatabaseRuntime) : ScramVerifier.NewPassword();
        if (owner is null || runtime is null) return ClusterInitResult.Failed;
        _secrets.Add(owner);
        _secrets.Add(runtime);
        if (!hasCluster)
        {
            // Both passwords go to the credential store first, so a crash after initdb never loses one.
            _credentials.Write(CredentialTargets.DatabaseOwner, owner);
            _credentials.Write(CredentialTargets.DatabaseRuntime, runtime);
            if (File.Exists(complete)) File.Delete(complete);
        }

        // initdb reads the owner's verifier (never the password) once from the channel; PostgreSQL stores a
        // pre-computed SCRAM verifier as it is.
        var ownerVerifier = ScramVerifier.Create(owner.Reveal());
        bool viaPwFile = false;
        if (!hasCluster)
        {
            int initdbExit;
            IOneShotSecretChannel? channel = TryCreateChannel();
            viaPwFile = channel is not null;
            if (channel is null)
            {
                // No pipe is possible: initdb rejects every login, and the verifier is set in single-user mode below.
                initdbExit = await StepExitAsync("initdb", _launcher.Start(_commands.InitDb(null)), ct).ConfigureAwait(false);
            }
            else
            {
                await using var _ = channel;
                using var stop = CancellationTokenSource.CreateLinkedTokenSource(ct);
                var serve = channel.ServeOnceAsync(Encoding.UTF8.GetBytes(ownerVerifier + "\n"), stop.Token);
                try
                {
                    var initdb = _launcher.Start(_commands.InitDb(channel.Path));
                    initdbExit = await StepExitAsync("initdb", initdb, ct).ConfigureAwait(false);
                }
                finally
                {
                    stop.Cancel(); // an initdb that never read the channel leaves the server waiting
                }
                try
                {
                    await serve.ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                    if (initdbExit == 0) return ClusterInitResult.Failed; // it never read its password
                }
            }
            if (initdbExit != 0) return ClusterInitResult.Failed;
        }

        // Single-user PostgreSQL loads required includes too. Create the target first, including on recovery from
        // an older interrupted setup. No listener starts here; the service rewrites the port before server startup.
        new PostgresConfigFiles(_commands.Paths).WriteManagedSettings(5432);
        var config = Path.Combine(books, "postgresql.conf");
        if (!File.ReadAllLines(config).Any(line => line.Trim() == PostgresConfig.IncludeLine))
            File.AppendAllText(config, "\n" + PostgresConfig.IncludeLine + "\n");
        File.WriteAllText(Path.Combine(books, "pg_hba.conf"), PostgresConfig.Hba(_commands.Roles));

        // On recovery always restore both verifiers: we cannot know which statements ran before interruption.
        var statements = PostgresConfig.SingleUserSetup(_commands.Roles, ScramVerifier.Create(runtime.Reveal()),
            viaPwFile ? null : ownerVerifier);
        if (!await SingleUserAsync(statements, ct).ConfigureAwait(false)) return ClusterInitResult.Failed;
        if (!await SingleUserAsync(PostgresConfig.SingleUserDatabaseCheck(_commands.Roles), ct).ConfigureAwait(false))
        {
            if (!await SingleUserAsync(PostgresConfig.SingleUserCreateDatabase(_commands.Roles), ct).ConfigureAwait(false))
                return ClusterInitResult.Failed;
            if (!await SingleUserAsync(PostgresConfig.SingleUserDatabaseCheck(_commands.Roles), ct).ConfigureAwait(false))
                return ClusterInitResult.Failed;
        }

        // Publish only after every step succeeds. A crash before the rename simply replays the idempotent setup.
        await File.WriteAllTextAsync(complete + ".tmp", "1\n", ct).ConfigureAwait(false);
        File.Move(complete + ".tmp", complete, overwrite: true);
        LastFailure = null;
        return ClusterInitResult.Initialised;
    }

    private async Task<bool> SingleUserAsync(string statements, CancellationToken ct)
    {
        var child = _launcher.Start(_commands.SingleUser(Encoding.UTF8.GetBytes(statements)));
        return await StepExitAsync("single-user", child, ct).ConfigureAwait(false) == 0;
    }

    private async Task<int> StepExitAsync(string step, IChildProcess child, CancellationToken ct)
    {
        var exit = await ExitAsync(child, ct).ConfigureAwait(false);
        if (exit != 0)
        {
            var filtered = PostgresDiagnosticFilter.Filter(child.ErrorDiagnostic, secrets: _secrets);
            var diagnostic = string.Join('\n', filtered.Split('\n', StringSplitOptions.RemoveEmptyEntries)
                .Where(line => !_log.WouldRefuse(line)));
            LastFailure = new PostgresStepFailure(step, exit, diagnostic);
            _log.Write($"PostgreSQL {step} exited {exit}.");
            foreach (var line in diagnostic.Split('\n', StringSplitOptions.RemoveEmptyEntries))
                _log.Write(line);
        }
        return exit;
    }

    private static async Task<int> ExitAsync(IChildProcess child, CancellationToken ct)
    {
        try
        {
            return await child.Exited.WaitAsync(ct).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            child.Kill();
            throw;
        }
    }
}

/// <summary>Writes the managed settings for a port before each PostgreSQL start.</summary>
public interface IPostgresConfigWriter
{
    void WriteManagedSettings(int port);
}

public sealed class PostgresConfigFiles : IPostgresConfigWriter
{
    private readonly ServicePaths _paths;

    public PostgresConfigFiles(ServicePaths paths) => _paths = paths;

    public void WriteManagedSettings(int port)
    {
        Directory.CreateDirectory(_paths.PostgresLogs);
        Directory.CreateDirectory(_paths.Books);
        var file = Path.Combine(_paths.Books, PostgresConfig.ManagedFileName);
        File.WriteAllText(file + ".tmp", PostgresConfig.ManagedSettings(port, _paths.PostgresLogs));
        File.Move(file + ".tmp", file, overwrite: true);
    }
}
