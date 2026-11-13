// Child processes: the service starts PostgreSQL, the engine and the engine's commands, each in its kill-on-close job.
namespace ErpClaw.Service.Core;

/// <summary>
/// One child to start: an executable and its arguments, never a secret among them (the guard checks). A child that
/// needs something secret gets it on <see cref="StandardInput"/>, written once and closed (in v3.0 only PostgreSQL's
/// single-user mode, and only SCRAM verifiers); the engine's commands read the database authority themselves
/// (SERVICE_COMMANDS r1b section 2).
/// </summary>
public sealed record ProcessSpec(string Executable, IReadOnlyList<string> Arguments)
{
    public string? WorkingDirectory { get; init; }

    /// <summary>Written to the child's stdin, then stdin is closed. Empty: stdin is closed at once.</summary>
    public ReadOnlyMemory<byte> StandardInput { get; init; }

    /// <summary>Collect stdout (an engine command's one JSON object).</summary>
    public bool CaptureOutput { get; init; }

    /// <summary>Drain stderr through PostgresDiagnosticFilter; never retain raw stderr or echo stdin.</summary>
    public bool CaptureErrorDiagnostic { get; init; }

    /// <summary>The executable and its arguments; never stdin.</summary>
    public override string ToString() =>
        $"ProcessSpec {{ {WindowsCommandLine.Build(Executable, Arguments)}{(StandardInput.IsEmpty ? "" : " (stdin withheld)")} }}";
}

/// <summary>A started child.</summary>
public interface IChildProcess
{
    int Id { get; }

    /// <summary>The process's creation time, as Windows reports it (recorded with the PID, 2.3 item 5).</summary>
    DateTimeOffset StartTime { get; }

    /// <summary>Completes with the exit code when the process has exited.</summary>
    Task<int> Exited { get; }

    /// <summary>Stdout, complete once <see cref="Exited"/> has completed; empty unless captured.</summary>
    string StandardOutput { get; }

    /// <summary>Filtered stderr, complete once Exited completes; empty unless CaptureErrorDiagnostic was requested.</summary>
    string ErrorDiagnostic { get; }

    void Kill();
}

/// <summary>
/// Starts children. The Windows implementation (still to write) calls CreateProcess with
/// EXTENDED_STARTUPINFO_PRESENT and PROC_THREAD_ATTRIBUTE_JOB_LIST naming the service's kill-on-close job, so each
/// child is in the job from its first instruction; it never creates a process suspended and never resumes a thread
/// (INSTALLER 2.3a item 1; the lint in the tests refuses both names).
/// </summary>
public interface IProcessLauncher
{
    IChildProcess Start(ProcessSpec spec);
}

/// <summary>Processes by PID, for the leftovers sweep (2.3 item 5). Windows: OpenProcess, GetProcessTimes, TerminateProcess.</summary>
public interface IProcessInspector
{
    /// <summary>The live process's creation time, or null when no process has that PID.</summary>
    DateTimeOffset? StartTimeOf(int pid);

    void Kill(int pid);
}

/// <summary>Refuses to start any child whose command line holds a registered secret.</summary>
public sealed class GuardedLauncher : IProcessLauncher
{
    private readonly IProcessLauncher _inner;
    private readonly SecretRegistry _secrets;

    public GuardedLauncher(IProcessLauncher inner, SecretRegistry secrets)
    {
        _inner = inner;
        _secrets = secrets;
    }

    public IChildProcess Start(ProcessSpec spec)
    {
        if (_secrets.HoldsSecret(WindowsCommandLine.Build(spec.Executable, spec.Arguments))
            || (spec.WorkingDirectory is { } dir && _secrets.HoldsSecret(dir)))
            throw new InvalidOperationException("A child's command line held a secret; it was not started.");
        return _inner.Start(spec);
    }
}

/// <summary>Builds a Windows command line from arguments, quoted as CommandLineToArgvW and the C runtime read them.</summary>
public static class WindowsCommandLine
{
    public static string Build(string executable, IEnumerable<string> arguments) =>
        string.Join(' ', new[] { Quote(executable) }.Concat(arguments.Select(Quote)));

    public static string Quote(string argument)
    {
        if (argument.Length > 0 && argument.IndexOfAny(new[] { ' ', '\t', '\n', '\v', '"' }) < 0) return argument;
        var sb = new System.Text.StringBuilder("\"");
        int backslashes = 0;
        foreach (char c in argument)
        {
            if (c == '\\')
            {
                backslashes++;
                continue;
            }
            // Backslashes before a quote are doubled, and the quote escaped.
            sb.Append('\\', c == '"' ? backslashes * 2 + 1 : backslashes);
            backslashes = 0;
            sb.Append(c);
        }
        // Backslashes before the closing quote are doubled.
        sb.Append('\\', backslashes * 2);
        return sb.Append('"').ToString();
    }
}
