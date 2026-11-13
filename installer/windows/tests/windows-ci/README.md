Run these checks on Windows as a standard user with the SDK pinned in `global.json`, PowerShell 7.2 or later,
and the bundled PostgreSQL binaries. Provision a second ordinary local account with interactive logon allowed;
it must differ from the test runner and must not belong to Administrators.

Set these environment variables in the test runner's PowerShell session through the CI secret store:

- `ERPCLAW_TEST_PG_BIN`: the bundled PostgreSQL `bin` directory containing `initdb.exe`, `postgres.exe`,
  `psql.exe`, and `pg_ctl.exe`.
- `ERPCLAW_TEST_OTHER_USER`: the second account's local user name, without a domain prefix.
- `ERPCLAW_TEST_OTHER_PASSWORD`: that account's password.

From the repository root, run all three checks before merge:

```powershell
Set-Location installer/windows
dotnet restore --locked-mode
dotnet test -c Release
pwsh -NoProfile -File tests/windows-ci/initdb-pwfile-pipe.ps1 -PgBin $env:ERPCLAW_TEST_PG_BIN
```

The C# Windows tests use `WindowsFact` / `WindowsTheory` to skip on other platforms. On Windows, missing
prerequisites fail the tests rather than skipping them. The real initializer theory tests both the named-pipe
route and forced fallback, fresh setup and interruptions after initdb, between runtime role creation and its
login/password assignment, and after database creation. It then starts PostgreSQL and checks both logins,
wrong passwords, runtime privilege restrictions, database ownership/encoding, and effective configuration.

The service-file test inspects the protected owner-only DACL and impersonates the second account to prove read
and write denial before and after replacement. A neighboring inherited file remains readable to that account,
so denial cannot be attributed to an inaccessible parent directory. The standalone PowerShell probe separately
checks initdb's one-shot pipe behavior and secret-file absence.
