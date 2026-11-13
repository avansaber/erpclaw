// At its start the service stops any PostgreSQL or engine left by an earlier service (INSTALLER 2.3 item 5).
namespace ErpClaw.Service.Core;

public sealed class LeftoverSweeper
{
    private readonly IProcessInspector _inspector;

    public LeftoverSweeper(IProcessInspector inspector) => _inspector = inspector;

    /// <summary>
    /// Stops each recorded child (the engine first) whose PID is alive with exactly the recorded start time; a PID
    /// alive with another start time is a reused PID and is left alone. Returns the PIDs stopped.
    /// </summary>
    public IReadOnlyList<int> Sweep(ServiceRecord? record)
    {
        var stopped = new List<int>();
        if (record is null) return stopped;
        foreach (var child in new[] { record.Engine, record.Postgres })
        {
            if (child is null) continue;
            if (_inspector.StartTimeOf(child.Pid) is { } started && started == child.StartTime)
            {
                _inspector.Kill(child.Pid);
                stopped.Add(child.Pid);
            }
        }
        return stopped;
    }
}
