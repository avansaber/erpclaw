// The control pipe's protocol and server (CONTROL_PIPE.md; DESKTOP_HOST 2.4 item 2; BACKUP_RESTORE 2.3).
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using ErpClaw.Service.Core;
using Xunit;

namespace ErpClaw.Service.Core.Tests;

/// <summary>A connection: reads from the given bytes (counting them), writes to a buffer.</summary>
public sealed class DuplexStream : Stream
{
    private readonly MemoryStream _in;

    public DuplexStream(byte[] input) => _in = new MemoryStream(input);

    public MemoryStream Written { get; } = new();
    public long BytesRead => _in.Position;
    public bool Disposed { get; private set; }

    public string Output => Encoding.UTF8.GetString(Written.ToArray());

    public override bool CanRead => true;
    public override bool CanSeek => false;
    public override bool CanWrite => true;
    public override long Length => throw new NotSupportedException();
    public override long Position { get => throw new NotSupportedException(); set => throw new NotSupportedException(); }
    public override void Flush() { }
    public override int Read(byte[] buffer, int offset, int count) => _in.Read(buffer, offset, count);
    public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
    public override void SetLength(long value) => throw new NotSupportedException();
    public override void Write(byte[] buffer, int offset, int count) => Written.Write(buffer, offset, count);

    protected override void Dispose(bool disposing)
    {
        Disposed = true;
        base.Dispose(disposing);
    }
}

public class ControlProtocolTests
{
    private static ControlRequest Parse(string json) => ControlProtocol.ParseRequest(Encoding.UTF8.GetBytes(json));

    private static string ErrorOf(string json) => Assert.Throws<ControlProtocolException>(() => Parse(json)).Error;

    [Theory]
    [InlineData("""{"op":"mint_code","principal":"owner","run_id":"0123456789abcdef0123456789abcdef"}""", "mint_code", "owner")]
    [InlineData("""{"op":"restore","backup":"erpclaw-2026-11-01.zip"}""", "restore", "erpclaw-2026-11-01.zip")]
    [InlineData("""{"op":"remove_replaced","name":"erpclaw_old"}""", "remove_replaced", "erpclaw_old")]
    [InlineData("""{"op":"backup_now"}""", "backup_now", null)]
    [InlineData("""{"op":"list_backups"}""", "list_backups", null)]
    [InlineData("""{"op":"status"}""", "status", null)]
    [InlineData("""{"op":"quit"}""", "quit", null)]
    public void Each_op_parses_with_its_one_argument(string json, string op, string? argument)
    {
        var request = Parse(json);
        Assert.Equal(op, request.Op);
        Assert.Equal(argument, request.Argument);
        Assert.Null(request.Challenge);
        Assert.Equal(op == "mint_code" ? "0123456789abcdef0123456789abcdef" : null, request.RunId);
    }

    [Fact]
    public void A_challenge_is_base64_of_16_to_64_bytes()
    {
        var challenge = RandomNumberGenerator.GetBytes(32);
        var request = Parse($$"""{"op":"status","challenge":"{{Convert.ToBase64String(challenge)}}"}""");
        Assert.Equal(challenge, request.Challenge);

        Assert.Equal(ControlErrors.BadRequest, ErrorOf("""{"op":"status","challenge":"AAAA"}"""));
        Assert.Equal(ControlErrors.BadRequest, ErrorOf("""{"op":"status","challenge":"not base64!"}"""));
    }

    [Theory]
    [InlineData("""{"op":"mint_code"}""")]
    [InlineData("""{"op":"mint_code","principal":"owner"}""")]
    [InlineData("""{"op":"mint_code","principal":"owner","run_id":""}""")]
    [InlineData("""{"op":"mint_code","principal":"owner","run_id":"has space"}""")]
    [InlineData("""{"op":"status","run_id":"0123456789abcdef0123456789abcdef"}""")]
    [InlineData("""{"op":"mint_code","principal":""}""")]
    [InlineData("""{"op":"mint_code","principal":7}""")]
    [InlineData("""{"op":"restore"}""")]
    [InlineData("""{"op":"restore","backup":"-x"}""")]
    [InlineData("""{"op":"remove_replaced","name":"a\nb"}""")]
    [InlineData("""{"op":"backup_now","principal":"owner"}""")]
    [InlineData("""{"op":"status","extra":1}""")]
    [InlineData("""{"principal":"owner"}""")]
    [InlineData("""[]""")]
    [InlineData("""not json""")]
    [InlineData("""{"op":"status"} {"op":"quit"}""")]
    public void Malformed_requests_are_bad_requests(string json)
    {
        Assert.Equal(ControlErrors.BadRequest, ErrorOf(json));
    }

    [Fact]
    public void An_argument_over_256_characters_is_a_bad_request()
    {
        Assert.Equal(ControlErrors.BadRequest, ErrorOf($$"""{"op":"mint_code","principal":"{{new string('a', 257)}}","run_id":"ab"}"""));
    }

    [Fact]
    public void An_unknown_op_is_named_as_such()
    {
        Assert.Equal(ControlErrors.UnknownOp, ErrorOf("""{"op":"drop_everything"}"""));
    }

    [Fact]
    public async Task Reading_stops_at_the_newline_and_reads_nothing_after_it()
    {
        var stream = new DuplexStream(Encoding.UTF8.GetBytes("{\"op\":\"status\"}\nGARBAGE"));
        var request = await ControlProtocol.ReadRequestAsync(stream, CancellationToken.None);
        Assert.Equal("status", request.Op);
        Assert.Equal(16, stream.BytesRead);
    }

    [Fact]
    public async Task A_request_with_no_newline_or_too_long_is_refused_without_reading_past_the_limit()
    {
        var unterminated = new DuplexStream(Encoding.UTF8.GetBytes("{\"op\":\"status\"}"));
        var e1 = await Assert.ThrowsAsync<ControlProtocolException>(() => ControlProtocol.ReadRequestAsync(unterminated, CancellationToken.None));
        Assert.Equal(ControlErrors.BadRequest, e1.Error);

        var huge = new DuplexStream(Encoding.UTF8.GetBytes(new string('x', 100_000) + "\n"));
        var e2 = await Assert.ThrowsAsync<ControlProtocolException>(() => ControlProtocol.ReadRequestAsync(huge, CancellationToken.None));
        Assert.Equal(ControlErrors.BadRequest, e2.Error);
        Assert.True(huge.BytesRead <= ControlProtocol.MaxRequestBytes);
    }

    [Fact]
    public void Responses_are_one_json_line()
    {
        var bytes = ControlProtocol.SerializeResponse(ControlResponse.Success(JsonNode.Parse("""{"file":"b.zip"}""")), null);
        var text = Encoding.UTF8.GetString(bytes);
        Assert.EndsWith("\n", text);
        Assert.Single(text.Split('\n', StringSplitOptions.RemoveEmptyEntries));
        var doc = JsonNode.Parse(text)!;
        Assert.True((bool)doc["ok"]!);
        Assert.Equal("b.zip", (string?)doc["result"]!["file"]);
    }

    [Fact]
    public void A_failure_carries_error_text_and_reason()
    {
        var text = Encoding.UTF8.GetString(ControlProtocol.SerializeResponse(
            ControlResponse.Failure(ControlErrors.Refused, "That is not allowed now.", "not_allowed"), null));
        var doc = JsonNode.Parse(text)!;
        Assert.False((bool)doc["ok"]!);
        Assert.Equal("refused", (string?)doc["error"]);
        Assert.Equal("That is not allowed now.", (string?)doc["text"]);
        Assert.Equal("not_allowed", (string?)doc["reason"]);
    }

    [Fact]
    public void A_minted_code_is_only_in_the_written_json_never_in_ToString()
    {
        var response = ControlResponse.Minted(new Secret("ABCDEFGHIJKLMNOPQRSTUVWXYZ"));
        Assert.DoesNotContain("ABCDEFGHIJKLMNOPQRSTUVWXYZ", response.ToString());
        var doc = JsonNode.Parse(ControlProtocol.SerializeResponse(response, null))!;
        Assert.Equal("ABCDEFGHIJKLMNOPQRSTUVWXYZ", (string?)doc["code"]);
    }

    [Fact]
    public void The_proof_is_hmac_sha256_over_the_label_and_the_challenge()
    {
        var key = RandomNumberGenerator.GetBytes(32);
        var challenge = RandomNumberGenerator.GetBytes(32);
        var expected = HMACSHA256.HashData(key, Encoding.UTF8.GetBytes(ControlProtocol.ProofLabel).Concat(challenge).ToArray());
        Assert.Equal(expected, ControlProtocol.Proof(key, challenge));
    }

    [Theory]
    [InlineData(0, true, null)]
    [InlineData(1, false, ControlErrors.Internal)]
    [InlineData(2, false, ControlErrors.Refused)]
    [InlineData(3, false, ControlErrors.CouldNotRun)]
    [InlineData(4, false, ControlErrors.FailedUnchanged)]
    public void A_command_outcome_maps_to_a_response(int exit, bool ok, string? error)
    {
        var response = ControlResponse.From(Outcome.ForExit(exit));
        Assert.Equal(ok, response.Ok);
        Assert.Equal(error, response.Error);
        if (!ok) Assert.Equal(Outcome.ForExit(exit).Text, response.Text);
    }

    [Fact]
    public void Malformed_command_output_is_an_internal_error()
    {
        var response = ControlResponse.From(Outcome.Of(0, "not json"));
        Assert.False(response.Ok);
        Assert.Equal(ControlErrors.Internal, response.Error);
        Assert.Equal(CommandOutcomes.InternalText, response.Text);
    }
}

public class ControlServerTests
{
    private sealed class Peers(bool trusted) : IPeerVerifier
    {
        public List<int> Asked { get; } = new();

        public bool IsTrustedHost(int clientProcessId)
        {
            Asked.Add(clientProcessId);
            return trusted;
        }
    }

    private sealed class FixedKey(byte[]? key) : IHostCheckKey
    {
        public byte[]? Key { get; } = key;
    }

    private sealed class Handler(Func<ControlRequest, ControlResponse> answer) : IControlHandler
    {
        public List<ControlRequest> Seen { get; } = new();

        public Task<ControlResponse> HandleAsync(ControlRequest request, CancellationToken ct)
        {
            Seen.Add(request);
            return Task.FromResult(answer(request));
        }
    }

    private static (ControlServer server, MemorySink sink) Server(IPeerVerifier peers, IControlHandler handler, byte[]? key = null)
    {
        var sink = new MemorySink();
        return (new ControlServer(peers, handler, new FixedKey(key), new ServiceLog(sink, new FakeClock(), new SecretRegistry())), sink);
    }

    private static DuplexStream Connection(string line) => new(Encoding.UTF8.GetBytes(line + "\n"));

    [Fact]
    public async Task A_peer_failing_the_check_is_closed_with_nothing_read_and_nothing_answered()
    {
        var peers = new Peers(false);
        var handler = new Handler(_ => ControlResponse.Success());
        var (server, sink) = Server(peers, handler);
        var connection = Connection("""{"op":"mint_code","principal":"owner","run_id":"0123456789abcdef0123456789abcdef"}""");

        await server.ServeConnectionAsync(connection, 4242, CancellationToken.None);

        Assert.Equal(new[] { 4242 }, peers.Asked);
        Assert.Equal(0, connection.BytesRead);
        Assert.Equal(0, connection.Written.Length);
        Assert.Empty(handler.Seen);
        Assert.True(connection.Disposed);
        Assert.Contains(sink.Lines, l => l.Contains("refused"));
    }

    [Fact]
    public async Task A_trusted_peer_gets_one_response_line_then_the_connection_closes()
    {
        var handler = new Handler(_ => ControlResponse.Minted(new Secret("ABCDEFGHIJKLMNOPQRSTUVWXYZ")));
        var (server, sink) = Server(new Peers(true), handler);
        var connection = Connection("""{"op":"mint_code","principal":"owner","run_id":"0123456789abcdef0123456789abcdef"}""");

        await server.ServeConnectionAsync(connection, 1, CancellationToken.None);

        Assert.Equal("owner", handler.Seen.Single().Argument);
        var doc = JsonNode.Parse(connection.Output)!;
        Assert.Equal("ABCDEFGHIJKLMNOPQRSTUVWXYZ", (string?)doc["code"]);
        Assert.True(connection.Disposed);
        Assert.DoesNotContain(sink.Lines, l => l.Contains("ABCDEFGHIJKLMNOPQRSTUVWXYZ"));
    }

    [Fact]
    public async Task A_bad_request_is_answered_with_the_plain_error_and_reaches_no_handler()
    {
        var handler = new Handler(_ => ControlResponse.Success());
        var (server, _) = Server(new Peers(true), handler);
        var connection = Connection("""{"op":"nope"}""");

        await server.ServeConnectionAsync(connection, 1, CancellationToken.None);

        Assert.Empty(handler.Seen);
        Assert.Equal("unknown_op", (string?)JsonNode.Parse(connection.Output)!["error"]);
    }

    [Fact]
    public async Task A_handler_exception_answers_internal_and_never_echoes_its_message()
    {
        var handler = new Handler(_ => throw new InvalidOperationException("leaked ABCDEFGHIJKLMNOPQRSTUVWXYZ"));
        var (server, sink) = Server(new Peers(true), handler);
        var connection = Connection("""{"op":"status"}""");

        await server.ServeConnectionAsync(connection, 1, CancellationToken.None);

        Assert.DoesNotContain("ABCDEFGHIJKLMNOPQRSTUVWXYZ", connection.Output);
        Assert.Equal("internal", (string?)JsonNode.Parse(connection.Output)!["error"]);
        Assert.DoesNotContain(sink.Lines, l => l.Contains("ABCDEFGHIJKLMNOPQRSTUVWXYZ"));
    }

    [Fact]
    public async Task A_challenge_is_answered_with_the_proof_when_the_service_holds_the_key()
    {
        var key = RandomNumberGenerator.GetBytes(32);
        var challenge = RandomNumberGenerator.GetBytes(32);
        var (server, _) = Server(new Peers(true), new Handler(_ => ControlResponse.WithState("running", null)), key);
        var connection = Connection($$"""{"op":"status","challenge":"{{Convert.ToBase64String(challenge)}}"}""");

        await server.ServeConnectionAsync(connection, 1, CancellationToken.None);

        var doc = JsonNode.Parse(connection.Output)!;
        Assert.Equal(Convert.ToBase64String(ControlProtocol.Proof(key, challenge)), (string?)doc["proof"]);
        Assert.Equal("running", (string?)doc["state"]);
    }

    [Fact]
    public async Task The_proof_is_the_hmac_of_the_service_secret_over_the_ascii_label_and_the_challenge_bytes()
    {
        var session = ServiceSession.Create("S-1-5-21-1-2-3-1001");
        var challenge = RandomNumberGenerator.GetBytes(24);
        var sink = new MemorySink();
        var server = new ControlServer(new Peers(true), new Handler(_ => ControlResponse.Success()), session,
            new ServiceLog(sink, new FakeClock(), new SecretRegistry()));
        var connection = Connection($$"""{"op":"status","challenge":"{{Convert.ToBase64String(challenge)}}"}""");

        await server.ServeConnectionAsync(connection, 1, CancellationToken.None);

        var expected = System.Security.Cryptography.HMACSHA256.HashData(
            Convert.FromHexString(session.Secret),
            Encoding.ASCII.GetBytes("erpclaw-service-check-v1").Concat(challenge).ToArray());
        Assert.Equal(Convert.ToBase64String(expected), (string?)JsonNode.Parse(connection.Output)!["proof"]);
    }

    [Fact]
    public async Task A_peer_failing_the_check_gets_no_proof_and_its_challenge_is_never_read()
    {
        var session = ServiceSession.Create("S-1-5-21-1-2-3-1001");
        var server = new ControlServer(new Peers(false), new Handler(_ => ControlResponse.Success()), session,
            new ServiceLog(new MemorySink(), new FakeClock(), new SecretRegistry()));
        var connection = Connection($$"""{"op":"status","challenge":"{{Convert.ToBase64String(RandomNumberGenerator.GetBytes(16))}}"}""");

        await server.ServeConnectionAsync(connection, 1, CancellationToken.None);

        Assert.Equal(0, connection.BytesRead);
        Assert.Equal(0, connection.Written.Length);
    }

    [Fact]
    public async Task No_challenge_no_proof()
    {
        var (server, _) = Server(new Peers(true), new Handler(_ => ControlResponse.Success()), RandomNumberGenerator.GetBytes(32));
        var connection = Connection("""{"op":"status"}""");
        await server.ServeConnectionAsync(connection, 1, CancellationToken.None);
        Assert.Null(JsonNode.Parse(connection.Output)!["proof"]);
    }
}

public class CommandOutcomeTests
{
    [Theory]
    [InlineData(0, OutcomeKind.Done)]
    [InlineData(1, OutcomeKind.Internal)]
    [InlineData(2, OutcomeKind.Refused)]
    [InlineData(3, OutcomeKind.CouldNotRun)]
    [InlineData(4, OutcomeKind.FailedUnchanged)]
    [InlineData(5, OutcomeKind.Internal)]
    [InlineData(-1, OutcomeKind.Internal)]
    public void Exit_codes_map_to_plain_outcomes(int exit, OutcomeKind kind)
    {
        var outcome = CommandOutcomes.Parse(exit, """{"reason":"r","text":"A plain sentence."}""");
        Assert.Equal(kind, outcome.Kind);
        Assert.Equal(exit, outcome.ExitCode);
    }

    [Fact]
    public void Done_keeps_the_object_and_has_no_text()
    {
        var outcome = CommandOutcomes.Parse(0, """{"file":"b.zip","size":3}""" + "\r\n");
        Assert.True(outcome.Succeeded);
        Assert.Equal("b.zip", outcome.Body!.Value.GetProperty("file").GetString());
        Assert.Equal("", outcome.Text);
    }

    [Fact]
    public void A_refusal_carries_its_reason_and_text()
    {
        var outcome = CommandOutcomes.Parse(2, """{"reason":"pointer_names_it","text":"Those books are still in use."}""");
        Assert.Equal("pointer_names_it", outcome.Reason);
        Assert.Equal("Those books are still in use.", outcome.Text);
    }

    [Fact]
    public void Internal_always_says_the_fixed_sentence()
    {
        var outcome = CommandOutcomes.Parse(1, """{"text":"Traceback at C:\\Users\\x\\secret.py"}""");
        Assert.Equal(CommandOutcomes.InternalText, outcome.Text);
    }

    [Theory]
    [InlineData(2)]
    [InlineData(3)]
    [InlineData(4)]
    public void A_failure_without_text_gets_a_plain_default(int exit)
    {
        var outcome = CommandOutcomes.Parse(exit, "{}");
        Assert.False(string.IsNullOrWhiteSpace(outcome.Text));
    }

    [Theory]
    [InlineData("")]
    [InlineData("not json")]
    [InlineData("[1,2]")]
    [InlineData("""{"a":1}{"b":2}""")]
    [InlineData("""{"a":1} trailing""")]
    [InlineData("\"ABCDEFGHIJKLMNOPQRSTUVWXYZ\"")]
    public void Output_that_is_not_exactly_one_object_is_malformed_and_never_copied(string stdout)
    {
        var outcome = CommandOutcomes.Parse(0, stdout);
        Assert.Equal(OutcomeKind.Malformed, outcome.Kind);
        Assert.Equal(CommandOutcomes.InternalText, outcome.Text);
        Assert.DoesNotContain("ABCDEFGHIJKLMNOPQRSTUVWXYZ", outcome.ToString());
    }

    [Fact]
    public void ToString_never_shows_the_body()
    {
        var outcome = CommandOutcomes.Parse(0, """{"code":"ABCDEFGHIJKLMNOPQRSTUVWXYZ"}""");
        Assert.DoesNotContain("ABCDEFGHIJKLMNOPQRSTUVWXYZ", outcome.ToString());
        Assert.Contains("Done", outcome.ToString());
    }

    [Fact]
    public async Task The_runner_starts_the_command_capturing_stdout_and_parses_it()
    {
        var journal = new Journal();
        var launcher = new FakeLauncher(journal, new FakeClock())
        {
            OnStart = child =>
            {
                child.StandardOutput = """{"database":"erpclaw","assistant_root":"a","previous":[],"source":"current"}""";
                child.Exit(0);
            },
        };
        var commands = ServiceCommands.Proposed(new ServicePaths(@"C:\P", @"C:\D"));

        var outcome = await new CommandRunner(launcher).RunAsync(commands.Pointer(), CancellationToken.None);

        Assert.True(outcome.Succeeded);
        Assert.True(launcher.Started.Single().Spec.CaptureOutput);
        Assert.Equal("erpclaw", outcome.Body!.Value.GetProperty("database").GetString());
    }

    private sealed class RefusedLauncher : IProcessLauncher
    {
        public IChildProcess Start(ProcessSpec spec) => throw new System.ComponentModel.Win32Exception(2, "private launch details");
    }

    [Fact]
    public async Task A_thrown_launch_error_is_a_plain_could_not_run_outcome()
    {
        var spec = ServiceCommands.Proposed(new ServicePaths(@"C:\P", @"C:\D")).Pointer();
        var outcome = await new CommandRunner(new RefusedLauncher()).RunAsync(spec, CancellationToken.None);
        Assert.Equal(OutcomeKind.CouldNotRun, outcome.Kind);
        Assert.Equal(3, outcome.ExitCode);
        Assert.Null(outcome.Body);
        Assert.DoesNotContain("private", outcome.Text);
        Assert.DoesNotContain("private", outcome.ToString());
    }
}

public class ProbeAndMinterTests
{
    private static readonly ServiceCommands Commands = ServiceCommands.Proposed(new ServicePaths(@"C:\P", @"C:\D"));

    private sealed class Scripted(CommandOutcome outcome) : ICommandRunner
    {
        public List<ProcessSpec> Specs { get; } = new();

        public Task<CommandOutcome> RunAsync(ProcessSpec spec, CancellationToken ct)
        {
            Specs.Add(spec);
            return Task.FromResult(outcome);
        }
    }

    [Theory]
    [InlineData(0, """{"login":true,"books":true}""", true, BooksState.Present)]
    [InlineData(0, """{"login":true,"books":false}""", true, BooksState.Absent)]
    [InlineData(0, """{"login":false,"books":false}""", false, BooksState.Unknown)]
    [InlineData(3, """{"reason":"database_down","text":"The database is not running."}""", false, BooksState.Unknown)]
    [InlineData(0, """{"login":true}""", true, BooksState.Unknown)]
    public async Task Db_check_answers_both_probes(int exit, string stdout, bool login, BooksState books)
    {
        var runner = new Scripted(Outcome.Of(exit, stdout));
        var probe = new DbCheckProbe(runner, Commands);
        Assert.Equal(login, await probe.CanLoginAsync(50001, CancellationToken.None));
        Assert.Equal(books, await probe.CheckAsync(CancellationToken.None));
        Assert.All(runner.Specs, s => Assert.Equal(Commands.DbCheck().Arguments, s.Arguments));
    }

    [Fact]
    public async Task The_minter_returns_the_code_from_the_command()
    {
        var runner = new Scripted(Outcome.Of(0, """{"code":"ABCDEFGHIJKLMNOPQRSTUVWXYZ"}"""));
        var result = await new CommandCodeMinter(runner, Commands).MintAsync("owner", "0123456789abcdef0123456789abcdef", CancellationToken.None);
        Assert.Equal(MintStatus.Minted, result.Status);
        Assert.Equal("ABCDEFGHIJKLMNOPQRSTUVWXYZ", result.Code!.Reveal());
        Assert.Equal(Commands.MintCode("owner", "0123456789abcdef0123456789abcdef").Arguments, runner.Specs.Single().Arguments);
        Assert.DoesNotContain("ABCDEFGHIJKLMNOPQRSTUVWXYZ", result.ToString());
    }

    [Fact]
    public async Task An_unknown_principal_gives_no_code()
    {
        var runner = new Scripted(Outcome.Of(2, """{"reason":"unknown_principal","text":"There is no one by that name."}"""));
        var result = await new CommandCodeMinter(runner, Commands).MintAsync("stranger", "0123456789abcdef0123456789abcdef", CancellationToken.None);
        Assert.Equal(MintStatus.UnknownPrincipal, result.Status);
        Assert.Null(result.Code);
    }

    [Theory]
    [InlineData(0, """{"code":"short"}""")]
    [InlineData(0, """{"code":"ABCDEFGHIJKLMNOPQRSTUVWXY1"}""")]
    [InlineData(0, """{}""")]
    [InlineData(3, """{"reason":"database_down","text":"The database is not running."}""")]
    [InlineData(2, """{"reason":"disabled","text":"That person is turned off."}""")]
    public async Task Anything_else_is_a_failure_that_shows_no_output(int exit, string stdout)
    {
        var runner = new Scripted(Outcome.Of(exit, stdout));
        var result = await new CommandCodeMinter(runner, Commands).MintAsync("owner", "0123456789abcdef0123456789abcdef", CancellationToken.None);
        Assert.Equal(MintStatus.Failed, result.Status);
        Assert.Null(result.Code);
        Assert.DoesNotContain("ABCDEFGHIJKLMNOPQRSTUVWXY1", result.Text);
    }
}
