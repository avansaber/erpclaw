// WINDOWS CI ONLY. These tests are skipped on other platforms, like the windows-ci probe.
using Xunit;

namespace ErpClaw.Service.Core.Tests;

public sealed class WindowsFactAttribute : FactAttribute
{
    public WindowsFactAttribute()
    {
        if (!OperatingSystem.IsWindows()) Skip = "Windows CI only.";
    }
}

public sealed class WindowsTheoryAttribute : TheoryAttribute
{
    public WindowsTheoryAttribute()
    {
        if (!OperatingSystem.IsWindows()) Skip = "Windows CI only.";
    }
}
