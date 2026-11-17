<#
.SYNOPSIS
    Registers the "Daily Applicant Scoring Pipeline" task in Windows Task Scheduler.

.DESCRIPTION
    Configures Windows Task Scheduler to execute the Daily Applicant Scoring Pipeline
    every day at 9:00 AM local time using the current Windows user's account (no password stored).
    - Program:   C:\projects\applicant-scoring\.venv\Scripts\python.exe
    - Arguments: run_pipeline.py
    - Start in:  C:\projects\applicant-scoring
    - Settings:  StartWhenAvailable, MultipleInstances=IgnoreNew, ExecutionTimeLimit=4 hours

.PARAMETER ValidateOnly
    Validates executable paths, working directory, logs directory, and Scheduled Task
    objects without registering the task in Windows Task Scheduler.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"

$TaskName   = "Daily Applicant Scoring Pipeline"
$WorkingDir = "C:\projects\applicant-scoring"
$PythonExe  = "C:\projects\applicant-scoring\.venv\Scripts\python.exe"
$RunnerFile = "C:\projects\applicant-scoring\run_pipeline.py"
$Arguments  = "run_pipeline.py"
$LogsDir    = "C:\projects\applicant-scoring\logs"
$DailyTime  = "09:00"

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

$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

$Action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument $Arguments `
    -WorkingDirectory $WorkingDir

$Trigger = New-ScheduledTaskTrigger -Daily -At $DailyTime

$Principal = New-ScheduledTaskPrincipal `
    -UserId $CurrentUser `
    -LogonType Interactive `
    -RunLevel Limited

$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

$Description = (
    "Runs the Daily Applicant Scoring Pipeline (Google Sheets -> Google Drive -> " +
    "Groq openai/gpt-oss-120b -> Coda) every day at 9:00 AM local time. " +
    "Logs are written to C:\projects\applicant-scoring\logs\pipeline.log."
)

if ($ValidateOnly) {
    Write-Output "=== Scheduled Task Configuration Validation (Not Registered) ==="
    Write-Output "TaskName          : $TaskName"
    Write-Output "Schedule          : Daily at 09:00 AM local time"
    Write-Output "Execute           : $($Action.Execute)"
    Write-Output "Arguments         : $($Action.Arguments)"
    Write-Output "WorkingDirectory  : $($Action.WorkingDirectory)"
    Write-Output "UserId            : $($Principal.UserId)"
    Write-Output "LogonType         : $($Principal.LogonType)"
    Write-Output "StartWhenAvailable: $($Settings.StartWhenAvailable)"
    Write-Output "MultipleInstances : $($Settings.MultipleInstances)"
    Write-Output "ExecutionTimeLimit: $($Settings.ExecutionTimeLimit)"
    Write-Output "LogsDirectory     : $LogsDir"
    Write-Output "ValidationResult  : PASSED"
    exit 0
}

if ($PSCmdlet.ShouldProcess($TaskName, "Register-ScheduledTask")) {
    $RegisteredTask = Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $Action `
        -Trigger $Trigger `
        -Principal $Principal `
        -Settings $Settings `
        -Description $Description `
        -Force

    $TaskInfo = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Output "=== Scheduled Task Registered Successfully ==="
    Write-Output "TaskName        : $($RegisteredTask.TaskName)"
    Write-Output "State           : $($RegisteredTask.State)"
    Write-Output "Schedule        : Daily at 9:00 AM local time"
    Write-Output "Program         : $PythonExe"
    Write-Output "Arguments       : $Arguments"
    Write-Output "StartIn         : $WorkingDir"
    Write-Output "Account         : $CurrentUser (Interactive, no password stored)"
    Write-Output "NextRunTime     : $($TaskInfo.NextRunTime)"
    Write-Output "LogsLocation    : $LogsDir\pipeline.log"
}
