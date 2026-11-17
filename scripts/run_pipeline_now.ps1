<#
.SYNOPSIS
    Manually triggers the Daily Applicant Scoring Pipeline.

.DESCRIPTION
    Executes C:\projects\applicant-scoring\.venv\Scripts\python.exe run_pipeline.py
    from C:\projects\applicant-scoring, forwarding any optional CLI flags, or
    triggers the registered Windows Scheduled Task when -UseScheduledTask is passed.

.PARAMETER DryRun
    Runs the pipeline in safe dry-run mode (no live Groq API calls and no Coda writes).

.PARAMETER Limit
    Maximum number of eligible applicants to process (--limit N).

.PARAMETER Email
    Filter to a single applicant email address (--email EMAIL).

.PARAMETER RetryFailed
    Process only applicants whose existing Coda status is Failed (--retry-failed).

.PARAMETER ApplicantIds
    Process only specific Applicant IDs (--applicant-ids APP-001 ...).

.PARAMETER UseScheduledTask
    Starts the registered Windows Scheduled Task ("Daily Applicant Scoring Pipeline")
    instead of invoking python.exe directly in the current console.

.PARAMETER ValidateOnly
    Verifies executable path, working directory, and constructed CLI arguments without running.
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [int]$Limit = 0,
    [string]$Email = "",
    [switch]$RetryFailed,
    [string[]]$ApplicantIds = @(),
    [switch]$UseScheduledTask,
    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"

$TaskName   = "Daily Applicant Scoring Pipeline"
$WorkingDir = "C:\projects\applicant-scoring"
$PythonExe  = "C:\projects\applicant-scoring\.venv\Scripts\python.exe"
$RunnerFile = "C:\projects\applicant-scoring\run_pipeline.py"
$LogsDir    = "C:\projects\applicant-scoring\logs"

if (-not (Test-Path -LiteralPath $WorkingDir -PathType Container)) {
    throw "Working directory not found: $WorkingDir"
}
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Python interpreter not found: $PythonExe"
}
if (-not (Test-Path -LiteralPath $RunnerFile -PathType Leaf)) {
    throw "Pipeline runner script not found: $RunnerFile"
}
if (-not (Test-Path -LiteralPath $LogsDir -PathType Container)) {
    New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null
}

$CliArgs = @("run_pipeline.py")
if ($DryRun) {
    $CliArgs += "--dry-run"
}
if ($Limit -gt 0) {
    $CliArgs += "--limit"
    $CliArgs += "$Limit"
}
if (-not [string]::IsNullOrWhiteSpace($Email)) {
    $CliArgs += "--email"
    $CliArgs += $Email.Trim()
}
if ($RetryFailed) {
    $CliArgs += "--retry-failed"
}
if ($ApplicantIds -and $ApplicantIds.Count -gt 0) {
    $CliArgs += "--applicant-ids"
    foreach ($id in $ApplicantIds) {
        if (-not [string]::IsNullOrWhiteSpace($id)) {
            $CliArgs += $id.Trim()
        }
    }
}

if ($ValidateOnly) {
    Write-Output "=== Manual Pipeline Runner Validation (Not Executed) ==="
    Write-Output "PythonExe        : $PythonExe"
    Write-Output "WorkingDirectory : $WorkingDir"
    Write-Output "Arguments        : $($CliArgs -join ' ')"
    Write-Output "LogsDirectory    : $LogsDir"
    Write-Output "ValidationResult : PASSED"
    exit 0
}

if ($UseScheduledTask) {
    $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $Task) {
        throw "Scheduled task '$TaskName' is not registered. Run scripts\setup_daily_task.ps1 first or omit -UseScheduledTask."
    }
    Start-ScheduledTask -TaskName $TaskName
    Write-Output "Started Windows Scheduled Task '$TaskName'. Check status with scripts\check_daily_task.ps1."
    exit 0
}

Push-Location -LiteralPath $WorkingDir
try {
    & $PythonExe @CliArgs
    $ExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

exit $ExitCode
