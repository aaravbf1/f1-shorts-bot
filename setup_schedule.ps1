<#
.SYNOPSIS
    Registers the twice-daily Windows Scheduled Task for the F1 Shorts Engine
    at 11:00 AM and 7:00 PM with "Run task as soon as possible after a scheduled start is missed" enabled.
#>

$ErrorActionPreference = "Stop"

$TaskName = "F1ShortsEngine_TwiceDaily"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$MainScript = Join-Path $ProjectDir "main.py"
$LogDir = Join-Path $ProjectDir "logs"

if (-not (Test-Path $PythonExe)) {
    throw "Virtual environment Python not found at: $PythonExe"
}
if (-not (Test-Path $MainScript)) {
    throw "main.py not found at: $MainScript"
}
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

# Remove existing task with same name if present
$Existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($Existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$Action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "`"$MainScript`"" `
    -WorkingDirectory $ProjectDir

# Twice daily: 11:00 AM and 7:00 PM (19:00)
$Trigger11AM = New-ScheduledTaskTrigger -Daily -At 11:00AM
$Trigger7PM  = New-ScheduledTaskTrigger -Daily -At 7:00PM

# Enable StartWhenAvailable ("Run task as soon as possible after a scheduled start is missed")
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -MultipleInstances IgnoreNew `
    -RestartCount 1 `
    -RestartInterval (New-TimeSpan -Minutes 10)

$Principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger @($Trigger11AM, $Trigger7PM) `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Autonomous F1 YouTube Shorts Engine: generates and uploads 2 vertical F1 Shorts twice daily at 11:00 AM and 7:00 PM." | Out-Null

Write-Host "Scheduled task '$TaskName' registered successfully!" -ForegroundColor Green
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State | Format-Table -AutoSize
(Get-ScheduledTask -TaskName $TaskName).Triggers | Select-Object StartBoundary, Enabled | Format-Table -AutoSize
(Get-ScheduledTask -TaskName $TaskName).Settings | Select-Object StartWhenAvailable, AllowStartIfOnBatteries, MultipleInstances | Format-Table -AutoSize
