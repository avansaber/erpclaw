using System.Text;
using System.Text.RegularExpressions;

namespace ErpClaw.Service.Core;

/// <summary>
/// Proposed stderr diagnostic: error lines only, bounded during reading as well as in the result. Suspicious or
/// oversized lines are dropped whole, never truncated before the secret check. No stdout or stdin is copied into the result.
/// </summary>
public sealed partial class PostgresDiagnosticFilter
{
    public const int DefaultMaxLines = 8;
    public const int DefaultMaxCharacters = 2048;

    private readonly int _maxLines;
    private readonly int _maxCharacters;
    private readonly string[] _inputLines;
    private readonly SecretRegistry? _secrets;
    private readonly StringBuilder _line = new();
    private readonly StringBuilder _result = new();
    private bool _oversized;
    private int _lines;

    public PostgresDiagnosticFilter(ReadOnlyMemory<byte> standardInput = default, SecretRegistry? secrets = null,
        int maxLines = DefaultMaxLines, int maxCharacters = DefaultMaxCharacters)
    {
        if (maxLines < 1) throw new ArgumentOutOfRangeException(nameof(maxLines));
        if (maxCharacters < 1) throw new ArgumentOutOfRangeException(nameof(maxCharacters));
        _maxLines = maxLines;
        _maxCharacters = maxCharacters;
        _secrets = secrets;
        // Used only to refuse echoes; never appended to the diagnostic.
        _inputLines = Encoding.UTF8.GetString(standardInput.Span).Split(new[] { '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries)
            .Select(line => line.Trim()).Where(line => line.Length > 0).ToArray();
    }

    /// <summary>Drains to EOF even when the diagnostic is full; never retains raw stderr in an unbounded string.</summary>
    public async Task<string> ReadAsync(TextReader reader)
    {
        var buffer = new char[512];
        int read;
        while ((read = await reader.ReadAsync(buffer.AsMemory()).ConfigureAwait(false)) != 0)
            Append(buffer.AsSpan(0, read));
        EndLine();
        return _result.ToString();
    }

    /// <summary>Rechecks diagnostics at a consumer boundary, including registered secrets, before reporting.</summary>
    public static string Filter(string text, ReadOnlyMemory<byte> standardInput = default, SecretRegistry? secrets = null)
    {
        var filter = new PostgresDiagnosticFilter(standardInput, secrets);
        filter.Append(text.AsSpan());
        filter.EndLine();
        return filter._result.ToString();
    }

    private void Append(ReadOnlySpan<char> text)
    {
        foreach (var c in text)
        {
            if (c is '\r' or '\n') EndLine();
            else if (!_oversized && _lines < _maxLines)
            {
                if (_line.Length < _maxCharacters) _line.Append(c);
                else { _oversized = true; _line.Clear(); }
            }
        }
    }

    private void EndLine()
    {
        if (!_oversized && _line.Length > 0 && _lines < _maxLines)
        {
            var line = _line.ToString();
            if (ErrorShape().IsMatch(line) && !SecretShape().IsMatch(line) && !StatementShape().IsMatch(line)
                && !line.Any(char.IsControl) && !(_secrets?.HoldsSecret(line) ?? false)
                && !_inputLines.Any(input => line.Contains(input, StringComparison.OrdinalIgnoreCase))
                && _result.Length + (_lines == 0 ? 0 : 1) + line.Length <= _maxCharacters)
            {
                if (_lines > 0) _result.Append('\n');
                _result.Append(line);
                _lines++;
            }
        }
        _line.Clear();
        _oversized = false;
    }

    [GeneratedRegex(@"\b(FATAL|ERROR|PANIC)\b|could not", RegexOptions.IgnoreCase)]
    private static partial Regex ErrorShape();

    // Conservatively refuse credential names, connection strings, hashes and long encoded tokens, even when
    // no particular secret has been registered. These are shape checks, not examples of usable credentials.
    [GeneratedRegex(@"password|verifier|SCRAM-SHA-256\$|\b(secret|credential|token|authorization|bearer|passwd|pwd|passphrase|api[_-]?key|access[_-]?key|private[_ ]key|client[_-]?secret|PG[A-Z_]+)\b|[a-z][a-z0-9+.-]*://|\b(host|hostaddr|dbname|user|sslmode|options)\s*=|\bmd5[0-9a-f]{32}\b|[A-Za-z0-9_+/=-]{26,}", RegexOptions.IgnoreCase)]
    private static partial Regex SecretShape();

    [GeneratedRegex(@"\b(STATEMENT|QUERY)\s*:|\bLINE\s+\d+\s*:|\b(ALTER|CREATE|DROP)\s+(ROLE|USER|DATABASE)\b|\bDO\s+\$\$|\bSELECT\b", RegexOptions.IgnoreCase)]
    private static partial Regex StatementShape();
}

/// <summary>A failed PostgreSQL step, carrying only a filtered diagnostic, never its input or command line.</summary>
public sealed record PostgresStepFailure(string Step, int ExitCode, string Diagnostic)
{
    public override string ToString() => $"PostgreSQL {Step} exited {ExitCode}." +
        (Diagnostic.Length == 0 ? "" : "\n" + Diagnostic);
}
