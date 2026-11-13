using Xunit;

namespace ErpClaw.Service.Core.Tests;

// DisableParallelization excludes every other collection in this assembly, including other disabled collections.
// All service-file ACL writers and second-account impersonation tests belong to this collection.
[CollectionDefinition(WindowsPermissionsCollection.Name, DisableParallelization = true)]
public sealed class WindowsPermissionsCollection
{
    public const string Name = "Windows permissions";
}

// The initializer owns private pipe ACLs and child processes; keep it separate from the permission tests.
[CollectionDefinition(PostgresInitializerCollection.Name, DisableParallelization = true)]
public sealed class PostgresInitializerCollection
{
    public const string Name = "PostgreSQL initializer";
}
