from __future__ import annotations

import os
import re
from difflib import SequenceMatcher
from dataclasses import dataclass
from typing import Final


UNKNOWN_COMMAND_REPLY: Final[str] = "Команда не распознана. Повторите, пожалуйста"
STOP_REPLY: Final[str] = "Останавливаю сервис. До свидания"
DONE_REPLY: Final[str] = "Готово"
STOP_COMMANDS: Final[tuple[str, ...]] = ("стоп", "выход", "завершить", "остановка")
COMMAND_CONFIDENCE_THRESHOLD: Final[float] = float(os.environ.get("COMMAND_CONFIDENCE_THRESHOLD", "0.74"))


@dataclass(frozen=True)
class CommandSpec:
    key: str
    phrases: tuple[str, ...]
    mqtt_topic: str


COMMAND_SPECS: Final[tuple[CommandSpec, ...]] = (
    CommandSpec(
        key="turn_on_light",
        phrases=("включи свет", "включить свет", "зажги свет"),
        mqtt_topic="factory/light/on",
    ),
    CommandSpec(
        key="turn_off_light",
        phrases=("выключи свет", "выключить свет", "потуши свет"),
        mqtt_topic="factory/light/off",
    ),
    CommandSpec(
        key="get_temperature",
        phrases=("какая температура", "температура", "сколько градусов"),
        mqtt_topic="factory/temperature/request",
    ),
)


def should_stop(text: str) -> bool:
    normalized = text.strip().lower()
    return any(_contains_phrase(normalized, stop_word) for stop_word in STOP_COMMANDS)


def resolve_command(text: str) -> CommandSpec | None:
    spec, score = resolve_command_with_score(text)
    if spec and score >= COMMAND_CONFIDENCE_THRESHOLD:
        return spec
    return None


def resolve_command_with_score(text: str) -> tuple[CommandSpec | None, float]:
    normalized = text.strip().lower()
    if not normalized:
        return None, 0.0

    best_spec: CommandSpec | None = None
    best_score = 0.0
    for spec in COMMAND_SPECS:
        for phrase in spec.phrases:
            if _contains_phrase(normalized, phrase):
                return spec, 1.0
            similarity = SequenceMatcher(None, normalized, phrase).ratio()
            if similarity > best_score:
                best_spec = spec
                best_score = similarity
    return best_spec, best_score


def response_text_for_command(command_key: str, temperature_value: float | int = 23) -> str:
    if command_key == "get_temperature":
        return f"Сейчас температура {temperature_value} градусов"
    return DONE_REPLY


def _contains_phrase(text: str, phrase: str) -> bool:
    pattern = r"(?<!\w)" + re.escape(phrase) + r"(?!\w)"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None
