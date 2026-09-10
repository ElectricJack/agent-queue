<#
.SYNOPSIS
    Starts the Agent Queue bootstrap from Windows in a supported WSL2 distro.

.DESCRIPTION
    Run this script from a regular PowerShell window:

        irm https://raw.githubusercontent.com/ElectricJack/agent-queue/main/scripts/install-windows.ps1 | iex

    It uses Ubuntu 24.04 on WSL2 because that is AQ's supported Windows host.
    AQ itself, its project checkout, database client, and agent harnesses run
    in Linux; this script never installs or runs AQ natively on Windows.

    When WSL is missing, Windows requires an elevated PowerShell for
    `wsl --install`.  The script stops after that action with explicit reboot
    and first-launch instructions.  Run the same command again afterwards;
    installed distributions are detected and reused.
#>
[CmdletBinding()]
param(
    [string]$Distro = "Ubuntu-24.04",
    [string]$Repository = "https://github.com/ElectricJack/agent-queue.git"
)

$ErrorActionPreference = "Stop"

function Write-NextAction([string]$Message) {
    Write-Host ""
    Write-Host "Next: $Message" -ForegroundColor Yellow
}

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-WslDistroVersion([string]$Name) {
    # `wsl --list --verbose` is the documented source for an installed
    # distribution's WSL generation.  Match only the requested name and the
    # final numeric version, so names containing spaces continue to work.
    $escaped = [Regex]::Escape($Name)
    foreach ($line in (& wsl.exe --list --verbose 2>$null)) {
        if ($line -match "^\s*\*?\s*$escaped\s+.+\s+([12])\s*$") {
            return [int]$Matches[1]
        }
    }
    return $null
}

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw "This entry point must be run from Windows PowerShell, not inside WSL."
}

if ([Environment]::OSVersion.Version.Build -lt 19041) {
    throw "AQ requires Windows 10 build 19041 (version 2004) or Windows 11 for WSL2."
}

if ($Distro -ne "Ubuntu-24.04") {
    throw "AQ's supported Windows path is Ubuntu-24.04 on WSL2; do not select $Distro."
}

if ($Repository -notmatch "^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git$") {
    throw "Repository must be an HTTPS GitHub clone URL ending in .git."
}

$wslVersion = Get-WslDistroVersion $Distro
if ($null -eq $wslVersion) {
    if (-not (Test-Administrator)) {
        Write-Host "Windows must enable WSL and install $Distro before AQ can run." -ForegroundColor Yellow
        Write-Host "Open PowerShell with 'Run as administrator', then run:"
        Write-Host "  wsl --install -d $Distro"
        Write-NextAction "Restart Windows when WSL requests it, launch $Distro once from Start, create the Linux username/password it requests, then rerun this command from a regular PowerShell window."
        exit 10
    }

    Write-Host "Installing WSL and $Distro (Windows-owned action)..."
    & wsl.exe --install -d $Distro
    if ($LASTEXITCODE -ne 0) {
        throw "wsl --install failed (exit $LASTEXITCODE). Update Windows/WSL, then rerun this command."
    }
    Write-NextAction "Restart Windows, launch $Distro once from Start to create its Linux user, then rerun this command from a regular PowerShell window."
    exit 3010
}

if ($wslVersion -ne 2) {
    Write-Host "$Distro is installed as WSL$wslVersion; converting it to WSL2..."
    & wsl.exe --set-version $Distro 2
    if ($LASTEXITCODE -ne 0) {
        throw "WSL conversion failed (exit $LASTEXITCODE). Back up the distribution, resolve the WSL error, then rerun this command."
    }
}

Write-Host "Using existing $Distro on WSL2. AQ will be installed in its Linux home filesystem."

# `--cd ~` prevents a PowerShell working directory from becoming /mnt/c/... .
# The command below is intentionally a small transport: the WSL script owns
# Linux setup and invokes the common `aq install` interface.
$repoArgument = "'" + $Repository.Replace("'", "'\"'\"'") + "'"
$command = "curl -fsSL https://raw.githubusercontent.com/ElectricJack/agent-queue/main/scripts/install-wsl.sh | bash -s -- $repoArgument"
& wsl.exe --distribution $Distro --cd ~ -- bash -lc $command
if ($LASTEXITCODE -ne 0) {
    throw "The WSL bootstrap stopped (exit $LASTEXITCODE). Read its next action, fix that condition in $Distro, then rerun this command."
}

Write-Host ""
Write-Host "AQ installation completed inside WSL." -ForegroundColor Green
Write-Host "When AQ reports a dashboard URL, open it from Windows with: Start-Process <url>"
Write-Host "Windows browsers can reach WSL services through localhost. For a Windows service needed from WSL, use the Windows host IP from: ip route show default | awk '{print `$3}'."
