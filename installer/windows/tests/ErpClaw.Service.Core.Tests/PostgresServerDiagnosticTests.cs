using ErpClaw.Service.Core;
using Xunit;

namespace ErpClaw.Service.Core.Tests;

public class PostgresServerDiagnosticTests
{
    [Fact]
    public async Task Mutation_server_exits_before_readiness_carries_filtered_stderr_to_reason_and_log()
    {
        var rig = new Rig { Options = new SupervisorOptions { MaxFailures = 1 } };
        rig.Launcher.OnStart = child =>
        {
            if (child.Name != "postgres") return;
            child.CaptureStandardError("LOG: noise\nFATAL: could not open file\nERROR: verifier <placeholder>");
            child.Exit(1);
        };
        rig.Start();
        await rig.WaitState(ServiceState.GaveUp);
        Assert.Contains("FATAL: could not open file", rig.Supervisor.Reason!);
        Assert.Contains(rig.Sink.Lines, line => line.Contains("FATAL: could not open file", StringComparison.Ordinal));
        Assert.DoesNotContain(rig.Sink.Lines, line => line.Contains("verifier", StringComparison.Ordinal));
        Assert.True(rig.Postgres.Spec.CaptureErrorDiagnostic);
        rig.Supervisor.RequestStop();
        await rig.Running!.WaitAsync(TimeSpan.FromSeconds(5));
    }

    [Fact]
    public async Task Mutation_running_server_crashes_carries_filtered_stderr_to_reason_and_log()
    {
        var rig = new Rig { Options = new SupervisorOptions { MaxFailures = 1 } };
        await rig.StartAndWaitRunning();
        rig.Postgres.CaptureStandardError("PANIC: disk failure\nERROR: PASSWORD <placeholder>");
        rig.Postgres.Exit(1);
        await rig.WaitState(ServiceState.GaveUp);
        Assert.Contains("PANIC: disk failure", rig.Supervisor.Reason!);
        Assert.Contains(rig.Sink.Lines, line => line.Contains("PANIC: disk failure", StringComparison.Ordinal));
        Assert.DoesNotContain(rig.Sink.Lines, line => line.Contains("PASSWORD", StringComparison.Ordinal));
        rig.Supervisor.RequestStop();
        await rig.Running!.WaitAsync(TimeSpan.FromSeconds(5));
    }
}
