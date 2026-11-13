#requires -Version 7.2
<#
WINDOWS CI ONLY. Not run on Linux; never run by the Linux test suite. (INSTALLER 2.2.)

Proves the accepted direction for initdb's superuser password: initdb reads --pwfile from a one-shot, owner-only
named pipe the service serves once, and no file is left.
  1. initdb opens \\.\pipe\erpclaw-initdb-<random> (created once, FirstPipeInstance, a protected owner-only DACL,
     outbound, one instance) and reads one line: the owner's SCRAM-SHA-256 verifier, never the password;
  2. initdb exits 0, and the pipe is gone afterwards;
  3. no file anywhere under the work folder or %TEMP% holds the password, and none outside books\ holds the verifier;
     no new file appeared in %TEMP% while initdb ran;
  4. PostgreSQL, started directly on 127.0.0.1 with scram-sha-256 only, accepts the owner's login with the password
     (so the verifier was stored as it was served) and refuses a wrong one.
Exit code 0 when every check passes; each check prints PASS or FAIL.

The alternative, if initdb cannot read a pipe path on some build: run initdb with --auth=reject and no --pwfile (it
then needs no password), write pg_hba.conf before the first start as now, and set the owner's verifier in the same
single-user-mode step that creates the runtime role, on postgres --single's stdin:
  ALTER ROLE "erpclaw_owner" PASSWORD 'SCRAM-SHA-256$...';
Only verifiers cross stdin; no file and no pipe are needed.

Usage (PowerShell 7, as a standard user):
  pwsh -NoProfile -File initdb-pwfile-pipe.ps1 -PgBin "C:\...\postgresql\bin"
#>
param(
    [Parameter(Mandatory = $true)][string]$PgBin,
    [string]$WorkRoot = (Join-Path $env:USERPROFILE ("ERPClawPipeTest-" + [guid]::NewGuid().ToString('N')))
)

$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw 'This test runs on Windows only.' }

$failures = 0
function Check([string]$name, [bool]$ok) {
    if ($ok) { Write-Output "PASS $name" } else { Write-Output "FAIL $name"; $script:failures++ }
}

function New-Password {
    $bytes = [Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
    [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

# PostgreSQL's SCRAM-SHA-256 verifier, as the service's ScramVerifier computes it.
function New-ScramVerifier([string]$password) {
    $salt = [Security.Cryptography.RandomNumberGenerator]::GetBytes(16)
    $salted = [Security.Cryptography.Rfc2898DeriveBytes]::Pbkdf2([Text.Encoding]::UTF8.GetBytes($password), $salt, 4096,
        [Security.Cryptography.HashAlgorithmName]::SHA256, 32)
    $clientKey = [Security.Cryptography.HMACSHA256]::HashData($salted, [Text.Encoding]::UTF8.GetBytes('Client Key'))
    $serverKey = [Security.Cryptography.HMACSHA256]::HashData($salted, [Text.Encoding]::UTF8.GetBytes('Server Key'))
    $stored = [Security.Cryptography.SHA256]::HashData($clientKey)
    'SCRAM-SHA-256$4096:' + [Convert]::ToBase64String($salt) + '$' + [Convert]::ToBase64String($stored) + ':' +
        [Convert]::ToBase64String($serverKey)
}

function Get-FreePort {
    $l = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $l.Start(); $port = $l.LocalEndpoint.Port; $l.Stop(); $port
}

function Find-Holding([string]$root, [string]$needle) {
    if (-not (Test-Path -LiteralPath $root)) { return @() }
    Get-ChildItem -LiteralPath $root -Recurse -File -Force -ErrorAction SilentlyContinue | Where-Object {
        try { [IO.File]::ReadAllText($_.FullName).Contains($needle) } catch { $false }
    } | ForEach-Object { $_.FullName }
}

$owner = 'erpclaw_owner'
$password = New-Password
$verifier = New-ScramVerifier $password
$books = Join-Path $WorkRoot 'books'
New-Item -ItemType Directory -Path $WorkRoot | Out-Null

try {
    # The one-shot pipe: owner-only, protected, created once.
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $security = [IO.Pipes.PipeSecurity]::new()
    $security.SetAccessRuleProtection($true, $false)
    $security.AddAccessRule([IO.Pipes.PipeAccessRule]::new($sid, [IO.Pipes.PipeAccessRights]::FullControl,
        [Security.AccessControl.AccessControlType]::Allow))
    $pipeName = 'erpclaw-initdb-' + [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(16)).ToLowerInvariant()
    $options = [IO.Pipes.PipeOptions]::Asynchronous -bor [IO.Pipes.PipeOptions]::FirstPipeInstance
    $server = [IO.Pipes.NamedPipeServerStreamAcl]::Create($pipeName, [IO.Pipes.PipeDirection]::Out, 1,
        [IO.Pipes.PipeTransmissionMode]::Byte, $options, 0, 0, $security)
    $pipePath = "\\.\pipe\$pipeName"

    $tempBefore = @(Get-ChildItem -LiteralPath $env:TEMP -Force -ErrorAction SilentlyContinue | ForEach-Object { $_.Name })
    $connected = $server.WaitForConnectionAsync()

    $psi = [Diagnostics.ProcessStartInfo]::new((Join-Path $PgBin 'initdb.exe'))
    foreach ($a in @('-D', $books, '-U', $owner, '-E', 'UTF8', '--locale-provider=builtin', '--builtin-locale=C.UTF-8',
            '--locale=C', '-A', 'scram-sha-256', "--pwfile=$pipePath", '--no-instructions')) { $psi.ArgumentList.Add($a) }
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.WorkingDirectory = $WorkRoot
    $initdb = [Diagnostics.Process]::Start($psi)
    $initdbOut = $initdb.StandardOutput.ReadToEndAsync()
    $initdbErr = $initdb.StandardError.ReadToEndAsync()

    $didConnect = $connected.Wait(60000)
    Check 'initdb opened the pipe path given as --pwfile' $didConnect
    if ($didConnect) {
        $line = [Text.Encoding]::UTF8.GetBytes($verifier + "`n")
        $server.Write($line, 0, $line.Length)
        $server.Flush()
        $server.WaitForPipeDrain()
    }
    $server.Dispose()

    $initdb.WaitForExit()
    $out = $initdbOut.Result + $initdbErr.Result
    Check 'initdb exited 0' ($initdb.ExitCode -eq 0)
    if ($initdb.ExitCode -ne 0) { Write-Output ($out -replace [regex]::Escape($password), '[password]') }
    Check 'initdb printed neither the password nor the verifier' (-not $out.Contains($password) -and -not $out.Contains($verifier))
    Check 'the pipe is gone' (-not ([IO.Directory]::GetFiles('\\.\pipe\') -match [regex]::Escape($pipeName)))

    $tempNew = @(Get-ChildItem -LiteralPath $env:TEMP -Force -ErrorAction SilentlyContinue | Where-Object { $tempBefore -notcontains $_.Name })
    Check 'no new file in %TEMP% while initdb ran' ($tempNew.Count -eq 0)
    Check 'no file under the work folder holds the password' ((Find-Holding $WorkRoot $password).Count -eq 0)
    Check 'no file in %TEMP% holds the password or the verifier' (((Find-Holding $env:TEMP $password).Count + (Find-Holding $env:TEMP $verifier).Count) -eq 0)
    Check 'no file outside books\ holds the verifier' (@(Find-Holding $WorkRoot $verifier | Where-Object { -not $_.StartsWith($books + '\') }).Count -eq 0)

    # The verifier works: loopback and scram only, PostgreSQL started directly (never pg_ctl start).
    $port = Get-FreePort
    Set-Content -LiteralPath (Join-Path $books 'pg_hba.conf') -Value "host all $owner 127.0.0.1/32 scram-sha-256" -Encoding ascii
    Add-Content -LiteralPath (Join-Path $books 'postgresql.conf') -Value @(
        "listen_addresses = '127.0.0.1'", "port = $port", "unix_socket_directories = ''", "password_encryption = 'scram-sha-256'"
    ) -Encoding ascii
    $pg = Start-Process -FilePath (Join-Path $PgBin 'postgres.exe') -ArgumentList @('-D', "`"$books`"") -WorkingDirectory $WorkRoot -PassThru -WindowStyle Hidden
    $psql = Join-Path $PgBin 'psql.exe'
    $ok = $false
    try {
        for ($i = 0; $i -lt 60 -and -not $ok; $i++) {
            $env:PGPASSWORD = $password   # this test's own throwaway password, in this test process only
            & $psql -h 127.0.0.1 -p $port -U $owner -d postgres -tAc 'select 1' 2>$null | Out-Null
            $ok = ($LASTEXITCODE -eq 0)
            if (-not $ok) { Start-Sleep -Milliseconds 500 }
        }
        Check "the owner's login succeeds with the password" $ok
        $env:PGPASSWORD = 'wrong-' + $password
        & $psql -h 127.0.0.1 -p $port -U $owner -d postgres -tAc 'select 1' 2>$null | Out-Null
        Check 'a wrong password is refused' ($LASTEXITCODE -ne 0)
    }
    finally {
        Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
        & (Join-Path $PgBin 'pg_ctl.exe') stop -D $books -m fast | Out-Null
        if (-not $pg.WaitForExit(30000)) { $pg.Kill() }
    }
}
finally {
    Remove-Item -LiteralPath $WorkRoot -Recurse -Force -ErrorAction SilentlyContinue
}

if ($failures -gt 0) { Write-Output "$failures check(s) failed"; exit 1 }
Write-Output 'all checks passed'
exit 0
