from __future__ import annotations

from app.commands.registry import (
    COMMAND_CONFIDENCE_THRESHOLD,
    DONE_REPLY,
    STOP_REPLY,
    UNKNOWN_COMMAND_REPLY,
    resolve_command,
    resolve_command_with_score,
    response_text_for_command,
    should_stop,
)


def test_resolve_known_commands():
    assert resolve_command("пожалуйста включи свет").key == "turn_on_light"
    assert resolve_command("нужно выключить свет срочно").key == "turn_off_light"
    assert resolve_command("какая температура в цехе").key == "get_temperature"


def test_unknown_command_returns_none():
    assert resolve_command("открой ворота") is None


def test_stop_detection():
    assert should_stop("стоп")
    assert should_stop("выполни стоп сейчас")
    assert should_stop("пора выход")
    assert not should_stop("продолжай работу")


def test_response_texts():
    assert response_text_for_command("turn_on_light") == DONE_REPLY
    assert response_text_for_command("get_temperature", temperature_value=21.5) == "Сейчас температура 21.5 градусов"
    assert UNKNOWN_COMMAND_REPLY == "Команда не распознана. Повторите, пожалуйста"
    assert STOP_REPLY == "Останавливаю сервис. До свидания"


def test_command_confidence_scoring():
    spec, score = resolve_command_with_score("включи свет пожалуйста")
    assert spec is not None
    assert score >= COMMAND_CONFIDENCE_THRESHOLD
