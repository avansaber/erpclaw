// INSTALLER 2.3a item 1: no process ERPClaw starts is ever created suspended and resumed. The lint refuses both
// names in every .cs file under installer/windows (the names are built here so this file does not hold them).
using Xunit;

namespace ErpClaw.Service.Core.Tests;

public static class SourceLint
{
    public static readonly string[] Refused = { "CREATE_" + "SUSPENDED", "Resume" + "Thread" };

    /// <summary>Each .cs file under <paramref name="root"/> (bin and obj skipped) holding a refused name.</summary>
    public static IReadOnlyList<string> Offenders(string root) =>
        Directory.EnumerateFiles(root, "*.cs", SearchOption.AllDirectories)
            .Where(f => !f.Split(Path.DirectorySeparatorChar).Any(part => part is "bin" or "obj"))
            .Where(f =>
            {
                var text = File.ReadAllText(f);
                return Refused.Any(name => text.Contains(name, StringComparison.Ordinal));
            })
            .Select(f => Path.GetRelativePath(root, f))
            .ToArray();

    /// <summary>installer/windows: the folder holding ErpClaw.Installer.slnx, found upwards from the test binaries.</summary>
    public static string InstallerRoot()
    {
        for (var dir = new DirectoryInfo(AppContext.BaseDirectory); dir is not null; dir = dir.Parent)
            if (File.Exists(Path.Combine(dir.FullName, "ErpClaw.Installer.slnx"))) return dir.FullName;
        throw new InvalidOperationException("ErpClaw.Installer.slnx not found above the test binaries");
    }
}

public class LintTests
{
    [Fact]
    public void No_source_under_installer_windows_creates_a_process_suspended_or_resumes_a_thread()
    {
        var root = SourceLint.InstallerRoot();
        Assert.True(Directory.EnumerateFiles(root, "*.cs", SearchOption.AllDirectories).Count() > 10);
        Assert.Empty(SourceLint.Offenders(root));
    }

    [Fact]
    public void The_lint_finds_a_planted_name_in_any_folder_and_either_spelling()
    {
        var root = Directory.CreateTempSubdirectory("erpclaw-lint-").FullName;
        try
        {
            Directory.CreateDirectory(Path.Combine(root, "src", "deep"));
            File.WriteAllText(Path.Combine(root, "src", "deep", "A.cs"), "const uint X = 4; // " + SourceLint.Refused[0]);
            File.WriteAllText(Path.Combine(root, "B.cs"), "static extern uint " + SourceLint.Refused[1] + "(IntPtr h);");
            File.WriteAllText(Path.Combine(root, "C.cs"), "// clean");
            Directory.CreateDirectory(Path.Combine(root, "obj"));
            File.WriteAllText(Path.Combine(root, "obj", "D.cs"), SourceLint.Refused[0]);

            Assert.Equal(new[] { "B.cs", Path.Combine("src", "deep", "A.cs") }, SourceLint.Offenders(root).OrderBy(x => x, StringComparer.Ordinal));
        }
        finally
        {
            Directory.Delete(root, true);
        }
    }
}
