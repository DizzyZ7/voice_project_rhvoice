# Матрица приёмки этапа 2

## Обязательные результаты

| Требование из кейса | Чем закрыто в репозитории |
| --- | --- |
| Рабочий TTS-прототип на русском языке | `app/core/speech.py`, `app/services/tts_api.py`, `app/cli/mvp.py` |
| Рабочий STT-прототип на русском языке | `app/core/speech.py`, `app/services/stt_api.py`, `app/cli/mvp.py` |
| Демонстрационное приложение CLI / web / GUI | `app/cli/mvp.py`, `app/ui/voice_command_gui.py`, FastAPI-сервисы в `app/services/` |
| Интеграция компонентов в единую систему | `app/services/orchestrator_api.py`, `docker-compose.yml`, `prometheus.yml` |
| Документация по архитектуре, API, ресурсам и сценариям | `README.md`, `docs/stage2_report.md`, `docs/demo_scenarios.md` |
| Тесты и измерения | `tests/`, `app/cli/benchmark.py`, `reports/stage2_measurements.json` |
| Экономическое и техническое обоснование | `docs/economic_justification.md`, разделы в `docs/stage2_report.md` |

## Желательные результаты

| Требование | Статус |
| --- | --- |
| Второй язык | Не реализован, указан как следующее расширение |
| Базовая устойчивость STT к шуму | Подготовлен контур измерений и тестовый набор; предобработка пока не включена |
| Выбор голосов / параметров голоса | Допускается через RHVoice CLI и переменные окружения, отдельного UI для выбора нет |
| Пилотная интеграция с имитатором ГГС | Закрыто через MQTT-топики `factory/...` и сценарий виртуального склада |
