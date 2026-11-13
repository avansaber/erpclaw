// run\service.json (three strings), the per-start session behind it, and the log's refusals.
using System.Security.Cryptography;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Runtime.Versioning;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using ErpClaw.Service.Core;
using Microsoft.Win32.SafeHandles;
using Xunit;

namespace ErpClaw.Service.Core.Tests;

public class ServiceSessionTests
{
    private const string Sid = "S-1-5-21-1111111111-2222222222-3333333333-1001";

    [Fact]
    public void The_pipe_name_is_the_user_sid_and_128_random_bits_in_lower_case_hex()
    {
        var session = ServiceSession.Create(Sid);
        Assert.Matches(new Regex(@"^\\\\\.\\pipe\\erpclaw-service-" + Regex.Escape(Sid) + "-[0-9a-f]{32}$"), session.ControlPipe);
    }

    [Fact]
    public void The_secret_is_32_bytes_as_64_lower_case_hex_and_is_the_proof_key()
    {
        var session = ServiceSession.Create(Sid);
        Assert.Matches("^[0-9a-f]{64}$", session.Secret);
        Assert.Equal(Convert.FromHexString(session.Secret), session.Key);
    }

    [Fact]
    public void Each_start_gets_a_new_pipe_name_and_a_new_secret()
    {
        var a = ServiceSession.Create(Sid);
        var b = ServiceSession.Create(Sid);
        Assert.NotEqual(a.ControlPipe, b.ControlPipe);
        Assert.NotEqual(a.Secret, b.Secret);
    }

    [Theory]
    [InlineData("")]
    [InlineData("S-1-5-21-1\\..\\x")]
    [InlineData("owner")]
    [InlineData("S-1-5-21-1 -2")]
    public void Only_a_sid_can_be_part_of_the_pipe_name(string sid)
    {
        Assert.Throws<ArgumentException>(() => ServiceSession.Create(sid));
    }

    [Fact]
    public void The_file_content_holds_the_pipe_the_secret_and_the_port_as_text()
    {
        var session = ServiceSession.Create(Sid);
        var content = session.FileContent(50123);
        Assert.Equal(new ServiceFileContent(session.ControlPipe, session.Secret, "50123"), content);
        Assert.DoesNotContain(session.Secret, content.ToString());
        Assert.DoesNotContain(session.Secret, session.ToString());
        Assert.DoesNotContain(session.ControlPipe, session.ToString());
    }

    [Fact]
    public void The_pipe_name_and_secret_are_registered_so_the_log_refuses_them()
    {
        var secrets = new SecretRegistry();
        var session = ServiceSession.Create(Sid, secrets);
        var log = new ServiceLog(new MemorySink(), new FakeClock(), secrets);
        Assert.True(log.WouldRefuse("secret " + session.Secret));
        Assert.True(log.WouldRefuse("pipe " + session.ControlPipe));
    }
}

public class ServiceFileJsonTests
{
    private static readonly string Pipe = @"\\.\pipe\erpclaw-service-S-1-5-21-1-2-3-1001-" + new string('a', 32);
    private static readonly string Secret = new string('b', 64);

    private static ServiceFileContent Good => new(Pipe, Secret, "50123");

    private static ServiceFileContent? Parse(string json) => ServiceFileJson.Parse(Encoding.UTF8.GetBytes(json));

    private static string Json(string pipe, string secret, string port) =>
        new JsonObject { ["control_pipe"] = pipe, ["service_secret"] = secret, ["database_port"] = port }.ToJsonString();

    [Fact]
    public void The_file_has_exactly_the_three_string_keys_and_round_trips()
    {
        var bytes = ServiceFileJson.Serialize(Good);
        var node = JsonNode.Parse(bytes)!.AsObject();
        Assert.Equal(new[] { "control_pipe", "database_port", "service_secret" }, node.Select(p => p.Key).OrderBy(k => k, StringComparer.Ordinal));
        Assert.All(node, p => Assert.IsType<string>((string?)p.Value));
        Assert.True(bytes.Length <= 4096);
        Assert.Equal(Good, ServiceFileJson.Parse(bytes));
    }

    [Fact]
    public void The_file_name_is_service_json()
    {
        Assert.Equal("service.json", ServiceFileJson.FileName);
    }

    [Theory]
    [InlineData("")]
    [InlineData("[]")]
    [InlineData("{")]
    [InlineData("{\"control_pipe\":1}")]
    public void Anything_but_one_object_with_the_three_keys_is_refused(string json)
    {
        Assert.Null(Parse(json));
    }

    [Fact]
    public void A_missing_key_or_an_extra_key_is_refused()
    {
        Assert.Null(Parse($$"""{"control_pipe":{{JsonValue.Create(Pipe)!.ToJsonString()}},"service_secret":"{{Secret}}"}"""));
        var extra = Json(Pipe, Secret, "50123").TrimEnd('}') + ",\"more\":\"x\"}";
        Assert.Null(Parse(extra));
    }

    [Fact]
    public void A_key_given_twice_is_refused_even_when_the_values_agree()
    {
        var json = Json(Pipe, Secret, "50123").TrimEnd('}') + ",\"database_port\":\"50123\"}";
        Assert.Null(Parse(json));
    }

    [Fact]
    public void A_value_that_is_not_a_string_is_refused()
    {
        Assert.Null(Parse($$"""{"control_pipe":{{JsonValue.Create(Pipe)!.ToJsonString()}},"service_secret":"{{Secret}}","database_port":50123}"""));
    }

    [Fact]
    public void Text_after_the_object_is_refused()
    {
        Assert.Null(Parse(Json(Pipe, Secret, "50123") + " {}"));
    }

    [Fact]
    public void More_than_4096_bytes_is_refused_even_when_the_rest_is_blank()
    {
        var padded = Json(Pipe, Secret, "50123") + new string(' ', 4096);
        Assert.Null(Parse(padded));
        var exact = Json(Pipe, Secret, "50123");
        exact += new string(' ', 4096 - exact.Length);
        Assert.Equal(4096, exact.Length);
        Assert.NotNull(Parse(exact));
    }

    [Theory]
    [InlineData(@"\\.\pipe\erpclaw-service-S-1-5-21-1-2-3-1001-" + "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")]
    [InlineData(@"\\.\pipe\erpclaw-service-S-1-5-21-1-2-3-1001-" + "aaaa")]
    [InlineData(@"\\.\pipe\other-S-1-5-21-1-2-3-1001-" + "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")]
    [InlineData(@"\\server\pipe\erpclaw-service-S-1-5-21-1-2-3-1001-" + "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")]
    public void A_pipe_name_of_another_shape_is_refused(string pipe)
    {
        Assert.Null(Parse(Json(pipe, Secret, "50123")));
    }

    [Theory]
    [InlineData("BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB")]
    [InlineData("bbbb")]
    [InlineData("gggggggggggggggggggggggggggggggggggggggggggggggggggggggggggggggg")]
    public void A_secret_of_another_shape_is_refused(string secret)
    {
        Assert.Null(Parse(Json(Pipe, secret, "50123")));
    }

    [Theory]
    [InlineData("")]
    [InlineData("0")]
    [InlineData("65536")]
    [InlineData("-5")]
    [InlineData("05000")]
    [InlineData("50 123")]
    [InlineData("5e3")]
    public void A_port_that_is_not_plain_decimal_in_range_is_refused(string port)
    {
        Assert.Null(Parse(Json(Pipe, Secret, port)));
    }
}

[Collection(WindowsPermissionsCollection.Name)]
public class FileServiceFileStoreTests
{
    private static ServiceFileContent Content(int port = 50123) => ServiceSession.Create("S-1-5-21-1-2-3-1001").FileContent(port);

    [Fact]
    public void Write_leaves_one_file_that_parses_and_no_temporary_file_and_a_second_write_replaces_it()
    {
        var run = Directory.CreateTempSubdirectory("erpclaw-run-").FullName;
        try
        {
            var store = new FileServiceFileStore(run);
            var first = Content(50001);
            var second = Content(50002);
            store.Write(first);
            store.Write(second);
            Assert.Equal(new[] { "service.json" }, Directory.GetFiles(run).Select(Path.GetFileName));
            Assert.Equal(Path.Combine(run, "service.json"), store.FilePath);
            Assert.Equal(second, ServiceFileJson.Parse(File.ReadAllBytes(store.FilePath)));
        }
        finally
        {
            Directory.Delete(run, true);
        }
    }

    [Fact]
    public void Write_creates_the_file_for_its_owner_alone_and_Delete_removes_it()
    {
        var run = Directory.CreateTempSubdirectory("erpclaw-run-").FullName;
        try
        {
            var store = new FileServiceFileStore(run);
            store.Write(Content());
            if (!OperatingSystem.IsWindows())
                Assert.Equal(UnixFileMode.UserRead | UnixFileMode.UserWrite, File.GetUnixFileMode(store.FilePath));
            else
                AssertProtectedOwnerDacl(store.FilePath);
            store.Delete();
            Assert.False(File.Exists(store.FilePath));
            store.Delete(); // nothing there: no error
        }
        finally
        {
            Directory.Delete(run, true);
        }
    }

    [SupportedOSPlatform("windows")]
    private static void AssertProtectedOwnerDacl(string path)
    {
        using var identity = WindowsIdentity.GetCurrent();
        var security = new FileInfo(path).GetAccessControl(AccessControlSections.Access);
        Assert.True(security.AreAccessRulesProtected);
        var rule = Assert.Single(security.GetAccessRules(true, true, typeof(SecurityIdentifier)).Cast<FileSystemAccessRule>());
        Assert.False(rule.IsInherited);
        Assert.Equal(identity.User, rule.IdentityReference);
        Assert.Equal(AccessControlType.Allow, rule.AccessControlType);
        Assert.Equal(FileSystemRights.FullControl, rule.FileSystemRights);
    }

    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool LogonUser(string user, string domain, string password, int logonType, int provider,
        out SafeAccessTokenHandle token);

    [WindowsFact]
    public void Another_ordinary_account_cannot_read_or_write_the_file_even_after_replacement()
    {
        if (!OperatingSystem.IsWindows()) return;
        // CI provisions a second ordinary local account. Fail on Windows if its credentials were not supplied.
        var user = Environment.GetEnvironmentVariable("ERPCLAW_TEST_OTHER_USER");
        var otherLogonSecret = Environment.GetEnvironmentVariable("ERPCLAW_TEST_OTHER_PASSWORD");
        Assert.False(string.IsNullOrWhiteSpace(user), "Set ERPCLAW_TEST_OTHER_USER for Windows CI.");
        Assert.False(string.IsNullOrWhiteSpace(otherLogonSecret), "Set ERPCLAW_TEST_OTHER_PASSWORD for Windows CI.");
        // LogonUser creates a token, not a loaded profile or a new process environment.
        // Keep the interactive logon type; no LoadUserProfile call means there is no profile to unload.
        const int Logon32LogonInteractive = 2;
        const int Logon32ProviderDefault = 0;
        var loggedOn = LogonUser(user!, ".", otherLogonSecret!, Logon32LogonInteractive, Logon32ProviderDefault, out var token);
        var logonError = loggedOn ? 0 : Marshal.GetLastWin32Error();
        try
        {
            if (!loggedOn) throw new Win32Exception(logonError);
            var run = Directory.CreateTempSubdirectory("erpclaw-run-").FullName;
            byte[]? originalDirectoryDescriptor = null;
            try
            {
                originalDirectoryDescriptor = new DirectoryInfo(run).GetAccessControl(AccessControlSections.Access)
                    .GetSecurityDescriptorBinaryForm();
                using var ownerIdentity = WindowsIdentity.GetCurrent();
                // Make the folder readable to the other account, so access denial is proved at the file itself.
                var directorySecurity = new DirectorySecurity();
                directorySecurity.SetAccessRuleProtection(true, false);
                directorySecurity.AddAccessRule(new FileSystemAccessRule(ownerIdentity.User!,
                    FileSystemRights.FullControl, InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit,
                    PropagationFlags.None, AccessControlType.Allow));
                directorySecurity.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(WellKnownSidType.WorldSid, null),
                    FileSystemRights.ReadAndExecute, InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit,
                    PropagationFlags.None, AccessControlType.Allow));
                new DirectoryInfo(run).SetAccessControl(directorySecurity);
                var readable = Path.Combine(run, "readable.txt");
                File.WriteAllText(readable, "readable");
                var owner = ownerIdentity.User;
                WindowsIdentity.RunImpersonated(token, () =>
                {
                    if (!OperatingSystem.IsWindows()) throw new PlatformNotSupportedException();
                    using var identity = WindowsIdentity.GetCurrent();
                    Assert.NotEqual(owner, identity.User);
                    Assert.False(new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator));
                    Assert.DoesNotContain(new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid, null),
                        identity.Groups!.Cast<IdentityReference>());
                    Assert.Equal("readable", File.ReadAllText(readable));
                });

                var store = new FileServiceFileStore(run);
                foreach (var port in new[] { 50001, 50002 })
                {
                    store.Write(Content(port));
                    AssertProtectedOwnerDacl(store.FilePath);
                    WindowsIdentity.RunImpersonated(token, () =>
                    {
                        Assert.Throws<UnauthorizedAccessException>(() => File.ReadAllBytes(store.FilePath));
                        Assert.Throws<UnauthorizedAccessException>(() =>
                        {
                            using var stream = File.Open(store.FilePath, FileMode.Open, FileAccess.Write, FileShare.ReadWrite);
                        });
                    });
                }
            }
            finally
            {
                // Restore only this generated folder, never the shared temp parent. Always attempt deletion,
                // including readable.txt, service.json and any interrupted replacement's temporary file.
                try
                {
                    if (originalDirectoryDescriptor is not null)
                    {
                        // Import the saved DACL so Access is marked modified and SetAccessControl persists it.
                        var originalDirectorySecurity = new DirectorySecurity();
                        originalDirectorySecurity.SetSecurityDescriptorBinaryForm(originalDirectoryDescriptor, AccessControlSections.Access);
                        new DirectoryInfo(run).SetAccessControl(originalDirectorySecurity);
                    }
                }
                finally
                {
                    Directory.Delete(run, true);
                }
            }
        }
        finally
        {
            token?.Dispose();
        }
    }

    [Fact]
    public void A_stale_temporary_file_from_a_crash_is_replaced()
    {
        var run = Directory.CreateTempSubdirectory("erpclaw-run-").FullName;
        try
        {
            var store = new FileServiceFileStore(run);
            File.WriteAllText(store.FilePath + ".tmp", "stale");
            store.Write(Content());
            Assert.Equal(new[] { "service.json" }, Directory.GetFiles(run).Select(Path.GetFileName));
        }
        finally
        {
            Directory.Delete(run, true);
        }
    }
}

public class ServiceLogRefusalTests
{
    private static bool Refuses(string message) =>
        new ServiceLog(new MemorySink(), new FakeClock(), new SecretRegistry()).WouldRefuse(message);

    [Theory]
    [InlineData("connecting with host=127.0.0.1 dbname=erpclaw")]
    [InlineData("postgresql://erpclaw_owner@127.0.0.1/erpclaw")]
    [InlineData("postgres://erpclaw_owner:pw@localhost/erpclaw")]
    [InlineData("using user=erpclaw_owner password=abc")]
    [InlineData("PGPASSWORD was set")]
    [InlineData("dbname=erpclaw user=erpclaw_runtime")]
    [InlineData(@"the pipe \\.\pipe\erpclaw-service-S-1-5-21-1-2-3-1001-0123456789abcdef0123456789abcdef is up")]
    [InlineData("the control_pipe was written")]
    [InlineData("service_secret changed")]
    public void A_line_naming_a_connection_parameter_is_refused(string message)
    {
        Assert.True(Refuses(message));
    }

    [Theory]
    [InlineData("ERPClaw is running.")]
    [InlineData("mint-code ok")]
    [InlineData("mint-code exit 3")]
    [InlineData("The database accepted the owner's login.")]
    public void The_service_s_own_plain_lines_are_not_refused(string message)
    {
        Assert.False(Refuses(message));
    }
}
