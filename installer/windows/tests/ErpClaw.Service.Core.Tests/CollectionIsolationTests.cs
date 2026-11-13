using System.Reflection;
using Xunit;

namespace ErpClaw.Service.Core.Tests;

public class CollectionIsolationTests
{
    [Fact]
    public void Permissions_and_initializer_tests_have_distinct_nonparallel_collections()
    {
        var permissions = Assert.Single(typeof(FileServiceFileStoreTests).GetCustomAttributes<CollectionAttribute>());
        var initializer = Assert.Single(typeof(PostgresInitializerWindowsTests).GetCustomAttributes<CollectionAttribute>());

        Assert.NotEqual(permissions.Name, initializer.Name);
        Assert.True(DefinitionFor(permissions).DisableParallelization);
        Assert.True(DefinitionFor(initializer).DisableParallelization);
    }

    private static CollectionDefinitionAttribute DefinitionFor(CollectionAttribute collection)
    {
        // Resolve the actual registered definition rather than assuming a particular definition class.
        return Assert.Single(typeof(CollectionIsolationTests).Assembly.GetTypes()
            .SelectMany(type => type.GetCustomAttributes<CollectionDefinitionAttribute>())
            .Where(definition => definition.Name == collection.Name));
    }
}
