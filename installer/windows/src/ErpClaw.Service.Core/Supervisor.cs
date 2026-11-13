// The supervisor (INSTALLER 2.3 items 1, 2, 4, 5, 7; SERVICE_COMMANDS r1b section 2's orders): PostgreSQL, the
// bootstrap, the update step and the engine, their restarts, the restore order, the daily backup and the clean stop.
//
// One event loop does everything that starts or stops a child, one item at a time: the start-up, each child's exit,
// the control pipe's backup_now, restore and remove_replaced, the daily backup, and the stop. A child's exit is acted
// on only while it is still the current PostgreSQL or engine, so a planned stop (a restore, a restart) is never
// counted as a crash. status, mint_code and list_backups answer at once from a snapshot.
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Threading.Channels;

namespace ErpClaw.Service.Core;

public sealed record SupervisorOptions
{
    public TimeSpan ReadinessTimeout { get; init; } = TimeSpan.FromSeconds(60);
    public TimeSpan ReadinessPoll { get; init; } = TimeSpan.FromMilliseconds(500);
    public TimeSpan StopTimeout { get; init; } = TimeSpan.FromSeconds(30);

    /// <summary>How long the engine has to stop after its stop event is signalled, before it is killed.</summary>
    public TimeSpan EngineStopTimeout { get; init; } = TimeSpan.FromSeconds(15);
    public int MaxFailures { get; init; } = 5;
    public TimeSpan FailureWindow { get; init; } = TimeSpan.FromMinutes(10);
    public TimeSpan MaxBackoff { get; init; } = TimeSpan.FromSeconds(60);

    /// <summary>A run this long resets the back-off to one second.</summary>
    public TimeSpan StableRun { get; init; } = TimeSpan.FromMinutes(10);

    /// <summary>remove-replaced is resumable (SERVICE_COMMANDS r1b): its exit 4 with "partial" is tried again,
    /// up to this many runs in all, with the back-off between them.</summary>
    public int RemoveReplacedAttempts { get; init; } = 3;
}

/// <summary>Everything the supervisor drives, each behind an interface.</summary>
public sealed record SupervisorParts(
    ServiceCommands Commands,
    IProcessLauncher Launcher,
    ICommandRunner Runner,
    IReadinessProbe Readiness,
    IBooksProbe Books,
    IRunFileWatcher RunFiles,
    ICodeMinter Minter,
    IPortProbe Ports,
    IPostgresConfigWriter Config,
    IRunRecordStore Records,
    IProcessInspector Inspector,
    IClock Clock,
    IScheduler Scheduler,
    ServiceLog Log,
    ServiceSession Session,
    IServiceFileStore ServiceFile,
    IEngineStopSignal StopSignal);

/// <summary>The plain sentences the host shows. No port, PID, path or secret in any of them.</summary>
public static class SupervisorReasons
{
    public const string PostgresStopped = "ERPClaw's database stopped unexpectedly.";
    public const string PostgresNotReady = "ERPClaw's database did not start.";
    public const string EngineStopped = "ERPClaw stopped unexpectedly.";
    public const string EngineNotStarted = "ERPClaw did not start.";
    public const string BooksUnknown = "ERPClaw could not open your books.";
    public const string BootstrapFailed = "Setting up your books did not finish.";
    public const string Internal = "ERPClaw hit an internal error.";
    public const string GaveUp = "ERPClaw stopped trying after five failures in ten minutes. Restart your computer, or ask whoever set it up for help.";
    public const string Busy = "ERPClaw is starting or busy. Try again in a moment.";
    public const string StaleRun = "ERPClaw restarted since this window checked it. Sign in again.";
    public const string Stopped = "ERPClaw is stopping.";
}

public sealed class Supervisor : IControlHandler
{
    /// <summary>pointer's refusal reason for a fresh install with no books.json yet (proposed, still to be confirmed).</summary>
    public const string FreshInstallPointerReason = "no_pointer";

    private readonly SupervisorParts _p;
    private readonly SupervisorOptions _o;
    private readonly Channel<Func<Task>> _queue = Channel.CreateUnbounded<Func<Task>>(new UnboundedChannelOptions { SingleReader = true });
    private readonly CancellationTokenSource _stop = new();
    private readonly object _sync = new();
    private readonly List<DateTimeOffset> _failures = new();

    private ServiceState _state = ServiceState.Stopped;
    private string? _reason;
    private string? _runId;
    private IChildProcess? _pg;
    private IChildProcess? _engine;
    private int? _port;
    private bool _prepared;
    private bool _freshInstall;
    private bool _finished;
    private int _consecutive;
    private DateTimeOffset? _runningSince;
    private CancellationToken _ct;

    public Supervisor(SupervisorParts parts, SupervisorOptions? options = null)
    {
        _p = parts;
        _o = options ?? new SupervisorOptions();
    }

    /// <summary>Raised each time the engine is ready (start, restart, after a restore); the daily schedule listens.</summary>
    public event EventHandler? EngineReady;

    public ServiceState State
    {
        get { lock (_sync) return _state; }
    }

    /// <summary>The plain reason after giving up; null otherwise.</summary>
    public string? Reason
    {
        get { lock (_sync) return _reason; }
    }

    /// <summary>Runs until a clean stop: the leftovers sweep, then pointer, PostgreSQL, update-step, serve.</summary>
    public async Task RunAsync(CancellationToken ct)
    {
        _ct = ct;
        var earlier = _p.Records.Read();
        var swept = new LeftoverSweeper(_p.Inspector).Sweep(earlier);
        if (swept.Count > 0) Log("Stopped what an earlier service left running.");
        // A file left by a crashed service names a pipe and a secret that no longer exist: replace it now, with
        // this start's own pipe and secret and the port the database will use.
        _p.ServiceFile.Delete();
        _port = PortChoice.Choose(earlier?.DatabasePort, _p.Ports);
        _p.ServiceFile.Write(_p.Session.FileContent(_port.Value));
        SetState(ServiceState.Starting, null);
        _queue.Writer.TryWrite(StartUpAsync);

        try
        {
            await foreach (var work in _queue.Reader.ReadAllAsync(ct).ConfigureAwait(false))
            {
                try
                {
                    await work().ConfigureAwait(false);
                }
                catch (OperationCanceledException) when (ct.IsCancellationRequested)
                {
                    throw;
                }
                catch (Exception e)
                {
                    Log($"The service hit an internal error ({e.GetType().Name}).");
                    await GiveUpAsync(SupervisorReasons.Internal).ConfigureAwait(false);
                }
                if (_finished) break;
            }
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            // The service is ending however it ends; the job ends the children too.
            _engine?.Kill();
            _pg?.Kill();
            _p.ServiceFile.Delete();
        }
    }

    /// <summary>Asks for a clean stop (sign-out, or the host's "Quit ERPClaw"): engine, then PostgreSQL.</summary>
    public void RequestStop()
    {
        _stop.Cancel();
        _queue.Writer.TryWrite(StopAsync);
    }

    /// <summary>Runs the daily backup (engine running); completes with its outcome, or null when not running.</summary>
    public Task<CommandOutcome?> RunDailyBackupAsync() =>
        Enqueue<CommandOutcome?>(async () => State == ServiceState.Running
            ? await _p.Runner.RunAsync(_p.Commands.Backup(BackupKind.Daily), _ct).ConfigureAwait(false)
            : null, null);

    public async Task<ControlResponse> HandleAsync(ControlRequest request, CancellationToken ct)
    {
        try
        {
            switch (request.Op)
            {
                case ControlOps.Status:
                    lock (_sync) return ControlResponse.WithState(JsonNamingPolicy.SnakeCaseLower.ConvertName(_state.ToString()), _reason);
                case ControlOps.MintCode:
                    return await MintAsync(request, ct).ConfigureAwait(false);
                case ControlOps.ListBackups:
                    return ControlResponse.From(await _p.Runner.RunAsync(_p.Commands.ListBackups(), ct).ConfigureAwait(false));
                case ControlOps.BackupNow:
                    return await Enqueue(async () => State == ServiceState.Running
                        ? ControlResponse.From(await _p.Runner.RunAsync(_p.Commands.Backup(BackupKind.Manual), _ct).ConfigureAwait(false))
                        : Unavailable(), Unavailable()).ConfigureAwait(false);
                case ControlOps.Restore:
                    var backup = request.Argument ?? throw new ArgumentException("restore needs a backup");
                    return await Enqueue(() => RestoreAsync(backup), Unavailable()).ConfigureAwait(false);
                case ControlOps.RemoveReplaced:
                    var name = request.Argument ?? throw new ArgumentException("remove_replaced needs a name");
                    return await Enqueue(() => RemoveReplacedAsync(name), Unavailable()).ConfigureAwait(false);
                case ControlOps.Quit:
                    RequestStop();
                    return ControlResponse.Success();
                default:
                    return ControlResponse.Failure(ControlErrors.UnknownOp, "The service does not know that request.");
            }
        }
        catch (ArgumentException)
        {
            return ControlResponse.Failure(ControlErrors.BadRequest, "The request's value cannot be used.");
        }
    }

    // ---- The start-up and the bring-up ----

    private async Task StartUpAsync()
    {
        while (true)
        {
            var pointer = await _p.Runner.RunAsync(_p.Commands.Pointer(), _ct).ConfigureAwait(false);
            if (pointer.Succeeded) break;
            if (pointer.Kind == OutcomeKind.Refused && pointer.Reason == FreshInstallPointerReason)
            {
                _freshInstall = true;
                break;
            }
            if (!Retryable(pointer))
            {
                await GiveUpAsync(pointer.Text).ConfigureAwait(false);
                return;
            }
            if (!await FailedAsync(pointer.Text).ConfigureAwait(false)) return;
        }
        await BringUpAsync(restartDatabase: true).ConfigureAwait(false);
    }

    private sealed record Failure(string Reason, bool Retryable, bool RestartDatabase);

    /// <summary>Brings PostgreSQL (when asked or when it is down) and the engine up, with the back-off between tries.</summary>
    private async Task<bool> BringUpAsync(bool restartDatabase)
    {
        while (true)
        {
            if (_stop.IsCancellationRequested) return false;
            var failure = await BringUpOnceAsync(restartDatabase).ConfigureAwait(false);
            if (failure is null)
            {
                MarkRunning();
                return true;
            }
            if (!failure.Retryable)
            {
                await GiveUpAsync(failure.Reason).ConfigureAwait(false);
                return false;
            }
            if (!await FailedAsync(failure.Reason).ConfigureAwait(false)) return false;
            restartDatabase = failure.RestartDatabase;
        }
    }

    private async Task<Failure?> BringUpOnceAsync(bool restartDatabase)
    {
        if (restartDatabase || _pg is null || _pg.Exited.IsCompleted)
        {
            // The engine never runs against an old address: it stops before PostgreSQL starts again.
            await StopEngineAsync().ConfigureAwait(false);
            await KillPostgresAsync().ConfigureAwait(false);
            int port = PortChoice.Choose(_port, _p.Ports);
            if (port != _port) _p.ServiceFile.Write(_p.Session.FileContent(port));
            _port = port;
            _p.Config.WriteManagedSettings(port);
            var pg = _p.Launcher.Start(_p.Commands.Postgres());
            _pg = pg;
            Watch(pg);
            WriteRecord();
            if (!await WaitReadyAsync(pg, port).ConfigureAwait(false))
            {
                bool exited = pg.Exited.IsCompleted;
                await KillPostgresAsync().ConfigureAwait(false);
                WriteRecord();
                return new Failure(PostgresFailureReason(exited ? SupervisorReasons.PostgresStopped : SupervisorReasons.PostgresNotReady, pg), true, true);
            }
            Log("The database accepted the owner's login.");
        }

        if (!_prepared)
        {
            var books = await _p.Books.CheckAsync(_ct).ConfigureAwait(false);
            if (books == BooksState.Unknown) return new Failure(SupervisorReasons.BooksUnknown, true, PostgresDown());
            if (books == BooksState.Absent)
            {
                // One transaction in the engine: a crash before its commit leaves no books (the next try runs it
                // again), a crash after it leaves books the next check finds.
                Log("No books yet: setting them up.");
                var bootstrap = await _p.Runner.RunAsync(_p.Commands.Bootstrap(), _ct).ConfigureAwait(false);
                if (bootstrap.ExitCode != 0) return new Failure(SupervisorReasons.BootstrapFailed, true, PostgresDown());
            }
            if (_freshInstall)
            {
                // No pointer yet: point at the books just made. switch is the pointer's only writer.
                var pointed = await _p.Runner.RunAsync(_p.Commands.Switch(_p.Commands.Roles.Database), _ct).ConfigureAwait(false);
                if (!pointed.Succeeded) return new Failure(pointed.Text, Retryable(pointed), PostgresDown());
                _freshInstall = false;
            }
            var update = await UpdateStepAsync().ConfigureAwait(false);
            if (update is not null) return update;
            _prepared = true;
        }

        if (PostgresDown()) return new Failure(PostgresFailureReason(SupervisorReasons.PostgresStopped, _pg), true, true);
        await StopEngineAsync().ConfigureAwait(false);
        var engine = _p.Launcher.Start(_p.Commands.Serve());
        _engine = engine;
        Watch(engine);
        WriteRecord();
        var run = await _p.RunFiles.WaitForRunAsync(engine, _ct).ConfigureAwait(false);
        if (run is null)
        {
            await StopEngineAsync().ConfigureAwait(false);
            WriteRecord();
            return new Failure(SupervisorReasons.EngineNotStarted, true, PostgresDown());
        }
        lock (_sync) _runId = run.RunId;
        return null;
    }

    /// <summary>
    /// The update step: exits 2 and 4 stop at once with the command's own text; exits 1 and 3 are run again after
    /// 1, 2, 4 ... seconds up to the longest back-off, and the run after that one stops too. Null: it succeeded.
    /// </summary>
    private async Task<Failure?> UpdateStepAsync()
    {
        bool waitedFullBackoff = false;
        for (int attempt = 1; ; attempt++)
        {
            var update = await _p.Runner.RunAsync(_p.Commands.UpdateStep(), _ct).ConfigureAwait(false);
            if (update.Succeeded) return null;
            if (!Retryable(update) || waitedFullBackoff) return new Failure(update.Text, false, false);
            var wait = Backoff(attempt);
            waitedFullBackoff = wait >= _o.MaxBackoff;
            SetState(ServiceState.Restarting, null);
            Log(update.Text + " Trying again after a pause.");
            try
            {
                await _p.Scheduler.Delay(wait, _stop.Token).ConfigureAwait(false);
            }
            catch (OperationCanceledException) when (_stop.IsCancellationRequested)
            {
                return new Failure(update.Text, true, PostgresDown());
            }
        }
    }

    private async Task<bool> WaitReadyAsync(IChildProcess pg, int port)
    {
        var deadline = _p.Clock.Now + _o.ReadinessTimeout;
        while (true)
        {
            if (pg.Exited.IsCompleted) return false;
            if (await _p.Readiness.CanLoginAsync(port, _ct).ConfigureAwait(false)) return !pg.Exited.IsCompleted;
            if (_p.Clock.Now >= deadline) return false;
            await _p.Scheduler.Delay(_o.ReadinessPoll, _ct).ConfigureAwait(false);
        }
    }

    private void MarkRunning()
    {
        lock (_sync)
        {
            _state = ServiceState.Running;
            _reason = null;
        }
        _runningSince = _p.Clock.Now;
        WriteRecord();
        Log("ERPClaw is running.");
        try
        {
            EngineReady?.Invoke(this, EventArgs.Empty);
        }
        catch (Exception e)
        {
            Log($"A ready listener failed ({e.GetType().Name}).");
        }
    }

    // ---- Failures, the back-off and giving up ----

    /// <summary>Counts a failure; gives up after too many in the window, else waits the back-off. False: stop trying.</summary>
    private async Task<bool> FailedAsync(string reason)
    {
        var now = _p.Clock.Now;
        if (_runningSince is { } since && now - since >= _o.StableRun) _consecutive = 0;
        _runningSince = null;
        _failures.Add(now);
        _failures.RemoveAll(t => now - t >= _o.FailureWindow);
        if (_failures.Count >= _o.MaxFailures)
        {
            await GiveUpAsync(reason + " " + SupervisorReasons.GaveUp).ConfigureAwait(false);
            return false;
        }
        _consecutive++;
        SetState(ServiceState.Restarting, null);
        Log(reason + " Trying again after a pause.");
        try
        {
            await _p.Scheduler.Delay(Backoff(_consecutive), _stop.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (_stop.IsCancellationRequested)
        {
            return false;
        }
        return true;
    }

    private TimeSpan Backoff(int failures) =>
        TimeSpan.FromSeconds(Math.Min(Math.Pow(2, failures - 1), _o.MaxBackoff.TotalSeconds));

    private async Task GiveUpAsync(string reason)
    {
        await StopEngineAsync().ConfigureAwait(false);
        await StopPostgresCleanlyAsync().ConfigureAwait(false);
        lock (_sync) _runId = null;
        SetState(ServiceState.GaveUp, reason);
        Log("ERPClaw stopped trying: " + reason);
    }

    private string PostgresFailureReason(string reason, IChildProcess? child)
    {
        if (child is null) return reason;
        var diagnostic = PostgresDiagnosticFilter.Filter(child.ErrorDiagnostic);
        // Apply the log's additional port/connection guard before the same text reaches a failure result.
        var lines = diagnostic.Split('\n', StringSplitOptions.RemoveEmptyEntries).Where(line => !_p.Log.WouldRefuse(line));
        var safe = string.Join('\n', lines);
        return safe.Length == 0 ? reason : reason + "\n" + safe;
    }

    private static bool Retryable(CommandOutcome outcome) =>
        outcome.Kind is OutcomeKind.CouldNotRun or OutcomeKind.Internal or OutcomeKind.Malformed;

    // ---- Children ----

    private void Watch(IChildProcess child) =>
        _ = child.Exited.ContinueWith(_ => _queue.Writer.TryWrite(() => OnExitedAsync(child)), CancellationToken.None,
            TaskContinuationOptions.ExecuteSynchronously, TaskScheduler.Default);

    /// <summary>Acts only on the current PostgreSQL or engine; a child stopped on purpose is no longer current.</summary>
    private async Task OnExitedAsync(IChildProcess child)
    {
        if (child == _pg)
        {
            _pg = null;
            WriteRecord();
            if (await FailedAsync(PostgresFailureReason(SupervisorReasons.PostgresStopped, child)).ConfigureAwait(false))
                await BringUpAsync(restartDatabase: true).ConfigureAwait(false);
        }
        else if (child == _engine)
        {
            _engine = null;
            WriteRecord();
            if (await FailedAsync(SupervisorReasons.EngineStopped).ConfigureAwait(false))
                await BringUpAsync(restartDatabase: false).ConfigureAwait(false);
        }
    }

    private bool PostgresDown() => _pg is null || _pg.Exited.IsCompleted;

    private async Task StopEngineAsync()
    {
        var engine = _engine;
        string? runId;
        lock (_sync)
        {
            runId = _runId;
            _runId = null;
        }
        _engine = null;
        if (engine is null || engine.Exited.IsCompleted) return;
        // Gracefully first: the engine waits on a named event for its run and stops by itself. It is killed when
        // it has no run yet, when the event cannot be opened, or when it has not exited in time.
        if (runId is not null && _p.StopSignal.Signal(StopEvents.NameFor(runId))
            && await ExitsWithinAsync(engine, _o.EngineStopTimeout).ConfigureAwait(false))
            return;
        engine.Kill();
        if (!await ExitsWithinAsync(engine, _o.StopTimeout).ConfigureAwait(false)) Log("The engine did not end when told to.");
    }

    private async Task KillPostgresAsync()
    {
        var pg = _pg;
        _pg = null;
        if (pg is null || pg.Exited.IsCompleted) return;
        pg.Kill();
        await ExitsWithinAsync(pg, _o.StopTimeout).ConfigureAwait(false);
    }

    /// <summary>pg_ctl stop -m fast (which starts no server), then the postmaster's own exit; killed after the timeout.</summary>
    private async Task StopPostgresCleanlyAsync()
    {
        var pg = _pg;
        _pg = null;
        if (pg is null || pg.Exited.IsCompleted) return;
        try
        {
            var ctl = _p.Launcher.Start(_p.Commands.PgCtlStop());
            await ExitsWithinAsync(ctl, _o.StopTimeout).ConfigureAwait(false);
        }
        catch (Exception e)
        {
            Log($"The database stop command could not run ({e.GetType().Name}); ending the database.");
            pg.Kill();
        }
        if (!await ExitsWithinAsync(pg, _o.StopTimeout).ConfigureAwait(false))
        {
            Log("The database did not stop when asked; ending it.");
            pg.Kill();
            await ExitsWithinAsync(pg, _o.StopTimeout).ConfigureAwait(false);
        }
    }

    private async Task<bool> ExitsWithinAsync(IChildProcess child, TimeSpan timeout)
    {
        if (child.Exited.IsCompleted) return true;
        using var cancel = new CancellationTokenSource();
        var delay = _p.Scheduler.Delay(timeout, cancel.Token);
        await Task.WhenAny(child.Exited, delay).ConfigureAwait(false);
        cancel.Cancel();
        return child.Exited.IsCompleted;
    }

    private async Task StopAsync()
    {
        SetState(ServiceState.Stopping, Reason);
        await StopEngineAsync().ConfigureAwait(false);
        await StopPostgresCleanlyAsync().ConfigureAwait(false);
        _p.ServiceFile.Delete();
        SetState(ServiceState.Stopped, null);
        Log("ERPClaw stopped.");
        _finished = true;
        _queue.Writer.TryComplete();
    }

    // ---- Operations from the control pipe ----

    private async Task<ControlResponse> MintAsync(ControlRequest request, CancellationToken ct)
    {
        var principal = request.Argument ?? throw new ArgumentException("mint_code needs a principal");
        ServiceState state;
        string? runId;
        lock (_sync)
        {
            state = _state;
            runId = _runId;
        }
        if (state != ServiceState.Running || runId is null) return Unavailable();
        // Only for the run the host has just verified (BROWSER_AUTH 2.3c item 7).
        if (request.RunId is null
            || !CryptographicOperations.FixedTimeEquals(Encoding.UTF8.GetBytes(request.RunId), Encoding.UTF8.GetBytes(runId)))
            return ControlResponse.Failure(ControlErrors.StaleRun, SupervisorReasons.StaleRun);

        var result = await _p.Minter.MintAsync(principal, runId, ct).ConfigureAwait(false);
        // Only the outcome is logged, never the code and never the command's output.
        Log(result.Status == MintStatus.Minted ? "mint-code ok"
            : result.ExitCode is { } exit ? $"mint-code exit {exit}" : "mint-code failed");
        return result.Status switch
        {
            MintStatus.Minted when result.Code is { } code => ControlResponse.Minted(code),
            MintStatus.UnknownPrincipal => ControlResponse.Failure(ControlErrors.UnknownPrincipal, result.Text),
            _ => ControlResponse.Failure(ControlErrors.CouldNotRun, result.Text),
        };
    }

    /// <summary>restore-stage (engine running), stop the engine, switch, start the engine (SERVICE_COMMANDS r1b section 2).</summary>
    private async Task<ControlResponse> RestoreAsync(string backup)
    {
        if (State != ServiceState.Running) return Unavailable();
        var stage = await _p.Runner.RunAsync(_p.Commands.RestoreStage(backup), _ct).ConfigureAwait(false);
        if (!stage.Succeeded) return ControlResponse.From(stage);
        ProcessSpec switchSpec;
        try
        {
            var staged = stage.Body is { } body && body.TryGetProperty("staged", out var s) && s.ValueKind == JsonValueKind.String
                ? s.GetString()
                : null;
            switchSpec = _p.Commands.Switch(staged ?? throw new ArgumentException("no staged database"));
        }
        catch (ArgumentException)
        {
            Log("restore-stage named no usable staged database; nothing was switched.");
            return ControlResponse.Failure(ControlErrors.Internal, CommandOutcomes.InternalText);
        }

        SetState(ServiceState.Restoring, null);
        await StopEngineAsync().ConfigureAwait(false);
        WriteRecord();
        CommandOutcome switched;
        try
        {
            switched = await _p.Runner.RunAsync(switchSpec, _ct).ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (_ct.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception e)
        {
            // The engine has stopped. Even a runner that throws must go through recovery below.
            Log($"The restore command could not finish ({e.GetType().Name}); starting ERPClaw again.");
            switched = CommandOutcomes.Parse(1, "");
        }
        // Either way the engine starts again: on the restored books, or on the old ones when the switch failed.
        var started = await BringUpAsync(restartDatabase: false).ConfigureAwait(false);
        if (!switched.Succeeded) return ControlResponse.From(switched);
        if (!started) return ControlResponse.Failure(ControlErrors.NotRestarted, Reason ?? SupervisorReasons.EngineNotStarted);
        return ControlResponse.From(switched);
    }

    /// <summary>remove-replaced, resumed while it exits 4 with "partial" (SERVICE_COMMANDS r1b), with the back-off.</summary>
    private async Task<ControlResponse> RemoveReplacedAsync(string name)
    {
        if (State != ServiceState.Running) return Unavailable();
        var spec = _p.Commands.RemoveReplaced(name);
        for (int attempt = 1; ; attempt++)
        {
            var outcome = await _p.Runner.RunAsync(spec, _ct).ConfigureAwait(false);
            bool partial = outcome.Kind == OutcomeKind.FailedUnchanged && outcome.Body is { } body
                && body.TryGetProperty("partial", out var steps) && steps.ValueKind == JsonValueKind.Array;
            if (!partial || attempt >= _o.RemoveReplacedAttempts) return ControlResponse.From(outcome);
            await _p.Scheduler.Delay(Backoff(attempt), _ct).ConfigureAwait(false);
        }
    }

    private ControlResponse Unavailable()
    {
        lock (_sync)
        {
            var text = _state switch
            {
                ServiceState.GaveUp => _reason ?? SupervisorReasons.EngineNotStarted,
                ServiceState.Stopping or ServiceState.Stopped => SupervisorReasons.Stopped,
                _ => SupervisorReasons.Busy,
            };
            return ControlResponse.Failure(ControlErrors.Unavailable, text);
        }
    }

    /// <summary>Runs <paramref name="work"/> on the event loop; <paramref name="closed"/> once the loop has ended.</summary>
    private Task<T> Enqueue<T>(Func<Task<T>> work, T closed)
    {
        var done = new TaskCompletionSource<T>(TaskCreationOptions.RunContinuationsAsynchronously);
        bool queued = _queue.Writer.TryWrite(async () =>
        {
            try
            {
                done.TrySetResult(await work().ConfigureAwait(false));
            }
            catch (Exception e)
            {
                // Report to the caller and let the event loop clean up and give up. Swallowing this could leave
                // a stopped engine permanently in Restoring if recovery itself throws.
                done.TrySetException(e);
                throw;
            }
        });
        if (!queued) done.TrySetResult(closed);
        return done.Task;
    }

    // ---- State, the record and the log ----

    private void SetState(ServiceState state, string? reason)
    {
        lock (_sync)
        {
            _state = state;
            _reason = reason;
        }
        WriteRecord();
    }

    private void WriteRecord()
    {
        ServiceRecord record;
        lock (_sync)
        {
            record = new ServiceRecord
            {
                Postgres = _pg is { } pg ? new ChildRecord(pg.Id, pg.StartTime) : null,
                Engine = _engine is { } engine ? new ChildRecord(engine.Id, engine.StartTime) : null,
                DatabasePort = _port,
                State = _state,
                Reason = _reason,
                UpdatedAt = _p.Clock.Now,
            };
        }
        _p.Records.Write(record);
    }

    private void Log(string message) => _p.Log.Write(message);
}
