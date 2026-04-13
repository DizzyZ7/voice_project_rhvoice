from __future__ import annotations

import argparse
import os
import time
from pathlib import Path
from typing import Callable

from app.commands.registry import DONE_REPLY, STOP_REPLY, UNKNOWN_COMMAND_REPLY, resolve_command, should_stop
from app.core.speech import RHVoiceTTS, VoskRecognizer, run_diagnostics, setup_logger

BASE_DIR = Path(__file__).resolve().parents[2]
logger = setup_logger("voice_service", "voice_service.log")


def turn_on_light() -> None:
    logger.info("[ACTION] Включение света")


def turn_off_light() -> None:
    logger.info("[ACTION] Выключение света")


def get_temperature(tts: RHVoiceTTS) -> None:
    temperature = 23.5
    logger.info("[ACTION] Температура: %.1f", temperature)
    tts.speak(f"Сейчас температура {temperature} градусов")


def unknown_command(command: str, tts: RHVoiceTTS) -> None:
    logger.warning("Неизвестная команда: %s", command)
    tts.speak(UNKNOWN_COMMAND_REPLY)


def parse_and_execute(command: str, tts: RHVoiceTTS) -> bool:
    if not command:
        logger.info("Пустая команда")
        return True

    if should_stop(command):
        logger.info("Получена команда остановки")
        tts.speak(STOP_REPLY)
        return False

    actions: dict[str, Callable[[], None]] = {
        "turn_on_light": turn_on_light,
        "turn_off_light": turn_off_light,
        "get_temperature": lambda: get_temperature(tts),
    }
    spec = resolve_command(command)
    if spec:
        logger.info("Команда сопоставлена: %s -> %s", command, spec.key)
        actions[spec.key]()
        if spec.key != "get_temperature":
            tts.speak(DONE_REPLY)
        return True

    unknown_command(command, tts)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Офлайн голосовой сервис на Vosk + RHVoice")
    parser.add_argument("--timeout", type=int, default=4, help="Длина окна записи с микрофона в секундах")
    parser.add_argument("--once", action="store_true", help="Считать одну команду и завершиться")
    args = parser.parse_args()

    diagnostics = run_diagnostics(os.environ.get("VOSK_MODEL_PATH", str(BASE_DIR / "vosk-model-ru")))
    logger.info(
        "Диагностика: model=%s rhvoice_available=%s rhvoice_backend=%s rhvoice_target=%s sounddevice=%s",
        diagnostics.vosk_model_exists,
        diagnostics.rhvoice_available,
        diagnostics.rhvoice_backend,
        diagnostics.rhvoice_target,
        diagnostics.sounddevice_available,
    )

    recognizer = VoskRecognizer()
    tts = RHVoiceTTS(logger=logger)
    logger.info("Сервис запущен")

    while True:
        result = recognizer.transcribe_from_microphone(timeout=args.timeout)
        if not result.success:
            logger.error("Ошибка STT: %s", result.error)
            tts.speak("Ошибка распознавания речи")
            if args.once:
                break
            time.sleep(0.5)
            continue

        logger.info("Распознано: %s", result.text)
        should_continue = parse_and_execute(result.text, tts)
        if not should_continue or args.once:
            break
        time.sleep(0.3)

    logger.info("Сервис остановлен")
