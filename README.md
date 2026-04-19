# Голосовой сервис на Vosk / faster-whisper + RHVoice / Piper

Этот репозиторий содержит два уровня реализации офлайн‑системы голосовых команд:

1. **Монолитная версия**: файл `voice_command_service.py` и GUI `voice_command_gui.py` – простой прототип, который слушает микрофон, распознаёт ключевые фразы и озвучивает ответ. Он подходит для быстрого старта на Raspberry Pi или ПК без контейнеризации.
2. **Микросервисная версия**: набор сервисов, оформленных как FastAPI‑приложения с Prometheus‑метриками. Это полноценная архитектура для промышленного внедрения: STT‑сервис (Vosk или faster-whisper), TTS‑сервис (RHVoice или Piper), оркестратор (логика команд + MQTT/local + сценарии оповещения), а также готовые файлы для Prometheus и Grafana.

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

Для промышленного внедрения прототип разделён на независимые сервисы. Каждый сервис запускается в собственном контейнере, имеет метрики для мониторинга и общается с другими сервисами через HTTP и MQTT.

### Компоненты

- **STT‑сервис (`stt_service.py`)**: принимает WAV‑файлы по HTTP (endpoint `/stt/recognize`), распознаёт речь через выбранный backend (`STT_BACKEND=vosk|faster_whisper`) и возвращает текст + confidence. Экспонирует метрики на порту `9101`.
- **TTS‑сервис (`tts_service.py`)**: принимает текст по HTTP (endpoint `/tts/generate`), синтезирует речь через выбранный backend (`TTS_BACKEND=rhvoice|piper`) и возвращает статус либо путь к сохранённому WAV. Поддерживает параметры `speed`, `pitch`, `voice`, `use_cache`. Метрики на `9102`.
- **Оркестратор (`orchestrator_service.py`)**: принимает аудио (`/process`), отправляет его в STT‑сервис, анализирует распознанный текст, публикует команды в MQTT/local и запрашивает TTS‑сервис для голосового ответа. Дополнительно поддерживает FSM оповещений с подтверждением (`/alerts/trigger`, `/alerts/{id}/ack`, `/alerts/{id}/close`). Метрики на `9103`.
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

- Реализуйте обработчики команд, которые отправляют реальные сигналы на оборудование (GPIO, Modbus, HTTP). В `orchestrator_service.py` это делается через MQTT – замените темы и payload на ваши нужды.
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

## Тестирование

Файл `test_stack.py` содержит примеры автоматизированных тестов для проверки логики команд и вызовов RHVoice. Запустите его, чтобы убедиться, что парсинг команд и синтез речи работают корректно:

```bash
python3 test_stack.py
```

Полный pytest-набор:

```bash
python -m pytest -q
```

Проверка quality-gates по отчёту benchmark:

```bash
python -m app.cli.benchmark --iterations 30 --output reports/ci_measurements.json
python scripts/check_quality_gates.py --report reports/ci_measurements.json
```

## Материалы этапа 2

Для сдачи второго этапа в репозитории добавлены отдельные артефакты:

- `docs/stage2_report.md` - краткий отчёт по архитектуре, API, ресурсам и результатам этапа;
- `docs/stage2_acceptance_matrix.md` - трассировка критериев приёмки к файлам проекта;
- `docs/demo_scenarios.md` - пошаговые демонстрационные сценарии;
- `docs/economic_justification.md` - технико-экономическое обоснование;
- `benchmarks/phrases_ru.json` - контрольный набор русских фраз;
- `app/cli/benchmark.py` - CLI для воспроизводимых замеров;
- `reports/stage2_measurements.json` - файл с результатами измерений.

Кратко о доработках в последнем обновлении:

- Добавлена отдельная инструкция сборки RHVoice из исходников для Linux (Debian 11 `aarch64`, Raspberry Pi 4): `docs/rhvoice_build_debian11_arm64.md`.
- Добавлен скрипт автоматической установки RHVoice под Linux: `scripts/install_rhvoice_linux_aarch64.sh`.
- Добавлен нормальный runtime fallback для Windows: если `RHVoice-test` не найден в `PATH`, TTS работает через Windows SAPI (`RHVOICE_WINDOWS_VOICE`, например `Anna`).
- Добавлены вспомогательные Windows-скрипты для быстрого старта: `scripts/setup_windows_runtime.ps1` и `scripts/run_voice_service_windows.ps1`.
- Улучшен `app/cli/benchmark.py`: TTS-бенчмарк больше не пропускается на Windows при отсутствии `RHVoice-test`, если доступен Windows SAPI голос.

Запуск измерений:

```bash
python -m app.cli.benchmark
```

Если в окружении доступны RHVoice и STT backend, CLI дополнительно выполнит реальные замеры TTS/STT. Для прогона TTS по своему набору фраз:

```bash
python -m app.cli.benchmark --tts-texts benchmarks/tts_messages.txt
```

Для расчёта WER/CER укажите STT кейсы:

```bash
python -m app.cli.benchmark --stt-cases /path/to/stt_cases.json
```

Шаблон кейсов и протокол сбора реальных шумовых треков:

- `benchmarks/stt_cases_template.json`
- `docs/stt_dataset_protocol.md`
