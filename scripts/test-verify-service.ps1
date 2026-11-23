[CmdletBinding()]
param([string]$VerifyScript = '')
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not $VerifyScript) { $VerifyScript = Join-Path $PSScriptRoot 'verify-service.ps1' }
$sample = Get-Content -LiteralPath (Join-Path $PSScriptRoot '../examples/request.json') -Raw | ConvertFrom-Json
$global:HouraheadVerifierTestState = @{fixture=@{};caseName='';writes=0;sample=$sample}
$global:HouraheadVerifierTestState.caseName = ''
$global:HouraheadVerifierTestState.writes = 0
function Copy-Fixture($Value) { $Value | ConvertTo-Json -Depth 30 | ConvertFrom-Json }
# This shadows the only network command used by the verifier. No server is contacted.
function Invoke-RestMethod {
    param($Uri, $TimeoutSec, $MaximumRedirection, $Method, $ContentType, $Body, $Headers)
    $path = ([uri]$Uri).AbsolutePath
    if ($Method -eq 'Post') { $global:HouraheadVerifierTestState.writes += 1 }
    switch ($path) {
        '/health' { return [pscustomobject]@{status='ready';model_version='fixture'} }
        '/metadata' { return [pscustomobject]@{model_version='fixture';model_sha256='fixture-sha'} }
        '/example' { return Copy-Fixture $global:HouraheadVerifierTestState.sample }
        '/logging' { return [pscustomobject]@{destination='local'} }
        '/predict' {
            $id = $Headers['Idempotency-Key']
            if (-not $global:HouraheadVerifierTestState.fixture.ContainsKey('prediction')) {
                $request = [Text.Encoding]::UTF8.GetString($Body) | ConvertFrom-Json
                $core = [ordered]@{target_hour=$request.target_hour;workload_stage='idle';stage_range='0';probabilities=@(1.0,0.0,0.0);model_version='fixture'}
                $global:HouraheadVerifierTestState.fixture.prediction = [pscustomobject]@{
                    event_type='prediction';event_id=[guid]::NewGuid().ToString();prediction_id=$id
                    recorded_at='2026-11-16T01:02:03.123456Z';data_source='demo';request=$request
                    response=$core;features=@{lag_1=0.125};model_sha256='fixture-sha';schema_version=1
                }
                $global:HouraheadVerifierTestState.fixture.feedback = @()
            }
            $result = Copy-Fixture $global:HouraheadVerifierTestState.fixture.prediction.response
            $result | Add-Member prediction_id $id
            $result | Add-Member recorded_at $global:HouraheadVerifierTestState.fixture.prediction.recorded_at
            $result | Add-Member log_status 'local'
            return $result
        }
        '/logs' {
            $items = @($global:HouraheadVerifierTestState.fixture.prediction) + @($global:HouraheadVerifierTestState.fixture.feedback)
            $items = @(foreach ($event in $items) { [pscustomobject]@{event=(Copy-Fixture $event);log_status='local'} })
            if ($global:HouraheadVerifierTestState.caseName -eq 'log-type') { $items[0].event.event_type='feedback' }
            return [pscustomobject]@{total=3;items=$items}
        }
    }
    if ($path -match '^/predictions/[^/]+/feedback$') {
        $answer = [Text.Encoding]::UTF8.GetString($Body) | ConvertFrom-Json
        $found = @($global:HouraheadVerifierTestState.fixture.feedback | Where-Object { $_.event_id -eq $answer.submission_id })
        if ($found.Count -eq 0) {
            $count = $answer.feedback.kind -eq 'actual_count'
            $event = [pscustomobject]@{
                event_type='feedback';event_id=$answer.submission_id;prediction_id=$global:HouraheadVerifierTestState.fixture.prediction.prediction_id
                recorded_at='2026-11-16T01:03:00Z';revision=($global:HouraheadVerifierTestState.fixture.feedback.Count + 1);data_source='demo'
                submission=$answer;actual_stage=$(if($count){'idle'}else{$null});is_correct=$count;schema_version=1
            }
            $global:HouraheadVerifierTestState.fixture.feedback += $event
            $found = @($event)
        }
        return [pscustomobject]@{event=(Copy-Fixture $found[0]);log_status='local'}
    }
    if ($path -match '^/predictions/[^/]+$') {
        $event = Copy-Fixture $global:HouraheadVerifierTestState.fixture.prediction
        $feedback = Copy-Fixture $global:HouraheadVerifierTestState.fixture.feedback[-1]
        switch ($global:HouraheadVerifierTestState.caseName) {
            'stored-id' { $event.prediction_id=[guid]::NewGuid().ToString() }
            'stored-history' { $event.request.history[0].count += 1 }
            'stored-response' { $event.response.probabilities=@(0.0,1.0,0.0) }
            'persist-prediction' { $event.features.lag_1=999 }
            'persist-feedback' { $feedback.recorded_at='2026-11-16T01:04:00Z' }
            'typed-dates' {
                $event.recorded_at=[datetimeoffset]::Parse('2026-11-16T10:02:03.123456+09:00')
                $event.request.target_hour=[datetime]::Parse('2011-09-27T06:00:00')
                $feedback.recorded_at=[datetime]::Parse('2026-11-16T01:03:00Z').ToUniversalTime()
            }
        }
        return [pscustomobject]@{event=$event;latest_feedback=[pscustomobject]@{event=$feedback;log_status='local'};log_status='local'}
    }
    throw 'Unexpected request in offline verifier test.'
}
$temporaryRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$fixtureRoot = Join-Path $temporaryRoot ('hourahead-verifier-' + [guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($fixtureRoot) | Out-Null
$reportPath = Join-Path $fixtureRoot 'acceptance.json'
try {
    foreach ($badCase in @('stored-id','stored-history','stored-response','log-type')) {
        $global:HouraheadVerifierTestState.fixture=@{}; $global:HouraheadVerifierTestState.caseName=$badCase; $rejected=$false
        try { & $VerifyScript | Out-Null } catch { $rejected=$true }
        if (-not $rejected) { throw "Verifier accepted invalid fixture: $badCase" }
    }
    $global:HouraheadVerifierTestState.fixture=@{}; $global:HouraheadVerifierTestState.caseName=''
    $accepted = & $VerifyScript -OutputPath $reportPath | ConvertFrom-Json
    if ($accepted.status -ne 'passed') { throw 'Valid acceptance fixture failed.' }
    $writesBefore = $global:HouraheadVerifierTestState.writes
    $persisted = & $VerifyScript -PreviousReport $reportPath | ConvertFrom-Json
    if ($persisted.status -ne 'passed' -or $global:HouraheadVerifierTestState.writes -ne $writesBefore) { throw 'Persistence must only read existing events.' }
    $global:HouraheadVerifierTestState.caseName='typed-dates'
    $typed = & $VerifyScript -PreviousReport $reportPath | ConvertFrom-Json
    if ($typed.prediction_event_sha256 -ne $accepted.prediction_event_sha256) { throw 'Timestamp normalization changed event identity.' }
    foreach ($badCase in @('persist-prediction','persist-feedback')) {
        $global:HouraheadVerifierTestState.caseName=$badCase; $rejected=$false
        try { & $VerifyScript -PreviousReport $reportPath | Out-Null } catch { $rejected=$true }
        if (-not $rejected) { throw "Verifier accepted changed persisted event: $badCase" }
    }
    'PASS: 6 invalid fixtures rejected; acceptance, persistence, and timestamp normalization verified offline.'
} finally {
    $resolvedFixture = [IO.Path]::GetFullPath($fixtureRoot)
    if (-not $resolvedFixture.StartsWith($temporaryRoot, [StringComparison]::OrdinalIgnoreCase) -or
        [IO.Path]::GetFileName($resolvedFixture) -notlike 'hourahead-verifier-*') { throw 'Unsafe fixture cleanup path.' }
    Remove-Item -LiteralPath $resolvedFixture -Recurse -Force
    Remove-Variable -Name HouraheadVerifierTestState -Scope Global
}
