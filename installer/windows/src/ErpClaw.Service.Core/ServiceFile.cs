// run\service.json: how the host finds the control pipe and proves the service. Three string keys, nothing else,
// parsed under the same rules as the engine's run file: one JSON object, at most 4096 bytes, each key once.
using System.Security.Cryptography;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace ErpClaw.Service.Core;

/// <summary>The file's three values. <see cref="ToString"/> shows none of them.</summary>
public sealed record ServiceFileContent(string ControlPipe, string ServiceSecret, string DatabasePort)
{
    public override string ToString() => "ServiceFileContent";
}

public static partial class ServiceFileJson
{
    public const string FileName = "service.json";

    /// <summary>The largest file the host will read, in bytes.</summary>
    public const int MaxBytes = 4096;

    public static byte[] Serialize(ServiceFileContent content) =>
        Encoding.UTF8.GetBytes(new JsonObject
        {
            ["control_pipe"] = content.ControlPipe,
            ["service_secret"] = content.ServiceSecret,
            ["database_port"] = content.DatabasePort,
        }.ToJsonString());

    /// <summary>The content, or null when the bytes are not exactly the three string keys, each once, in range.</summary>
    public static ServiceFileContent? Parse(ReadOnlySpan<byte> bytes)
    {
        if (bytes.Length > MaxBytes) return null;
        try
        {
            // Text after the object is a parse error; so is a key given twice.
            using var doc = JsonDocument.Parse(bytes.ToArray(), new JsonDocumentOptions { AllowDuplicateProperties = false });
            var root = doc.RootElement;
            if (root.ValueKind != JsonValueKind.Object) return null;
            string? pipe = null, secret = null, port = null;
            int count = 0;
            foreach (var property in root.EnumerateObject())
            {
                count++;
                if (property.Value.ValueKind != JsonValueKind.String) return null;
                switch (property.Name)
                {
                    case "control_pipe": pipe = property.Value.GetString(); break;
                    case "service_secret": secret = property.Value.GetString(); break;
                    case "database_port": port = property.Value.GetString(); break;
                    default: return null;
                }
            }
            if (count != 3 || pipe is null || secret is null || port is null) return null;
            if (!PipeShape().IsMatch(pipe) || !SecretShape().IsMatch(secret) || !PortShape().IsMatch(port)) return null;
            if (int.Parse(port) > 65535) return null;
            return new ServiceFileContent(pipe, secret, port);
        }
        catch (Exception e) when (e is JsonException or InvalidOperationException)
        {
            return null;
        }
    }

    [GeneratedRegex(@"^\\\\\.\\pipe\\erpclaw-service-S-1-\d+(-\d+)+-[0-9a-f]{32}$")]
    private static partial Regex PipeShape();

    [GeneratedRegex("^[0-9a-f]{64}$")]
    private static partial Regex SecretShape();

    [GeneratedRegex("^[1-9][0-9]{0,4}$")]
    private static partial Regex PortShape();
}

/// <summary>
/// What is new at each service start: the control pipe's name and the secret the host checks the service with.
/// It is the key of the proof (<see cref="IHostCheckKey"/>); nothing here is ever written to the log.
/// </summary>
public sealed partial class ServiceSession : IHostCheckKey
{
    private ServiceSession(string pipe, string secret)
    {
        ControlPipe = pipe;
        Secret = secret;
        Key = Convert.FromHexString(secret);
    }

    /// <summary>A new session for the account with <paramref name="userSid"/>; the name and secret join <paramref name="secrets"/>.</summary>
    public static ServiceSession Create(string userSid, SecretRegistry? secrets = null)
    {
        if (!SidShape().IsMatch(userSid)) throw new ArgumentException("Not a security identifier.", nameof(userSid));
        var pipe = $@"\\.\pipe\erpclaw-service-{userSid}-{Convert.ToHexStringLower(RandomNumberGenerator.GetBytes(16))}";
        var secret = Convert.ToHexStringLower(RandomNumberGenerator.GetBytes(32));
        secrets?.Add(pipe);
        secrets?.Add(secret);
        return new ServiceSession(pipe, secret);
    }

    public string ControlPipe { get; }

    /// <summary>64 lower-case hex characters: 32 random bytes.</summary>
    public string Secret { get; }

    /// <summary>The 32 bytes the proof's HMAC is keyed with.</summary>
    public byte[]? Key { get; }

    public ServiceFileContent FileContent(int port) =>
        new(ControlPipe, Secret, port.ToString(System.Globalization.CultureInfo.InvariantCulture));

    public override string ToString() => "ServiceSession";

    [GeneratedRegex(@"^S-1-\d+(-\d+)+$")]
    private static partial Regex SidShape();
}

public interface IServiceFileStore
{
    void Write(ServiceFileContent content);

    void Delete();
}

/// <summary>
/// The file in the run folder, created for its owner alone in the creating call itself (never made first and
/// restricted after), written under a temporary name and renamed into place.
/// </summary>
public sealed class FileServiceFileStore : IServiceFileStore
{
    private readonly string _folder;

    public FileServiceFileStore(string runFolder)
    {
        _folder = runFolder;
        FilePath = Path.Combine(runFolder, ServiceFileJson.FileName);
    }

    public string FilePath { get; }

    public void Write(ServiceFileContent content)
    {
        Directory.CreateDirectory(_folder);
        var temp = FilePath + ".tmp";
        File.Delete(temp); // a leftover from a crash
        using (var stream = CreateOwnerOnly(temp))
        {
            stream.Write(ServiceFileJson.Serialize(content));
            stream.Flush(flushToDisk: true);
        }
        File.Move(temp, FilePath, overwrite: true);
    }

    public void Delete() => File.Delete(FilePath);

    private static FileStream CreateOwnerOnly(string path)
    {
        if (!OperatingSystem.IsWindows())
            return new FileStream(path, new FileStreamOptions
            {
                Mode = FileMode.CreateNew,
                Access = FileAccess.Write,
                Share = FileShare.None,
                UnixCreateMode = UnixFileMode.UserRead | UnixFileMode.UserWrite,
            });
        // A protected descriptor (no inherited entries) granting the current user alone, given to CreateFile.
        var security = new FileSecurity();
        security.SetAccessRuleProtection(isProtected: true, preserveInheritance: false);
        security.AddAccessRule(new FileSystemAccessRule(WindowsIdentity.GetCurrent().User!, FileSystemRights.FullControl, AccessControlType.Allow));
        return FileSystemAclExtensions.Create(new FileInfo(path), FileMode.CreateNew, FileSystemRights.Write | FileSystemRights.ReadPermissions,
            FileShare.None, 4096, FileOptions.WriteThrough, security);
    }
}
