// Time and waiting, injectable so the supervisor's back-off and timeouts (INSTALLER 2.3 item 4) are testable.
namespace ErpClaw.Service.Core;

/// <summary>The service's own clock (the service does not reference the app repo's host seams).</summary>
public interface IClock
{
    DateTimeOffset Now { get; }
}

public sealed class SystemClock : IClock
{
    public DateTimeOffset Now => DateTimeOffset.UtcNow;
}

/// <summary>Waiting, separate from the clock so a test can make a 60-second back-off pass at once.</summary>
public interface IScheduler
{
    Task Delay(TimeSpan delay, CancellationToken ct);
}

/// <summary>Tells a running engine to stop by itself, by the named event it waits on.</summary>
public interface IEngineStopSignal
{
    /// <summary>Signals the event; false when it cannot be opened.</summary>
    bool Signal(string eventName);
}

public static class StopEvents
{
    public static string NameFor(string runId) => "Local\\erpclaw-stop-" + runId;
}

/// <summary>Opens the engine's named event (owner-only) and sets it.</summary>
public sealed class NamedEventStopSignal : IEngineStopSignal
{
    public bool Signal(string eventName)
    {
        if (!OperatingSystem.IsWindows()) return false;
        try
        {
            using var handle = EventWaitHandleAcl.OpenExisting(eventName, System.Security.AccessControl.EventWaitHandleRights.Modify);
            return handle.Set();
        }
        catch (Exception e) when (e is WaitHandleCannotBeOpenedException or UnauthorizedAccessException or IOException)
        {
            return false;
        }
    }
}

public sealed class SystemScheduler : IScheduler
{
    public Task Delay(TimeSpan delay, CancellationToken ct) => Task.Delay(delay, ct);
}
