<#
.SYNOPSIS
    Displays the status and last execution result of the "Daily Applicant Scoring Pipeline" scheduled task.

.DESCRIPTION
    Checks Windows Task Scheduler for "Daily Applicant Scoring Pipeline", reports its
    current state, next run time, last run time, last execution result code, active
    execution lock state, and the most recent pipeline summary line from logs/pipeline.log.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$TaskName   = "Daily Applicant Scoring Pipeline"
$WorkingDir = "C:\projects\applicant-scoring"
$LogsDir    = "C:\projects\applicant-scoring\logs"
$LogFile    = "C:\projects\applicant-scoring\logs\pipeline.log"
$LockFile   = "C:\projects\applicant-scoring\logs\pipeline.lock"

function Format-TaskResult {
    param([int]$ResultCode)
    switch ($ResultCode) {
        0          { return "0 (0x0) - Success" }
        1          { return "1 (0x1) - Pipeline returned an error (check logs/pipeline.log)" }
        2          { return "2 (0x2) - Skipped due to active execution lock" }
        267008     { return "267008 (0x41300) - Task is ready to run at its next scheduled time" }
        267009     { return "267009 (0x41301) - Task is currently running" }
        267011     { return "267011 (0x41303) - Task has not yet run" }
        default    { return "$ResultCode (0x$('{0:X}' -f $ResultCode))" }
    }
}

Write-Output "=== Daily Applicant Scoring Pipeline - Task Status ==="

$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $Task) {
    Write-Output "Registered       : False"
    Write-Output "Status           : Not registered in Windows Task Scheduler"
    Write-Output "To Enable        : powershell -ExecutionPolicy Bypass -File `"$WorkingDir\scripts\setup_daily_task.ps1`""
}
else {
    $Info = Get-ScheduledTaskInfo -TaskName $TaskName
    $Action = $Task.Actions | Select-Object -First 1
    Write-Output "Registered       : True"
    Write-Output "TaskName         : $($Task.TaskName)"
    Write-Output "State            : $($Task.State)"
    Write-Output "Program          : $($Action.Execute)"
    Write-Output "Arguments        : $($Action.Arguments)"
    Write-Output "WorkingDirectory : $($Action.WorkingDirectory)"
    Write-Output "LastRunTime      : $($Info.LastRunTime)"
    Write-Output "LastTaskResult   : $(Format-TaskResult -ResultCode $Info.LastTaskResult)"
    Write-Output "NextRunTime      : $($Info.NextRunTime)"
    Write-Output "MissedRuns       : $($Info.NumberOfMissedRuns)"
}

Write-Output ""
Write-Output "=== Runtime Lock & Logs ==="
if (Test-Path -LiteralPath $LockFile -PathType Leaf) {
    $LockContent = (Get-Content -LiteralPath $LockFile -ErrorAction SilentlyContinue) -join " | "
    Write-Output "ExecutionLock    : ACTIVE ($LockFile -> $LockContent)"
}
else {
    Write-Output "ExecutionLock    : Idle (no active lock file)"
}

if (Test-Path -LiteralPath $LogFile -PathType Leaf) {
    Write-Output "LogFile          : $LogFile"
    $LastSummary = Select-String -Path $LogFile -Pattern "=== Pipeline Summary:" | Select-Object -Last 1
    if ($null -ne $LastSummary) {
        Write-Output "LastSummaryLog   : $($LastSummary.Line)"
    }
}
else {
    Write-Output "LogFile          : Not created yet ($LogFile)"
}
