# Test Commands (Copy/Paste)

```powershell
cd "C:\Users\DizZy\OneDrive\Desktop\проекты\voice_project_rhvoice"
```

## 0) One-time setup

```powershell
.\scripts\install_piper_windows.ps1
.\scripts\setup_windows_runtime.ps1
```

## 1) Auto tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

## 2) Benchmarks by phrase length

```powershell
.\.venv\Scripts\python.exe -m app.cli.benchmark --phrases benchmarks/phrases_short_ru.json --output reports/bench_short.json
.\.venv\Scripts\python.exe -m app.cli.benchmark --phrases benchmarks/phrases_medium_ru.json --output reports/bench_medium.json
.\.venv\Scripts\python.exe -m app.cli.benchmark --phrases benchmarks/phrases_long_ru.json --output reports/bench_long.json
```

## 3) Piper-only TTS benchmark (12 phrases)

```powershell
$ts = Get-Date -Format 'yyyyMMdd_HHmmss'
$env:TTS_BACKEND='piper'
$env:PIPER_BIN='C:\piper\piper\piper.exe'
$env:PIPER_MODEL='C:\piper\models\ru_RU-ruslan-medium.onnx'
$env:TTS_CACHE_DIR="cache/tts_only_piper_$ts"
.\.venv\Scripts\python.exe -m app.cli.benchmark --tts-texts benchmarks/tts_messages_ab.txt --output "reports/bench_tts_only_piper_$ts.json"
```

## 4) Play generated WAV files

```powershell
Get-ChildItem reports\tts\tts_case_*.wav | Sort-Object Name | ForEach-Object {
  Write-Host "Playing $($_.Name)"
  $p = New-Object System.Media.SoundPlayer $_.FullName
  $p.PlaySync()
}
```

## 5) A/B RHVoice vs Piper (same texts, cold cache)

```powershell
.\scripts\run_tts_ab_compare.ps1 `
  -RhvoiceWindowsVoice "Anna" `
  -PiperBin "C:\piper\piper\piper.exe" `
  -PiperModel "C:\piper\models\ru_RU-ruslan-medium.onnx" `
  -TextsPath "benchmarks/tts_messages_ab.txt"
```

## 6) A/B with immediate playback

```powershell
.\scripts\run_tts_ab_compare.ps1 -Play
```

## 7) Service demo (manual E2E)

### Terminal 1: TTS

```powershell
cd "C:\Users\DizZy\OneDrive\Desktop\проекты\voice_project_rhvoice"
$env:VOICE_API_TOKEN="dev-token-change-me"
$env:TTS_BACKEND="auto"
$env:PIPER_BIN="C:\piper\piper\piper.exe"
$env:PIPER_MODEL="C:\piper\models\ru_RU-ruslan-medium.onnx"
.\.venv\Scripts\python.exe -m uvicorn tts_service:app --host 127.0.0.1 --port 8001
```

### Terminal 2: STT

```powershell
cd "C:\Users\DizZy\OneDrive\Desktop\проекты\voice_project_rhvoice"
$env:VOICE_API_TOKEN="dev-token-change-me"
$env:STT_BACKEND="vosk"
$env:VOSK_MODEL_PATH="models/vosk-model-small-ru-0.22"
.\.venv\Scripts\python.exe -m uvicorn stt_service:app --host 127.0.0.1 --port 8000
```

### Terminal 3: Orchestrator

```powershell
cd "C:\Users\DizZy\OneDrive\Desktop\проекты\voice_project_rhvoice"
$env:VOICE_API_TOKEN="dev-token-change-me"
$env:STT_SERVICE_URL="http://127.0.0.1:8000/stt/recognize"
$env:TTS_SERVICE_URL="http://127.0.0.1:8001/tts/generate"
$env:COMMAND_TRANSPORT="local"
.\.venv\Scripts\python.exe -m uvicorn orchestrator_service:app --host 127.0.0.1 --port 8002
```

### Terminal 4: API calls

```powershell
curl.exe --noproxy "*" -X POST "http://127.0.0.1:8002/alerts/raise" `
  -H "Authorization: Bearer dev-token-change-me" `
  -H "Content-Type: application/json" `
  -d "{\"message\":\"Тревога для демо\",\"timeout_seconds\":30}"

curl.exe --noproxy "*" "http://127.0.0.1:8002/alerts/pending" `
  -H "Authorization: Bearer dev-token-change-me"
```
