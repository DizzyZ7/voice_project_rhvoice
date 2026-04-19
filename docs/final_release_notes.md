# Final Release Notes

Дата: 2026-04-19

## 1. Цель релиза

Закрыть критические замечания по ТЗ для промышленного voice-контура:

- расширить STT/TTS стек;
- покрыть длинные команды/оповещения;
- ввести WER-направленные тесты на шуме;
- усилить оркестратор (эскалации, идемпотентность, отказоустойчивость);
- подготовить путь к реальным интеграциям Modbus/GPIO.

## 2. Ключевые изменения

### STT/TTS

- Добавлены backend-и STT: `vosk`, `faster_whisper`.
- Добавлены backend-и TTS: `rhvoice/windows_sapi`, `piper`.
- Добавлен VAD-гейт.
- Добавлено кэширование TTS (TTL + каталог кэша).

### Оркестратор

- Добавлен транспорт команд `local|mqtt`.
- Реализован alert flow:
  - `POST /alerts/raise`
  - `POST /alerts/{alert_id}/ack`
  - `GET /alerts/pending`
- Добавлены эскалации по таймауту.
- Добавлена идемпотентность для `/process` и `/alerts/raise` через `Idempotency-Key`.
- Добавлена персистентность состояния alert-ов в SQLite.

### Интеграции с оборудованием

- Добавлен слой `app/integrations/runtime.py`.
- GPIO:
  - `gpiod` (приоритет),
  - sysfs fallback.
- Modbus TCP:
  - реальная запись `write_coil` через `pymodbus` в боевом режиме.
- Режимы:
  - `INTEGRATION_DRY_RUN=1` (безопасный по умолчанию),
  - `INTEGRATION_DRY_RUN=0` (боевой),
  - `INTEGRATION_STRICT=1` (fail-fast).

### Датасеты и качество распознавания

- Добавлены расширенные наборы фраз:
  - `benchmarks/phrases_short_ru.json`
  - `benchmarks/phrases_medium_ru.json`
  - `benchmarks/phrases_long_ru.json` (10+ слов)
- Добавлены инструменты шумового тестирования:
  - `app/cli/stt_noise_benchmark.py`
  - `benchmarks/stt_cases_template.json`
  - `docs/stt_dataset_protocol.md`
  - `benchmarks/noise_cases/README.md`
- Добавлен регулярный WER-регресс:
  - `scripts/run_wer_regression.py`
  - `scripts/check_quality_gates.py`

### Тестирование и CI

- Расширен набор `pytest` (edge-cases API, integrations, noise-pipeline, idempotency, escalation).
- CI workflow: `.github/workflows/ci.yml`.

## 3. Практические результаты стенда

- E2E voice `/process`: успешно (`turn_on_light`).
- Alert flow: успешно (`raise -> pending -> ack`).
- Idempotency: подтверждена повторяемость результата при одинаковом ключе.
- TTS latency (5 запросов, уникальные тексты):
  - Piper: `219.6062 ms` (`~43.9 ms/запрос`)
  - RHVoice/SAPI: `4336.6057 ms` (`~867.3 ms/запрос`)
  - Piper быстрее примерно в `19.7x` на стенде.

## 4. Остаточные задачи

- собрать и зафиксировать реальный шумовой датасет с объекта;
- провести длительный soak (например, 24 часа) в целевом окружении;
- провести pilot на реальном Modbus/GPIO сегменте с `INTEGRATION_DRY_RUN=0`.
