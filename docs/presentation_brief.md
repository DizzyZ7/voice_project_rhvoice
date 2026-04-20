# Выжимка для презентации

Актуально на: 2026-04-20.

## 1) Примеры тест-кейсов

1. `TC-API-Auth-001`  
Проверка авторизации (Bearer и `X-API-Key`), негативный кейс с неверным токеном.  
Ожидание: валидный токен пропускается, невалидный дает `401`.

2. `TC-TTS-Validation-002`  
`/tts/generate` с пустым и слишком длинным текстом.  
Ожидание: `400` и `413`.

3. `TC-ORC-Idempotency-003`  
Два вызова `/process` с одним `Idempotency-Key`.  
Ожидание: одинаковый ответ, без повторной полной обработки.

4. `TC-Alert-Flow-004`  
`raise -> pending -> ack` для alert API.  
Ожидание: тревога создается, видна в pending и корректно подтверждается.

5. `TC-Command-Router-005`  
Контрольные наборы short/medium/long фраз.  
Ожидание: корректный роутинг команд, `mismatches = []`.

6. `TC-TTS-AB-006`  
Сравнение RHVoice/SAPI vs Piper в одинаковых условиях (один и тот же набор из 12 фраз, холодный кэш).  
Ожидание: получить сопоставимые `mean/p95` и явного winner по latency.

## 2) Ручное и автоматическое тестирование

- Автотесты (`pytest`): `44 passed`, `0 failed`.
- Ручной A/B прогон TTS (2026-04-20 23:26): выполнен успешно, отчеты и WAV сгенерированы.
- Прослушивание WAV: оба набора (RHVoice и Piper) проиграны последовательно.

### Короткие/длинные фразы (router accuracy)

- Short: `6/6`, accuracy `100%`, mean `5.469 ms`.
- Medium: `6/6`, accuracy `100%`, mean `14.815 ms`.
- Long: `6/6`, accuracy `100%`, mean `54.547 ms`.

Интерпретация: на коротких фразах система работает быстрее, на длинных latency выше, но точность в текущем наборе сохраняется (`100%`).

## 3) Общие метрики для слайдов

- `pytest pass rate`: `100%` (`44/44`).
- Router accuracy по контрольным наборам: `100%` (short/medium/long).
- Latency рост по длине фразы: `5.469 -> 14.815 -> 54.547 ms`.
- TTS A/B (cold cache, 12 одинаковых сообщений):
  - RHVoice/SAPI: mean `883.161 ms`, p95 `968.548 ms`
  - Piper: mean `1023.232 ms`, p95 `1180.655 ms`
  - Ratio RH/Piper: `0.863` (в этом прогоне быстрее RHVoice/SAPI)

Источники:
- `reports/bench_short.json`
- `reports/bench_medium.json`
- `reports/bench_long.json`
- `reports/bench_tts_ab_compare_20260420_232618.json`

## 4) Выжимка по изменениям (git)

Последние изменения после базового релиза `05402aa`:
- `app/core/speech.py`
- `app/services/orchestrator_api.py`
- `app/cli/benchmark.py`
- `tests/test_stage3_features.py`
- `app/core/__init__.py`

Что НЕ менялось в этих последних фиксах:
- контракты FastAPI endpoint-ов STT/TTS/Orchestrator;
- интеграционный слой `app/integrations/*`;
- наборы `benchmarks/phrases_*.json`;
- CI workflow (`.github/workflows/ci.yml`).

## 5) Файл с командами для демонстрации

Пошаговые команды для завтрашней встречи:
- `docs/presentation_runbook.md`
