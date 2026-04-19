from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    import sounddevice as sd
except (ImportError, OSError):
    sd = None

try:
    import vosk
except ImportError:
    vosk = None

try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None

try:
    import winsound
except ImportError:
    winsound = None


BASE_DIR = Path(__file__).resolve().parents[2]
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_RHVOICE_BIN = os.environ.get("RHVOICE_BIN")
DEFAULT_WINDOWS_VOICE = os.environ.get("RHVOICE_WINDOWS_VOICE")
DEFAULT_PIPER_BIN = os.environ.get("PIPER_BIN")
DEFAULT_PIPER_MODEL = os.environ.get("PIPER_MODEL")
DEFAULT_STT_BACKEND = os.environ.get("STT_BACKEND", "vosk").strip().lower()
DEFAULT_TTS_BACKEND = os.environ.get("TTS_BACKEND", "auto").strip().lower()
DEFAULT_TTS_CACHE_DIR = Path(os.environ.get("TTS_CACHE_DIR", str(BASE_DIR / "tmp-tts-cache"))).resolve()
DEFAULT_TTS_CACHE_TTL_SECONDS = int(os.environ.get("TTS_CACHE_TTL_SECONDS", "86400"))
STT_ENABLE_VAD = os.environ.get("STT_ENABLE_VAD", "1").strip().lower() in {"1", "true", "yes", "on"}
STT_VAD_RMS_THRESHOLD = int(os.environ.get("STT_VAD_RMS_THRESHOLD", "220"))
STT_VAD_MIN_SPEECH_RATIO = float(os.environ.get("STT_VAD_MIN_SPEECH_RATIO", "0.02"))
DEFAULT_FASTER_WHISPER_MODEL = os.environ.get("FASTER_WHISPER_MODEL", "small")
DEFAULT_FASTER_WHISPER_DEVICE = os.environ.get("FASTER_WHISPER_DEVICE", "cpu")
DEFAULT_FASTER_WHISPER_COMPUTE_TYPE = os.environ.get("FASTER_WHISPER_COMPUTE_TYPE", "int8")


def choose_vosk_model_path(base_dir: Path, env_model_path: str | None) -> str:
    candidates: list[Path] = []
    if env_model_path:
        candidates.append(Path(env_model_path))
    candidates.append(base_dir / "models" / "vosk-model-small-ru-0.22")
    candidates.append(base_dir / "vosk-model-ru")
    for candidate in candidates:
        if candidate.is_dir():
            return str(candidate)
    return str(candidates[0])


DEFAULT_VOSK_MODEL_PATH = choose_vosk_model_path(BASE_DIR, os.environ.get("VOSK_MODEL_PATH"))


@dataclass
class STTResult:
    text: str
    success: bool
    error: Optional[str] = None
    confidence: Optional[float] = None


@dataclass
class Diagnostics:
    vosk_model_exists: bool
    rhvoice_binary: Optional[str]
    rhvoice_backend: Optional[str]
    rhvoice_target: Optional[str]
    rhvoice_available: bool
    piper_binary: Optional[str]
    piper_model: Optional[str]
    piper_available: bool
    faster_whisper_available: bool
    sounddevice_available: bool
    stt_backend: str = "vosk"
    stt_backend_available: bool = False
    faster_whisper_available: bool = False
    tts_backend: str = "rhvoice"
    tts_backend_available: bool = False
    piper_binary: Optional[str] = None
    piper_model_path: Optional[str] = None


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


@dataclass(frozen=True)
class SimpleEnergyVAD:
    rms_threshold: int = STT_VAD_RMS_THRESHOLD
    min_speech_ratio: float = STT_VAD_MIN_SPEECH_RATIO
    frame_ms: int = 30

    def has_speech(self, pcm_bytes: bytes, sample_rate: int) -> bool:
        if not pcm_bytes or sample_rate <= 0:
            return False
        frame_bytes = int(sample_rate * self.frame_ms / 1000) * 2
        if frame_bytes <= 0:
            return False
        frames_total = 0
        speech_frames = 0
        for offset in range(0, len(pcm_bytes), frame_bytes):
            frame = pcm_bytes[offset : offset + frame_bytes]
            if len(frame) < 2:
                continue
            frames_total += 1
            if _rms_int16(frame) >= self.rms_threshold:
                speech_frames += 1
        if frames_total == 0:
            return False
        return (speech_frames / frames_total) >= self.min_speech_ratio


def _rms_int16(pcm_bytes: bytes) -> float:
    sample_count = len(pcm_bytes) // 2
    if sample_count == 0:
        return 0.0
    energy = 0.0
    for offset in range(0, sample_count * 2, 2):
        value = int.from_bytes(pcm_bytes[offset : offset + 2], byteorder="little", signed=True)
        energy += float(value * value)
    return (energy / sample_count) ** 0.5


def _read_wav_pcm16_mono_16k(wav_path: str) -> tuple[int, bytes] | tuple[None, None]:
    try:
        with wave.open(wav_path, "rb") as wf:
            if wf.getnchannels() != 1 or wf.getframerate() != 16000 or wf.getsampwidth() != 2:
                return None, None
            return wf.getframerate(), wf.readframes(wf.getnframes())
    except Exception:
        return None, None


def _play_wav_file(path: Path, logger: logging.Logger) -> None:
    if os.name == "nt" and winsound is not None:
        winsound.PlaySound(str(path), winsound.SND_FILENAME)
        return
    aplay = shutil.which("aplay")
    if aplay:
        subprocess.run([aplay, str(path)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    logger.warning("Аудиоплеер не найден, WAV сохранен: %s", path)


class RHVoiceTTS:
    """Thin adapter around the RHVoice CLI tools."""

    def __init__(self, binary: Optional[str] = None, logger: Optional[logging.Logger] = None):
        self.logger = logger or setup_logger("tts", "tts.log")
        self.binary = binary or self._discover_binary()
        self.backend = "rhvoice_cli"
        self.windows_voice: Optional[str] = None
        self.cache_enabled = DEFAULT_TTS_CACHE_ENABLED
        self.cache_dir = DEFAULT_TTS_CACHE_DIR
        self._prosody_warning_emitted = False
        self._pitch_warning_emitted = False

        if self.cache_enabled:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

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

    @property
    def engine_id(self) -> str:
        if self.backend == "windows_sapi":
            return f"rhvoice:{self.backend}:{self.windows_voice or 'default'}"
        return f"rhvoice:{self.backend}:{self.binary or 'unknown'}"

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

    @staticmethod
    def _normalize_speed(speed: float) -> float:
        return min(2.0, max(0.5, float(speed)))

    @staticmethod
    def _normalize_pitch(pitch: float) -> float:
        return min(12.0, max(-12.0, float(pitch)))

    @staticmethod
    def _to_sapi_rate(speed: float) -> int:
        return max(-10, min(10, int(round((speed - 1.0) * 10))))

    def _cache_key(self, text: str, speed: float, pitch: float, voice: Optional[str]) -> str:
        payload = {
            "backend": self.backend,
            "binary": self.binary or "",
            "voice": voice or self.windows_voice or "",
            "speed": round(speed, 3),
            "pitch": round(pitch, 3),
            "text": text,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _cached_wav_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.wav"

    def _sapi_speak(
        self,
        text: str,
        output_path: Optional[Path] = None,
        speed: float = 1.0,
        pitch: float = 0.0,
        voice: Optional[str] = None,
    ) -> None:
        if not self.windows_voice:
            raise RuntimeError("Windows SAPI голос не инициализирован")

        selected_voice = voice or self.windows_voice
        speed = self._normalize_speed(speed)
        pitch = self._normalize_pitch(pitch)
        if pitch != 0.0 and not self._pitch_warning_emitted:
            self.logger.warning("Windows SAPI backend не поддерживает pitch напрямую; параметр будет проигнорирован")
            self._pitch_warning_emitted = True

        text_b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        voice_b64 = base64.b64encode(selected_voice.encode("utf-8")).decode("ascii")
        output_b64 = base64.b64encode(str(output_path).encode("utf-8")).decode("ascii") if output_path else ""

        script = (
            "$ErrorActionPreference='Stop';"
            "Add-Type -AssemblyName System.Speech;"
            f"$txt=[System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{text_b64}'));"
            f"$voice=[System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{voice_b64}'));"
            f"$rate={self._to_sapi_rate(speed)};"
            "$out='';"
            + (
                f"$out=[System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{output_b64}'));"
                if output_b64
                else ""
            )
            + "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
            "$s.Rate=$rate;"
            "if($voice){$s.SelectVoice($voice)};"
            "if($out){$s.SetOutputToWaveFile($out)}else{$s.SetOutputToDefaultAudioDevice()};"
            "$s.Speak($txt);"
            "$s.Dispose();"
        )
        self._run_powershell(script)

    def _synthesize_cli_to_wav(self, text: str, output: Path, speed: float, pitch: float, voice: Optional[str]) -> None:
        if (speed != 1.0 or pitch != 0.0 or voice) and not self._prosody_warning_emitted:
            self.logger.warning(
                "Текущий RHVoice CLI backend не поддерживает speed/pitch/voice через API; "
                "параметры учитываются в кэше, но синтез остаётся базовым"
            )
            self._prosody_warning_emitted = True

        binary_name = Path(self.binary or "").name
        if binary_name in {"RHVoice-test", "rhvoice.test"}:
            subprocess.run([self.binary, "-o", str(output)], input=text, text=True, check=True, capture_output=True)
            return

        with open(output, "wb") as fh:
            subprocess.run([self.binary], input=text.encode("utf-8"), stdout=fh, stderr=subprocess.PIPE, check=True)

    def speak(
        self,
        text: str,
        speed: float = 1.0,
        pitch: float = 0.0,
        voice: Optional[str] = None,
        use_cache: bool = True,
    ) -> None:
        text = text.strip()
        if not text:
            return
        speed = self._normalize_speed(speed)
        pitch = self._normalize_pitch(pitch)
        self.logger.info("TTS запрос: %s", text)

        if self.backend == "windows_sapi":
            self._sapi_speak(text, speed=speed, pitch=pitch, voice=voice)
            return

        aplay = shutil.which("aplay")
        if not aplay and Path(self.binary or "").name in {"RHVoice-test", "rhvoice.test"} and speed == 1.0 and pitch == 0.0 and not voice:
            result = subprocess.run([self.binary], input=text, text=True, capture_output=True, check=True)
            if result.stderr:
                self.logger.info("RHVoice stderr: %s", result.stderr.strip())
            return

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav_path = Path(tmp.name)
        try:
            self.synthesize_to_wav(text, wav_path, speed=speed, pitch=pitch, voice=voice, use_cache=use_cache)
            if aplay:
                subprocess.run([aplay, str(wav_path)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                self.logger.warning("aplay не найден, WAV сохранен во временный файл: %s", wav_path)
        finally:
            try:
                wav_path.unlink()
            except FileNotFoundError:
                pass

    def synthesize_to_wav(
        self,
        text: str,
        output_path: str | Path,
        speed: float = 1.0,
        pitch: float = 0.0,
        voice: Optional[str] = None,
        use_cache: bool = True,
    ) -> Path:
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        text = text.strip()
        if not text:
            raise ValueError("Пустой текст для синтеза")

        speed = self._normalize_speed(speed)
        pitch = self._normalize_pitch(pitch)
        cache_path: Optional[Path] = None

        if use_cache and self.cache_enabled:
            cache_key = self._cache_key(text=text, speed=speed, pitch=pitch, voice=voice)
            cache_path = self._cached_wav_path(cache_key)
            if cache_path.exists():
                shutil.copy2(cache_path, output)
                self.logger.info("TTS cache hit: %s", cache_key[:12])
                return output

        if self.backend == "windows_sapi":
            self._sapi_speak(text, output_path=output, speed=speed, pitch=pitch, voice=voice)
            self.logger.info("Windows SAPI сохранил WAV: %s", output)
        else:
            self._synthesize_cli_to_wav(text=text, output=output, speed=speed, pitch=pitch, voice=voice)
            self.logger.info("RHVoice сохранил WAV: %s", output)

        if cache_path and output.exists():
            try:
                shutil.copy2(output, cache_path)
            except Exception:
                self.logger.warning("Не удалось обновить TTS cache: %s", cache_path)
        return output


class PiperTTS:
    def __init__(self, binary: Optional[str] = None, model: Optional[str] = None, logger: Optional[logging.Logger] = None):
        self.logger = logger or setup_logger("tts", "tts.log")
        self.binary = binary or self._discover_binary()
        self.model = model or DEFAULT_PIPER_MODEL
        if not self.binary:
            raise FileNotFoundError("Не найден piper CLI. Установите Piper и укажите PIPER_BIN.")
        if not self.model:
            raise FileNotFoundError("Не задан PIPER_MODEL (путь к .onnx модели Piper).")
        if not Path(self.model).exists():
            raise FileNotFoundError(f"Модель Piper не найдена: {self.model}")
        self.logger.info("Используется Piper: %s, модель: %s", self.binary, self.model)

    @property
    def engine_id(self) -> str:
        return f"piper:{self.binary}:{self.model}"

    @staticmethod
    def _discover_binary() -> Optional[str]:
        candidates = [DEFAULT_PIPER_BIN, "piper"]
        for candidate in candidates:
            if candidate and shutil.which(candidate):
                return candidate
        return None

    def speak(self, text: str) -> None:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav_path = Path(tmp.name)
        try:
            self.synthesize_to_wav(text, wav_path)
            _play_wav_file(wav_path, self.logger)
        finally:
            if wav_path.exists():
                wav_path.unlink()

    def synthesize_to_wav(self, text: str, output_path: str | Path) -> Path:
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        text = text.strip()
        if not text:
            raise ValueError("Пустой текст для синтеза")

        command = [
            self.binary,
            "--model",
            self.model,
            "--output_file",
            str(output),
        ]
        subprocess.run(command, input=text.encode("utf-8"), check=True, capture_output=True)
        self.logger.info("Piper сохранил WAV: %s", output)
        return output


class CachedTTSEngine:
    def __init__(
        self,
        engine: RHVoiceTTS | PiperTTS,
        cache_dir: Path = DEFAULT_TTS_CACHE_DIR,
        cache_ttl_seconds: int = DEFAULT_TTS_CACHE_TTL_SECONDS,
        logger: Optional[logging.Logger] = None,
    ):
        self._engine = engine
        self._cache_dir = cache_dir
        self._cache_ttl_seconds = cache_ttl_seconds
        self.logger = logger or setup_logger("tts", "tts.log")
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    @property
    def engine_id(self) -> str:
        return f"{self._engine.engine_id}+cache"

    def _cache_path(self, text: str) -> Path:
        payload = json.dumps({"engine": self._engine.engine_id, "text": text.strip()}, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return self._cache_dir / f"{digest}.wav"

    def _is_fresh(self, path: Path) -> bool:
        if not path.exists():
            return False
        if self._cache_ttl_seconds <= 0:
            return True
        age = time.time() - path.stat().st_mtime
        return age <= self._cache_ttl_seconds

    def _ensure_cached(self, text: str) -> Path:
        key = text.strip()
        if not key:
            raise ValueError("Пустой текст для синтеза")
        cached_path = self._cache_path(key)
        if self._is_fresh(cached_path):
            self.logger.info("TTS cache hit: %s", cached_path.name)
            return cached_path
        self._engine.synthesize_to_wav(key, cached_path)
        self.logger.info("TTS cache miss: %s", cached_path.name)
        return cached_path

    def speak(self, text: str) -> None:
        if not text.strip():
            return
        cached_path = self._ensure_cached(text)
        _play_wav_file(cached_path, self.logger)

    def synthesize_to_wav(self, text: str, output_path: str | Path) -> Path:
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        cached_path = self._ensure_cached(text)
        shutil.copy2(cached_path, output)
        return output


class VoskRecognizer:
    def __init__(self, model_path: str = DEFAULT_VOSK_MODEL_PATH, logger: Optional[logging.Logger] = None, use_vad: bool = STT_ENABLE_VAD):
        if vosk is None:
            raise RuntimeError("Не установлен vosk. Установите: pip install vosk")
        self.model_path = model_path
        self.logger = logger or setup_logger("stt", "stt.log")
        self.vad = SimpleEnergyVAD() if use_vad else None
        if not os.path.isdir(model_path):
            raise FileNotFoundError(
                f"Модель Vosk не найдена: {model_path}. Скачайте русскую модель и задайте VOSK_MODEL_PATH."
            )
        self.logger.info("Загрузка модели Vosk: %s", model_path)
        try:
            self.model = vosk.Model(model_path)
        except Exception as exc:
            hint = ""
            if os.name == "nt" and any(ord(ch) > 127 for ch in model_path):
                hint = (
                    " На Windows это часто связано с не-ASCII путём. "
                    "Переместите модель в путь типа C:\\vosk-model-small-ru-0.22 и укажите VOSK_MODEL_PATH."
                )
            raise RuntimeError(f"Не удалось загрузить модель Vosk: {model_path}.{hint}") from exc

    @property
    def backend_name(self) -> str:
        return "vosk"

    def transcribe_from_wav(self, wav_path: str) -> STTResult:
        if self.vad is not None:
            sample_rate, pcm_data = _read_wav_pcm16_mono_16k(wav_path)
            if sample_rate is not None and pcm_data is not None and not self.vad.has_speech(pcm_data, sample_rate):
                return STTResult(text="", success=False, error="Речь не обнаружена (VAD)")

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
        text, confidence = self._extract_text_and_confidence(chunks)
        return STTResult(text=text, success=True, confidence=confidence)

    def transcribe_from_microphone(self, timeout: int = 5) -> STTResult:
        if sd is None:
            return STTResult(text="", success=False, error="sounddevice не установлен")

        chunks, error = self._capture_microphone_chunks(timeout=timeout, logger=self.logger)
        if error:
            return STTResult(text="", success=False, error=error)
        chunks = self._apply_energy_vad(chunks, self.mic_vad_rms_threshold)
        if not chunks:
            return STTResult(text="", success=False, error="VAD отфильтровал весь сигнал")

        recognizer = vosk.KaldiRecognizer(self.model, 16000)
        results: list[str] = []
        for data in chunks:
            if recognizer.AcceptWaveform(data):
                results.append(recognizer.Result())
        results.append(recognizer.FinalResult())

        text, confidence = self._extract_text_and_confidence(results)
        self.logger.info("Распознано: %s", text)
        return STTResult(text=text, success=True, confidence=confidence)

    @staticmethod
    def _extract_text_and_confidence(json_chunks: list[str]) -> tuple[str, Optional[float]]:
        parts: list[str] = []
        confidence_values: list[float] = []
        for chunk in json_chunks:
            try:
                parsed = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            if parsed.get("text"):
                parts.append(parsed["text"])
            for token in parsed.get("result", []) or []:
                conf = token.get("conf")
                if isinstance(conf, (float, int)):
                    confidence_values.append(float(conf))

        text = _normalize_text(" ".join(parts))
        if not confidence_values:
            return text, None
        return text, round(sum(confidence_values) / len(confidence_values), 4)


class FasterWhisperRecognizer(SpeechRecognizer):
    backend_name = "faster_whisper"

    def __init__(
        self,
        model_ref: str = DEFAULT_FASTER_WHISPER_MODEL_REF,
        logger: Optional[logging.Logger] = None,
        device: str = DEFAULT_FASTER_WHISPER_DEVICE,
        compute_type: str = DEFAULT_FASTER_WHISPER_COMPUTE_TYPE,
        language: str = DEFAULT_FASTER_WHISPER_LANGUAGE,
        beam_size: int = DEFAULT_FASTER_WHISPER_BEAM_SIZE,
        vad_filter: bool = DEFAULT_FASTER_WHISPER_VAD_FILTER.strip().lower() in {"1", "true", "yes", "on"},
    ):
        if WhisperModel is None:
            raise RuntimeError("Не установлен faster-whisper. Установите: pip install faster-whisper")
        self.logger = logger or setup_logger("stt", "stt.log")
        self.model_ref = model_ref
        self.language = language
        self.beam_size = beam_size
        self.vad_filter = vad_filter
        self.mic_vad_rms_threshold = DEFAULT_STT_MIC_VAD_RMS_THRESHOLD
        self.logger.info(
            "Загрузка faster-whisper модели: %s (device=%s, compute_type=%s)",
            model_ref,
            device,
            compute_type,
        )
        self.model = WhisperModel(model_ref, device=device, compute_type=compute_type)

    def transcribe_from_wav(self, wav_path: str) -> STTResult:
        wav_file = Path(wav_path)
        if not wav_file.exists():
            return STTResult(text="", success=False, error=f"Файл не найден: {wav_path}")

        try:
            segments, info = self.model.transcribe(
                str(wav_file),
                language=self.language,
                beam_size=self.beam_size,
                vad_filter=self.vad_filter,
                condition_on_previous_text=False,
                temperature=0.0,
            )
            text = _normalize_text(" ".join(segment.text.strip() for segment in segments))
            confidence = None
            language_probability = getattr(info, "language_probability", None)
            if isinstance(language_probability, (float, int)):
                confidence = round(float(language_probability), 4)
            return STTResult(text=text, success=True, confidence=confidence)
        except Exception as exc:
            self.logger.exception("Ошибка faster-whisper STT")
            return STTResult(text="", success=False, error=str(exc))

    def transcribe_from_microphone(self, timeout: int = 5) -> STTResult:
        chunks, error = self._capture_microphone_chunks(timeout=timeout, logger=self.logger)
        if error:
            return STTResult(text="", success=False, error=error)
        chunks = self._apply_energy_vad(chunks, self.mic_vad_rms_threshold)
        if not chunks:
            return STTResult(text="", success=False, error="VAD отфильтровал весь сигнал")

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            temp_path = Path(tmp.name)
        try:
            with wave.open(str(temp_path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(b"".join(chunks))
            return self.transcribe_from_wav(str(temp_path))
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def create_recognizer(
    backend: Optional[str] = None,
    model_path: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> SpeechRecognizer:
    resolved_backend = _normalize_stt_backend_name(backend or DEFAULT_STT_BACKEND)
    if resolved_backend == "vosk":
        return VoskRecognizer(model_path=model_path or DEFAULT_VOSK_MODEL_PATH, logger=logger)
    if resolved_backend in {"faster_whisper", "whisper"}:
        return FasterWhisperRecognizer(model_ref=model_path or DEFAULT_FASTER_WHISPER_MODEL_REF, logger=logger)
    raise ValueError(f"Неподдерживаемый STT backend: {backend}")


def create_tts_engine(
    backend: Optional[str] = None,
    model_path: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> SpeechSynthesizer:
    resolved_backend = _normalize_tts_backend_name(backend or DEFAULT_TTS_BACKEND)
    if resolved_backend in {"rhvoice", "rhvoice_cli"}:
        return RHVoiceTTS(logger=logger)
    if resolved_backend == "piper":
        return PiperTTS(model_path=model_path or DEFAULT_PIPER_MODEL_PATH, logger=logger)
    raise ValueError(f"Неподдерживаемый TTS backend: {backend}")


class FasterWhisperRecognizer:
    def __init__(
        self,
        model_name: str = DEFAULT_FASTER_WHISPER_MODEL,
        device: str = DEFAULT_FASTER_WHISPER_DEVICE,
        compute_type: str = DEFAULT_FASTER_WHISPER_COMPUTE_TYPE,
        logger: Optional[logging.Logger] = None,
        use_vad: bool = STT_ENABLE_VAD,
    ):
        if WhisperModel is None:
            raise RuntimeError("Не установлен faster-whisper. Установите: pip install faster-whisper")
        self.logger = logger or setup_logger("stt", "stt.log")
        self.model_name = model_name
        self.vad = SimpleEnergyVAD() if use_vad else None
        self.logger.info(
            "Загрузка модели faster-whisper: %s (device=%s, compute_type=%s)",
            model_name,
            device,
            compute_type,
        )
        self.model = WhisperModel(model_name, device=device, compute_type=compute_type)

    @property
    def backend_name(self) -> str:
        return "faster_whisper"

    def transcribe_from_wav(self, wav_path: str) -> STTResult:
        if self.vad is not None:
            sample_rate, pcm_data = _read_wav_pcm16_mono_16k(wav_path)
            if sample_rate is not None and pcm_data is not None and not self.vad.has_speech(pcm_data, sample_rate):
                return STTResult(text="", success=False, error="Речь не обнаружена (VAD)")
        try:
            segments, _ = self.model.transcribe(
                wav_path,
                language="ru",
                beam_size=1,
                condition_on_previous_text=False,
                vad_filter=False,
            )
            text = " ".join(segment.text.strip() for segment in segments if segment.text.strip()).strip().lower()
            return STTResult(text=text, success=True)
        except Exception as exc:
            return STTResult(text="", success=False, error=str(exc))


def build_stt_recognizer(backend: str = DEFAULT_STT_BACKEND):
    backend_name = (backend or "vosk").strip().lower()
    if backend_name in {"faster_whisper", "whisper"}:
        return FasterWhisperRecognizer()
    if backend_name == "vosk":
        return VoskRecognizer()
    raise ValueError(f"Неизвестный STT_BACKEND: {backend_name}")


def build_tts_engine(backend: str = DEFAULT_TTS_BACKEND) -> CachedTTSEngine:
    backend_name = (backend or "auto").strip().lower()
    if backend_name == "rhvoice":
        return CachedTTSEngine(RHVoiceTTS())
    if backend_name == "piper":
        return CachedTTSEngine(PiperTTS())
    if backend_name == "auto":
        try:
            return CachedTTSEngine(PiperTTS())
        except Exception:
            return CachedTTSEngine(RHVoiceTTS())
    raise ValueError(f"Неизвестный TTS_BACKEND: {backend_name}")


def run_diagnostics(model_path: str = DEFAULT_VOSK_MODEL_PATH) -> Diagnostics:
    rhvoice_binary = RHVoiceTTS._discover_binary()
    rhvoice_backend: Optional[str] = None
    rhvoice_target: Optional[str] = None
    rhvoice_available = False
    piper_binary = PiperTTS._discover_binary()
    piper_models = PiperTTS._load_voice_models(DEFAULT_PIPER_VOICE_MODELS)
    piper_model_path = DEFAULT_PIPER_MODEL_PATH

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

    piper_binary = PiperTTS._discover_binary()
    piper_model = DEFAULT_PIPER_MODEL if DEFAULT_PIPER_MODEL and Path(DEFAULT_PIPER_MODEL).exists() else None

    return Diagnostics(
        vosk_model_exists=os.path.isdir(model_path),
        rhvoice_binary=rhvoice_binary,
        rhvoice_backend=rhvoice_backend,
        rhvoice_target=rhvoice_target,
        rhvoice_available=rhvoice_available,
        piper_binary=piper_binary,
        piper_model=piper_model,
        piper_available=bool(piper_binary and piper_model),
        faster_whisper_available=WhisperModel is not None,
        sounddevice_available=sd is not None and vosk is not None,
    )
