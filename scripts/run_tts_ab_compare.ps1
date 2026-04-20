param(
    [string]$RhvoiceWindowsVoice = "Anna",
    [string]$PiperBin = "C:\piper\piper\piper.exe",
    [string]$PiperModel = "C:\piper\models\ru_RU-ruslan-medium.onnx",
    [string]$TextsPath = "benchmarks/tts_messages_ab.txt",
    [switch]$Play
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path "$PSScriptRoot\..").Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (!(Test-Path $python)) {
    throw "Python venv not found: $python"
}

Push-Location $projectRoot
try {
    $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $rhReport = "reports/bench_tts_ab_rhvoice_$timestamp.json"
    $piperReport = "reports/bench_tts_ab_piper_$timestamp.json"
    $compareReport = "reports/bench_tts_ab_compare_$timestamp.json"
    $rhAudioDir = "reports/tts_ab_rhvoice_$timestamp"
    $piperAudioDir = "reports/tts_ab_piper_$timestamp"
    $rhCacheDir = "cache/tts_ab_rhvoice_$timestamp"
    $piperCacheDir = "cache/tts_ab_piper_$timestamp"

    if (!(Test-Path $TextsPath)) {
        throw "Texts file not found: $TextsPath"
    }
    if (!(Test-Path $PiperBin)) {
        throw "Piper binary not found: $PiperBin"
    }
    if (!(Test-Path $PiperModel)) {
        throw "Piper model not found: $PiperModel"
    }

    Write-Host "Running RHVoice/SAPI benchmark..."
    $env:TTS_BACKEND = "rhvoice"
    $env:RHVOICE_WINDOWS_VOICE = $RhvoiceWindowsVoice
    $env:TTS_CACHE_DIR = $rhCacheDir
    & $python -m app.cli.benchmark --tts-texts $TextsPath --output $rhReport
    New-Item -ItemType Directory -Force $rhAudioDir | Out-Null
    Copy-Item "reports/tts/*.wav" $rhAudioDir -Force

    Write-Host "Running Piper benchmark..."
    $env:TTS_BACKEND = "piper"
    $env:PIPER_BIN = $PiperBin
    $env:PIPER_MODEL = $PiperModel
    $env:TTS_CACHE_DIR = $piperCacheDir
    & $python -m app.cli.benchmark --tts-texts $TextsPath --output $piperReport
    New-Item -ItemType Directory -Force $piperAudioDir | Out-Null
    Copy-Item "reports/tts/*.wav" $piperAudioDir -Force

    $rh = Get-Content $rhReport | ConvertFrom-Json
    $pp = Get-Content $piperReport | ConvertFrom-Json

    $rhMean = [double]$rh.tts_runtime.summary.mean_ms
    $ppMean = [double]$pp.tts_runtime.summary.mean_ms
    $rhP95 = [double]$rh.tts_runtime.summary.p95_ms
    $ppP95 = [double]$pp.tts_runtime.summary.p95_ms

    $speedup = if ($ppMean -gt 0) { [math]::Round($rhMean / $ppMean, 3) } else { 0.0 }
    $winner = if ($rhMean -lt $ppMean) { "RHVoice/SAPI" } elseif ($ppMean -lt $rhMean) { "Piper" } else { "Tie" }

    $summary = [ordered]@{
        generated_at = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
        texts_file = $TextsPath
        messages_total = [int]$rh.tts_runtime.messages_total
        rhvoice = @{
            report = $rhReport
            audio_dir = $rhAudioDir
            mean_ms = [math]::Round($rhMean, 3)
            p95_ms = [math]::Round($rhP95, 3)
        }
        piper = @{
            report = $piperReport
            audio_dir = $piperAudioDir
            mean_ms = [math]::Round($ppMean, 3)
            p95_ms = [math]::Round($ppP95, 3)
        }
        ratio_rhvoice_to_piper = $speedup
        faster_backend = $winner
    }

    ($summary | ConvertTo-Json -Depth 5) | Set-Content $compareReport -Encoding utf8

    Write-Host ""
    Write-Host "A/B completed."
    Write-Host "RHVoice report: $rhReport"
    Write-Host "Piper report:   $piperReport"
    Write-Host "Compare report: $compareReport"
    Write-Host "RHVoice mean/p95: $($summary.rhvoice.mean_ms) / $($summary.rhvoice.p95_ms) ms"
    Write-Host "Piper   mean/p95: $($summary.piper.mean_ms) / $($summary.piper.p95_ms) ms"
    Write-Host "Ratio RH/Piper: $($summary.ratio_rhvoice_to_piper)"
    Write-Host "Faster backend: $($summary.faster_backend)"

    if ($Play) {
        Write-Host ""
        Write-Host "Playing RHVoice samples..."
        Get-ChildItem "$rhAudioDir\*.wav" | ForEach-Object {
            Write-Host "  RH -> $($_.Name)"
            $player = New-Object System.Media.SoundPlayer $_.FullName
            $player.PlaySync()
        }
        Write-Host "Playing Piper samples..."
        Get-ChildItem "$piperAudioDir\*.wav" | ForEach-Object {
            Write-Host "  PP -> $($_.Name)"
            $player = New-Object System.Media.SoundPlayer $_.FullName
            $player.PlaySync()
        }
    }
}
finally {
    Pop-Location
}
