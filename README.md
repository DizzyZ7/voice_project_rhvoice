# Голосовой сервис (Vosk / faster-whisper + RHVoice / Piper)

Этот репозиторий содержит два уровня реализации офлайн‑системы голосовых команд:

1. **Монолитная версия**: файл `voice_command_service.py` и GUI `voice_command_gui.py` – простой прототип, который слушает микрофон, распознаёт ключевые фразы и озвучивает ответ. Он подходит для быстрого старта на Raspberry Pi или ПК без контейнеризации.
2. **Микросервисная версия**: набор сервисов, оформленных как FastAPI‑приложения с Prometheus‑метриками. Поддерживаются переключаемые backend’ы STT (`vosk`, `faster_whisper`) и TTS (`rhvoice`, `piper`), а также сценарий промышленного alert flow.

Ниже представлены инструкции для обоих вариантов.

## Содержимое репозитория

- **speech_core.py** – общие классы для STT/TTS и диагностики.
- **voice_command_service.py** – консольный сервис (монолит), который слушает микрофон, распознаёт команды и озвучивает ответ.
- **voice_command_gui.py** – простой GUI на Tkinter.
- **mvp_tts_stt.py** – утилита для проверки связки STT + TTS с микрофоном или WAV‑файлом.
- **stt_service.py** – REST‑служба распознавания речи (FastAPI + Vosk/faster-whisper) с метриками Prometheus.
- **tts_service.py** – REST‑служба синтеза речи (FastAPI + RHVoice/Piper) с метриками Prometheus.
- **orchestrator_service.py** – REST‑служба‑оркестратор, которая принимает аудио, вызывает STT и TTS сервисы, публикует команды в MQTT и собирает метрики.
- **docker-compose.yml** – конфигурация для запуска всех микросервисов вместе с Mosquitto, Prometheus и Grafana.
- **prometheus.yml** – конфигурация Prometheus для сбора метрик со всех сервисов.
- **grafana_dashboard_voice.json** – готовый дашборд Grafana (p95 latency STT/TTS, команды и ошибки).
- **requirements.txt** – зафиксированные production-зависимости.
- **requirements-dev.txt** – зависимости для разработки и тестов.
- **logs/** – примеры логов работы монолита и тестов.

Начиная с текущей версии основная реализация разложена по пакетам в каталоге `app/`:

- **app/core/** – STT/TTS, диагностика, логирование.
- **app/commands/** – реестр команд и выполнение команд монолита.
- **app/services/** – FastAPI-сервисы STT, TTS и orchestrator.
- **app/ui/** – GUI.
- **app/cli/** – CLI-утилиты.
- **app/integrations/** – интеграции с оборудованием (GPIO/Modbus), маппинг команд на действия.
- **tests/** – основной pytest-набор.

Файлы в корне (`speech_core.py`, `stt_service.py`, `tts_service.py`, `voice_command_service.py` и т.д.) сохранены как совместимые entrypoint-обёртки, чтобы старые команды запуска и импорты не сломались.

## Быстрый старт (монолит)

Монолитный сервис подходит для быстрой проверки работоспособности связки STT + TTS на Raspberry Pi.

1. Установите зависимости:
   ```bash
   pip install -r requirements.txt
   pip install -r requirements-dev.txt
   ```
2. Установите TTS-движок (RHVoice или Piper) и задайте backend-и. Пример переменных окружения:
   ```bash
   export STT_BACKEND=vosk  # или faster_whisper
   export VOSK_MODEL_PATH=/path/to/vosk-model-small-ru-0.22
   export FASTER_WHISPER_MODEL=small
   export TTS_BACKEND=rhvoice  # или piper
   export RHVOICE_BIN=RHVoice-test  # или rhvoice.test
   export PIPER_MODEL_PATH=/path/to/ru_RU-model.onnx
   export VOICE_API_TOKEN=change-me-in-prod
   ```
   Для Raspberry Pi 4 / Debian 11 (`aarch64`), если пакеты RHVoice не ставятся, используйте инструкцию по сборке из исходников: `docs/rhvoice_build_debian11_arm64.md`.
   На Windows можно использовать установленный RHVoice SAPI голос (без `RHVoice-test` в `PATH`), задав:
   ```powershell
   $env:VOSK_MODEL_PATH="C:\path\to\vosk-model-small-ru-0.22"
   $env:RHVOICE_WINDOWS_VOICE="Anna"
   ```
   Либо запустить подготовительный скрипт:
   ```powershell
   .\scripts\setup_windows_runtime.ps1
   ```
   Запуск сервиса в Windows (с автонастройкой окружения):
   ```powershell
   .\scripts\run_voice_service_windows.ps1
   ```
3. Запустите сервис:
   ```bash
   python3 voice_command_service.py
   ```
   или GUI:
   ```bash
   python3 voice_command_gui.py
   ```

Сервис распознаёт команды ("включи свет", "выключи свет", "какая температура", "стоп") и озвучивает ответы. Логи пишутся в каталог `logs/`.

## Микросервисная архитектура

Для промышленного внедрения прототип разделён на независимые сервисы. Каждый сервис запускается в собственном контейнере, имеет метрики для мониторинга и общается через HTTP; транспорт команд настраивается (`local` или `mqtt`).

### Компоненты

- **STT‑сервис (`stt_service.py`)**: принимает WAV‑файлы по HTTP (endpoint `/stt/recognize`), распознаёт речь через `Vosk` или `faster-whisper` (env `STT_BACKEND`) и возвращает текст. Экспонирует метрики на порту `9101`.
- **TTS‑сервис (`tts_service.py`)**: принимает текст по HTTP (endpoint `/tts/generate`), синтезирует речь через `RHVoice` или `Piper` (env `TTS_BACKEND`), поддерживает файловый кэш синтеза. Метрики на `9102`.
- **Оркестратор (`orchestrator_service.py`)**: принимает аудио (`/process`), вызывает STT/TTS, выполняет dispatch команд в `local` или `mqtt` режимах, поддерживает idempotency (`Idempotency-Key`) и alert flow (`/alerts/raise`, `/alerts/{id}/ack`, `/alerts/pending`). Метрики на `9103`.
- **Mosquitto**: брокер MQTT, который получает команды от оркестратора и раздаёт их подписчикам (например, контроллеры оборудования).
- **Prometheus**: собирает метрики со всех сервисов.
- **Grafana**: визуализирует метрики (dashboards/ панель).

### Запуск через Docker Compose

Прежде чем запускать, убедитесь, что у вас есть:

1. **Модель Vosk**. Скачайте русскую модель (например, `vosk-model-small-ru-0.22`) и поместите её в папку `models` внутри репозитория:
   ```bash
   mkdir -p models
   wget -O models/vosk-model-small-ru-0.22.zip https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip
   unzip models/vosk-model-small-ru-0.22.zip -d models
   ```
2. **TTS**. Для `TTS_BACKEND=rhvoice` установите пакет `rhvoice` (или `RHVoice-test` в `PATH`). Для `TTS_BACKEND=piper` установите бинарник `piper` и укажите `PIPER_MODEL_PATH`.

Теперь запустите все сервисы:

```bash
docker compose up --build
```

Docker Compose соберёт образы для Python‑сервисов, запустит Mosquitto, Prometheus и Grafana. Сервисы будут доступны по портам:

- STT API – `http://localhost:8000/stt/recognize`
- TTS API – `http://localhost:8001/tts/generate`
- Оркестратор – `http://localhost:8002/process`
- Prometheus – `http://localhost:9090`
- Grafana – `http://localhost:3000`

Все API защищены токеном. Для запросов используйте заголовок:

```bash
Authorization: Bearer <VOICE_API_TOKEN>
```

Если оркестратор работает на одном узле без брокера, включите локальный транспорт:

```bash
export COMMAND_TRANSPORT=local
```

Параметры сценария оповещений:

```bash
export ALERT_ACK_TIMEOUT_SECONDS=30
export ALERT_MAX_ESCALATION_LEVEL=2
```

### Настройка Prometheus и Grafana

Файл `prometheus.yml` конфигурирует Prometheus для сбора метрик со всех сервисов (порты `9101`–`9103`). Grafana автоматически подключается к Prometheus (при добавлении data source) и может импортировать готовый дашборд `grafana_dashboard_voice.json`.

#### Импорт дашборда

1. Откройте Grafana (`http://localhost:3000`), зайдите под администратором (по умолчанию admin/admin).
2. Добавьте источник данных Prometheus: **Configuration → Data Sources → Prometheus**, URL – `http://prometheus:9090` (если Grafana запущена в Docker Compose). Нажмите **Save & Test**.
3. Импортируйте дашборд: **Create → Import**, загрузите `grafana_dashboard_voice.json` или вставьте его содержимое. Выберите источник данных Prometheus.
4. Дашборд покажет p95‑латентность STT и TTS, количество команд в минуту и количество ошибок в минуту.

### Расширение

Чтобы довести систему до индустриального стандарта:

- Реализованы обработчики интеграций через `app/integrations/runtime.py` и конфиг `config/integration_map.example.json` (GPIO/Modbus). Для жёсткого режима проверки интеграции используйте `INTEGRATION_STRICT=1`.
- Добавьте wake‑word (например, через snowboy или Porcupine), чтобы сервис реагировал только после ключевой фразы.
- Расширьте список команд, вынеся его в конфигурационный JSON/YAML.
- Усовершенствуйте STT/TTS: используйте более точные модели Vosk, другие движки TTS (Piper, Silero) при необходимости.
- Настройте TLS и авторизацию для MQTT, Prometheus и Grafana.

## Безопасность и эксплуатация

- **Auth**: сервисы требуют `VOICE_API_TOKEN` (Bearer token или `X-API-Key`).
- **Rate limiting**: базовое ограничение частоты запросов включено для STT/TTS/Orchestrator.
- **DoS-защита**: ограничение размера аудио (`MAX_AUDIO_BYTES`) и длины текста TTS (`MAX_TTS_TEXT_LENGTH`).
- **Надёжность интеграций**: оркестратор использует retries для upstream вызовов STT/TTS.
- **Качество распознавания команд**: сопоставление с confidence threshold (`COMMAND_CONFIDENCE_THRESHOLD`).
- **Idempotency**: `/process` и `/alerts/raise` поддерживают заголовок `Idempotency-Key`; состояние хранится в SQLite (`data/orchestrator.db`) с TTL (`ORC_IDEMPOTENCY_TTL_SECONDS`).
- **Proxy-safe upstream**: по умолчанию upstream HTTP у оркестратора работает с `trust_env=False`; при необходимости можно включить `ORC_HTTP_TRUST_ENV=1`.

## Ключевые переменные окружения

- `STT_BACKEND`: `vosk` | `faster_whisper`
- `STT_DENOISE_BACKEND`: `none` | `rnnoise` (опциональный шумодав перед STT)
- `STT_DENOISE_FAIL_OPEN`: `1` | `0` (продолжать без шумодава при ошибке)
- `STT_RNNOISE_LIB`: путь к RNNoise библиотеке (`.dll` на Windows, `.so` на Linux)
- `STT_RNNOISE_VOICE_PROB_THRESHOLD`: порог voice-prob RNNoise (опционально)
- `TTS_BACKEND`: `auto` | `rhvoice` | `piper`
- `VOSK_MODEL_PATH`: путь к модели Vosk
- `FASTER_WHISPER_MODEL`, `FASTER_WHISPER_DEVICE`, `FASTER_WHISPER_COMPUTE_TYPE`
- `PIPER_BIN`, `PIPER_MODEL`
- `STT_ENABLE_VAD`, `STT_VAD_RMS_THRESHOLD`, `STT_VAD_MIN_SPEECH_RATIO`
- `TTS_CACHE_DIR`, `TTS_CACHE_TTL_SECONDS`
- `COMMAND_TRANSPORT`: `local` | `mqtt`
- `INTEGRATION_MAP_PATH`, `INTEGRATION_DRY_RUN`, `INTEGRATION_STRICT`
- `ORC_DB_PATH`, `ORC_IDEMPOTENCY_TTL_SECONDS`

Для Windows в проект добавлен исходник `third_party/rnnoise-windows`. Сборка:

```powershell
.\scripts\build_rnnoise_windows.ps1
```

После сборки укажите:

```powershell
$env:STT_RNNOISE_LIB="C:\path\to\third_party\rnnoise-windows\x64\Release\rnnoise_share.dll"
```

Для Linux можно использовать Python wrapper:

```bash
pip install git+https://github.com/dbklim/RNNoise_Wrapper.git
```

Для Raspberry Pi/Linux в проект также добавлен локальный fallback:
`third_party/RNNoise_Wrapper/rnnoise_wrapper/libs/librnnoise_default.so.0.4.1`.
То есть при `STT_DENOISE_BACKEND=rnnoise` сервис может работать и без отдельного `pip install`,
если зависимости `numpy` и `pydub` доступны в окружении.

Быстрая проверка на Linux/Raspberry Pi:

```bash
export STT_DENOISE_BACKEND=rnnoise
export STT_DENOISE_FAIL_OPEN=0
export STT_RNNOISE_LIB="$(pwd)/third_party/RNNoise_Wrapper/rnnoise_wrapper/libs/librnnoise_default.so.0.4.1"
python -m uvicorn stt_service:app --host 127.0.0.1 --port 8000
curl -s -X POST "http://127.0.0.1:8000/stt/recognize" \
  -H "Authorization: Bearer change-me-in-prod" \
  -F "file=@voice_test.wav;type=audio/wav"
```

## Alert Flow API

- `POST /alerts/raise` – создать тревогу (поддерживает `Idempotency-Key`)
- `POST /alerts/{alert_id}/ack` – подтверждение оператором
- `GET /alerts/pending` – список активных тревог

Пример:

```bash
curl -X POST http://localhost:8002/alerts/raise \
  -H "Authorization: Bearer <VOICE_API_TOKEN>" \
  -H "Idempotency-Key: alert-incident-001" \
  -H "Content-Type: application/json" \
  -d '{"message":"Тревога: превышение давления", "timeout_seconds":30}'
```

## Тестирование

Файл `test_stack.py` содержит примеры автоматизированных тестов для проверки логики команд и вызовов RHVoice. Запустите его, чтобы убедиться, что парсинг команд и синтез речи работают корректно:

```bash
python3 test_stack.py
```

Полный pytest-набор:

```bash
python -m pytest -q
```

CI: в репозитории добавлен workflow `.github/workflows/ci.yml` (pytest на Python 3.11/3.12).

## Материалы этапа 2

Для сдачи второго этапа в репозитории добавлены отдельные артефакты:

- `docs/stage2_report.md` - краткий отчёт по архитектуре, API, ресурсам и результатам этапа;
- `docs/stage2_acceptance_matrix.md` - трассировка критериев приёмки к файлам проекта;
- `docs/demo_scenarios.md` - пошаговые демонстрационные сценарии;
- `docs/economic_justification.md` - технико-экономическое обоснование;
- `benchmarks/phrases_ru.json` - контрольный набор русских фраз;
- `benchmarks/phrases_short_ru.json` - короткие команды (1-3 слова);
- `benchmarks/phrases_medium_ru.json` - средние команды (примерно 4-8 слов);
- `benchmarks/phrases_long_ru.json` - длинные промышленные оповещения (10+ слов);
- `benchmarks/stt_cases_template.json` - шаблон кейсов для STT/WER на реальных шумах;
- `docs/stt_dataset_protocol.md` - протокол сбора STT-датасета для шумовых условий;
- `app/cli/benchmark.py` - CLI для воспроизводимых замеров;
- `reports/stage2_measurements.json` - файл с результатами измерений.

Кратко о доработках в последнем обновлении:

- Добавлена отдельная инструкция сборки RHVoice из исходников для Linux (Debian 11 `aarch64`, Raspberry Pi 4): `docs/rhvoice_build_debian11_arm64.md`.
- Добавлен скрипт автоматической установки RHVoice под Linux: `scripts/install_rhvoice_linux_aarch64.sh`.
- Добавлен нормальный runtime fallback для Windows: если `RHVoice-test` не найден в `PATH`, TTS работает через Windows SAPI (`RHVOICE_WINDOWS_VOICE`, например `Anna`).
- Добавлены вспомогательные Windows-скрипты для быстрого старта: `scripts/setup_windows_runtime.ps1` и `scripts/run_voice_service_windows.ps1`.
- Улучшен `app/cli/benchmark.py`: TTS-бенчмарк больше не пропускается на Windows при отсутствии `RHVoice-test`, если доступен Windows SAPI голос.

## Материалы финального этапа

Ниже собраны ключевые улучшения, выполненные в рамках финального этапа по замечаниям ТЗ (включая доработки, пришедшие из ветки коллеги и адаптированные под текущую архитектуру).

### 1) STT/TTS ядро

- Добавлены backend-и STT: `vosk` и `faster_whisper` (`STT_BACKEND`).
- Добавлены backend-и TTS: `rhvoice`/`windows sapi` и `piper` (`TTS_BACKEND`).
- Добавлен VAD (energy-based) перед распознаванием.
- Добавлено кэширование TTS с TTL (`TTS_CACHE_DIR`, `TTS_CACHE_TTL_SECONDS`).
- Добавлен безопасный fallback и диагностика backend-ов.

### 2) Оркестратор и промышленный сценарий

- Добавлен `COMMAND_TRANSPORT=local|mqtt` (можно работать без брокера на одном узле).
- Реализован сценарий оповещений:
  - `POST /alerts/raise`
  - `POST /alerts/{alert_id}/ack`
  - `GET /alerts/pending`
- Добавлены таймауты и эскалация alert-ов.
- Добавлены idempotency-ключи:
  - `POST /process` (`Idempotency-Key`)
  - `POST /alerts/raise` (`Idempotency-Key`)
- Добавлена персистентность alert state в SQLite (`ORC_DB_PATH`).

### 3) Интеграции с оборудованием

- Добавлен слой `app/integrations/runtime.py`.
- Поддержка `GPIO`:
  - приоритетно через `gpiod`,
  - fallback через sysfs (`/sys/class/gpio`).
- Поддержка `Modbus TCP` в боевом режиме через `pymodbus` (`write_coil`).
- Режимы интеграций:
  - безопасный `INTEGRATION_DRY_RUN=1` (по умолчанию),
  - боевой `INTEGRATION_DRY_RUN=0`,
  - строгий `INTEGRATION_STRICT=1` для fail-fast поведения.
- Пример маппинга топиков: `config/integration_map.example.json`.

### 4) Метрики качества и датасеты

- Расширен benchmark:
  - `WER` для STT кейсов,
  - latency/CPU/memory в отчётах.
- Добавлен шумовой benchmark:
  - `app/cli/stt_noise_benchmark.py`,
  - смешивание clean+noise по SNR,
  - итоговый `wer_percent`.
- Добавлены наборы фраз по длине:
  - `benchmarks/phrases_short_ru.json`
  - `benchmarks/phrases_medium_ru.json`
  - `benchmarks/phrases_long_ru.json` (10+ слов)
- Добавлен шаблон STT кейсов и протокол:
  - `benchmarks/stt_cases_template.json`
  - `docs/stt_dataset_protocol.md`
- Добавлен регулярный регрессионный прогон:
  - `scripts/run_wer_regression.py`
  - история в `reports/wer_history.jsonl`
- Добавлены quality gates:
  - `scripts/check_quality_gates.py`

### 5) Тестирование и CI

- Расширен автотестовый набор (`pytest`), включая edge-cases API.
- Добавлены тесты на:
  - idempotency,
  - alert escalation,
  - integrations runtime,
  - noise pipeline.
- CI workflow: `.github/workflows/ci.yml` (pytest на Python 3.11/3.12).

### 6) Практические итоги прогона

- Подтверждён голосовой E2E (`/process`) на реальной записи.
- Подтверждён alert flow (`raise -> pending -> ack`).
- Подтверждена idempotency-повторяемость результатов.
- Зафиксировано сравнение TTS latency на стенде:
  - `Piper`: `219.6062 ms / 5 запросов` (`~43.9 ms/запрос`)
  - `RHVoice/SAPI`: `4336.6057 ms / 5 запросов` (`~867.3 ms/запрос`)
  - `Piper` быстрее примерно в `19.7x` на данном стенде.

Запуск измерений:

```bash
python -m app.cli.benchmark
```

Если в окружении доступны RHVoice и STT backend, CLI дополнительно выполнит реальные замеры TTS/STT. Для прогона TTS по своему набору фраз:

```bash
python -m app.cli.benchmark --tts-texts benchmarks/tts_messages.txt
```

Раздельные прогоны по длине фраз:

```bash
python -m app.cli.benchmark --phrases benchmarks/phrases_short_ru.json --output reports/bench_short.json
python -m app.cli.benchmark --phrases benchmarks/phrases_medium_ru.json --output reports/bench_medium.json
python -m app.cli.benchmark --phrases benchmarks/phrases_long_ru.json --output reports/bench_long.json
```

### Soak-тест `/process`

Длительный прогон для проверки стабильности/латентности:

```bash
python -m app.cli.soak_test --audio voice_test.wav --minutes 60 --rpm 10 --output reports/soak_report.json
```

### STT Noise Benchmark (WER на шуме)

CLI смешивает clean speech с шумовыми дорожками (по SNR) и считает `WER`:

```bash
python -m app.cli.stt_noise_benchmark \
  --backend vosk \
  --stt-cases path/to/stt_cases.json \
  --noise-dir path/to/noise_wavs \
  --snr 20,10,5 \
  --output reports/stt_noise_benchmark.json
```

Формат `stt_cases.json`:

```json
[
  {"wav_path": "samples/case1.wav", "expected_text": "включи свет"},
  {"wav_path": "samples/case2.wav", "expected_text": "какая температура"}
]
```

Проверка quality gates по итоговому отчёту:

```bash
python scripts/check_quality_gates.py --report reports/stage2_measurements.json
```

Регулярный регрессионный прогон WER (с записью истории):

```bash
python scripts/run_wer_regression.py \
  --stt-cases benchmarks/stt_cases.json \
  --noise-dir benchmarks/noise_cases \
  --backend vosk \
  --history reports/wer_history.jsonl
```

## Реальные интеграции (Modbus/GPIO)

`app/integrations/runtime.py` поддерживает два режима:

- `INTEGRATION_DRY_RUN=1` (по умолчанию): безопасный режим без реальной записи в оборудование.
- `INTEGRATION_DRY_RUN=0`: боевой режим.

Для Modbus в боевом режиме требуется `pymodbus`:

```bash
pip install pymodbus
```

GPIO в боевом режиме:

- приоритетно через `gpiod` (если установлен);
- fallback через Linux sysfs (`/sys/class/gpio`).

Для жесткой проверки успешности интеграции в orchestrator:

```bash
export INTEGRATION_STRICT=1
```

## Практические результаты (локальный прогон)

Дата прогона: `2026-04-19`, Windows, локальный стенд.

- E2E `/process` (голосовая команда): успешно распознана фраза `включи свет`, ответ оркестратора:
  `{"text":"включи свет","command":"turn_on_light","status":"ok"}`.
- Idempotency `/process`: два последовательных запроса с одинаковым `Idempotency-Key` вернули идентичный результат.
- Alert flow: подтверждено `raise -> pending -> ack`.

### Сравнение TTS latency (`/tts/generate`, 5 запросов, уникальный текст)

- `Piper`: `219.6062 ms` суммарно (`~43.9 ms/запрос`).
- `RHVoice/SAPI`: `4336.6057 ms` суммарно (`~867.3 ms/запрос`).
- На этом стенде `Piper` быстрее примерно в `19.7x`.

> Примечание: значения зависят от железа, длины текста, состояния кэша и параметров модели, но относительное преимущество Piper на данном стенде устойчиво.

### Быстрый голосовой тест с Piper

1. Запустить `tts_service` с Piper:

```powershell
$env:VOICE_API_TOKEN="dev-token-change-me"
$env:TTS_BACKEND="piper"
$env:PIPER_BIN="C:\piper\piper.exe"
$env:PIPER_MODEL="C:\piper\models\ru_RU-ruslan-medium.onnx"
python -m uvicorn tts_service:app --host 127.0.0.1 --port 8001
```

2. Записать тестовую команду и отправить в `/process`:

```powershell
@'
import wave, queue, sounddevice as sd
samplerate, channels, blocksize, seconds = 16000, 1, 8000, 3
q = queue.Queue()
def callback(indata, frames, time_info, status):
    q.put(bytes(indata))
print("Скажи: включи свет")
with sd.RawInputStream(samplerate=samplerate, blocksize=blocksize, dtype="int16", channels=channels, callback=callback):
    sd.sleep(seconds * 1000)
frames = []
while not q.empty():
    frames.append(q.get())
with wave.open("voice_test.wav", "wb") as wf:
    wf.setnchannels(channels)
    wf.setsampwidth(2)
    wf.setframerate(samplerate)
    wf.writeframes(b"".join(frames))
'@ | .\.venv\Scripts\python.exe -

curl.exe --noproxy "*" -i -X POST "http://127.0.0.1:8002/process" -H "Authorization: Bearer dev-token-change-me" -F "file=@voice_test.wav;type=audio/wav"
```
