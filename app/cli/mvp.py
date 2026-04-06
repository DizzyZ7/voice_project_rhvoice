from __future__ import annotations

import argparse

from app.core.speech import RHVoiceTTS, VoskRecognizer, setup_logger

logger = setup_logger("mvp", "mvp.log")


def main():
    parser = argparse.ArgumentParser(description="MVP для связки Vosk + RHVoice")
    parser.add_argument("--mic", action="store_true", help="Использовать микрофон")
    parser.add_argument("--wav", type=str, help="Путь к mono 16kHz WAV для STT")
    parser.add_argument("--text", type=str, help="Текст для синтеза через RHVoice")
    parser.add_argument("--save-tts", type=str, help="Если задано, RHVoice сохранит WAV в указанный путь")
    args = parser.parse_args()

    tts = RHVoiceTTS(logger=logger)

    if args.text:
        if args.save_tts:
            output = tts.synthesize_to_wav(args.text, args.save_tts)
            print(f"WAV сохранён: {output}")
        else:
            tts.speak(args.text)

    if args.mic or args.wav:
        recognizer = VoskRecognizer(logger=logger)
        if args.wav:
            result = recognizer.transcribe_from_wav(args.wav)
        else:
            result = recognizer.transcribe_from_microphone(timeout=5)
        if not result.success:
            print("Ошибка STT:", result.error)
            return
        print("Распознано:", result.text)
        if result.text:
            if args.save_tts:
                output = tts.synthesize_to_wav(result.text, args.save_tts)
                print(f"Ответ сохранён: {output}")
            else:
                tts.speak(result.text)
