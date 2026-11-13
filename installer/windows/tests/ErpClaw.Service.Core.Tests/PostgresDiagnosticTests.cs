using System.Text;
using ErpClaw.Service.Core;
using Xunit;

namespace ErpClaw.Service.Core.Tests;

public class PostgresDiagnosticTests
{
    [Fact]
    public void Mutation_noise_and_secret_shapes_leave_only_error_lines()
    {
        // Probes describe shapes or construct repeated characters; none is a credential.
        var probes = new[]
        {
            "LOG: starting", "DETAIL: ordinary detail", "WARNING: ordinary warning",
            "ERROR: PaSsWoRd placeholder", "FATAL: VeRiFiEr placeholder",
            "PANIC: scram-sha-256$<iterations>:<salt>$<stored>:<server>",
            "ERROR: " + new string('x', 43), // base64url session-key shape
            "ERROR: " + new string('A', 26), // base32 sign-in-code shape
            "ERROR: md5" + new string('a', 32), // legacy verifier shape
            "ERROR: token=<placeholder>", "ERROR: postgresql://<authority>",
        };
        var safe = new[] { "FATAL: required include is absent", "ERROR: syntax error", "PANIC: disk failure", "initdb: could not open file" };
        Assert.Equal(string.Join('\n', safe), PostgresDiagnosticFilter.Filter(string.Join('\n', probes.Concat(safe))));
    }

    [Fact]
    public void Mutation_registered_secret_in_an_error_line_is_dropped()
    {
        var secrets = new SecretRegistry();
        secrets.Add("<registered placeholder>");
        Assert.Equal("ERROR: syntax error", PostgresDiagnosticFilter.Filter(
            "FATAL: <registered placeholder>\nERROR: syntax error", secrets: secrets));
    }

    [Fact]
    public async Task Mutation_excess_lines_and_characters_still_drain_to_eof()
    {
        using var reader = new StringReader(string.Join('\n', Enumerable.Repeat("ERROR: ordinary failure", 100)));
        var filter = new PostgresDiagnosticFilter(maxLines: 2, maxCharacters: 50);
        var diagnostic = await filter.ReadAsync(reader);
        Assert.Equal("ERROR: ordinary failure\nERROR: ordinary failure", diagnostic);
        Assert.True(diagnostic.Length <= 50);
        Assert.Equal(-1, reader.Peek());

        using var characters = new StringReader("ERROR: ordinary failure\nFATAL: another failure");
        Assert.Equal("ERROR: ordinary failure", await new PostgresDiagnosticFilter(maxCharacters: 30).ReadAsync(characters));
    }

    [Fact]
    public async Task Mutation_oversized_line_with_secret_suffix_is_dropped_whole_across_chunks()
    {
        using var reader = new StringReader("ERROR: " + new string(' ', 4096) + "PASSWORD <placeholder>\r\nFATAL: disk failure");
        Assert.Equal("FATAL: disk failure", await new PostgresDiagnosticFilter().ReadAsync(reader));
    }

    [Fact]
    public void Mutation_stdin_echo_with_error_words_is_never_diagnostic()
    {
        var input = "RAISE EXCEPTION 'ERROR: stdin sentinel';\n"u8.ToArray();
        Assert.Equal("ERROR: syntax error", PostgresDiagnosticFilter.Filter(
            "ERROR: syntax error\nSTATEMENT: RAISE EXCEPTION 'ERROR: stdin sentinel';\nRAISE EXCEPTION 'ERROR: stdin sentinel';\nERROR: ALTER ROLE placeholder;", input));
    }
}

public class PostgresStdinFramingTests
{
    private static readonly ServiceCommands Commands = ServiceCommands.Proposed(
        new ServicePaths(@"C:\P\ERPClaw\3.0.0", @"C:\Users\owner\ERPClaw"));

    // This is a byte-sensitive observer, not a PostgreSQL parser. It distinguishes candidate mutations only;
    // whether PostgreSQL accepts them still requires the Windows diagnostic observation.
    private static async Task ObserveMutation(string original, string mutated)
    {
        var expected = Encoding.UTF8.GetBytes(original);
        var launcher = new FakeLauncher(new Journal(), new FakeClock())
        {
            OnStart = child => child.Exit(child.Spec.StandardInput.Span.SequenceEqual(expected) ? 0 : 1),
        };
        Assert.Equal(0, await launcher.Start(Commands.SingleUser(expected)).Exited);
        Assert.Equal(1, await launcher.Start(Commands.SingleUser(Encoding.UTF8.GetBytes(mutated))).Exited);
    }

    private static string Setup(bool owner = false)
    {
        // Generate the verifier in memory; never embed a credential-shaped literal in a probe.
        var runtime = ScramVerifier.Compute("<runtime placeholder>", new byte[16]);
        var ownerValue = owner ? ScramVerifier.Compute("<owner placeholder>", new byte[16]) : null;
        return PostgresConfig.SingleUserSetup(Commands.Roles, runtime, ownerValue);
    }

    [Fact]
    public async Task Mutation_statement_separator_removed_is_observable_at_fake_launcher()
    {
        var sql = Setup();
        Assert.Equal(2, sql.Split('\n', StringSplitOptions.RemoveEmptyEntries).Length);
        await ObserveMutation(sql, sql.Replace(";\nALTER", "; ALTER", StringComparison.Ordinal));
    }

    [Fact]
    public async Task Mutation_lf_to_crlf_is_observable_at_fake_launcher()
    {
        var sql = Setup(owner: true);
        Assert.True(!sql.Contains('\r'), "Single-user input must retain LF line endings.");
        await ObserveMutation(sql, sql.Replace("\n", "\r\n", StringComparison.Ordinal));
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task Mutation_verifier_single_quotes_to_double_quotes_is_observable_at_fake_launcher(bool owner)
    {
        var sql = Setup(owner);
        var alter = sql.Split('\n').First(line => line.StartsWith("ALTER ROLE", StringComparison.Ordinal));
        var value = alter[(alter.IndexOf("PASSWORD '", StringComparison.Ordinal) + "PASSWORD '".Length)..^2];
        Assert.True(alter.EndsWith("PASSWORD '" + value + "';", StringComparison.Ordinal),
            "ALTER ROLE must quote the generated value with single quotes.");
        var mutated = sql.Replace("PASSWORD '" + value + "'", "PASSWORD \"" + value + "\"", StringComparison.Ordinal);
        await ObserveMutation(sql, mutated);
    }

    [Fact]
    public async Task Mutation_trailing_newline_removed_is_observable_for_each_single_user_step()
    {
        foreach (var sql in new[] { Setup(), Setup(owner: true), PostgresConfig.SingleUserDatabaseCheck(Commands.Roles), PostgresConfig.SingleUserCreateDatabase(Commands.Roles) })
        {
            Assert.True(sql.EndsWith(";\n", StringComparison.Ordinal), "Each single-user input must end with a newline.");
            await ObserveMutation(sql, sql[..^1]);
        }
    }
}
