[CmdletBinding()]
param(
    [string]$BaseUrl = 'http://127.0.0.1:18001',
    [string]$OutputPath = '',
    [string]$PreviousReport = ''
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$uri = [uri]$BaseUrl
if ($uri.Scheme -ne 'http' -or $uri.Host -notin @('127.0.0.1', 'localhost', '[::1]') -or
    $uri.UserInfo -or $uri.Query -or $uri.Fragment -or $uri.AbsolutePath -ne '/') {
    throw 'Use an HTTP localhost service URL without a path or credentials.'
}
$base = $BaseUrl.TrimEnd('/')
function Read-API([string]$Path) {
    Invoke-RestMethod -Uri ($base + $Path) -TimeoutSec 10 -MaximumRedirection 0
}
function Write-API([string]$Path, $Payload, [hashtable]$Headers = @{}) {
    $json = $Payload | ConvertTo-Json -Depth 20 -Compress
    Invoke-RestMethod -Uri ($base + $Path) -Method Post -ContentType 'application/json' `
        -Body ([Text.Encoding]::UTF8.GetBytes($json)) -Headers $Headers -TimeoutSec 10 -MaximumRedirection 0
}
function Assert-Check([bool]$Condition, [string]$Name) {
    if (-not $Condition) { throw "Service verification failed: $Name" }
}
function Get-CanonicalValue($Value, [string]$Field = '') {
    if ($null -eq $Value) { return 'null' }
    # PowerShell 7 may decode ISO timestamps as DateTime; 5.1 keeps strings.
    # Only timestamp fields are normalized, so ordinary string identities stay exact.
    if ($Field -in @('hour', 'target_hour', 'recorded_at')) {
        if ($Value -is [datetimeoffset]) {
            $Value = $Value.UtcDateTime.ToString("yyyy-MM-ddTHH:mm:ss.fffffff'Z'")
        } elseif ($Value -is [datetime]) {
            if ($Value.Kind -eq [DateTimeKind]::Unspecified) {
                $Value = $Value.ToString('yyyy-MM-ddTHH:mm:ss.fffffff')
            } else { $Value = $Value.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffffff'Z'") }
        } elseif ($Value -is [string]) {
            $culture = [Globalization.CultureInfo]::InvariantCulture
            if ($Value -match '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$') {
                $Value = [datetimeoffset]::Parse($Value, $culture).UtcDateTime.ToString("yyyy-MM-ddTHH:mm:ss.fffffff'Z'")
            } elseif ($Value -match '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?$') {
                $Value = [datetime]::Parse($Value, $culture).ToString('yyyy-MM-ddTHH:mm:ss.fffffff')
            }
        }
    }
    if ($Value -is [string]) { return ConvertTo-Json -InputObject $Value -Compress }
    if ($Value -is [bool]) { if ($Value) { return 'true' }; return 'false' }
    if ($Value -is [double] -or $Value -is [single]) {
        Assert-Check (-not [double]::IsNaN($Value) -and -not [double]::IsInfinity($Value)) 'finite numeric value'
        return ([double]$Value).ToString('G17', [Globalization.CultureInfo]::InvariantCulture)
    }
    if ($Value -is [ValueType]) { return $Value.ToString($null, [Globalization.CultureInfo]::InvariantCulture) }
    if ($Value -is [Collections.IDictionary]) {
        $keys = [string[]]@($Value.Keys)
    } elseif ($Value -is [Collections.IEnumerable]) {
        $parts = @($Value | ForEach-Object { Get-CanonicalValue $_ $Field })
        return '[' + ($parts -join ',') + ']'
    } else { $keys = [string[]]@($Value.PSObject.Properties.Name) }
    [Array]::Sort($keys, [StringComparer]::Ordinal)
    $parts = foreach ($key in $keys) {
        $item = if ($Value -is [Collections.IDictionary]) { $Value[$key] } else { $Value.$key }
        (ConvertTo-Json -InputObject $key -Compress) + ':' + (Get-CanonicalValue $item $key)
    }
    return '{' + ($parts -join ',') + '}'
}
function Get-EventDigest($Value) {
    $bytes = [Text.Encoding]::UTF8.GetBytes((Get-CanonicalValue $Value))
    $hasher = [Security.Cryptography.SHA256]::Create()
    try { return -join ($hasher.ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') }) }
    finally { $hasher.Dispose() }
}
function Assert-Same($Actual, $Expected, [string]$Name) {
    Assert-Check ((Get-EventDigest $Actual) -ceq (Get-EventDigest $Expected)) $Name
}
function Get-ForecastCore($Prediction) {
    [ordered]@{
        target_hour=$Prediction.target_hour; workload_stage=$Prediction.workload_stage
        stage_range=$Prediction.stage_range; probabilities=$Prediction.probabilities
        model_version=$Prediction.model_version
    }
}
$health = Read-API '/health'
$metadata = Read-API '/metadata'
Assert-Check ($health.status -eq 'ready' -and $health.model_version -eq $metadata.model_version) 'model readiness'

if ($PreviousReport) {
    $previous = Get-Content -LiteralPath $PreviousReport -Raw | ConvertFrom-Json
    Assert-Check ($previous.status -eq 'passed' -and $previous.data_source -eq 'demo') 'previous verification report'
    $predictionId = [guid]::Parse($previous.prediction_id).ToString()
    $detail = Read-API ("/predictions/$predictionId")
    Assert-Check ($previous.canonical_version -eq 1 -and
        $detail.event.event_id -eq $previous.prediction_event_id -and
        $detail.event.prediction_id -eq $predictionId -and $detail.event.data_source -eq 'demo' -and
        $detail.latest_feedback.event.event_id -eq $previous.feedback_id -and
        $detail.latest_feedback.event.revision -eq 2 -and
        $detail.latest_feedback.event.is_correct -eq $false -and
        $detail.latest_feedback.event.submission.feedback.value -is [bool] -and
        $detail.latest_feedback.event.submission.feedback.value -eq $false) 'feedback persistence after restart'
    Assert-Check ((Get-EventDigest $detail.event) -ceq $previous.prediction_event_sha256 -and
        (Get-EventDigest $detail.latest_feedback.event) -ceq $previous.feedback_event_sha256) 'unchanged persisted event content'
    $feedbackId = $previous.feedback_id
    $countFeedbackId = $previous.count_feedback_id
    $countFeedbackDigest = $previous.count_feedback_sha256
    $mode = 'persistence'
} else {
    $sample = Read-API '/example'
    Assert-Check ($sample.history.Count -eq 168) '168-hour input'
    $predictionId = [guid]::NewGuid().ToString()
    $headers = @{'X-Data-Source'='demo'; 'Idempotency-Key'=$predictionId}
    $prediction = Write-API '/predict' $sample $headers
    Assert-Check ($prediction.prediction_id -eq $predictionId -and
        $prediction.model_version -eq $metadata.model_version -and
        $prediction.target_hour -eq $sample.target_hour -and
        $prediction.workload_stage -in @('idle', 'normal', 'busy') -and
        $prediction.probabilities.Count -eq 3) 'prediction contract'
    $repeated = Write-API '/predict' $sample $headers
    Assert-Check ($repeated.prediction_id -eq $predictionId) 'prediction idempotency'
    Assert-Same (Get-ForecastCore $repeated) (Get-ForecastCore $prediction) 'repeated prediction content'
    Assert-Same $repeated.recorded_at $prediction.recorded_at 'repeated prediction timestamp'
    $countAnswer = @{submission_id=[guid]::NewGuid().ToString(); feedback=@{kind='actual_count'; value=0}}
    $countReceipt = Write-API ("/predictions/$predictionId/feedback") $countAnswer
    Assert-Check ($countReceipt.event.event_id -eq $countAnswer.submission_id -and
        $countReceipt.event.prediction_id -eq $predictionId -and
        $countReceipt.event.actual_stage -eq 'idle' -and $countReceipt.event.revision -eq 1 -and
        $countReceipt.event.is_correct -is [bool] -and
        $countReceipt.event.is_correct -eq ($prediction.workload_stage -eq 'idle')) 'count feedback'
    Assert-Same $countReceipt.event.submission $countAnswer 'stored count submission'
    $countFeedbackId = $countReceipt.event.event_id
    $countFeedbackDigest = Get-EventDigest $countReceipt.event
    $feedbackId = [guid]::NewGuid().ToString()
    $falseAnswer = @{submission_id=$feedbackId; feedback=@{kind='correctness'; value=$false}}
    $receipt = Write-API ("/predictions/$predictionId/feedback") $falseAnswer
    $duplicate = Write-API ("/predictions/$predictionId/feedback") $falseAnswer
    Assert-Check ($receipt.event.event_id -eq $feedbackId -and $receipt.event.prediction_id -eq $predictionId -and
        $receipt.event.revision -eq 2 -and $receipt.event.is_correct -is [bool] -and
        $receipt.event.is_correct -eq $false -and $null -eq $receipt.event.actual_stage) 'false feedback'
    Assert-Same $receipt.event.submission $falseAnswer 'stored false submission'
    Assert-Same $duplicate.event $receipt.event 'feedback idempotency'
    $detail = Read-API ("/predictions/$predictionId")
    Assert-Check ($detail.event.data_source -eq 'demo' -and $detail.event.prediction_id -eq $predictionId -and
        $detail.event.model_sha256 -eq $metadata.model_sha256 -and
        $detail.latest_feedback.event.event_id -eq $feedbackId) 'stored input and latest feedback'
    Assert-Same $detail.event.request $sample 'stored request content'
    Assert-Same $detail.event.response (Get-ForecastCore $prediction) 'stored prediction content'
    Assert-Same $detail.latest_feedback.event $receipt.event 'stored latest feedback content'
    $mode = 'prediction_and_feedback'
}
$events = Read-API ("/logs?prediction_id=$predictionId&limit=10")
Assert-Check ($events.total -eq 3 -and $events.items.Count -eq 3 -and
    @($events.items | Where-Object { $_.event.data_source -ne 'demo' -or $_.event.prediction_id -ne $predictionId }).Count -eq 0) 'three demo events for the same prediction'
$predictionEvents = @($events.items | Where-Object { $_.event.event_type -eq 'prediction' })
$countEvents = @($events.items | Where-Object { $_.event.event_type -eq 'feedback' -and $_.event.revision -eq 1 })
$latestEvents = @($events.items | Where-Object { $_.event.event_type -eq 'feedback' -and $_.event.revision -eq 2 })
Assert-Check ($predictionEvents.Count -eq 1 -and $countEvents.Count -eq 1 -and $latestEvents.Count -eq 1 -and
    $countEvents[0].event.event_id -eq $countFeedbackId -and $latestEvents[0].event.event_id -eq $feedbackId) 'exact event types, identifiers and revisions'
Assert-Same $predictionEvents[0].event $detail.event 'listed prediction content'
Assert-Same $latestEvents[0].event $detail.latest_feedback.event 'listed latest feedback content'
Assert-Check ((Get-EventDigest $countEvents[0].event) -ceq $countFeedbackDigest) 'listed original feedback content'
$logging = Read-API '/logging'
$report = [ordered]@{
    status='passed'; checked_at_utc=[datetime]::UtcNow.ToString('o'); verification=$mode
    base_url=$base; model_version=$metadata.model_version; model_sha256=$metadata.model_sha256
    prediction_id=$predictionId; feedback_id=$feedbackId; feedback_revision=2
    canonical_version=1; prediction_event_id=$detail.event.event_id
    prediction_event_sha256=(Get-EventDigest $detail.event)
    feedback_event_sha256=(Get-EventDigest $detail.latest_feedback.event)
    count_feedback_id=$countFeedbackId; count_feedback_sha256=$countFeedbackDigest
    data_source='demo'; feedback_values='Synthetic count=0 and correctness=false; not ground truth'
    event_count=$events.total; logging_destination=$logging.destination
    remote_delivery_verified=$false
}
$jsonReport = $report | ConvertTo-Json -Depth 5
if ($OutputPath) {
    $fullOutput = [IO.Path]::GetFullPath($OutputPath)
    [IO.Directory]::CreateDirectory((Split-Path -Parent $fullOutput)) | Out-Null
    [IO.File]::WriteAllText($fullOutput, $jsonReport + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
}
$jsonReport
