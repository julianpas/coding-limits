#Requires -Version 5.1
# coding-limits gateway - startup launcher
# Starts the gateway in the background and logs to logs\gateway.log.
# Safe to run more than once: exits quietly if the port is already serving.

$Repo    = "C:\Projects\Tools\coding-limits"
$Python  = "C:\Python313\python.exe"   # absolute path so it works in the startup environment
$Port    = 8765
$LogDir  = Join-Path $Repo "logs"
$LogFile = Join-Path $LogDir "gateway.log"

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}

function Test-GatewayUp {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $client.Connect("127.0.0.1", $Port)
        return $true
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

if (Test-GatewayUp) {
    # The running gateway holds gateway.log with an exclusive handle,
    # so we can't write to it safely - just exit quietly.
    exit 0
}

Set-Location $Repo
# cmd /c keeps the log as raw bytes (PowerShell's *>> would write UTF-16,
# which clashes with the UTF-8 writes of tray.py).
cmd /c "`"$Python`" server.py 1>> `"$LogFile`" 2>&1"
