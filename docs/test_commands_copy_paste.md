# Полный Прогон Проекта (Windows, Copy/Paste)

## Что это за инструкция
- Это чек-лист: от установки и автотестов до E2E и Grafana.
- Команды ниже рассчитаны на `PowerShell`.
- Если запускаете в `cmd.exe` или Git Bash, используйте только `python ...` и `curl.exe ...` команды без `$env:`/`Invoke-RestMethod`.

## 0) Переход в проект

```powershell
cd "C:\Users\DizZy\OneDrive\Desktop\проекты\voice_project_rhvoice"
```

## 1) Разовая подготовка окружения

```powershell
.\scripts\install_piper_windows.ps1
.\scripts\setup_windows_runtime.ps1
```

## 2) Базовая проверка Python-части (обязательно перед демо)

```powershell
.\.venv\Scripts\python.exe -m pytest -vv -rA --color=yes
```

Ожидаемо: `44 passed`.

## 3) Бенчмарки распознавания по длине фраз

```powershell
.\.venv\Scripts\python.exe -m app.cli.benchmark --phrases benchmarks/phrases_short_ru.json --output reports/bench_short.json
.\.venv\Scripts\python.exe -m app.cli.benchmark --phrases benchmarks/phrases_medium_ru.json --output reports/bench_medium.json
.\.venv\Scripts\python.exe -m app.cli.benchmark --phrases benchmarks/phrases_long_ru.json --output reports/bench_long.json
```

## 4) Бенчмарк только Piper TTS (мужской голос)

```powershell
$ts = Get-Date -Format 'yyyyMMdd_HHmmss'
$env:TTS_BACKEND='piper'
$env:PIPER_BIN='C:\piper\piper\piper.exe'
$env:PIPER_MODEL='C:\piper\models\ru_RU-ruslan-medium.onnx'
$env:TTS_CACHE_DIR="cache/tts_only_piper_$ts"
.\.venv\Scripts\python.exe -m app.cli.benchmark --tts-texts benchmarks/tts_messages_ab.txt --output "reports/bench_tts_only_piper_$ts.json"
```

## 5) A/B сравнение RHVoice vs Piper

```powershell
.\scripts\run_tts_ab_compare.ps1 `
  -RhvoiceWindowsVoice "Anna" `
  -PiperBin "C:\piper\piper\piper.exe" `
  -PiperModel "C:\piper\models\ru_RU-ruslan-medium.onnx" `
  -TextsPath "benchmarks/tts_messages_ab.txt"
```

```powershell
.\scripts\run_tts_ab_compare.ps1 -Play
```

## 6) Ручной E2E без Docker (4 терминала)

### 6.1 Терминал A: TTS

```powershell
cd "C:\Users\DizZy\OneDrive\Desktop\проекты\voice_project_rhvoice"
$env:VOICE_API_TOKEN="dev-token-change-me"
$env:TTS_BACKEND="auto"
$env:PIPER_BIN="C:\piper\piper\piper.exe"
$env:PIPER_MODEL="C:\piper\models\ru_RU-ruslan-medium.onnx"
$env:TTS_OUTPUT_DIR="reports/tts_demo"
.\.venv\Scripts\python.exe -m uvicorn tts_service:app --host 127.0.0.1 --port 8001
```

### 6.2 Терминал B: STT

```powershell
cd "C:\Users\DizZy\OneDrive\Desktop\проекты\voice_project_rhvoice"
$env:VOICE_API_TOKEN="dev-token-change-me"
$env:STT_BACKEND="vosk"
$env:VOSK_MODEL_PATH="models/vosk-model-small-ru-0.22"
.\.venv\Scripts\python.exe -m uvicorn stt_service:app --host 127.0.0.1 --port 8000
```

### 6.3 Терминал C: Orchestrator

```powershell
cd "C:\Users\DizZy\OneDrive\Desktop\проекты\voice_project_rhvoice"
$env:VOICE_API_TOKEN="dev-token-change-me"
$env:STT_URL="http://127.0.0.1:8000/stt/recognize"
$env:TTS_URL="http://127.0.0.1:8001/tts/generate"
$env:COMMAND_TRANSPORT="local"
.\.venv\Scripts\python.exe -m uvicorn orchestrator_service:app --host 127.0.0.1 --port 8002
```

### 6.4 Терминал D: API-запросы к оркестратору (без проблем с JSON)

```powershell
$body = @{
  message = "Тревога для демо"
  timeout_seconds = 30
} | ConvertTo-Json

Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8002/alerts/raise" `
  -Headers @{ Authorization = "Bearer dev-token-change-me" } `
  -ContentType "application/json" `
  -Body $body

Invoke-RestMethod -Method Get `
  -Uri "http://127.0.0.1:8002/alerts/pending" `
  -Headers @{ Authorization = "Bearer dev-token-change-me" }
```

### 6.5 Терминал D: полный прогон 10 команд (TTS -> STT -> Orchestrator)

```powershell
$token = "dev-token-change-me"
$cmds = @(
  "включи свет",
  "выключи свет",
  "какая температура в цехе",
  "подтвердить тревогу",
  "отмена тревоги",
  "отбой эвакуации",
  "включи свет в коридоре",
  "выключи свет в коридоре",
  "доложи текущую температуру",
  "подтвердить тревогу оператором"
)

Remove-Item reports\tts_demo\demo\tts_*.wav -ErrorAction SilentlyContinue

$i = 1
$cmds | ForEach-Object {
  $body = @{ text = $_; save_to_file = "demo/tts_$i.wav"; use_cache = $false } | ConvertTo-Json
  Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8001/tts/generate" `
    -Headers @{ Authorization = "Bearer $token" } `
    -ContentType "application/json" `
    -Body $body | Out-Null
  $i++
}

$wavSet = Get-ChildItem reports\tts_demo\demo\tts_*.wav | Sort-Object { [int]($_.BaseName -replace '\D','') }
if (-not $wavSet -or ($wavSet | Where-Object Length -le 44)) {
  throw "Обнаружены пустые WAV. Проверьте TTS логи и настройки Piper/RHVoice."
}

$wavSet | Select-Object Name, Length

$wavSet | ForEach-Object {
  curl.exe -s -X POST "http://127.0.0.1:8000/stt/recognize" `
    -H "Authorization: Bearer $token" `
    -F "file=@$($_.FullName);type=audio/wav" | Out-Null
}

$wavSet | ForEach-Object {
  curl.exe -s -X POST "http://127.0.0.1:8002/process" `
    -H "Authorization: Bearer $token" `
    -F "file=@$($_.FullName);type=audio/wav" | Out-Null
}

Write-Host "Полный прогон 10 команд завершён."
```

### 6.6 Терминал D: быстрое прослушивание

```powershell
Start-Process (Resolve-Path "reports\tts_demo\demo\tts_1.wav")
```

## 7) Piper мужской голос: отдельная генерация и прослушивание

```powershell
cd "C:\Users\DizZy\OneDrive\Desktop\проекты\voice_project_rhvoice"
$env:TTS_BACKEND="piper"
$env:PIPER_BIN="C:\piper\piper\piper.exe"
$env:PIPER_MODEL="C:\piper\models\ru_RU-ruslan-medium.onnx"

$script = @'
from pathlib import Path
from app.core.speech import PiperTTS

cmds = [
    "включи свет",
    "выключи свет",
    "какая температура в цехе",
    "подтвердить тревогу",
    "отмена тревоги",
    "отбой эвакуации",
    "включи свет в коридоре",
    "выключи свет в коридоре",
    "доложи текущую температуру",
    "подтвердить тревогу оператором",
]

tts = PiperTTS()
out_dir = Path("reports/tts_demo/piper_male_cmds")
out_dir.mkdir(parents=True, exist_ok=True)

for i, text in enumerate(cmds, start=1):
    out = out_dir / f"tts_{i}.wav"
    tts.synthesize_to_wav(text, out, use_cache=False)
    print(out, out.stat().st_size)
'@

$script | .\.venv\Scripts\python.exe -
```

```powershell
$playList = Get-ChildItem reports\tts_demo\piper_male_cmds\tts_*.wav | Sort-Object { [int]($_.BaseName -replace '\D','') }
$playList | ForEach-Object {
  Write-Host "Воспроизведение $($_.Name)"
  Start-Process $_.FullName
  Start-Sleep -Seconds 3
}
```

## 8) Docker + Grafana (полный мониторинг)

### 8.1 Старт контейнеров

```powershell
cd "C:\Users\DizZy\OneDrive\Desktop\проекты\voice_project_rhvoice"
docker compose up --build -d
docker compose ps
```

### 8.2 Проверка health эндпоинтов

```powershell
curl.exe --noproxy "*" -s "http://127.0.0.1:8000/health"
curl.exe --noproxy "*" -s "http://127.0.0.1:8001/health"
curl.exe --noproxy "*" -s "http://127.0.0.1:8002/health"
```

### 8.3 Генерация трафика для графиков

```powershell
$env:VOICE_API_TOKEN="change-me-in-prod"
.\scripts\grafana_demo.ps1
```

### 8.4 Открыть Grafana

- URL: `http://localhost:3000`
- Логин: `admin`
- Пароль: `admin` (если попросит сменить, задайте новый)
- В дашборде выставить `Last 15 minutes` и нажать `Refresh`.

## 9) Надо показать в презентации

```powershell
.\.venv\Scripts\python.exe -m pytest -vv -rA --color=yes
```

- Для слайда по качеству: строка `44 passed`.
- Для слайда по мониторингу: скрин Grafana после `.\scripts\grafana_demo.ps1`.
- Для слайда по голосу: папка `reports\tts_demo\piper_male_cmds`.

## 10) Быстрая диагностика типовых ошибок

- Ошибка `WinError 10048`: порт занят, убейте старый процесс `uvicorn` и перезапустите.
- Ошибка JSON в `curl` на PowerShell: используйте `Invoke-RestMethod` с `ConvertTo-Json`.
- `No data` в Grafana: сначала сгенерируйте трафик через `.\scripts\grafana_demo.ps1`.
- `WAV` размером 46 байт: это битый файл, перегенерировать через блок `7` (Piper мужской голос).
- Команды `$env:...` не работают: вы не в PowerShell.
