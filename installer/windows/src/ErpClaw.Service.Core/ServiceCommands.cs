// The commands the service runs, as data (SERVICE_COMMANDS r1b; INSTALLER 2.2, 2.3). The engine's real entry
// points plug in by changing EnginePrefix; every argv shape here is the contract's.
namespace ErpClaw.Service.Core;

/// <summary>The program folder (read-only) and the data folder (%USERPROFILE%\ERPClaw).</summary>
public sealed record ServicePaths(string ProgramFolder, string DataFolder)
{
    public string Books => Path.Combine(DataFolder, "books");
    public string Run => Path.Combine(DataFolder, "run");
    public string Logs => Path.Combine(DataFolder, "logs");
    public string PostgresLogs => Path.Combine(Logs, "postgresql");
    public string PostgresBin => Path.Combine(ProgramFolder, "postgresql", "bin");
    public string Python => Path.Combine(ProgramFolder, "python", "python.exe");
}

public enum BackupKind
{
    Daily,
    Manual,
    BeforeUpdate,
    BeforeRestore,
}

public sealed record ServiceCommands(ServicePaths Paths, IReadOnlyList<string> EnginePrefix, DatabaseRoles Roles)
{
    /// <summary>The proposed engine entry: the bundled, PSF-signed python.exe, isolated (-I), running erpclaw's main.</summary>
    public static ServiceCommands Proposed(ServicePaths paths, DatabaseRoles? roles = null) =>
        new(paths, new[] { paths.Python, "-I", "-m", "erpclaw_cli.main" }, roles ?? new DatabaseRoles());

    public static string KindName(BackupKind kind) => kind switch
    {
        BackupKind.Daily => "daily",
        BackupKind.Manual => "manual",
        BackupKind.BeforeUpdate => "before-update",
        BackupKind.BeforeRestore => "before-restore",
        _ => throw new ArgumentOutOfRangeException(nameof(kind)),
    };

    // PostgreSQL (INSTALLER 2.2, 2.3 item 1): postgres.exe started directly, in the job; never pg_ctl start.
    public ProcessSpec Postgres() => Pg("postgres.exe", "-D", Paths.Books) with { CaptureErrorDiagnostic = true };

    public ProcessSpec PgCtlStop() => Pg("pg_ctl.exe", "stop", "-D", Paths.Books, "-m", "fast");

    public ProcessSpec InitDb(string? pwFilePath) =>
        Pg("initdb.exe", PostgresConfig.InitDbArguments(Paths.Books, Roles, pwFilePath).ToArray()) with { CaptureErrorDiagnostic = true };

    /// <summary>Single-user mode on the "postgres" database, one statement per stdin line, stopping at the first error.</summary>
    public ProcessSpec SingleUser(ReadOnlyMemory<byte> statements) =>
        Pg("postgres.exe", "--single", "-D", Paths.Books, "-F", "-c", "exit_on_error=on", "postgres") with { StandardInput = statements, CaptureErrorDiagnostic = true };

    // The engine (proposed shapes, still to be confirmed).
    public ProcessSpec Serve() => Engine(capture: false, "serve", "--release", "--data-folder", Paths.DataFolder);

    public ProcessSpec Bootstrap() => Engine(capture: true, "init", "--own-books", "--data-folder", Paths.DataFolder);

    public ProcessSpec DbCheck() => Service("db-check");

    public ProcessSpec MintCode(string principal, string runId) =>
        Service("mint-code", "--principal", Value(principal), "--run-id", Value(runId));

    // SERVICE_COMMANDS r1b section 2, exactly.
    public ProcessSpec Backup(BackupKind kind, string? copyTo = null) => copyTo is null
        ? Service("backup", "--kind", KindName(kind))
        : Service("backup", "--kind", KindName(kind), "--copy-to", Value(copyTo));

    public ProcessSpec ListBackups() => Service("list-backups");

    public ProcessSpec RestoreStage(string backupFile) => Service("restore-stage", "--backup", Value(backupFile));

    public ProcessSpec Switch(string database) => Service("switch", "--to", Value(database));

    public ProcessSpec RemoveReplaced(string database) => Service("remove-replaced", "--name", Value(database));

    public ProcessSpec UpdateStep() => Service("update-step");

    public ProcessSpec Pointer() => Service("pointer");

    /// <summary><c>erpclaw service &lt;command&gt; --data-folder P ...</c>: the only path passed is the data folder's root.</summary>
    private ProcessSpec Service(string command, params string[] rest) =>
        Engine(capture: true, new[] { "service", command, "--data-folder", Paths.DataFolder }.Concat(rest).ToArray());

    private ProcessSpec Engine(bool capture, params string[] args) =>
        new(EnginePrefix[0], EnginePrefix.Skip(1).Concat(args).ToArray())
        {
            WorkingDirectory = Paths.DataFolder,
            CaptureOutput = capture,
        };

    private ProcessSpec Pg(string exe, params string[] args) =>
        new(Path.Combine(Paths.PostgresBin, exe), args) { WorkingDirectory = Paths.DataFolder };

    /// <summary>A value argument: not empty, not readable as an option, no control characters.</summary>
    private static string Value(string value)
    {
        if (string.IsNullOrEmpty(value) || value[0] == '-' || value.Any(char.IsControl))
            throw new ArgumentException("That value cannot be passed to a command.", nameof(value));
        return value;
    }
}
