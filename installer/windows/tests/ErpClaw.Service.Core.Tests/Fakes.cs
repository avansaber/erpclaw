// Fakes for the service's Windows-only parts and the engine's commands, and a rig wiring them to a Supervisor.
using System.Text;
using ErpClaw.Service.Core;

namespace ErpClaw.Service.Core.Tests;

/// <summary>A shared, ordered record of what happened, for the order tests.</summary>
public sealed class Journal
{
    private readonly List<string> _entries = new();

    public void Add(string entry)
    {
        lock (_entries) _entries.Add(entry);
    }

    public IReadOnlyList<string> Entries
    {
        get { lock (_entries) return _entries.ToArray(); }
    }

    public int Count(string entry) => Entries.Count(e => e == entry);

    public int IndexOf(string entry, int from = 0)
    {
        var all = Entries;
        for (int i = from; i < all.Count; i++)
            if (all[i] == entry) return i;
        return -1;
    }

    public int LastIndexOf(string entry)
    {
        var all = Entries;
        for (int i = all.Count - 1; i >= 0; i--)
            if (all[i] == entry) return i;
        return -1;
    }

    /// <summary>The entries from <paramref name="from"/> on, keeping only those in <paramref name="keep"/>.</summary>
    public IReadOnlyList<string> Only(int from, params string[] keep) =>
        Entries.Skip(from).Where(e => keep.Contains(e)).ToArray();
}

public sealed class FakeClock : IClock
{
    private DateTimeOffset _now = new(2026, 11, 2, 9, 0, 0, TimeSpan.Zero);
    private readonly object _sync = new();

    public DateTimeOffset Now
    {
        get { lock (_sync) return _now; }
    }

    public void Advance(TimeSpan by)
    {
        lock (_sync) _now += by;
    }
}

/// <summary>Every delay passes at once, moving the fake clock; each is recorded.</summary>
public sealed class FakeScheduler : IScheduler
{
    private readonly FakeClock _clock;
    private readonly List<TimeSpan> _delays = new();

    public FakeScheduler(FakeClock clock) => _clock = clock;

    public IReadOnlyList<TimeSpan> Delays
    {
        get { lock (_delays) return _delays.ToArray(); }
    }

    /// <summary>Delays of a second or more: the back-offs (readiness polls are shorter).</summary>
    public IReadOnlyList<double> BackoffSeconds => Delays.Where(d => d >= TimeSpan.FromSeconds(1)).Select(d => d.TotalSeconds).ToArray();

    public Task Delay(TimeSpan delay, CancellationToken ct)
    {
        ct.ThrowIfCancellationRequested();
        lock (_delays) _delays.Add(delay);
        _clock.Advance(delay);
        return Task.CompletedTask;
    }
}

public sealed class FakeChild : IChildProcess
{
    private readonly TaskCompletionSource<int> _exit = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly Journal _journal;

    public FakeChild(int id, DateTimeOffset start, ProcessSpec spec, string name, Journal journal)
    {
        Id = id;
        StartTime = start;
        Spec = spec;
        Name = name;
        _journal = journal;
    }

    public int Id { get; }
    public DateTimeOffset StartTime { get; }
    public ProcessSpec Spec { get; }
    public string Name { get; }
    public Task<int> Exited => _exit.Task;
    public string StandardOutput { get; set; } = "";
    public string ErrorDiagnostic { get; private set; } = "";

    public void CaptureStandardError(string text) => ErrorDiagnostic = Spec.CaptureErrorDiagnostic
        ? PostgresDiagnosticFilter.Filter(text, Spec.StandardInput) : "";
    public bool Running => !_exit.Task.IsCompleted;

    public void Kill()
    {
        if (!Running) return;
        _journal.Add("kill " + Name);
        _exit.TrySetResult(-1);
    }

    public void Exit(int code)
    {
        if (!Running) return;
        _journal.Add("exit " + Name);
        _exit.TrySetResult(code);
    }
}

public static class Names
{
    /// <summary>A short name for a spec: postgres, postgres-single, pg_ctl, initdb, engine, bootstrap, or the service command.</summary>
    public static string Of(ProcessSpec spec)
    {
        var exe = Path.GetFileName(spec.Executable.Replace('\\', '/'));
        if (exe == "postgres.exe") return spec.Arguments.Contains("--single") ? "postgres-single" : "postgres";
        if (exe == "pg_ctl.exe") return "pg_ctl";
        if (exe == "initdb.exe") return "initdb";
        var args = spec.Arguments.ToList();
        int main = args.IndexOf("erpclaw_cli.main");
        var rest = args.Skip(main + 1).ToList();
        if (rest.Count == 0) return exe;
        return rest[0] switch
        {
            "serve" => "engine",
            "init" => "bootstrap",
            "service" => rest[1] == "backup" ? "backup " + rest[rest.IndexOf("--kind") + 1] : rest[1],
            _ => rest[0],
        };
    }
}

public sealed class FakeLauncher : IProcessLauncher
{
    private readonly Journal _journal;
    private readonly FakeClock _clock;
    private readonly List<FakeChild> _started = new();
    private int _nextPid = 1000;

    public FakeLauncher(Journal journal, FakeClock clock)
    {
        _journal = journal;
        _clock = clock;
    }

    /// <summary>Runs after each start; the default lets pg_ctl stop the running postmaster.</summary>
    public Action<FakeChild>? OnStart { get; set; }

    /// <summary>When set, pg_ctl stop exits but the postmaster keeps running.</summary>
    public bool PostmasterIgnoresStop { get; set; }

    public IReadOnlyList<FakeChild> Started
    {
        get { lock (_started) return _started.ToArray(); }
    }

    public IReadOnlyList<FakeChild> Named(string name) => Started.Where(c => c.Name == name).ToArray();

    public FakeChild? Latest(string name) => Named(name).LastOrDefault();

    public IChildProcess Start(ProcessSpec spec)
    {
        var name = Names.Of(spec);
        FakeChild child;
        lock (_started)
        {
            child = new FakeChild(++_nextPid, _clock.Now.AddTicks(_nextPid), spec, name, _journal);
            _started.Add(child);
        }
        _journal.Add("start " + name);
        if (name == "pg_ctl")
        {
            if (!PostmasterIgnoresStop)
                foreach (var pg in Named("postgres").Where(p => p.Running)) pg.Exit(0);
            child.Exit(0);
        }
        OnStart?.Invoke(child);
        return child;
    }
}

public sealed class FakeInspector : IProcessInspector
{
    public Dictionary<int, DateTimeOffset> Alive { get; } = new();
    public List<int> Killed { get; } = new();
    public Journal? Journal { get; set; }

    public DateTimeOffset? StartTimeOf(int pid) => Alive.TryGetValue(pid, out var t) ? t : null;

    public void Kill(int pid)
    {
        Killed.Add(pid);
        Alive.Remove(pid);
        Journal?.Add("kill leftover " + pid);
    }
}

public sealed class FakePorts : IPortProbe
{
    private int _next = 50001;

    public HashSet<int> Taken { get; } = new();

    public bool IsFree(int port) => !Taken.Contains(port);

    public int DrawFree()
    {
        while (Taken.Contains(_next)) _next++;
        return _next++;
    }
}

public sealed class FakeConfig : IPostgresConfigWriter
{
    private readonly Journal _journal;

    public FakeConfig(Journal journal) => _journal = journal;

    public List<int> Ports { get; } = new();

    public void WriteManagedSettings(int port)
    {
        Ports.Add(port);
        _journal.Add("config");
    }
}

public sealed class MemoryRecords : IRunRecordStore
{
    private readonly List<ServiceRecord> _written = new();

    public ServiceRecord? Initial { get; set; }

    public IReadOnlyList<ServiceRecord> Written
    {
        get { lock (_written) return _written.ToArray(); }
    }

    public ServiceRecord? Current => Written.LastOrDefault() ?? Initial;

    public ServiceRecord? Read() => Current;

    public void Write(ServiceRecord record)
    {
        lock (_written) _written.Add(record);
    }
}

public sealed class FakeServiceFiles : IServiceFileStore
{
    private readonly Journal _journal;

    public FakeServiceFiles(Journal journal) => _journal = journal;

    public List<ServiceFileContent> Written { get; } = new();
    public ServiceFileContent? Current { get; private set; }

    public void Write(ServiceFileContent content)
    {
        _journal.Add("service file write");
        lock (Written) Written.Add(content);
        Current = content;
    }

    public void Delete()
    {
        _journal.Add("service file delete");
        Current = null;
    }
}

/// <summary>The engine's stop event: by default the running engine stops by itself when it is signalled.</summary>
public sealed class FakeStopSignal : IEngineStopSignal
{
    private readonly Journal _journal;
    private readonly FakeLauncher _launcher;

    public FakeStopSignal(Journal journal, FakeLauncher launcher)
    {
        _journal = journal;
        _launcher = launcher;
    }

    public List<string> Names { get; } = new();
    public bool EngineExits { get; set; } = true;
    public bool CanOpen { get; set; } = true;

    public bool Signal(string eventName)
    {
        _journal.Add("signal");
        lock (Names) Names.Add(eventName);
        if (!CanOpen) return false;
        if (EngineExits) _launcher.Latest("engine")?.Exit(0);
        return true;
    }
}

public sealed class FakeReadiness : IReadinessProbe
{
    private readonly Journal _journal;

    public FakeReadiness(Journal journal) => _journal = journal;

    /// <summary>Answers in turn; when empty, <see cref="Default"/>.</summary>
    public Queue<bool> Answers { get; } = new();
    public bool Default { get; set; } = true;
    public List<int> PortsProbed { get; } = new();

    public Task<bool> CanLoginAsync(int port, CancellationToken ct)
    {
        _journal.Add("probe");
        lock (PortsProbed) PortsProbed.Add(port);
        lock (Answers) return Task.FromResult(Answers.Count > 0 ? Answers.Dequeue() : Default);
    }
}

public sealed class FakeBooks : IBooksProbe
{
    private readonly Journal _journal;

    public FakeBooks(Journal journal) => _journal = journal;

    public BooksState State { get; set; } = BooksState.Present;

    public Task<BooksState> CheckAsync(CancellationToken ct)
    {
        _journal.Add("books?");
        return Task.FromResult(State);
    }
}

public sealed class FakeRunFiles : IRunFileWatcher
{
    private readonly Journal _journal;
    private int _n;

    public FakeRunFiles(Journal journal) => _journal = journal;

    /// <summary>When true, every wait ends with no run file (as a timeout).</summary>
    public bool AlwaysFail { get; set; }

    /// <summary>How many of the next waits end with no run file.</summary>
    public int FailNext { get; set; }

    /// <summary>The run id of the last run file the engine wrote.</summary>
    public string? Last { get; private set; }

    public Task<RunInfo?> WaitForRunAsync(IChildProcess engine, CancellationToken ct)
    {
        _journal.Add("run file?");
        if (AlwaysFail || engine.Exited.IsCompleted) return Task.FromResult<RunInfo?>(null);
        if (FailNext > 0)
        {
            FailNext--;
            return Task.FromResult<RunInfo?>(null);
        }
        Last = "run" + Interlocked.Increment(ref _n).ToString("x32");
        return Task.FromResult<RunInfo?>(new RunInfo(Last));
    }
}

/// <summary>The engine's commands: each run is journaled as "run name"; outcomes are scripted.</summary>
public sealed class FakeRunner : ICommandRunner
{
    private readonly Journal _journal;
    private readonly List<ProcessSpec> _specs = new();

    public FakeRunner(Journal journal) => _journal = journal;

    /// <summary>Returns the outcome for a spec, or null for the default (exit 0, "{}").</summary>
    public Func<string, ProcessSpec, CommandOutcome?>? Script { get; set; }

    public IReadOnlyList<ProcessSpec> Specs
    {
        get { lock (_specs) return _specs.ToArray(); }
    }

    public Task<CommandOutcome> RunAsync(ProcessSpec spec, CancellationToken ct)
    {
        var name = Names.Of(spec);
        lock (_specs) _specs.Add(spec);
        _journal.Add("run " + name);
        var outcome = Script?.Invoke(name, spec) ?? Outcome.Of(0, name == "restore-stage" ? """{"staged":"erpclaw_r2","proof":"ok"}""" : "{}");
        return Task.FromResult(outcome);
    }
}

public static class Outcome
{
    public static CommandOutcome Of(int exit, string stdout) => CommandOutcomes.Parse(exit, stdout);

    public static CommandOutcome ForExit(int exit) => exit switch
    {
        0 => Of(0, "{}"),
        1 => Of(1, """{"text":"ERPClaw hit an internal error"}"""),
        2 => Of(2, """{"reason":"not_allowed","text":"That is not allowed now."}"""),
        3 => Of(3, """{"reason":"disk_full","text":"There is not enough disk space."}"""),
        4 => Of(4, """{"reason":"rolled_back","text":"That failed and nothing was changed."}"""),
        _ => throw new ArgumentOutOfRangeException(nameof(exit)),
    };
}

public sealed class FakeMinter : ICodeMinter
{
    public MintResult Next { get; set; } = new(MintStatus.Minted, new Secret("ABCDEFGHIJKLMNOPQRSTUVWXYZ"), "");
    public List<string> Principals { get; } = new();
    public List<string> RunIds { get; } = new();

    public Task<MintResult> MintAsync(string principal, string runId, CancellationToken ct)
    {
        Principals.Add(principal);
        RunIds.Add(runId);
        return Task.FromResult(Next);
    }
}

public sealed class MemorySink : ILogSink
{
    private readonly List<string> _lines = new();

    public IReadOnlyList<string> Lines
    {
        get { lock (_lines) return _lines.ToArray(); }
    }

    public void WriteLine(string line)
    {
        lock (_lines) _lines.Add(line);
    }
}

public static class Eventually
{
    public static async Task True(Func<bool> condition, string what, int timeoutMs = 5000)
    {
        var until = DateTime.UtcNow.AddMilliseconds(timeoutMs);
        while (!condition())
        {
            if (DateTime.UtcNow > until) throw new TimeoutException("timed out waiting for: " + what);
            await Task.Delay(2);
        }
    }
}

/// <summary>A Supervisor on fakes.</summary>
public sealed class Rig
{
    public Journal Journal { get; } = new();
    public FakeClock Clock { get; } = new();
    public FakeScheduler Scheduler { get; }
    public FakeLauncher Launcher { get; }
    public FakeRunner Runner { get; }
    public FakeReadiness Readiness { get; }
    public FakeBooks Books { get; }
    public FakeRunFiles RunFiles { get; }
    public FakeMinter Minter { get; } = new();
    public FakePorts Ports { get; } = new();
    public FakeConfig Config { get; }
    public MemoryRecords Records { get; } = new();
    public FakeInspector Inspector { get; } = new();
    public MemorySink Sink { get; } = new();
    public SecretRegistry Secrets { get; } = new();
    public ServiceSession Session { get; }
    public FakeServiceFiles ServiceFiles { get; }
    public FakeStopSignal StopSignal { get; }
    public ServiceCommands Commands { get; } = ServiceCommands.Proposed(new ServicePaths(@"C:\P\ERPClaw\3.0.0", @"C:\Users\owner\ERPClaw"));
    public SupervisorOptions Options { get; set; } = new();
    public Task? Running { get; private set; }

    private Supervisor? _supervisor;

    public Rig()
    {
        Scheduler = new FakeScheduler(Clock);
        Launcher = new FakeLauncher(Journal, Clock);
        Runner = new FakeRunner(Journal);
        Readiness = new FakeReadiness(Journal);
        Books = new FakeBooks(Journal);
        RunFiles = new FakeRunFiles(Journal);
        Config = new FakeConfig(Journal);
        Inspector.Journal = Journal;
        Session = ServiceSession.Create("S-1-5-21-1111111111-2222222222-3333333333-1001", Secrets);
        ServiceFiles = new FakeServiceFiles(Journal);
        StopSignal = new FakeStopSignal(Journal, Launcher);
    }

    public Supervisor Supervisor => _supervisor ??= new Supervisor(
        new SupervisorParts(Commands, Launcher, Runner, Readiness, Books, RunFiles, Minter, Ports, Config, Records, Inspector,
            Clock, Scheduler, new ServiceLog(Sink, Clock, Secrets), Session, ServiceFiles, StopSignal),
        Options);

    public FakeChild Postgres => Launcher.Latest("postgres") ?? throw new InvalidOperationException("no postgres");
    public FakeChild Engine => Launcher.Latest("engine") ?? throw new InvalidOperationException("no engine");

    public void Start()
    {
        var s = Supervisor;
        Running = Task.Run(() => s.RunAsync(CancellationToken.None));
    }

    public async Task StartAndWaitRunning()
    {
        Start();
        await WaitState(ServiceState.Running);
    }

    public Task WaitState(ServiceState state) =>
        Eventually.True(() => Supervisor.State == state || (Running?.IsFaulted ?? false), "state " + state)
            .ContinueWith(t =>
            {
                if (Running is { IsFaulted: true }) throw Running.Exception!;
                t.GetAwaiter().GetResult();
            });

    /// <summary>Waits until the engine has been started <paramref name="count"/> times and the service is running.</summary>
    public async Task WaitEngineStarts(int count)
    {
        await Eventually.True(() => Launcher.Named("engine").Count >= count && Supervisor.State == ServiceState.Running
            || (Running?.IsFaulted ?? false), $"engine start {count}");
        if (Running is { IsFaulted: true }) throw Running.Exception!;
    }

    public Task<ControlResponse> Ask(string op, string? argument = null) =>
        Supervisor.HandleAsync(new ControlRequest(op, argument, null, op == ControlOps.MintCode ? RunFiles.Last ?? "none" : null),
            CancellationToken.None);

    public Task<ControlResponse> Mint(string principal, string runId) =>
        Supervisor.HandleAsync(new ControlRequest(ControlOps.MintCode, principal, null, runId), CancellationToken.None);

    public static string Utf8(ReadOnlyMemory<byte> bytes) => Encoding.UTF8.GetString(bytes.Span);
}
