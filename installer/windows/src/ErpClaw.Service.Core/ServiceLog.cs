// The service's own log (INSTALLER 2.3, its last line): no secret, no session key, no port, no sign-in code, no
// connection parameter.
using System.Globalization;
using System.Text.RegularExpressions;

namespace ErpClaw.Service.Core;

/// <summary>Where log lines go: a file under the data folder's logs\ in the product.</summary>
public interface ILogSink
{
    void WriteLine(string line);
}

public sealed partial class ServiceLog
{
    /// <summary>What is written instead of a refused line. It names nothing of the line.</summary>
    public const string WithheldNotice = "a log line was withheld: it held a secret, a port, a sign-in code or a connection parameter";

    private readonly ILogSink _sink;
    private readonly IClock _clock;
    private readonly SecretRegistry _secrets;

    public ServiceLog(ILogSink sink, IClock clock, SecretRegistry secrets)
    {
        _sink = sink;
        _clock = clock;
        _secrets = secrets;
    }

    /// <summary>Writes one line, or refuses it (writing only <see cref="WithheldNotice"/>) and returns false.</summary>
    public bool Write(string message)
    {
        var refused = WouldRefuse(message);
        var text = refused ? WithheldNotice : ControlChars().Replace(message, " ");
        _sink.WriteLine(_clock.Now.UtcDateTime.ToString("yyyy-MM-dd'T'HH:mm:ss.fff'Z'", CultureInfo.InvariantCulture) + " " + text);
        return !refused;
    }

    /// <summary>True when <paramref name="message"/> would be refused.</summary>
    public bool WouldRefuse(string message) =>
        _secrets.HoldsSecret(message)
        || _secrets.HoldsPort(message)
        || PortShape().IsMatch(message)
        || ConnectionShape().IsMatch(message)
        || SignInCodeShape().IsMatch(message)
        || SessionKeyShape().IsMatch(message);

    /// <summary>host:port, or the word port followed by a number.</summary>
    [GeneratedRegex(@"(127\.0\.0\.1|localhost|\[::1\]|0\.0\.0\.0):\d{1,5}\b|\bport\b\s*[:=]?\s*\d{1,5}\b", RegexOptions.IgnoreCase)]
    private static partial Regex PortShape();

    /// <summary>
    /// A connection parameter: a database URL, a key=value pair of a connection string, a PG environment variable,
    /// a pipe path, or the names of the service file's pipe and secret.
    /// </summary>
    [GeneratedRegex(@"postgres(ql)?://|\b(host|hostaddr|dbname|user|password|sslmode|options)\s*=|\bPG(PASSWORD|HOST|HOSTADDR|PORT|USER|DATABASE|PASSFILE|SSLMODE)\b|\\\\[.?]\\pipe\\|\b(control_pipe|service_secret)\b", RegexOptions.IgnoreCase)]
    private static partial Regex ConnectionShape();

    /// <summary>A sign-in code: 128 bits as base32 without padding, 26 characters (BROWSER_AUTH 2.1).</summary>
    [GeneratedRegex(@"(?<![A-Za-z0-9])[A-Za-z2-7]{26}(?![A-Za-z0-9])")]
    private static partial Regex SignInCodeShape();

    /// <summary>A session key: 256 bits as base64url, 43 characters.</summary>
    [GeneratedRegex(@"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])")]
    private static partial Regex SessionKeyShape();

    [GeneratedRegex(@"[\x00-\x1f\x7f]")]
    private static partial Regex ControlChars();
}
