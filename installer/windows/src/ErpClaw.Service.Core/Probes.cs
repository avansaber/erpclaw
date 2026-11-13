// What the supervisor asks between steps: can the owner log in, do books exist, has the engine written its run file,
// and the sign-in code minter. Each has a command-based implementation over the engine's CLI.
using System.Text.Json;
using System.Text.RegularExpressions;

namespace ErpClaw.Service.Core;

public interface IReadinessProbe
{
    /// <summary>True when PostgreSQL on 127.0.0.1:<paramref name="port"/> accepts the owner's login.</summary>
    Task<bool> CanLoginAsync(int port, CancellationToken ct);
}

public enum BooksState
{
    Present,
    Absent,
    Unknown,
}

public interface IBooksProbe
{
    Task<BooksState> CheckAsync(CancellationToken ct);
}

/// <summary>Both probes by the proposed <c>erpclaw service db-check</c>: exit 0 with <c>{"login": true, "books": bool}</c>.</summary>
public sealed class DbCheckProbe : IReadinessProbe, IBooksProbe
{
    private readonly ICommandRunner _runner;
    private readonly ServiceCommands _commands;

    public DbCheckProbe(ICommandRunner runner, ServiceCommands commands)
    {
        _runner = runner;
        _commands = commands;
    }

    /// <summary>The command finds the port in the run folder's record, which the supervisor wrote before probing.</summary>
    public async Task<bool> CanLoginAsync(int port, CancellationToken ct) => Bool(await Check(ct), "login") == true;

    public async Task<BooksState> CheckAsync(CancellationToken ct)
    {
        var outcome = await Check(ct);
        if (Bool(outcome, "login") != true) return BooksState.Unknown;
        return Bool(outcome, "books") switch
        {
            true => BooksState.Present,
            false => BooksState.Absent,
            null => BooksState.Unknown,
        };
    }

    private Task<CommandOutcome> Check(CancellationToken ct) => _runner.RunAsync(_commands.DbCheck(), ct);

    private static bool? Bool(CommandOutcome outcome, string name) =>
        outcome.Succeeded && outcome.Body is { } body && body.TryGetProperty(name, out var v) && v.ValueKind is JsonValueKind.True or JsonValueKind.False
            ? v.GetBoolean()
            : null;
}

/// <summary>The engine's run file (DESKTOP_HOST 2.3): its run id, the field the service needs.</summary>
public sealed record RunInfo(string RunId)
{
    public override string ToString() => "RunInfo";
}

public interface IRunFileWatcher
{
    /// <summary>
    /// Waits for a run file written by <paramref name="engine"/> (newer than its start, owner-only ACL); null when the
    /// engine exits first or the watcher's own timeout passes.
    /// </summary>
    Task<RunInfo?> WaitForRunAsync(IChildProcess engine, CancellationToken ct);
}

public enum MintStatus
{
    Minted,
    UnknownPrincipal,
    Failed,
}

public sealed record MintResult(MintStatus Status, Secret? Code, string Text, int? ExitCode = null)
{
    public override string ToString() => $"MintResult {{ Status = {Status} }}";
}

public interface ICodeMinter
{
    Task<MintResult> MintAsync(string principal, string runId, CancellationToken ct);
}

/// <summary>Mints by the proposed <c>erpclaw service mint-code</c> (core's one mint function), the code on its stdout.</summary>
public sealed partial class CommandCodeMinter : ICodeMinter
{
    public const string UnknownPrincipalReason = "unknown_principal";
    private const string FailedText = "ERPClaw could not make a sign-in code just now. Try again.";

    private readonly ICommandRunner _runner;
    private readonly ServiceCommands _commands;

    public CommandCodeMinter(ICommandRunner runner, ServiceCommands commands)
    {
        _runner = runner;
        _commands = commands;
    }

    public async Task<MintResult> MintAsync(string principal, string runId, CancellationToken ct)
    {
        var outcome = await _runner.RunAsync(_commands.MintCode(principal, runId), ct).ConfigureAwait(false);
        if (outcome.Kind == OutcomeKind.Refused && outcome.Reason == UnknownPrincipalReason)
            return new MintResult(MintStatus.UnknownPrincipal, null, outcome.Text, outcome.ExitCode);
        if (outcome.Succeeded && outcome.Body is { } body && body.TryGetProperty("code", out var c)
            && c.ValueKind == JsonValueKind.String && c.GetString() is { } code && CodeShape().IsMatch(code))
            return new MintResult(MintStatus.Minted, new Secret(code), "", outcome.ExitCode);
        // Never the output itself: it may hold a code of the wrong shape.
        return new MintResult(MintStatus.Failed, null, outcome.Succeeded ? FailedText : outcome.Text, outcome.ExitCode);
    }

    /// <summary>128 bits, base32 without padding: 26 characters (BROWSER_AUTH 2.1).</summary>
    [GeneratedRegex("^[A-Za-z2-7]{26}$")]
    private static partial Regex CodeShape();
}
