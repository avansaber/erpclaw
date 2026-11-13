// The service's record in the run folder (INSTALLER 2.3 item 5): each child's PID and start time, the database's
// port and the service's state. No secret in it. It never shares a name with the engine's books.json, books.json.prev or
// installed.json, nor with the engine's run file.
using System.Text.Json;
using System.Text.Json.Serialization;

namespace ErpClaw.Service.Core;

public enum ServiceState
{
    Stopped,
    Starting,
    Running,
    Restarting,
    Restoring,
    Stopping,
    GaveUp,
}

public sealed record ChildRecord(int Pid, DateTimeOffset StartTime);

public sealed record ServiceRecord
{
    public int Version { get; init; } = 1;

    public ChildRecord? Postgres { get; init; }

    public ChildRecord? Engine { get; init; }

    /// <summary>PostgreSQL's current port on 127.0.0.1; the engine's commands read it here (not a secret, never logged).</summary>
    public int? DatabasePort { get; init; }

    public ServiceState State { get; init; } = ServiceState.Stopped;

    /// <summary>The plain reason the host shows when the service gave up.</summary>
    public string? Reason { get; init; }

    public DateTimeOffset UpdatedAt { get; init; }
}

public interface IRunRecordStore
{
    ServiceRecord? Read();

    void Write(ServiceRecord record);
}

public static class ServiceRecordJson
{
    /// <summary>The record's file name in the run folder.</summary>
    public const string FileName = "service-state.json";

    /// <summary>Names in the run folder that belong to others (the engine's books pointer and install record).</summary>
    public static readonly IReadOnlySet<string> ReservedNames =
        new HashSet<string>(StringComparer.OrdinalIgnoreCase) { "books.json", "books.json.prev", "installed.json" };

    private static readonly JsonSerializerOptions Options = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        Converters = { new JsonStringEnumConverter(JsonNamingPolicy.SnakeCaseLower, allowIntegerValues: false) },
        WriteIndented = true,
    };

    public static string Serialize(ServiceRecord record) => JsonSerializer.Serialize(record, Options);

    /// <summary>The record, or null when the text is not a readable record (treated as no record).</summary>
    public static ServiceRecord? Parse(string json)
    {
        try
        {
            var record = JsonSerializer.Deserialize<ServiceRecord>(json, Options);
            return record is { Version: 1 } ? record : null;
        }
        catch (JsonException)
        {
            return null;
        }
    }
}

/// <summary>The record as a file: written to a temporary name, then moved over the old one.</summary>
public sealed class FileRunRecordStore : IRunRecordStore
{
    private readonly string _folder;

    public FileRunRecordStore(string runFolder) => _folder = runFolder;

    public string FilePath => Path.Combine(_folder, ServiceRecordJson.FileName);

    public ServiceRecord? Read()
    {
        try
        {
            return ServiceRecordJson.Parse(File.ReadAllText(FilePath));
        }
        catch (Exception e) when (e is FileNotFoundException or DirectoryNotFoundException)
        {
            return null;
        }
    }

    public void Write(ServiceRecord record)
    {
        // The run folder's own owner-only ACL (INSTALLER 2.1) covers the file; it holds no secret.
        Directory.CreateDirectory(_folder);
        var temp = FilePath + ".tmp";
        using (var stream = new FileStream(temp, FileMode.Create, FileAccess.Write, FileShare.None))
        {
            stream.Write(System.Text.Encoding.UTF8.GetBytes(ServiceRecordJson.Serialize(record)));
            stream.Flush(flushToDisk: true);
        }
        File.Move(temp, FilePath, overwrite: true);
    }
}
