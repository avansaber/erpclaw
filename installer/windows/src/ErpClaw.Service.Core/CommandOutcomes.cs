// An engine command's result: its exit code and its one JSON object on stdout (SERVICE_COMMANDS r1b section 2).
using System.Text.Json;

namespace ErpClaw.Service.Core;

/// <summary>The contract's exit codes, plus Malformed for output that is not exactly one JSON object.</summary>
public enum OutcomeKind
{
    Done = 0,
    Internal = 1,
    Refused = 2,
    CouldNotRun = 3,
    FailedUnchanged = 4,
    Malformed = 5,
}

/// <summary>
/// One command's outcome. <see cref="Body"/> may hold a sign-in code (mint-code), so <see cref="ToString"/> shows only
/// the kind and the reason code.
/// </summary>
public sealed record CommandOutcome(OutcomeKind Kind, int ExitCode, JsonElement? Body, string? Reason, string Text)
{
    public bool Succeeded => Kind == OutcomeKind.Done;

    public override string ToString() => $"CommandOutcome {{ Kind = {Kind}, ExitCode = {ExitCode}, Reason = {Reason ?? "-"} }}";
}

public static class CommandOutcomes
{
    public const string InternalText = "ERPClaw hit an internal error";

    /// <summary>Maps the exit code and stdout to a plain outcome. Never copies stdout into any text.</summary>
    public static CommandOutcome Parse(int exitCode, string stdout)
    {
        var body = OneObject(stdout);
        var kind = exitCode switch
        {
            0 => OutcomeKind.Done,
            2 => OutcomeKind.Refused,
            3 => OutcomeKind.CouldNotRun,
            4 => OutcomeKind.FailedUnchanged,
            _ => OutcomeKind.Internal,
        };
        if (body is null && kind != OutcomeKind.Internal) kind = OutcomeKind.Malformed;
        var reason = body is { } b && b.TryGetProperty("reason", out var r) && r.ValueKind == JsonValueKind.String ? r.GetString() : null;
        var text = body is { } t && t.TryGetProperty("text", out var x) && x.ValueKind == JsonValueKind.String ? x.GetString() : null;
        text = kind switch
        {
            OutcomeKind.Done => "",
            OutcomeKind.Internal or OutcomeKind.Malformed => InternalText,
            OutcomeKind.Refused => Plain(text, "ERPClaw could not do that now."),
            OutcomeKind.CouldNotRun => Plain(text, "ERPClaw could not do that just now. Try again later."),
            _ => Plain(text, "That did not work, and nothing was changed."),
        };
        return new CommandOutcome(kind, exitCode, body, reason, text);
    }

    private static string Plain(string? text, string fallback) => string.IsNullOrWhiteSpace(text) ? fallback : text;

    /// <summary>Exactly one JSON object, surrounded by nothing but white space; else null.</summary>
    private static JsonElement? OneObject(string stdout)
    {
        try
        {
            var reader = new Utf8JsonReader(System.Text.Encoding.UTF8.GetBytes(stdout), new JsonReaderOptions { AllowMultipleValues = false });
            using var doc = JsonDocument.ParseValue(ref reader);
            if (doc.RootElement.ValueKind != JsonValueKind.Object) return null;
            if (reader.Read()) return null; // anything after the object
            return doc.RootElement.Clone();
        }
        catch (JsonException)
        {
            return null;
        }
    }
}

public interface ICommandRunner
{
    Task<CommandOutcome> RunAsync(ProcessSpec spec, CancellationToken ct);
}

/// <summary>Runs a command as a child (in the job, through the launcher) and parses its outcome.</summary>
public sealed class CommandRunner : ICommandRunner
{
    private readonly IProcessLauncher _launcher;

    public CommandRunner(IProcessLauncher launcher) => _launcher = launcher;

    public async Task<CommandOutcome> RunAsync(ProcessSpec spec, CancellationToken ct)
    {
        ct.ThrowIfCancellationRequested();
        IChildProcess child;
        try
        {
            child = _launcher.Start(spec with { CaptureOutput = true });
        }
        catch (Exception e) when (e is System.ComponentModel.Win32Exception or IOException or UnauthorizedAccessException)
        {
            // A launch failure has no command stdout. Never expose the exception's paths or other private details.
            return new CommandOutcome(OutcomeKind.CouldNotRun, 3, null, "launch_failed",
                "ERPClaw could not do that just now. Try again later.");
        }
        int exit;
        try
        {
            exit = await child.Exited.WaitAsync(ct).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            child.Kill();
            throw;
        }
        return CommandOutcomes.Parse(exit, child.StandardOutput);
    }
}
