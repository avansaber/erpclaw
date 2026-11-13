// The service's control pipe (DESKTOP_HOST 2.4 item 2, BACKUP_RESTORE 2.3): the protocol over a stream. The
// framing is documented for the engine side in installer/windows/CONTROL_PIPE.md; keep the two in step.
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace ErpClaw.Service.Core;

public static class ControlOps
{
    public const string MintCode = "mint_code";
    public const string Status = "status";
    public const string BackupNow = "backup_now";
    public const string ListBackups = "list_backups";
    public const string Restore = "restore";
    public const string RemoveReplaced = "remove_replaced";
    public const string Quit = "quit";
}

public static class ControlErrors
{
    public const string BadRequest = "bad_request";
    public const string UnknownOp = "unknown_op";
    public const string Unavailable = "unavailable";
    public const string UnknownPrincipal = "unknown_principal";
    public const string Refused = "refused";
    public const string CouldNotRun = "could_not_run";
    public const string FailedUnchanged = "failed_unchanged";
    public const string Internal = "internal";
    public const string NotRestarted = "not_restarted";
    public const string StaleRun = "stale_run";
}

/// <summary>
/// One request. <see cref="Argument"/> is the op's one argument (principal, backup or name); <see cref="RunId"/> is
/// mint_code's run id, the engine run the host has just verified (BROWSER_AUTH 2.3c item 7).
/// </summary>
public sealed record ControlRequest(string Op, string? Argument, byte[]? Challenge, string? RunId = null);

/// <summary>One response. A minted code is carried as a <see cref="Secret"/> and appears only in the written JSON.</summary>
public sealed class ControlResponse
{
    private ControlResponse(bool ok) => Ok = ok;

    public bool Ok { get; }
    public string? Error { get; private init; }
    public string? Text { get; private init; }
    public string? Reason { get; private init; }
    public JsonNode? Result { get; private init; }
    public Secret? Code { get; private init; }
    public string? State { get; private init; }

    public static ControlResponse Success(JsonNode? result = null) => new(true) { Result = result };
    public static ControlResponse Minted(Secret code) => new(true) { Code = code };
    public static ControlResponse WithState(string state, string? reason) => new(true) { State = state, Reason = reason };

    public static ControlResponse Failure(string error, string text, string? reason = null) =>
        new(false) { Error = error, Text = text, Reason = reason };

    /// <summary>The engine command's outcome as a response: its stdout object on success, a plain error otherwise.</summary>
    public static ControlResponse From(CommandOutcome outcome) => outcome.Kind switch
    {
        OutcomeKind.Done => Success(outcome.Body is { } body ? JsonNode.Parse(body.GetRawText()) : null),
        OutcomeKind.Refused => Failure(ControlErrors.Refused, outcome.Text, outcome.Reason),
        OutcomeKind.CouldNotRun => Failure(ControlErrors.CouldNotRun, outcome.Text, outcome.Reason),
        OutcomeKind.FailedUnchanged => Failure(ControlErrors.FailedUnchanged, outcome.Text, outcome.Reason),
        _ => Failure(ControlErrors.Internal, CommandOutcomes.InternalText),
    };

    public override string ToString() => Ok ? "ControlResponse { Ok }" : $"ControlResponse {{ Error = {Error} }}";
}

public sealed class ControlProtocolException : Exception
{
    public ControlProtocolException(string error, string text) : base(text) => Error = error;

    public string Error { get; }
}

/// <summary>
/// The framing, in one place (CONTROL_PIPE.md says the same): one connection carries exactly one request
/// and one response. The request is one UTF-8 JSON object on one line ending in "\n", at most
/// <see cref="MaxRequestBytes"/> bytes with the newline. The response is one JSON object on one line ending in "\n";
/// then the service closes the connection.
/// </summary>
public static class ControlProtocol
{
    /// <summary>A request line's largest size in bytes, the newline included.</summary>
    public const int MaxRequestBytes = 4096;

    /// <summary>The label of the server's proof (an HMAC-SHA256 over the label and the host's challenge).</summary>
    public const string ProofLabel = "erpclaw-service-check-v1";

    private const int MaxArgument = 256;

    /// <summary>Each op and the name of its one argument (null: none).</summary>
    private static readonly Dictionary<string, string?> Ops = new(StringComparer.Ordinal)
    {
        [ControlOps.MintCode] = "principal",
        [ControlOps.Restore] = "backup",
        [ControlOps.RemoveReplaced] = "name",
        [ControlOps.BackupNow] = null,
        [ControlOps.ListBackups] = null,
        [ControlOps.Status] = null,
        [ControlOps.Quit] = null,
    };

    /// <summary>Reads exactly one newline-terminated UTF-8 JSON request; throws <see cref="ControlProtocolException"/>.</summary>
    public static async Task<ControlRequest> ReadRequestAsync(Stream stream, CancellationToken ct)
    {
        var line = new byte[MaxRequestBytes];
        var one = new byte[1];
        for (int length = 0; length < MaxRequestBytes; length++)
        {
            // One byte at a time, so nothing past the newline is read.
            if (await stream.ReadAsync(one, ct).ConfigureAwait(false) == 0)
                throw Bad("The request ended before its newline.");
            if (one[0] == (byte)'\n') return ParseRequest(line.AsSpan(0, length));
            line[length] = one[0];
        }
        throw Bad("The request is too long.");
    }

    public static ControlRequest ParseRequest(ReadOnlySpan<byte> line)
    {
        JsonElement root;
        try
        {
            var text = new UTF8Encoding(false, true).GetString(line);
            using var doc = JsonDocument.Parse(text);
            root = doc.RootElement.Clone();
        }
        catch (Exception e) when (e is JsonException or DecoderFallbackException)
        {
            throw Bad("The request is not one JSON object.");
        }
        if (root.ValueKind != JsonValueKind.Object) throw Bad("The request is not one JSON object.");

        var op = String(root, "op") ?? throw Bad("The request names no op.");
        if (!Ops.TryGetValue(op, out var argumentName))
            throw new ControlProtocolException(ControlErrors.UnknownOp, "The service does not know that request.");

        var allowed = new HashSet<string>(StringComparer.Ordinal) { "op", "challenge" };
        if (argumentName is not null) allowed.Add(argumentName);
        if (op == ControlOps.MintCode) allowed.Add("run_id");
        foreach (var property in root.EnumerateObject())
            if (!allowed.Contains(property.Name)) throw Bad("The request has a field its op does not take.");

        string? argument = null;
        if (argumentName is not null) argument = Argument(root, argumentName);
        string? runId = null;
        if (op == ControlOps.MintCode)
        {
            runId = String(root, "run_id");
            if (runId is null || runId.Length is 0 or > 128 || !runId.All(c => char.IsAsciiLetterOrDigit(c) || c is '-' or '_'))
                throw Bad("mint_code needs the run id the host verified.");
        }

        byte[]? challenge = null;
        if (root.TryGetProperty("challenge", out _))
        {
            var text = String(root, "challenge") ?? throw Bad("The challenge is not text.");
            try
            {
                challenge = Convert.FromBase64String(text);
            }
            catch (FormatException)
            {
                throw Bad("The challenge is not base64.");
            }
            if (challenge.Length is < 16 or > 64) throw Bad("The challenge must be 16 to 64 bytes.");
        }
        return new ControlRequest(op, argument, challenge, runId);
    }

    /// <summary>One line of JSON, newline-terminated; with <paramref name="proof"/> when the request carried a challenge.</summary>
    public static byte[] SerializeResponse(ControlResponse response, byte[]? proof)
    {
        var o = new JsonObject { ["ok"] = response.Ok };
        if (response.Error is not null) o["error"] = response.Error;
        if (response.Text is not null) o["text"] = response.Text;
        if (response.Reason is not null) o["reason"] = response.Reason;
        if (response.State is not null) o["state"] = response.State;
        if (response.Result is not null) o["result"] = response.Result.DeepClone();
        if (response.Code is not null) o["code"] = response.Code.Reveal();
        if (proof is not null) o["proof"] = Convert.ToBase64String(proof);
        return Encoding.UTF8.GetBytes(o.ToJsonString() + "\n");
    }

    public static byte[] Proof(byte[] key, byte[] challenge)
    {
        var label = Encoding.UTF8.GetBytes(ProofLabel);
        var message = new byte[label.Length + challenge.Length];
        label.CopyTo(message, 0);
        challenge.CopyTo(message, label.Length);
        return HMACSHA256.HashData(key, message);
    }

    private static string? String(JsonElement root, string name) =>
        root.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;

    private static string Argument(JsonElement root, string name)
    {
        var value = String(root, name);
        if (string.IsNullOrEmpty(value) || value.Length > MaxArgument || value[0] == '-' || value.Any(char.IsControl))
            throw Bad($"The request needs a plain {name}.");
        return value;
    }

    private static ControlProtocolException Bad(string text) => new(ControlErrors.BadRequest, text);
}

/// <summary>
/// Who is at the other end. Windows (still to write): GetNamedPipeClientProcessId, then the client's user SID equals
/// the service's, and its image is the signed host executable in the program folder (path and Authenticode publisher;
/// on the Store route, the package family and the install location), DESKTOP_HOST 2.3.
/// </summary>
public interface IPeerVerifier
{
    bool IsTrustedHost(int clientProcessId);
}

/// <summary>The key of the server's proof, shared with the host. Its distribution is still to be confirmed.</summary>
public interface IHostCheckKey
{
    byte[]? Key { get; }
}

public interface IControlHandler
{
    Task<ControlResponse> HandleAsync(ControlRequest request, CancellationToken ct);
}

/// <summary>Serves one connection: the peer check before reading anything, one request, one response, close.</summary>
public sealed class ControlServer
{
    private readonly IPeerVerifier _peers;
    private readonly IControlHandler _handler;
    private readonly IHostCheckKey _key;
    private readonly ServiceLog _log;

    public ControlServer(IPeerVerifier peers, IControlHandler handler, IHostCheckKey key, ServiceLog log)
    {
        _peers = peers;
        _handler = handler;
        _key = key;
        _log = log;
    }

    public async Task ServeConnectionAsync(Stream stream, int clientProcessId, CancellationToken ct)
    {
        await using var _ = stream;
        if (!_peers.IsTrustedHost(clientProcessId))
        {
            _log.Write("A control connection was refused: the other end is not ERPClaw's window.");
            return;
        }

        ControlResponse response;
        byte[]? challenge = null;
        try
        {
            var request = await ControlProtocol.ReadRequestAsync(stream, ct).ConfigureAwait(false);
            challenge = request.Challenge;
            response = await _handler.HandleAsync(request, ct).ConfigureAwait(false);
        }
        catch (ControlProtocolException e)
        {
            response = ControlResponse.Failure(e.Error, e.Message);
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            return;
        }
        catch (Exception e)
        {
            // Never the message: it may hold anything.
            _log.Write($"A control request failed inside the service ({e.GetType().Name}).");
            response = ControlResponse.Failure(ControlErrors.Internal, CommandOutcomes.InternalText);
        }

        var proof = challenge is not null && _key.Key is { } key ? ControlProtocol.Proof(key, challenge) : null;
        await stream.WriteAsync(ControlProtocol.SerializeResponse(response, proof), ct).ConfigureAwait(false);
        await stream.FlushAsync(ct).ConfigureAwait(false);
    }
}
