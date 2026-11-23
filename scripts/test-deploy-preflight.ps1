[CmdletBinding()]
param([string]$DeployScript = (Join-Path $PSScriptRoot 'deploy-local.ps1'))

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ast = [Management.Automation.Language.Parser]::ParseFile($DeployScript, [ref]$null, [ref]$null)
foreach ($name in @('Get-ByteDigest', 'Get-ApprovedCandidate')) {
    $definition = $ast.Find({ param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name
    }, $true)
    if ($null -eq $definition) { throw "Missing preflight function: $name" }
    . ([scriptblock]::Create($definition.Extent.Text))
}

# Even a broken preflight must not reach the real installation or process APIs.
function New-Item { throw 'Unexpected installation mutation in preflight test.' }
function Start-Process { throw 'Unexpected process start in preflight test.' }
function Stop-Process { throw 'Unexpected process stop in preflight test.' }
function Get-CimInstance { throw 'Unexpected process access in preflight test.' }

$temporaryRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$fixture = Join-Path $temporaryRoot ('hourahead-preflight-' + [guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($fixture) | Out-Null
$model = Join-Path $fixture 'model.json'
$reportFile = Join-Path $fixture 'report.json'
$utf8 = [Text.UTF8Encoding]::new($false)
try {
    foreach ($case in @('historical-artifact', 'historical-report', 'failed-quality', 'ineligible',
        'string-quality', 'numeric-promotion', 'wrong-sha', 'wrong-version', 'missing-report',
        'array-model', 'array-report')) {
        $artifact = @{ model_version = 'feedback-fixture'; training_purpose = 'real_feedback' }
        if ($case -eq 'historical-artifact') { $artifact.training_purpose = 'historical_replay' }
        $modelJson = $artifact | ConvertTo-Json
        if ($case -eq 'array-model') { $modelJson = '[' + $modelJson + ']' }
        [IO.File]::WriteAllText($model, $modelJson, $utf8)
        $report = @{
            purpose = 'real_feedback'; candidate_version = 'feedback-fixture'
            candidate_sha256 = (Get-FileHash -LiteralPath $model).Hash
            quality_gate_passed = $true; promotion_eligible = $true
        }
        switch ($case) {
            'historical-report' { $report.purpose = 'historical_replay' }
            'failed-quality' { $report.quality_gate_passed = $false }
            'ineligible' { $report.promotion_eligible = $false }
            'string-quality' { $report.quality_gate_passed = 'true' }
            'numeric-promotion' { $report.promotion_eligible = 1 }
            'wrong-sha' { $report.candidate_sha256 = '0' * 64 }
            'wrong-version' { $report.candidate_version = 'different-model' }
        }
        $reportJson = $report | ConvertTo-Json
        if ($case -eq 'array-report') { $reportJson = '[' + $reportJson + ']' }
        [IO.File]::WriteAllText($reportFile, $reportJson, $utf8)
        if ($case -eq 'missing-report') { [IO.File]::Delete($reportFile) }
        $rejected = $false
        try { & $DeployScript -Action Deploy -ModelPath $model }
        catch {
            if ($_.Exception.Message -notlike 'Candidate model rejected:*') { throw }
            $rejected = $true
        }
        if (-not $rejected) { throw "Invalid candidate accepted: $case" }
        Write-Output "PASS preflight rejects $case before installation/process access"
    }
    # Positive parsing checks only the helper: this metadata fixture is never deployed.
    [IO.File]::WriteAllText($model, (@{
        model_version = 'feedback-fixture'; training_purpose = 'real_feedback'
    } | ConvertTo-Json), $utf8)
    [IO.File]::WriteAllText($reportFile, (@{
        purpose = 'real_feedback'; candidate_version = 'feedback-fixture'
        candidate_sha256 = (Get-FileHash -LiteralPath $model).Hash
        quality_gate_passed = $true; promotion_eligible = $true
    } | ConvertTo-Json), $utf8)
    $approved = Get-ApprovedCandidate $model ''
    $explicit = Get-ApprovedCandidate $model $reportFile
    if ($approved.ModelSha256 -ne $explicit.ModelSha256 -or
        $approved.ReportSha256 -ne $explicit.ReportSha256) { throw 'Report selection mismatch.' }
    Write-Output 'PASS matching real-feedback metadata with exact true booleans (helper only)'
} finally {
    $fullFixture = [IO.Path]::GetFullPath($fixture)
    if (-not $fullFixture.StartsWith($temporaryRoot.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Refusing cleanup outside the test temporary directory.'
    }
    Remove-Item -LiteralPath $fullFixture -Recurse -Force
}
