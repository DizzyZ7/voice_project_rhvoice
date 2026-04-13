from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import base64
import subprocess
import tempfile
import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    import sounddevice as sd
except ImportError:
    sd = None

try:
    import vosk
except ImportError:
    vosk = None


BASE_DIR = Path(__file__).resolve().parents[2]
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_VOSK_MODEL_PATH = os.environ.get("VOSK_MODEL_PATH", str(BASE_DIR / "vosk-model-ru"))
DEFAULT_RHVOICE_BIN = os.environ.get("RHVOICE_BIN")
DEFAULT_WINDOWS_VOICE = os.environ.get("RHVOICE_WINDOWS_VOICE")


@dataclass
class STTResult:
    text: str
    success: bool
    error: Optional[str] = None


@dataclass
class Diagnostics:
    vosk_model_exists: bool
    rhvoice_binary: Optional[str]
    rhvoice_backend: Optional[str]
    rhvoice_target: Optional[str]
    rhvoice_available: bool
    sounddevice_available: bool


def setup_logger(name: str, log_file: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    file_handler = logging.FileHandler(LOG_DIR / log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger


class RHVoiceTTS:
    """Thin adapter around the RHVoice CLI tools."""

    def __init__(self, binary: Optional[str] = None, logger: Optional[logging.Logger] = None):
        self.logger = logger or setup_logger("tts", "tts.log")
        self.binary = binary or self._discover_binary()
        self.backend = "rhvoice_cli"
        self.windows_voice: Optional[str] = None

        if self.binary:
            self.logger.info("Используется RHVoice бинарник: %s", self.binary)
            return

        if os.name == "nt":
            self.windows_voice = self._discover_windows_voice()
            if self.windows_voice:
                self.backend = "windows_sapi"
                self.logger.info("RHVoice CLI не найден, используется Windows SAPI голос: %s", self.windows_voice)
                return

        raise FileNotFoundError(
            "Не найден RHVoice CLI (RHVoice-test/rhvoice.test) и не удалось инициализировать Windows SAPI голос. "
            "Установите RHVoice и укажите RHVOICE_BIN или RHVOICE_WINDOWS_VOICE."
        )

    @staticmethod
    def _discover_binary() -> Optional[str]:
        candidates = [
            DEFAULT_RHVOICE_BIN,
            "RHVoice-test",
            "rhvoice.test",
            "RHVoice-client",
            "rhvoice-client",
        ]
        for candidate in candidates:
            if candidate and shutil.which(candidate):
                return candidate
        return None

    @staticmethod
    def _run_powershell(script: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            text=True,
            capture_output=True,
            check=True,
        )

    @classmethod
    def _list_windows_voices(cls) -> list[str]:
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$s.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name }; "
            "$s.Dispose()"
        )
        result = cls._run_powershell(script)
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    @classmethod
    def _discover_windows_voice(cls) -> Optional[str]:
        try:
            voices = cls._list_windows_voices()
        except Exception:
            return None

        if DEFAULT_WINDOWS_VOICE and DEFAULT_WINDOWS_VOICE in voices:
            return DEFAULT_WINDOWS_VOICE

        for voice in voices:
            if not voice.lower().startswith("microsoft"):
                return voice
        return voices[0] if voices else None

    def _sapi_speak(self, text: str, output_path: Optional[Path] = None) -> None:
        if not self.windows_voice:
            raise RuntimeError("Windows SAPI голос не инициализирован")

        text_b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        voice_b64 = base64.b64encode(self.windows_voice.encode("utf-8")).decode("ascii")
        output_b64 = base64.b64encode(str(output_path).encode("utf-8")).decode("ascii") if output_path else ""

        script = (
            "$ErrorActionPreference='Stop';"
            "Add-Type -AssemblyName System.Speech;"
            f"$txt=[System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{text_b64}'));"
            f"$voice=[System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{voice_b64}'));"
            "$out='';"
            + (
                f"$out=[System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{output_b64}'));"
                if output_b64
                else ""
            )
            + "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
            "if($voice){$s.SelectVoice($voice)};"
            "if($out){$s.SetOutputToWaveFile($out)}else{$s.SetOutputToDefaultAudioDevice()};"
            "$s.Speak($txt);"
            "$s.Dispose();"
        )
        self._run_powershell(script)

    def speak(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self.logger.info("TTS запрос: %s", text)

        if self.backend == "windows_sapi":
            self._sapi_speak(text)
            return

        if Path(self.binary).name in {"RHVoice-test", "rhvoice.test"}:
            result = subprocess.run([self.binary], input=text, text=True, capture_output=True, check=True)
            if result.stderr:
                self.logger.info("RHVoice stderr: %s", result.stderr.strip())
            return

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav_path = tmp.name
        try:
            with open(wav_path, "wb") as fh:
                subprocess.run([self.binary], input=text.encode("utf-8"), stdout=fh, stderr=subprocess.PIPE, check=True)
            aplay = shutil.which("aplay")
            if aplay:
                subprocess.run([aplay, wav_path], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                self.logger.warning("aplay не найден, WAV сохранен во временный файл: %s", wav_path)
        finally:
            if os.path.exists(wav_path):
                os.unlink(wav_path)

    def synthesize_to_wav(self, text: str, output_path: str | Path) -> Path:
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        text = text.strip()
        if not text:
            raise ValueError("Пустой текст для синтеза")

        if self.backend == "windows_sapi":
            self._sapi_speak(text, output_path=output)
            self.logger.info("Windows SAPI сохранил WAV: %s", output)
            return output

        if Path(self.binary).name in {"RHVoice-test", "rhvoice.test"}:
            subprocess.run([self.binary, "-o", str(output)], input=text, text=True, check=True, capture_output=True)
        else:
            with open(output, "wb") as fh:
                subprocess.run([self.binary], input=text.encode("utf-8"), stdout=fh, stderr=subprocess.PIPE, check=True)
        self.logger.info("RHVoice сохранил WAV: %s", output)
        return output


class VoskRecognizer:
    def __init__(self, model_path: str = DEFAULT_VOSK_MODEL_PATH, logger: Optional[logging.Logger] = None):
        if vosk is None:
            raise RuntimeError("Не установлен vosk. Установите: pip install vosk")
        self.model_path = model_path
        self.logger = logger or setup_logger("stt", "stt.log")
        if not os.path.isdir(model_path):
            raise FileNotFoundError(
                f"Модель Vosk не найдена: {model_path}. Скачайте русскую модель и задайте VOSK_MODEL_PATH."
            )
        self.logger.info("Загрузка модели Vosk: %s", model_path)
        self.model = vosk.Model(model_path)

    def transcribe_from_wav(self, wav_path: str) -> STTResult:
        try:
            wf = wave.open(wav_path, "rb")
        except FileNotFoundError:
            return STTResult(text="", success=False, error=f"Файл не найден: {wav_path}")
        except wave.Error as exc:
            return STTResult(text="", success=False, error=str(exc))

        if wf.getnchannels() != 1 or wf.getframerate() != 16000:
            wf.close()
            return STTResult(text="", success=False, error="WAV должен быть mono 16kHz")

        recognizer = vosk.KaldiRecognizer(self.model, wf.getframerate())
        chunks: list[str] = []
        while True:
            data = wf.readframes(4000)
            if not data:
                break
            if recognizer.AcceptWaveform(data):
                chunks.append(recognizer.Result())
        chunks.append(recognizer.FinalResult())
        wf.close()
        return STTResult(text=self._extract_text(chunks), success=True)

    def transcribe_from_microphone(self, timeout: int = 5) -> STTResult:
        if sd is None:
            return STTResult(text="", success=False, error="sounddevice не установлен")

        audio_queue: queue.Queue[bytes] = queue.Queue()
        stop_event = threading.Event()
        recognizer = vosk.KaldiRecognizer(self.model, 16000)
        chunks: list[str] = []

        def callback(indata, frames, time_info, status):
            if status:
                self.logger.warning("Audio status: %s", status)
            audio_queue.put(bytes(indata))
            if stop_event.is_set():
                raise sd.CallbackStop()

        try:
            with sd.RawInputStream(samplerate=16000, blocksize=8000, dtype="int16", channels=1, callback=callback):
                self.logger.info("Начало записи с микрофона на %s сек", timeout)
                sd.sleep(int(timeout * 1000))
                stop_event.set()
                while not audio_queue.empty():
                    data = audio_queue.get()
                    if recognizer.AcceptWaveform(data):
                        chunks.append(recognizer.Result())
                chunks.append(recognizer.FinalResult())
        except Exception as exc:
            self.logger.exception("Ошибка захвата аудио")
            return STTResult(text="", success=False, error=str(exc))

        text = self._extract_text(chunks)
        self.logger.info("Распознано: %s", text)
        return STTResult(text=text, success=True)

    @staticmethod
    def _extract_text(json_chunks: list[str]) -> str:
        parts: list[str] = []
        for chunk in json_chunks:
            try:
                parsed = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            if parsed.get("text"):
                parts.append(parsed["text"])
        return " ".join(parts).strip().lower()


def run_diagnostics(model_path: str = DEFAULT_VOSK_MODEL_PATH) -> Diagnostics:
    rhvoice_binary = RHVoiceTTS._discover_binary()
    rhvoice_backend: Optional[str] = None
    rhvoice_target: Optional[str] = None
    rhvoice_available = False

    if rhvoice_binary:
        rhvoice_backend = "rhvoice_cli"
        rhvoice_target = rhvoice_binary
        rhvoice_available = True
    elif os.name == "nt":
        windows_voice = RHVoiceTTS._discover_windows_voice()
        if windows_voice:
            rhvoice_backend = "windows_sapi"
            rhvoice_target = windows_voice
            rhvoice_available = True

    return Diagnostics(
        vosk_model_exists=os.path.isdir(model_path),
        rhvoice_binary=rhvoice_binary,
        rhvoice_backend=rhvoice_backend,
        rhvoice_target=rhvoice_target,
        rhvoice_available=rhvoice_available,
        sounddevice_available=sd is not None and vosk is not None,
    )
