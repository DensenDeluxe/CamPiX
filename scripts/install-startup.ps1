[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PythonExecutable,
    [Parameter(Mandatory = $true)][string]$ServiceArguments,
    [Parameter(Mandatory = $true)][string]$WorkingDirectory,
    [switch]$CheckOnly,
    [switch]$StatusOnly
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'CamPiX startup requires a limited user account.'
}
$existing = Get-ScheduledTask -TaskName 'CamPiX' -ErrorAction SilentlyContinue
if ($StatusOnly) {
    if (-not $existing -or $existing.State.ToString() -ne 'Running') { Write-Output 0; return }
    $processes = @(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | Where-Object {
        $_.CommandLine -and $_.CommandLine.EndsWith($ServiceArguments)
    })
    $roots = @($processes | Where-Object { $_.ExecutablePath -eq $PythonExecutable })
    if ($roots.Count -ne 1) { Write-Output 0; return }
    $serviceProcess = $roots[0]
    # Windows venv redirectors can launch the base Python as a child process.
    for ($depth = 0; $depth -lt 4; $depth++) {
        $children = @($processes | Where-Object { $_.ParentProcessId -eq $serviceProcess.ProcessId })
        if ($children.Count -eq 0) { Write-Output $serviceProcess.ProcessId; return }
        if ($children.Count -ne 1) { Write-Output 0; return }
        $serviceProcess = $children[0]
    }
    Write-Output 0
    return
}
if ($existing) {
    throw 'A CamPiX startup task already exists; inspect it before updating.'
}
if ($CheckOnly) { return }
$action = New-ScheduledTaskAction -Execute $PythonExecutable -Argument $ServiceArguments -WorkingDirectory $WorkingDirectory
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity.Name
$taskPrincipal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'CamPiX' -Action $action -Trigger $trigger -Principal $taskPrincipal -Settings $settings | Out-Null
Start-ScheduledTask -TaskName 'CamPiX'
