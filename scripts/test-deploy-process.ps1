[CmdletBinding()]
param([string]$DeployScript = (Join-Path $PSScriptRoot 'deploy-local.ps1'))

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ast = [Management.Automation.Language.Parser]::ParseFile($DeployScript, [ref]$null, [ref]$null)
foreach ($name in @('Get-OwnedProcess', 'Stop-Owned')) {
    $definition = $ast.Find({ param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name
    }, $true)
    if ($null -eq $definition) { throw "Missing process function: $name" }
    . ([scriptblock]::Create($definition.Extent.Text))
}

# All operating-system process access is replaced; no live PID is inspected or stopped.
function Assert-ReleasePath([string]$Path) { return $Path }
function Get-Content { return 'home = C:\test-base-python' }
function Get-CimInstance([string]$ClassName, [string]$Filter) {
    if ($Filter -eq 'ParentProcessId = 101') { return $script:child }
    if ($Filter -eq 'ProcessId = 102') {
        if (-not $script:childStopped) { return $script:child }
        return $null
    }
    if ($Filter -ne 'ProcessId = 101') { throw 'Unexpected mock process query.' }
    if ($script:rootExited) { return $null }
    if ($script:childStopped -and $script:scenario -eq 'natural-exit') {
        return [pscustomobject]@{
            ProcessId=101; CreationDate=$script:created; ExecutablePath=$null; CommandLine=$null
        }
    }
    if ($script:childStopped -and $script:scenario -eq 'pid-reused') {
        return [pscustomobject]@{
            ProcessId=101; CreationDate=$script:created.AddSeconds(5)
            ExecutablePath='C:\unrelated.exe'; CommandLine='unrelated.exe'
        }
    }
    return $script:root
}
function Stop-Process([int]$Id) {
    $script:stopped += $Id
    if ($Id -eq 102) { $script:childStopped = $true }
    elseif ($Id -eq 101 -and $script:scenario -eq 'parent-remains') { $script:rootExited = $true }
    else { throw 'Attempted to stop an unrelated or already exiting process.' }
}
function Wait-Process([int]$Id, [int]$Timeout) {
    if ($Id -eq 101 -and $Timeout -eq 2) {
        $script:graceWaits++
        if ($script:scenario -eq 'natural-exit') { $script:rootExited = $true }
    }
}

$created = [datetime]'2026-11-09T00:00:00Z'
$state = [pscustomobject]@{
    Release='C:\test-release'; ProcessId=101; CreatedUtc=$created.ToUniversalTime().ToString('o'); Port=18001
}
$root = [pscustomobject]@{
    ProcessId=101; CreationDate=$created; ExecutablePath='C:\test-release\.venv\Scripts\python.exe'
    CommandLine='python.exe -m uvicorn workload.api:app --host 127.0.0.1 --port 18001'
}
$child = [pscustomobject]@{
    ProcessId=102; ParentProcessId=101; Name='python.exe'; CreationDate=$created.AddMilliseconds(10)
    ExecutablePath='C:\test-base-python\python.exe'; CommandLine=$root.CommandLine
}
foreach ($scenario in @('natural-exit', 'parent-remains', 'pid-reused')) {
    $childStopped=$false; $rootExited=$false; $stopped=@(); $graceWaits=0; $refused=$false
    try { Stop-Owned $state }
    catch {
        if ($scenario -ne 'pid-reused' -or $_.Exception.Message -notlike '*creation_time*') { throw }
        $refused=$true
    }
    if ($graceWaits -ne 1 -or 102 -notin $stopped) { throw 'Missing child stop or bounded parent-exit wait.' }
    if ($scenario -eq 'natural-exit' -and 101 -in $stopped) { throw 'Naturally exiting redirector was killed.' }
    if ($scenario -eq 'parent-remains' -and 101 -notin $stopped) { throw 'Verified surviving parent was not stopped.' }
    if ($scenario -eq 'pid-reused' -and (-not $refused -or 101 -in $stopped)) { throw 'Reused PID was not protected.' }
    Write-Output "PASS redirector shutdown: $scenario (mock processes only)"
}
