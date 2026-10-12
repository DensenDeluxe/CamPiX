# Run directly, or invoke the downloaded script block from an interactive terminal.
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$NonInteractive,
    [switch]$WithoutMediaMTX,
    [string]$Uv
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "CamPiX bootstrap command failed: $Program (exit $LASTEXITCODE)" }
}

$installerArgs = @()
if ($DryRun) { $installerArgs += '--dry-run' }
if ($NonInteractive) { $installerArgs += '--non-interactive' }
if ($WithoutMediaMTX) { $installerArgs += '--without-mediamtx' }
if ($Uv) { $installerArgs += @('--uv', $Uv) }
$sourceRef = if ($env:CAMPIX_REF) { $env:CAMPIX_REF } else { 'main' }
if ($sourceRef -notmatch '^[A-Za-z0-9][A-Za-z0-9._/-]*$' -or $sourceRef.Contains('..')) {
    throw 'Invalid CAMPIX_REF; use a commit, tag, or branch name.'
}

if ($DryRun) {
    if ($PSScriptRoot -and (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'scripts/install.py'))) {
        $localPython = Get-Command python -ErrorAction SilentlyContinue
        if ($localPython) {
            Invoke-Checked $localPython.Source (@((Join-Path $PSScriptRoot 'scripts/install.py')) + $installerArgs)
            return
        }
    }
    Write-Output "CamPiX bootstrap dry run: Windows source-ref=$sourceRef"
    Write-Output 'A normal run installs missing git/Python prerequisites through WinGet, fetches a detached source snapshot, and runs its platform installer.'
    return
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run from a regular PowerShell terminal. The runtime and startup task use your limited user account.'
}

function Refresh-ExecutablePath {
    $env:PATH = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
}

function Find-Python {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs/Python/Python312/python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs/Python/Python313/python.exe')
    )
    $command = Get-Command python -ErrorAction SilentlyContinue
    if ($command -and $command.Source -notlike '*WindowsApps*') { $candidates += $command.Source }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            & $candidate -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'
            if ($LASTEXITCODE -eq 0) { return $candidate }
        }
    }
    return $null
}

$pythonExecutable = Find-Python
$gitExecutable = Get-Command git -ErrorAction SilentlyContinue
if (-not $pythonExecutable -or -not $gitExecutable) {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) { throw 'WinGet is required. Install Microsoft App Installer, then rerun this command.' }
    $common = @('--exact', '--source', 'winget', '--accept-package-agreements', '--accept-source-agreements', '--disable-interactivity')
    if (-not $pythonExecutable) {
        Invoke-Checked $winget.Source (@('install', '--id', 'Python.Python.3.12', '--scope', 'user') + $common)
    }
    if (-not $gitExecutable) {
        Invoke-Checked $winget.Source (@('install', '--id', 'Git.Git') + $common)
    }
    Refresh-ExecutablePath
    $pythonExecutable = Find-Python
    $gitExecutable = Get-Command git -ErrorAction SilentlyContinue
}
if (-not $pythonExecutable -or -not $gitExecutable) {
    throw 'Python 3.11+ or Git is unavailable. Open a new terminal after the prerequisite installation, then rerun.'
}

$stagingDirectory = Join-Path ([IO.Path]::GetTempPath()) ('campix-bootstrap-' + [Guid]::NewGuid().ToString('N'))
$env:GIT_TERMINAL_PROMPT = '0'
$env:GIT_HTTP_LOW_SPEED_LIMIT = '1000'
$env:GIT_HTTP_LOW_SPEED_TIME = '60'
$sourceDirectory = Join-Path $stagingDirectory 'source'
New-Item -ItemType Directory -Path $stagingDirectory | Out-Null
Write-Output "CamPiX source staging: $stagingDirectory"
Invoke-Checked $gitExecutable.Source @('init', '--quiet', $sourceDirectory)
Invoke-Checked $gitExecutable.Source @('-C', $sourceDirectory, 'remote', 'add', 'origin', 'https://github.com/DensenDeluxe/CamPiX.git')
Invoke-Checked $gitExecutable.Source @('-C', $sourceDirectory, '-c', 'credential.interactive=false', 'fetch', '--quiet', '--depth', '1', 'origin', $sourceRef)
Invoke-Checked $gitExecutable.Source @('-C', $sourceDirectory, 'checkout', '--quiet', '--detach', 'FETCH_HEAD')
$sourceCommit = & $gitExecutable.Source -C $sourceDirectory rev-parse HEAD
if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve the source commit.' }
Write-Output "CamPiX source commit: $sourceCommit"
$installerPath = Join-Path $sourceDirectory 'scripts/install.py'
if (-not (Test-Path -LiteralPath $installerPath)) {
    throw 'This source revision has no all-in-one installer. Select a published installer revision with CAMPIX_REF.'
}
Invoke-Checked $pythonExecutable (@($installerPath) + $installerArgs)
$commandFolder = Join-Path $env:LOCALAPPDATA 'CamPiX\bin'
if ((Test-Path -LiteralPath (Join-Path $commandFolder 'campix.cmd')) -and -not (($env:PATH -split ';') -contains $commandFolder)) {
    # New terminals read the user PATH from the registry; offer campix in this one as well.
    $env:PATH = "$env:PATH;$commandFolder"
}
