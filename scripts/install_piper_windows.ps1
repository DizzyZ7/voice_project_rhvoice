param(
    [string]$PiperRoot = "C:\piper",
    [string]$PiperZip = "C:\piper\piper_windows_amd64.zip",
    [string]$PiperModel = "C:\piper\models\ru_RU-ruslan-medium.onnx",
    [string]$RhvoiceWindowsVoice = "Anna",
    [switch]$Persist = $true
)

$ErrorActionPreference = "Stop"

function Add-UserPathEntry {
    param([Parameter(Mandatory = $true)][string]$Entry)

    $current = [Environment]::GetEnvironmentVariable("Path", "User")
    if ([string]::IsNullOrWhiteSpace($current)) {
        [Environment]::SetEnvironmentVariable("Path", $Entry, "User")
        return
    }

    $parts = $current.Split(";") | Where-Object { $_ -and $_.Trim() -ne "" }
    $already = $parts | Where-Object { $_.Trim().ToLowerInvariant() -eq $Entry.Trim().ToLowerInvariant() }
    if (-not $already) {
        $updated = ($parts + $Entry) -join ";"
        [Environment]::SetEnvironmentVariable("Path", $updated, "User")
    }
}

if (!(Test-Path $PiperRoot)) {
    New-Item -ItemType Directory -Path $PiperRoot -Force | Out-Null
}

$defaultExe = Join-Path $PiperRoot "piper\piper.exe"
if (!(Test-Path $defaultExe)) {
    if (Test-Path $PiperZip) {
        Write-Host "Extracting Piper archive: $PiperZip"
        Expand-Archive -Path $PiperZip -DestinationPath $PiperRoot -Force
    }
}

$resolvedExe = Get-ChildItem $PiperRoot -Recurse -File -Filter "piper.exe" -ErrorAction SilentlyContinue |
    Select-Object -First 1 -ExpandProperty FullName

if (-not $resolvedExe) {
    throw "piper.exe not found under $PiperRoot. Check Piper archive and extraction."
}

if (!(Test-Path $PiperModel)) {
    throw "Piper model not found: $PiperModel"
}

$resolvedExeDir = Split-Path $resolvedExe -Parent

# Current session
$env:TTS_BACKEND = "auto"
$env:PIPER_BIN = $resolvedExe
$env:PIPER_MODEL = $PiperModel
$env:RHVOICE_WINDOWS_VOICE = $RhvoiceWindowsVoice

if ($Persist) {
    [Environment]::SetEnvironmentVariable("TTS_BACKEND", "auto", "User")
    [Environment]::SetEnvironmentVariable("PIPER_BIN", $resolvedExe, "User")
    [Environment]::SetEnvironmentVariable("PIPER_MODEL", $PiperModel, "User")
    [Environment]::SetEnvironmentVariable("RHVOICE_WINDOWS_VOICE", $RhvoiceWindowsVoice, "User")
    Add-UserPathEntry -Entry $resolvedExeDir
}

Write-Host "Piper setup completed."
Write-Host "TTS_BACKEND=$env:TTS_BACKEND"
Write-Host "PIPER_BIN=$env:PIPER_BIN"
Write-Host "PIPER_MODEL=$env:PIPER_MODEL"
Write-Host "RHVOICE_WINDOWS_VOICE=$env:RHVOICE_WINDOWS_VOICE"

Write-Host "Checking piper executable..."
& $resolvedExe --help | Out-Null
Write-Host "piper --help OK"
