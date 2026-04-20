param(
    [string]$VoskModelPath = "models/vosk-model-small-ru-0.22",
    [string]$RhvoiceWindowsVoice = "Anna",
    [string]$PiperBin = "C:\piper\piper\piper.exe",
    [string]$PiperModel = "C:\piper\models\ru_RU-ruslan-medium.onnx"
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path "$PSScriptRoot\..").Path
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (!(Test-Path $venvPython)) {
    throw "Не найден .venv Python: $venvPython"
}

Push-Location $projectRoot
try {
    $resolvedModelPath = (Resolve-Path $VoskModelPath).Path
}
finally {
    Pop-Location
}

# Relative path is more robust for Vosk on Windows when project path contains non-ASCII symbols.
$env:VOSK_MODEL_PATH = $VoskModelPath
$env:RHVOICE_WINDOWS_VOICE = $RhvoiceWindowsVoice
$env:TTS_BACKEND = if ($env:TTS_BACKEND) { $env:TTS_BACKEND } else { "auto" }
$env:VOICE_API_TOKEN = if ($env:VOICE_API_TOKEN) { $env:VOICE_API_TOKEN } else { "dev-token-change-me" }

if (Test-Path $PiperBin) {
    $env:PIPER_BIN = $PiperBin
}
if (Test-Path $PiperModel) {
    $env:PIPER_MODEL = $PiperModel
}

Write-Host "Configured for current session:"
Write-Host "VOSK_MODEL_PATH=$env:VOSK_MODEL_PATH"
Write-Host "RHVOICE_WINDOWS_VOICE=$env:RHVOICE_WINDOWS_VOICE"
Write-Host "TTS_BACKEND=$env:TTS_BACKEND"
Write-Host "PIPER_BIN=$env:PIPER_BIN"
Write-Host "PIPER_MODEL=$env:PIPER_MODEL"
Write-Host "VOICE_API_TOKEN=$env:VOICE_API_TOKEN"

Push-Location $projectRoot
try {
    & $venvPython -c "from app.core.speech import RHVoiceTTS; t=RHVoiceTTS(); print('TTS backend:', t.backend, '| voice:', t.windows_voice or t.binary)"
}
finally {
    Pop-Location
}
