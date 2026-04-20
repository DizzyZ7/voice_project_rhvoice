# Presentation Runbook (Windows, PowerShell)

Дата подготовки: 2026-04-20.

Этот файл содержит пошаговые команды для:
- автотестов;
- ручного demo-прогона;
- честного A/B сравнения RHVoice/SAPI vs Piper в одинаковых условиях;
- прослушивания WAV перед презентацией.

## 1. Подготовка окружения

```powershell
cd "C:\Users\DizZy\OneDrive\Desktop\проекты\voice_project_rhvoice"

# Установка/проверка Piper (один раз)
.\scripts\install_piper_windows.ps1

# (опционально) установка зависимостей
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

## 2. Быстрая проверка проекта

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Ожидаемо: все тесты проходят.

## 3. Базовый benchmark по коротким/средним/длинным фразам

```powershell
.\.venv\Scripts\python.exe -m app.cli.benchmark --phrases benchmarks/phrases_short_ru.json --output reports/bench_short.json
.\.venv\Scripts\python.exe -m app.cli.benchmark --phrases benchmarks/phrases_medium_ru.json --output reports/bench_medium.json
.\.venv\Scripts\python.exe -m app.cli.benchmark --phrases benchmarks/phrases_long_ru.json --output reports/bench_long.json
```

## 4. Честное A/B сравнение TTS (одинаковые тексты, холодный кэш)

Используется набор `benchmarks/tts_messages_ab.txt`.

```powershell
.\scripts\run_tts_ab_compare.ps1 `
  -RhvoiceWindowsVoice "Anna" `
  -PiperBin "C:\piper\piper\piper.exe" `
  -PiperModel "C:\piper\models\ru_RU-ruslan-medium.onnx" `
  -TextsPath "benchmarks/tts_messages_ab.txt"
```

Скрипт создаст:
- `reports/bench_tts_ab_rhvoice_<timestamp>.json`
- `reports/bench_tts_ab_piper_<timestamp>.json`
- `reports/bench_tts_ab_compare_<timestamp>.json`
- `reports/tts_ab_rhvoice_<timestamp>/tts_case_*.wav`
- `reports/tts_ab_piper_<timestamp>/tts_case_*.wav`

## 5. Прослушивание аудио для сравнения

Вариант A: сразу в A/B скрипте с проигрыванием:

```powershell
.\scripts\run_tts_ab_compare.ps1 -Play
```

Вариант B: вручную проиграть последний набор:

```powershell
Get-ChildItem reports\tts_ab_rhvoice_* | Sort-Object LastWriteTime -Descending | Select-Object -First 1
Get-ChildItem reports\tts_ab_piper_*   | Sort-Object LastWriteTime -Descending | Select-Object -First 1
```

Далее подставить найденные папки:

```powershell
Get-ChildItem "reports\tts_ab_rhvoice_<timestamp>\*.wav" | ForEach-Object {
  Write-Host "RH -> $($_.Name)"
  $p = New-Object System.Media.SoundPlayer $_.FullName
  $p.PlaySync()
}

Get-ChildItem "reports\tts_ab_piper_<timestamp>\*.wav" | ForEach-Object {
  Write-Host "PP -> $($_.Name)"
  $p = New-Object System.Media.SoundPlayer $_.FullName
  $p.PlaySync()
}
```

## 6. Ручной E2E прогон (для демонстрации команде)

Терминал 1 (TTS):

```powershell
$env:VOICE_API_TOKEN="dev-token-change-me"
$env:TTS_BACKEND="auto"
$env:PIPER_BIN="C:\piper\piper\piper.exe"
$env:PIPER_MODEL="C:\piper\models\ru_RU-ruslan-medium.onnx"
.\.venv\Scripts\python.exe -m uvicorn tts_service:app --host 127.0.0.1 --port 8001
```

Терминал 2 (STT):

```powershell
$env:VOICE_API_TOKEN="dev-token-change-me"
$env:STT_BACKEND="vosk"
$env:VOSK_MODEL_PATH="models/vosk-model-small-ru-0.22"
.\.venv\Scripts\python.exe -m uvicorn stt_service:app --host 127.0.0.1 --port 8000
```

Терминал 3 (Orchestrator):

```powershell
$env:VOICE_API_TOKEN="dev-token-change-me"
$env:STT_SERVICE_URL="http://127.0.0.1:8000/stt/recognize"
$env:TTS_SERVICE_URL="http://127.0.0.1:8001/tts/generate"
$env:COMMAND_TRANSPORT="local"
.\.venv\Scripts\python.exe -m uvicorn orchestrator_service:app --host 127.0.0.1 --port 8002
```

Терминал 4 (вызовы API):

```powershell
curl.exe --noproxy "*" -X POST "http://127.0.0.1:8002/alerts/raise" `
  -H "Authorization: Bearer dev-token-change-me" `
  -H "Content-Type: application/json" `
  -d "{\"message\":\"Тревога для демо\",\"timeout_seconds\":30}"

curl.exe --noproxy "*" "http://127.0.0.1:8002/alerts/pending" `
  -H "Authorization: Bearer dev-token-change-me"
```

## 7. Что показывать на слайде по итогам прогона

- `pytest`: сколько тестов всего, сколько passed/failed.
- Accuracy short/medium/long (`reports/bench_short.json`, `bench_medium.json`, `bench_long.json`).
- A/B TTS latency из `reports/bench_tts_ab_compare_<timestamp>.json`:
  - mean_ms;
  - p95_ms;
  - winner/faster_backend;
  - ratio_rhvoice_to_piper.
- Субъективное качество (прослушивание WAV) по двум движкам.
