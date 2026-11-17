<#
.SYNOPSIS
    Removes the "Daily Applicant Scoring Pipeline" task from Windows Task Scheduler.

.DESCRIPTION
    Checks whether the scheduled task "Daily Applicant Scoring Pipeline" exists and
    unregisters it without requiring interactive confirmation.

.PARAMETER ValidateOnly
    Verifies script syntax and checks current task existence without removing the task.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"

$TaskName = "Daily Applicant Scoring Pipeline"

$ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

if ($ValidateOnly) {
    Write-Output "=== Remove Scheduled Task Validation (No Changes Made) ==="
    Write-Output "TaskName          : $TaskName"
    Write-Output "CurrentlyExists   : $([bool]($null -ne $ExistingTask))"
    Write-Output "ValidationResult  : PASSED"
    exit 0
}

if ($null -eq $ExistingTask) {
    Write-Output "Scheduled task '$TaskName' is not currently registered. Nothing to remove."
    exit 0
}

if ($PSCmdlet.ShouldProcess($TaskName, "Unregister-ScheduledTask")) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "=== Scheduled Task Removed ==="
    Write-Output "TaskName : $TaskName"
    Write-Output "Status   : Unregistered"
}
