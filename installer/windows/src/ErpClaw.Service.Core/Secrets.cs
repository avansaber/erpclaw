// Secrets held in memory only: a wrapper that never prints itself, and the registry the log and the launcher guard read.
using System.Text.RegularExpressions;

namespace ErpClaw.Service.Core;

/// <summary>A password, a verifier or a sign-in code. <see cref="ToString"/> never shows it.</summary>
public sealed class Secret
{
    private readonly string _value;

    public Secret(string value)
    {
        if (string.IsNullOrEmpty(value)) throw new ArgumentException("A secret cannot be empty.", nameof(value));
        _value = value;
    }

    /// <summary>The value, for the one place that must send it (a credential write, a pipe response).</summary>
    public string Reveal() => _value;

    public override string ToString() => "[secret]";
}

/// <summary>
/// What the service has been told is secret (passwords, verifiers) and which ports are in use. The log refuses any
/// line holding one; the launcher guard refuses any command line holding a secret.
/// </summary>
public sealed class SecretRegistry
{
    private readonly HashSet<string> _secrets = new(StringComparer.Ordinal);
    private readonly HashSet<int> _ports = new();
    private readonly object _sync = new();

    public void Add(string secret)
    {
        if (string.IsNullOrEmpty(secret)) return;
        lock (_sync) _secrets.Add(secret);
    }

    public void Add(Secret secret) => Add(secret.Reveal());

    public void AddPort(int port)
    {
        lock (_sync) _ports.Add(port);
    }

    /// <summary>True when the text holds a registered secret.</summary>
    public bool HoldsSecret(string text)
    {
        lock (_sync) return _secrets.Any(s => text.Contains(s, StringComparison.Ordinal));
    }

    /// <summary>True when the text holds a registered port as a number of its own.</summary>
    public bool HoldsPort(string text)
    {
        int[] ports;
        lock (_sync) ports = _ports.ToArray();
        if (ports.Length == 0) return false;
        foreach (Match m in Regex.Matches(text, @"(?<!\d)\d{1,5}(?!\d)"))
            if (int.TryParse(m.ValueSpan, out int n) && ports.Contains(n)) return true;
        return false;
    }
}
