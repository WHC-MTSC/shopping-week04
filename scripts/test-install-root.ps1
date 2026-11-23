[CmdletBinding()]
param([string]$DeployScript = (Join-Path $PSScriptRoot 'deploy-local.ps1'))

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ast = [Management.Automation.Language.Parser]::ParseFile($DeployScript, [ref]$null, [ref]$null)
foreach ($name in @('Assert-NoReparseAncestors', 'Resolve-InstallRoot', 'Get-ByteDigest', 'Get-StartupRegistration')) {
    $definition = $ast.Find({ param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name
    }, $true)
    if ($null -eq $definition) { throw "Missing root function: $name" }
    . ([scriptblock]::Create($definition.Extent.Text))
}

# Any accidental access to a service is a test failure; all state is synthetic.
function Start-Process { throw 'Unexpected process start in installation-root test.' }
function Stop-Process { throw 'Unexpected process stop in installation-root test.' }
function Get-CimInstance { throw 'Unexpected process access in installation-root test.' }

$temporaryRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$fixture = Join-Path $temporaryRoot ('hourahead-root-' + [guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($fixture) | Out-Null
$utf8 = [Text.UTF8Encoding]::new($false)
try {
    $first = Join-Path $fixture 'first recipient'
    $second = Join-Path $fixture 'second-recipient'
    foreach ($root in @($first, $second)) {
        $release = Join-Path $root 'releases\release-1'
        [IO.Directory]::CreateDirectory($release) | Out-Null
        $state = @{
            Release=$release; ProcessId=0; Port=$(if ($root -eq $first) { 18001 } else { 18002 }); Backend='local'
        }
        [IO.File]::WriteAllText((Join-Path $root 'current.json'), ($state | ConvertTo-Json), $utf8)
    }
    $firstBefore = (Get-FileHash -LiteralPath (Join-Path $first 'current.json')).Hash
    $secondBefore = (Get-FileHash -LiteralPath (Join-Path $second 'current.json')).Hash
    $one = & $DeployScript -Action Status -InstallRoot $first
    $two = & $DeployScript -Action Status -InstallRoot $second
    if ($one.InstallRoot -ne $first -or $one.Port -ne 18001 -or
        $two.InstallRoot -ne $second -or $two.Port -ne 18002) { throw 'Status crossed installation roots.' }
    $null = & $DeployScript -Action Stop -InstallRoot $first
    if ((Get-FileHash -LiteralPath (Join-Path $first 'current.json')).Hash -ne $firstBefore -or
        (Get-FileHash -LiteralPath (Join-Path $second 'current.json')).Hash -ne $secondBefore) {
        throw 'Synthetic Stop changed another installation state.'
    }
    $absent = Join-Path $fixture 'not-installed'
    $null = & $DeployScript -Action Status -InstallRoot $absent
    if (Test-Path -LiteralPath $absent) { throw 'Read-only Status created an installation.' }
    $recursive = Join-Path $fixture 'workload\installation'
    $rejected=$false
    try { $null = & $DeployScript -Action Deploy -SourcePath $fixture -InstallRoot $recursive }
    catch {
        if ($_.Exception.Message -ne 'InstallRoot cannot be inside a copied source directory.') { throw }
        $rejected=$true
    }
    if (-not $rejected -or (Test-Path -LiteralPath $recursive)) { throw 'Recursive source copy was not rejected before mutation.' }
    Write-Output 'PASS separate installation state and read-only Status (no live processes)'

    foreach ($invalid in @('relative-root', 'C:relative', 'C:\', '\\server\share\root',
        'C:\safe\..\escaped', 'C:\safe\NUL', 'C:\safe\bad:stream', $env:WINDIR,
        [Environment]::GetFolderPath('UserProfile'), (Join-Path $first 'current.json'))) {
        $rejected=$false
        try { $null = Resolve-InstallRoot $invalid } catch { $rejected=$true }
        if (-not $rejected) { throw "Unsafe root accepted: $invalid" }
    }
    $junction = Join-Path $fixture 'linked-root'
    New-Item -ItemType Junction -Path $junction -Target $second | Out-Null
    $rejected=$false
    try { $null = & $DeployScript -Action Status -InstallRoot $junction } catch { $rejected=$true }
    if (-not $rejected) { throw 'Installation through a junction was accepted.' }
    # The link target is also inside this isolated fixture; remove only the link now.
    Remove-Item -LiteralPath $junction -Force
    Write-Output 'PASS relative, device, broad, file and junction roots are rejected'

    $firstStartup = Get-StartupRegistration ($first + '\')
    $secondStartup = Get-StartupRegistration $second
    if ($firstStartup.Name -eq $secondStartup.Name -or $firstStartup.Name -eq 'Hourahead.lnk' -or
        $firstStartup.WorkingDirectory -ne $first -or
        $firstStartup.Arguments -notlike ('*-InstallRoot "' + $first + '"')) {
        throw 'Startup did not preserve the selected root or avoid link collisions.'
    }
    if ((Get-StartupRegistration $first.ToUpperInvariant()).Name -ne $firstStartup.Name) {
        throw 'Case-insensitive aliases produced different Startup names.'
    }
    Write-Output 'PASS Startup keeps the explicit root and distinct link names (registration only)'
} finally {
    $fullFixture = [IO.Path]::GetFullPath($fixture)
    if (-not $fullFixture.StartsWith($temporaryRoot.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Refusing cleanup outside the test temporary directory.'
    }
    Remove-Item -LiteralPath $fullFixture -Recurse -Force
}
