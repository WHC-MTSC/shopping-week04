[CmdletBinding()]
param(
    [ValidateSet('Deploy', 'Start', 'Stop', 'Status', 'Rollback')]
    [string]$Action = 'Deploy',
    [string]$SourcePath = (Split-Path -Parent $PSScriptRoot),
    [ValidateRange(1024, 65535)][int]$Port = 8001,
    [ValidateSet('local', 'cloudwatch')][string]$Backend = 'local',
    [string]$LogGroup = '',
    [string]$LogStream = '',
    [string]$Profile = 'workload',
    [string]$Region = 'ap-northeast-2',
    [string]$UvPath = 'uv',
    [string]$PythonPath = '3.12',
    [string]$NodePath = 'node',
    [string]$ModelPath = '',
    [string]$ModelReportPath = '',
    [switch]$InstallStartup
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$install = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Hourahead'
$releases = Join-Path $install 'releases'
$currentFile = Join-Path $install 'current.json'
$previousFile = Join-Path $install 'previous.json'

function Get-ByteDigest([byte[]]$Bytes) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($Bytes))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Get-ApprovedCandidate([string]$CandidatePath, [string]$ReportPath) {
    try {
        $modelFile = (Resolve-Path -LiteralPath $CandidatePath).Path
        if (-not $ReportPath) { $ReportPath = Join-Path (Split-Path -Parent $modelFile) 'report.json' }
        $reportFile = (Resolve-Path -LiteralPath $ReportPath).Path
        foreach ($file in @($modelFile, $reportFile)) {
            if (-not (Test-Path -LiteralPath $file -PathType Leaf) -or
                ((Get-Item -LiteralPath $file).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw 'Model and report must be regular files.'
            }
        }
        $modelBytes = [IO.File]::ReadAllBytes($modelFile)
        $reportBytes = [IO.File]::ReadAllBytes($reportFile)
        $modelText = [Text.Encoding]::UTF8.GetString($modelBytes).TrimStart([char]0xFEFF)
        $reportText = [Text.Encoding]::UTF8.GetString($reportBytes).TrimStart([char]0xFEFF)
        # PowerShell can unwrap a one-element JSON array; require object roots first.
        if (-not $modelText.TrimStart().StartsWith('{') -or
            -not $reportText.TrimStart().StartsWith('{')) { throw 'JSON objects are required.' }
        $artifact = $modelText | ConvertFrom-Json
        $report = $reportText | ConvertFrom-Json
        $digest = Get-ByteDigest $modelBytes
        if ($artifact -isnot [pscustomobject] -or $report -isnot [pscustomobject] -or
            $artifact.training_purpose -isnot [string] -or $artifact.training_purpose -cne 'real_feedback' -or
            $report.purpose -isnot [string] -or $report.purpose -cne 'real_feedback' -or
            $artifact.model_version -isnot [string] -or -not $artifact.model_version -or
            $report.candidate_version -isnot [string] -or $report.candidate_version -cne $artifact.model_version -or
            $report.candidate_sha256 -isnot [string] -or $report.candidate_sha256 -notmatch '^[a-fA-F0-9]{64}$' -or
            $report.candidate_sha256 -ine $digest -or
            $report.quality_gate_passed -isnot [bool] -or $report.quality_gate_passed -ne $true -or
            $report.promotion_eligible -isnot [bool] -or $report.promotion_eligible -ne $true) {
            throw 'Model identity, real-feedback purpose and true quality/promotion approvals are required.'
        }
        return [pscustomobject]@{
            ModelPath = $modelFile; ReportPath = $reportFile; ModelSha256 = $digest
            ReportSha256 = (Get-ByteDigest $reportBytes); ModelVersion = $artifact.model_version
        }
    } catch { throw 'Candidate model rejected: a matching, approved real-feedback model and report are required.' }
}

function Assert-ReleasePath([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    $prefix = [IO.Path]::GetFullPath($releases).TrimEnd('\') + '\'
    if (-not $full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Release path is outside the Hourahead installation.'
    }
    if (-not (Test-Path -LiteralPath $full -PathType Container)) { throw 'Release missing.' }
    return $full
}

function Read-State([string]$Path) {
    if (Test-Path -LiteralPath $Path) { return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json }
    return $null
}

function Get-ServingModel($State) {
    $release = Assert-ReleasePath $State.Release
    $modelName = if ($State.PSObject.Properties['ModelFile']) { [string]$State.ModelFile } else { 'artifacts\model.json' }
    if ($modelName -notin @('artifacts\model.json', 'serving-model.json')) { throw 'Invalid release model path.' }
    $model = Join-Path $release $modelName
    if (-not (Test-Path -LiteralPath $model -PathType Leaf) -or
        ((Get-Item -LiteralPath $model).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Release model is missing or is a link.'
    }
    return $model
}

function Save-State([string]$Path, $State) {
    $temp = $Path + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    $State | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $temp -Encoding UTF8
    Move-Item -LiteralPath $temp -Destination $Path -Force
}

function Get-OwnedProcess($State, [switch]$AllowStale) {
    if ($null -eq $State -or -not $State.ProcessId) { return $null }
    $release = Assert-ReleasePath $State.Release
    $process = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + [int]$State.ProcessId)
    if ($null -eq $process) { return $null }
    $python = Join-Path $release '.venv\Scripts\python.exe'
    $created = ([datetime]$process.CreationDate).ToUniversalTime().Ticks
    $recorded = ([datetime]$State.CreatedUtc).ToUniversalTime().Ticks
    # A later process can reuse a PID after logout/reboot. Never stop that process.
    if ($created -ne $recorded -and $AllowStale) { return $null }
    if ($process.ExecutablePath -ne $python -or $created -ne $recorded -or
        $process.CommandLine -notmatch '-m uvicorn workload\.api:app' -or
        $process.CommandLine -notmatch ('--port ' + [int]$State.Port + '(\s|$)')) {
        throw 'PID ownership cannot be verified; refusing to stop or reuse this process.'
    }
    return $process
}

function Stop-Owned($State) {
    $process = Get-OwnedProcess $State
    if ($null -ne $process) {
        # Windows venv python.exe can be a launcher whose child owns the server socket.
        $venvConfig = Join-Path $State.Release '.venv\pyvenv.cfg'
        $homeLine = Get-Content -LiteralPath $venvConfig | Where-Object { $_ -match '^home\s*=\s*(.+)$' } | Select-Object -First 1
        if (-not $homeLine) { throw 'Cannot identify the virtual environment base interpreter.' }
        $basePython = Join-Path (($homeLine -split '=', 2)[1].Trim()) 'python.exe'
        $children = @(Get-CimInstance Win32_Process -Filter ('ParentProcessId = ' + $process.ProcessId))
        $serverChildren = @()
        foreach ($child in $children) {
            # Start-Process also creates a Windows console host. It exits with its
            # console clients; do not kill or mistake it for an unknown server.
            if ($child.Name -eq 'conhost.exe' -and
                $child.ExecutablePath -eq (Join-Path $env:WINDIR 'System32\conhost.exe') -and
                $child.CreationDate -ge $process.CreationDate) { continue }
            if ($child.Name -ne 'python.exe' -or $child.ExecutablePath -ne $basePython -or
                $child.CommandLine -notmatch '-m uvicorn workload\.api:app' -or
                $child.CommandLine -notmatch ('--port ' + [int]$State.Port + '(\s|$)') -or
                $child.CreationDate -lt $process.CreationDate) {
                throw 'Unknown child process; refusing to stop the process tree.'
            }
            $serverChildren += $child
        }
        foreach ($child in $serverChildren) {
            $live = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $child.ProcessId)
            if ($null -eq $live) { continue }
            if ($live.CreationDate -ne $child.CreationDate -or
                $live.ExecutablePath -ne $child.ExecutablePath -or
                $live.CommandLine -ne $child.CommandLine -or
                $live.ParentProcessId -ne $process.ProcessId) {
                throw 'Child process identity changed; refusing to stop it.'
            }
            Stop-Process -Id $child.ProcessId -ErrorAction Stop
            Wait-Process -Id $child.ProcessId -Timeout 15 -ErrorAction SilentlyContinue
        }
        if ($null -ne (Get-OwnedProcess $State)) {
            Stop-Process -Id $process.ProcessId -ErrorAction Stop
        }
        Wait-Process -Id $process.ProcessId -Timeout 15 -ErrorAction SilentlyContinue
    }
}

function Assert-PortFree([int]$Number) {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $Number)
    try { $listener.Start() } finally { $listener.Stop() }
}

function Get-ProbePort {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    try { $listener.Start(); return ([Net.IPEndPoint]$listener.LocalEndpoint).Port }
    finally { $listener.Stop() }
}

function Start-Owned($Config, [int]$ListenPort) {
    $release = Assert-ReleasePath $Config.Release
    Assert-PortFree $ListenPort
    $python = Join-Path $release '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Release Python missing.' }
    $settings = @{
        WORKLOAD_MODEL_PATH = (Get-ServingModel $Config)
        WORKLOAD_LOG_BACKEND = [string]$Config.Backend
        WORKLOAD_LOG_DB = [string]$Config.Database
        WORKLOAD_S3_BUCKET = ''
        WORKLOAD_CLOUDWATCH_LOG_GROUP = [string]$Config.LogGroup
        WORKLOAD_CLOUDWATCH_LOG_STREAM = [string]$Config.LogStream
        AWS_PROFILE = $(if ($Config.Backend -eq 'cloudwatch') { [string]$Config.Profile } else { '' })
        AWS_REGION = [string]$Config.Region
    }
    $saved = @{}
    $logs = Join-Path $install 'process-logs'
    New-Item -ItemType Directory -Path $logs -Force | Out-Null
    $id = [guid]::NewGuid().ToString('N')
    try {
        foreach ($key in $settings.Keys) {
            $saved[$key] = @{
                Exists = Test-Path -LiteralPath ('Env:\' + $key)
                Value = [Environment]::GetEnvironmentVariable($key, 'Process')
            }
            [Environment]::SetEnvironmentVariable($key, $settings[$key], 'Process')
        }
        $process = Start-Process -FilePath $python -ArgumentList @(
            '-m', 'uvicorn', 'workload.api:app', '--host', '127.0.0.1', '--port', $ListenPort
        ) -WorkingDirectory $release -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $logs "$id.stdout.log") `
            -RedirectStandardError (Join-Path $logs "$id.stderr.log")
    }
    finally {
        foreach ($key in $saved.Keys) {
            if ($saved[$key].Exists) {
                [Environment]::SetEnvironmentVariable($key, $saved[$key].Value, 'Process')
            } else { Remove-Item -LiteralPath ('Env:\' + $key) -ErrorAction SilentlyContinue }
        }
    }
    $identity = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $process.Id)
    if ($null -eq $identity) { throw 'Server exited immediately; inspect process-logs.' }
    return [pscustomobject]@{
        Release = $release; Port = $ListenPort; Backend = $Config.Backend
        Database = $Config.Database; LogGroup = $Config.LogGroup; LogStream = $Config.LogStream
        Profile = $Config.Profile; Region = $Config.Region; ProcessId = $process.Id
        ModelFile = $(if ($Config.PSObject.Properties['ModelFile']) { $Config.ModelFile } else { 'artifacts\model.json' })
        CreatedUtc = ([datetime]$identity.CreationDate).ToUniversalTime().ToString('o')
    }
}

function Test-Server($State) {
    $ready = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        if ($null -eq (Get-OwnedProcess $State)) { throw 'Server exited during readiness check.' }
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$($State.Port)/health" -TimeoutSec 2
            if ($health.status -eq 'ready') { $ready = $true; break }
        } catch { }
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) { throw 'Readiness timeout.' }
    $python = Join-Path $State.Release '.venv\Scripts\python.exe'
    Push-Location $State.Release
    try {
        & $python -m workload.model_check --model (Get-ServingModel $State) `
            --base-url "http://127.0.0.1:$($State.Port)" | Write-Host
        if ($LASTEXITCODE -ne 0) { throw 'Serving model verification failed.' }
    } finally { Pop-Location }
}

function Activate($Config, $Old) {
    $probe = $null
    $candidate = $null
    $oldStopped = $false
    $oldRunning = $null -ne (Get-OwnedProcess $Old -AllowStale)
    try {
        # Verify the release in local mode before any real CloudWatch delivery.
        $probeConfig = $Config | ConvertTo-Json | ConvertFrom-Json
        $probeConfig.Backend = 'local'
        $probeConfig.Database = Join-Path $Config.Release 'probe.sqlite3'
        $probe = Start-Owned $probeConfig (Get-ProbePort)
        Test-Server $probe
        Stop-Owned $probe
        $probe = $null
        if ($oldRunning -and $Old.Port -eq $Config.Port) {
            Stop-Owned $Old
            $oldStopped = $true
        }
        $candidate = Start-Owned $Config $Config.Port
        Test-Server $candidate
        if ($null -ne $Old) {
            if ($oldRunning -and -not $oldStopped) { Stop-Owned $Old; $oldStopped = $true }
            Save-State $previousFile $Old
        }
        Save-State $currentFile $candidate
        return $candidate
    } catch {
        if ($null -ne $probe) { Stop-Owned $probe }
        if ($null -ne $candidate) { Stop-Owned $candidate }
        if ($oldStopped -and $null -ne $Old) {
            $restored = Start-Owned $Old $Old.Port
            Test-Server $restored
            Save-State $currentFile $restored
        }
        throw
    }
}

function Install-Startup {
    $startup = [Environment]::GetFolderPath('Startup')
    if (-not $startup -or -not (Test-Path -LiteralPath $startup -PathType Container)) {
        throw 'Current-user Startup folder is unavailable.'
    }
    $launcher = Join-Path $install 'deploy-local.ps1'
    if ([IO.Path]::GetFullPath($PSCommandPath) -ne [IO.Path]::GetFullPath($launcher)) {
        Copy-Item -LiteralPath $PSCommandPath -Destination $launcher -Force
    }
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut((Join-Path $startup 'Hourahead.lnk'))
    $link.TargetPath = Join-Path $PSHOME 'powershell.exe'
    if (-not (Test-Path -LiteralPath $link.TargetPath)) { $link.TargetPath = Join-Path $PSHOME 'pwsh.exe' }
    $link.Arguments = '-NoProfile -WindowStyle Hidden -File "' + $launcher + '" -Action Start'
    $link.WorkingDirectory = $install
    $link.WindowStyle = 7
    $link.Save()
}

$approvedCandidate = $null
if ($ModelPath -or $ModelReportPath) {
    if ($Action -ne 'Deploy' -or -not $ModelPath) { throw 'Model options require Deploy and ModelPath.' }
    # Read-only preflight precedes every installation, process and Startup mutation.
    $approvedCandidate = Get-ApprovedCandidate $ModelPath $ModelReportPath
}
New-Item -ItemType Directory -Path $releases -Force | Out-Null
$current = Read-State $currentFile
switch ($Action) {
    'Status' {
        if ($null -eq $current) { Write-Output 'Not deployed.'; break }
        $running = $null -ne (Get-OwnedProcess $current -AllowStale)
        [pscustomobject]@{ Running = $running; Release = $current.Release; Port = $current.Port; Backend = $current.Backend }
    }
    'Stop' { Stop-Owned $current; Write-Output 'Owned server stopped; release preserved.' }
    'Start' {
        if ($null -eq $current) { throw 'Deploy a release first.' }
        if ($null -eq (Get-OwnedProcess $current -AllowStale)) {
            $started = Start-Owned $current $current.Port
            try { Test-Server $started; Save-State $currentFile $started }
            catch { Stop-Owned $started; throw }
        } else { Test-Server $current }
    }
    'Rollback' {
        $previous = Read-State $previousFile
        if ($null -eq $previous) { throw 'No previous release is available.' }
        $current = Activate $previous $current
    }
    'Deploy' {
        if ($Backend -eq 'cloudwatch' -and (-not $LogGroup -or -not $LogStream -or -not $Profile)) {
            throw 'CloudWatch requires an existing group, stream and local profile.'
        }
        $source = (Resolve-Path -LiteralPath $SourcePath).Path
        $release = Join-Path $releases ((Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
        New-Item -ItemType Directory -Path $release | Out-Null
        foreach ($name in @('pyproject.toml', 'uv.lock', 'train.py', 'workload', 'tests', 'artifacts', 'data', 'examples', '.github')) {
            $item = Join-Path $source $name
            if (-not (Test-Path -LiteralPath $item)) { throw "Required release input missing: $name" }
            if ((Get-Item -LiteralPath $item).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Release input cannot be a link.' }
            $links = @(Get-ChildItem -LiteralPath $item -Recurse -Force | Where-Object {
                $_.Attributes -band [IO.FileAttributes]::ReparsePoint
            })
            if ($links.Count) { throw 'Release inputs contain links.' }
            Copy-Item -LiteralPath $item -Destination $release -Recurse
        }
        $uv = (Get-Command $UvPath -ErrorAction Stop).Source
        $node = (Get-Command $NodePath -ErrorAction Stop).Source
        $qualitySettings = @{
            WORKLOAD_LOG_BACKEND = 'local'
            WORKLOAD_MODEL_PATH = (Join-Path $release 'artifacts\model.json')
        }
        $qualitySaved = @{}
        Push-Location $release
        try {
            foreach ($key in $qualitySettings.Keys) {
                $qualitySaved[$key] = @{
                    Exists = Test-Path -LiteralPath ('Env:\' + $key)
                    Value = [Environment]::GetEnvironmentVariable($key, 'Process')
                }
                [Environment]::SetEnvironmentVariable($key, $qualitySettings[$key], 'Process')
            }
            & $uv sync --locked --python $PythonPath
            if ($LASTEXITCODE -ne 0) { throw 'Locked dependency installation failed.' }
            foreach ($arguments in @(
                @('run','--locked','ruff','check','.'), @('run','--locked','ruff','format','--check','.'),
                @('run','--locked','basedpyright'), @('run','--locked','pytest','-q')
            )) {
                & $uv @arguments
                if ($LASTEXITCODE -ne 0) { throw 'Required quality check failed.' }
            }
            & $node --test tests/input.test.mjs
            if ($LASTEXITCODE -ne 0) { throw 'Browser contract tests failed.' }
        } finally {
            Pop-Location
            foreach ($key in $qualitySaved.Keys) {
                if ($qualitySaved[$key].Exists) {
                    [Environment]::SetEnvironmentVariable($key, $qualitySaved[$key].Value, 'Process')
                } else { Remove-Item -LiteralPath ('Env:\' + $key) -ErrorAction SilentlyContinue }
            }
        }
        $modelFile = 'artifacts\model.json'
        if ($null -ne $approvedCandidate) {
            $stagedModel = Join-Path $release 'serving-model.json'
            $stagedReport = Join-Path $release 'serving-report.json'
            Copy-Item -LiteralPath $approvedCandidate.ModelPath -Destination $stagedModel
            Copy-Item -LiteralPath $approvedCandidate.ReportPath -Destination $stagedReport
            $copied = Get-ApprovedCandidate $stagedModel $stagedReport
            if ($copied.ModelSha256 -ne $approvedCandidate.ModelSha256 -or
                $copied.ReportSha256 -ne $approvedCandidate.ReportSha256) {
                throw 'Candidate model/report changed after preflight; refusing activation.'
            }
            $modelFile = 'serving-model.json'
        }
        $scope = if ($Backend -eq 'local') { 'local' } else {
            $sha = [Security.Cryptography.SHA256]::Create()
            try { ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes("$Profile|$Region|$LogGroup|$LogStream")))).Replace('-', '').ToLowerInvariant() }
            finally { $sha.Dispose() }
        }
        $config = [pscustomobject]@{
            Release = $release; Port = $Port; Backend = $Backend; LogGroup = $LogGroup; LogStream = $LogStream
            Profile = $Profile; Region = $Region; Database = (Join-Path $install "data\$scope\events.sqlite3")
            ModelFile = $modelFile
        }
        $current = Activate $config $current
    }
}
if ($InstallStartup) {
    if ($null -eq (Read-State $currentFile)) { throw 'A verified release is required for Startup.' }
    Install-Startup
}
