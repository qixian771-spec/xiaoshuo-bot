<#
.SYNOPSIS
    Establish a local loopback SSH tunnel (127.0.0.1:8317 -> 127.0.0.1:8317) to Korea server CPA.
.DESCRIPTION
    PowerShell 5.1 compatible zero-fallback tunnel helper.
    Requires explicit -ServerIp and -IdentityFile parameters (no hardcoded IPs or secrets).
.EXAMPLE
    .\deploy\ssh_cpa_tunnel.ps1 -ServerIp 43.x.x.x -IdentityFile C:\Users\qixia\.ssh\id_ed25519_mac
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ServerIp,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$IdentityFile,

    [string]$RemoteUser = "root",

    [ValidateRange(1, 65535)]
    [int]$SshPort = 22,

    [ValidateRange(1024, 65535)]
    [int]$LocalPort = 8317,

    [ValidateRange(1, 65535)]
    [int]$RemotePort = 8317
)

$ErrorActionPreference = "Stop"

if ($ServerIp -match "^(<|_*REPLACE|example|43\.x\.x\.x)") {
    throw "Refusing placeholder ServerIp '$ServerIp'. Pass the actual server IP address."
}

if (-not (Test-Path -LiteralPath $IdentityFile -PathType Leaf)) {
    throw "SSH identity file not found: '$IdentityFile'"
}

$sshCmd = Get-Command ssh -ErrorAction Stop
if (-not $sshCmd) {
    throw "OpenSSH client 'ssh' was not found in PATH."
}

$forwardSpec = "127.0.0.1:${LocalPort}:127.0.0.1:${RemotePort}"
$remoteTarget = "${RemoteUser}@${ServerIp}"

$sshArgs = @(
    "-i", $IdentityFile,
    "-p", $SshPort.ToString(),
    "-N",
    "-o", "BatchMode=yes",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3",
    "-L", $forwardSpec,
    $remoteTarget
)

Write-Host "Opening loopback CPA SSH tunnel ${forwardSpec} via ${remoteTarget}..."
& $sshCmd.Source @sshArgs
if ($LASTEXITCODE -ne 0) {
    throw "SSH tunnel exited with non-zero status code: $LASTEXITCODE"
}
