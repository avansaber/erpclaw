// When the daily backup runs (BACKUP_RESTORE 2.2 item 1: the first time the service runs on a calendar day, 10
// minutes after the engine is ready; SERVICE_COMMANDS r1b: a backup that gives up on its capture deadline exits 3,
// so it is tried again later). The service's timer asks IsDue, runs the backup, and records the outcome here.
namespace ErpClaw.Service.Core;

public sealed record DailyBackupOptions
{
    /// <summary>The wait after the engine is ready, so the backup never delays the owner's first turn.</summary>
    public TimeSpan AfterReady { get; init; } = TimeSpan.FromMinutes(10);

    /// <summary>The wait after a backup that could not run (exit 3, or skipped with the engine down).</summary>
    public TimeSpan RetryAfter { get; init; } = TimeSpan.FromMinutes(15);

    /// <summary>Tries after the first on one day; then the next calendar day's.</summary>
    public int RetriesPerDay { get; init; } = 4;
}

public sealed class DailyBackupSchedule
{
    private readonly IClock _clock;
    private readonly TimeZoneInfo _zone;
    private readonly DailyBackupOptions _options;
    private readonly object _sync = new();
    private DateOnly? _settledDay;
    private DateTimeOffset? _readyAt;
    private DateTimeOffset? _retryAt;
    private DateOnly _retryDay;
    private int _retries;

    /// <param name="zone">The owner's time zone, which sets the calendar day.</param>
    /// <param name="lastDaily">The day of the newest daily backup already taken (from list-backups), if any.</param>
    public DailyBackupSchedule(IClock clock, TimeZoneInfo zone, DateOnly? lastDaily = null, DailyBackupOptions? options = null)
    {
        _clock = clock;
        _zone = zone;
        _settledDay = lastDaily;
        _options = options ?? new DailyBackupOptions();
    }

    /// <summary>The supervisor's engine became ready (each start and restart).</summary>
    public void EngineReady()
    {
        lock (_sync) _readyAt = _clock.Now;
    }

    /// <summary>When the next attempt is due; null until the engine has been ready.</summary>
    public DateTimeOffset? NextDue
    {
        get
        {
            lock (_sync)
            {
                if (_readyAt is not { } ready) return null;
                var afterReady = ready + _options.AfterReady;
                var today = Today();
                if (_settledDay == today) return Max(StartOf(today.AddDays(1)), afterReady);
                return _retryAt is { } retry && _retryDay == today ? Max(retry, afterReady) : afterReady;
            }
        }
    }

    public bool IsDue => NextDue is { } due && _clock.Now >= due;

    /// <summary>Records an attempt's outcome (null: skipped because the engine was not running, treated as exit 3).</summary>
    public void Record(CommandOutcome? outcome)
    {
        lock (_sync)
        {
            var today = Today();
            if (_retryDay != today)
            {
                _retryDay = today;
                _retries = 0;
                _retryAt = null;
            }
            bool tryAgain = outcome is null || outcome.Kind == OutcomeKind.CouldNotRun;
            if (tryAgain && _retries < _options.RetriesPerDay)
            {
                _retries++;
                _retryAt = _clock.Now + _options.RetryAfter;
                return;
            }
            // Done, or failed in a way a retry will not mend, or out of tries: the next calendar day's.
            _settledDay = today;
            _retryAt = null;
            _retries = 0;
        }
    }

    private DateOnly Today() => DateOnly.FromDateTime(TimeZoneInfo.ConvertTime(_clock.Now, _zone).DateTime);

    private DateTimeOffset StartOf(DateOnly day)
    {
        var local = day.ToDateTime(TimeOnly.MinValue);
        return new DateTimeOffset(local, _zone.GetUtcOffset(local));
    }

    private static DateTimeOffset Max(DateTimeOffset a, DateTimeOffset b) => a >= b ? a : b;
}
