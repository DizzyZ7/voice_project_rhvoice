from __future__ import annotations

import base64
import ctypes
import contextlib
import hashlib
import importlib
import json
import logging
import os
import queue
import shutil
import struct
import subprocess
import sys
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
    from rnnoise_wrapper import RNNoise as RNNoisePyWrapper
except ImportError:
    RNNoisePyWrapper = None


BASE_DIR = Path(__file__).resolve().parents[2]
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


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


DEFAULT_STT_BACKEND = os.environ.get("STT_BACKEND", "vosk")
DEFAULT_VOSK_MODEL_PATH = choose_vosk_model_path(BASE_DIR, os.environ.get("VOSK_MODEL_PATH"))
DEFAULT_FASTER_WHISPER_MODEL_REF = os.environ.get("FASTER_WHISPER_MODEL", "small")
DEFAULT_FASTER_WHISPER_DEVICE = os.environ.get("FASTER_WHISPER_DEVICE", "cpu")
DEFAULT_FASTER_WHISPER_COMPUTE_TYPE = os.environ.get("FASTER_WHISPER_COMPUTE_TYPE", "int8")
DEFAULT_FASTER_WHISPER_LANGUAGE = os.environ.get("FASTER_WHISPER_LANGUAGE", "ru")
DEFAULT_FASTER_WHISPER_BEAM_SIZE = int(os.environ.get("FASTER_WHISPER_BEAM_SIZE", "1"))
DEFAULT_FASTER_WHISPER_VAD_FILTER = os.environ.get("FASTER_WHISPER_VAD_FILTER", "1")
DEFAULT_STT_MIC_VAD_RMS_THRESHOLD = int(os.environ.get("STT_MIC_VAD_RMS_THRESHOLD", "0"))
DEFAULT_STT_DENOISE_BACKEND = os.environ.get("STT_DENOISE_BACKEND", "none")
DEFAULT_STT_DENOISE_FAIL_OPEN = os.environ.get("STT_DENOISE_FAIL_OPEN", "1").strip().lower() in {"1", "true", "yes", "on"}
DEFAULT_STT_RNNOISE_LIB = os.environ.get("STT_RNNOISE_LIB", "").strip()

DEFAULT_RHVOICE_BIN = os.environ.get("RHVOICE_BIN")
DEFAULT_WINDOWS_VOICE = os.environ.get("RHVOICE_WINDOWS_VOICE")
DEFAULT_TTS_BACKEND = os.environ.get("TTS_BACKEND", "auto")
DEFAULT_PIPER_BIN = os.environ.get("PIPER_BIN")
DEFAULT_PIPER_MODEL_PATH = os.environ.get("PIPER_MODEL_PATH") or os.environ.get("PIPER_MODEL")
DEFAULT_PIPER_VOICE_MODELS = os.environ.get("PIPER_VOICE_MODELS", "{}")
DEFAULT_TTS_CACHE_ENABLED = os.environ.get("TTS_CACHE_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
DEFAULT_TTS_CACHE_DIR = Path(os.environ.get("TTS_CACHE_DIR", str(BASE_DIR / "cache" / "tts"))).resolve()
_RNNOISE_INSTANCE = None
_RNNOISE_LOCK = threading.Lock()
_RNNOISE_DLL = None
_RNNOISE_DLL_PATH = None


def _parse_optional_float(value: str) -> Optional[float]:
    raw = value.strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


DEFAULT_STT_RNNOISE_VOICE_PROB_THRESHOLD = _parse_optional_float(
    os.environ.get("STT_RNNOISE_VOICE_PROB_THRESHOLD", "")
)


def _load_rnnoise_py_wrapper_class():
    if RNNoisePyWrapper is not None:
        return RNNoisePyWrapper
    vendored_root = BASE_DIR / "third_party" / "RNNoise_Wrapper"
    if vendored_root.exists():
        vendored_path = str(vendored_root.resolve())
        if vendored_path not in sys.path:
            sys.path.insert(0, vendored_path)
    try:
        mod = importlib.import_module("rnnoise_wrapper")
        return getattr(mod, "RNNoise", None)
    except Exception:
        return None


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
    sounddevice_available: bool
    stt_backend: str = "vosk"
    stt_backend_available: bool = False
    faster_whisper_available: bool = False
    tts_backend: str = "rhvoice"
    tts_backend_available: bool = False
    piper_binary: Optional[str] = None
    piper_model_path: Optional[str] = None


@dataclass(frozen=True)
class SimpleEnergyVAD:
    rms_threshold: int = int(os.environ.get("STT_VAD_RMS_THRESHOLD", "220"))
    min_speech_ratio: float = float(os.environ.get("STT_VAD_MIN_SPEECH_RATIO", "0.02"))
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
    squared_total = 0.0
    for offset in range(0, sample_count * 2, 2):
        value = int.from_bytes(pcm_bytes[offset : offset + 2], byteorder="little", signed=True)
        squared_total += float(value * value)
    return (squared_total / sample_count) ** 0.5


def _ensure_nonempty_wav(path: Path) -> None:
    if not path.exists():
        raise RuntimeError(f"WAV не создан: {path}")
    if path.stat().st_size <= 44:
        raise RuntimeError(f"WAV пустой или повреждён (слишком маленький): {path}")
    with contextlib.closing(wave.open(str(path), "rb")) as wf:
        if wf.getnframes() <= 0:
            raise RuntimeError(f"WAV не содержит аудиофреймов: {path}")


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


def _normalize_stt_backend_name(value: str) -> str:
    return value.strip().lower().replace("-", "_")


def _normalize_tts_backend_name(value: str) -> str:
    return value.strip().lower().replace("-", "_")


def _normalize_denoise_backend_name(value: str) -> str:
    return value.strip().lower().replace("-", "_")


def _normalize_text(value: str) -> str:
    return " ".join(value.strip().split()).lower()


def _discover_windows_rnnoise_lib() -> Optional[str]:
    if DEFAULT_STT_RNNOISE_LIB and Path(DEFAULT_STT_RNNOISE_LIB).exists():
        return str(Path(DEFAULT_STT_RNNOISE_LIB).resolve())

    base = BASE_DIR / "third_party" / "rnnoise-windows"
    candidates = [
        base / "x64" / "Release" / "rnnoise_share.dll",
        base / "x64" / "Debug" / "rnnoise_share.dll",
        base / "Release" / "rnnoise_share.dll",
        base / "Debug" / "rnnoise_share.dll",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate.resolve())

    if base.exists():
        for dll in base.rglob("rnnoise*.dll"):
            return str(dll.resolve())
    return None


def _load_windows_rnnoise_dll(logger: logging.Logger):
    global _RNNOISE_DLL, _RNNOISE_DLL_PATH
    if _RNNOISE_DLL is not None:
        return _RNNOISE_DLL

    dll_path = _discover_windows_rnnoise_lib()
    if not dll_path:
        logger.warning("RNNoise DLL не найдена. Укажите STT_RNNOISE_LIB или соберите rnnoise_share.dll в third_party/rnnoise-windows.")
        return None

    with _RNNOISE_LOCK:
        if _RNNOISE_DLL is not None:
            return _RNNOISE_DLL
        try:
            lib = ctypes.CDLL(dll_path)
            lib.rnnoise_create.argtypes = [ctypes.c_void_p]
            lib.rnnoise_create.restype = ctypes.c_void_p
            lib.rnnoise_destroy.argtypes = [ctypes.c_void_p]
            lib.rnnoise_destroy.restype = None
            lib.rnnoise_process_frame.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_float),
                ctypes.POINTER(ctypes.c_float),
            ]
            lib.rnnoise_process_frame.restype = ctypes.c_float
            _RNNOISE_DLL = lib
            _RNNOISE_DLL_PATH = dll_path
            logger.info("RNNoise DLL loaded: %s", dll_path)
            return _RNNOISE_DLL
        except Exception as exc:
            logger.warning("Не удалось загрузить RNNoise DLL (%s): %s", dll_path, exc)
            return None


def _denoise_pcm16_with_rnnoise_dll(pcm_bytes: bytes, sample_rate: int, logger: logging.Logger) -> bytes:
    if not pcm_bytes:
        return pcm_bytes
    lib = _load_windows_rnnoise_dll(logger)
    if lib is None:
        raise RuntimeError("RNNoise DLL is not available")

    working_bytes = _resample_pcm16_mono(pcm_bytes, sample_rate, 48000) if sample_rate != 48000 else pcm_bytes

    if len(working_bytes) % 2:
        working_bytes += b"\x00"

    sample_count = len(working_bytes) // 2
    frame_size = 480
    total_frames = (sample_count + frame_size - 1) // frame_size
    padded_sample_count = total_frames * frame_size
    if padded_sample_count > sample_count:
        working_bytes += b"\x00" * ((padded_sample_count - sample_count) * 2)

    samples = struct.unpack("<" + "h" * padded_sample_count, working_bytes)
    in_frame = (ctypes.c_float * frame_size)()
    out_frame = (ctypes.c_float * frame_size)()
    output_samples: list[int] = []
    state = lib.rnnoise_create(None)
    if not state:
        raise RuntimeError("rnnoise_create returned NULL")
    try:
        for frame_idx in range(total_frames):
            base = frame_idx * frame_size
            for i in range(frame_size):
                in_frame[i] = float(samples[base + i])
            lib.rnnoise_process_frame(state, out_frame, in_frame)
            for i in range(frame_size):
                value = int(round(out_frame[i]))
                if value > 32767:
                    value = 32767
                elif value < -32768:
                    value = -32768
                output_samples.append(value)
    finally:
        lib.rnnoise_destroy(state)

    denoised_bytes = struct.pack("<" + "h" * len(output_samples), *output_samples)
    denoised_bytes = denoised_bytes[: sample_count * 2]
    if sample_rate != 48000:
        denoised_bytes = _resample_pcm16_mono(denoised_bytes, 48000, sample_rate)
    return denoised_bytes


def _resample_pcm16_mono(pcm_bytes: bytes, src_rate: int, dst_rate: int) -> bytes:
    if src_rate <= 0 or dst_rate <= 0:
        raise ValueError("Invalid sample rate")
    if src_rate == dst_rate or not pcm_bytes:
        return pcm_bytes
    if len(pcm_bytes) % 2:
        pcm_bytes += b"\x00"
    src_count = len(pcm_bytes) // 2
    if src_count == 0:
        return b""

    src_samples = struct.unpack("<" + "h" * src_count, pcm_bytes)
    dst_count = max(1, int(round(src_count * dst_rate / src_rate)))
    ratio = src_rate / dst_rate
    out: list[int] = []
    max_index = src_count - 1
    for i in range(dst_count):
        pos = i * ratio
        i0 = int(pos)
        if i0 >= max_index:
            value = src_samples[max_index]
        else:
            frac = pos - i0
            v0 = src_samples[i0]
            v1 = src_samples[i0 + 1]
            value = int(round(v0 + (v1 - v0) * frac))
        if value > 32767:
            value = 32767
        elif value < -32768:
            value = -32768
        out.append(value)
    return struct.pack("<" + "h" * len(out), *out)


class SpeechRecognizer:
    backend_name = "unknown"

    def transcribe_from_wav(self, wav_path: str) -> STTResult:
        raise NotImplementedError

    def transcribe_from_microphone(self, timeout: int = 5) -> STTResult:
        raise NotImplementedError

    @staticmethod
    def _capture_microphone_chunks(
        timeout: int,
        logger: logging.Logger,
        sample_rate: int = 16000,
        blocksize: int = 8000,
    ) -> tuple[list[bytes], Optional[str]]:
        if sd is None:
            return [], "sounddevice не установлен"

        audio_queue: queue.Queue[bytes] = queue.Queue()
        stop_event = threading.Event()

        def callback(indata, frames, time_info, status):
            del frames, time_info
            if status:
                logger.warning("Audio status: %s", status)
            audio_queue.put(bytes(indata))
            if stop_event.is_set():
                raise sd.CallbackStop()

        try:
            with sd.RawInputStream(
                samplerate=sample_rate,
                blocksize=blocksize,
                dtype="int16",
                channels=1,
                callback=callback,
            ):
                logger.info("Начало записи с микрофона на %s сек", timeout)
                sd.sleep(int(timeout * 1000))
                stop_event.set()
        except Exception as exc:
            logger.exception("Ошибка захвата аудио")
            return [], str(exc)

        chunks: list[bytes] = []
        while not audio_queue.empty():
            chunks.append(audio_queue.get())
        return chunks, None

    @staticmethod
    def _apply_energy_vad(chunks: list[bytes], rms_threshold: int) -> list[bytes]:
        if rms_threshold <= 0:
            return chunks
        return [chunk for chunk in chunks if SpeechRecognizer._chunk_rms(chunk) >= rms_threshold]

    @staticmethod
    def _chunk_rms(chunk: bytes) -> float:
        if not chunk:
            return 0.0
        sample_count = len(chunk) // 2
        if sample_count <= 0:
            return 0.0
        squared_total = 0.0
        for (sample,) in struct.iter_unpack("<h", chunk[: sample_count * 2]):
            squared_total += float(sample * sample)
        return (squared_total / sample_count) ** 0.5

    def _maybe_denoise_wav(self, wav_path: str) -> tuple[str, Optional[Path]]:
        backend = _normalize_denoise_backend_name(DEFAULT_STT_DENOISE_BACKEND)
        if backend != "rnnoise":
            return wav_path, None

        temp_path: Optional[Path] = None
        try:
            with wave.open(wav_path, "rb") as wf:
                channels = wf.getnchannels()
                sample_width = wf.getsampwidth()
                sample_rate = wf.getframerate()
                frames = wf.readframes(wf.getnframes())
            if channels != 1 or sample_width != 2:
                self.logger.warning(
                    "RNNoise ожидает mono PCM16. Получено channels=%s sample_width=%s, пропускаю шумодав.",
                    channels,
                    sample_width,
                )
                return wav_path, None

            if os.name == "nt":
                denoised_frames = _denoise_pcm16_with_rnnoise_dll(frames, sample_rate, self.logger)
            else:
                denoiser = self._get_rnnoise_wrapper_instance()
                if denoiser is None:
                    raise RuntimeError("rnnoise_wrapper is not available")
                kwargs: dict[str, float] = {}
                if DEFAULT_STT_RNNOISE_VOICE_PROB_THRESHOLD is not None:
                    kwargs["voice_prob_threshold"] = DEFAULT_STT_RNNOISE_VOICE_PROB_THRESHOLD
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as in_tmp:
                    in_tmp_path = Path(in_tmp.name)
                try:
                    with wave.open(str(in_tmp_path), "wb") as in_wf:
                        in_wf.setnchannels(1)
                        in_wf.setsampwidth(2)
                        in_wf.setframerate(sample_rate)
                        in_wf.writeframes(frames)
                    audio = denoiser.read_wav(str(in_tmp_path))
                    denoised_audio = denoiser.filter(audio, **kwargs)
                    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as out_tmp:
                        out_tmp_path = Path(out_tmp.name)
                    try:
                        denoiser.write_wav(str(out_tmp_path), denoised_audio)
                        with wave.open(str(out_tmp_path), "rb") as out_wf:
                            denoised_frames = out_wf.readframes(out_wf.getnframes())
                    finally:
                        try:
                            out_tmp_path.unlink()
                        except Exception:
                            pass
                finally:
                    try:
                        in_tmp_path.unlink()
                    except Exception:
                        pass

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                temp_path = Path(tmp.name)
            with wave.open(str(temp_path), "wb") as out_wf:
                out_wf.setnchannels(1)
                out_wf.setsampwidth(2)
                out_wf.setframerate(sample_rate)
                out_wf.writeframes(denoised_frames)
            return str(temp_path), temp_path
        except Exception as exc:
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except Exception:
                    pass
            if DEFAULT_STT_DENOISE_FAIL_OPEN:
                self.logger.warning("RNNoise шумодав не применён, продолжаю без него: %s", exc)
                return wav_path, None
            raise RuntimeError(f"RNNoise шумодав не применён: {exc}") from exc

    def _get_rnnoise_wrapper_instance(self):
        global _RNNOISE_INSTANCE
        if _RNNOISE_INSTANCE is not None:
            return _RNNOISE_INSTANCE
        with _RNNOISE_LOCK:
            if _RNNOISE_INSTANCE is not None:
                return _RNNOISE_INSTANCE
            rnnoise_cls = _load_rnnoise_py_wrapper_class()
            if rnnoise_cls is None:
                self.logger.warning("rnnoise_wrapper не установлен. На Windows используйте rnnoise DLL через STT_RNNOISE_LIB.")
                return None
            try:
                kwargs = {}
                if DEFAULT_STT_RNNOISE_LIB:
                    kwargs["f_name_lib"] = DEFAULT_STT_RNNOISE_LIB
                _RNNOISE_INSTANCE = rnnoise_cls(**kwargs)
                return _RNNOISE_INSTANCE
            except Exception as exc:
                self.logger.warning("RNNoise init failed: %s", exc)
                return None


class SpeechSynthesizer:
    backend_name = "unknown"

    def speak(
        self,
        text: str,
        speed: float = 1.0,
        pitch: float = 0.0,
        voice: Optional[str] = None,
        use_cache: bool = True,
    ) -> None:
        raise NotImplementedError

    def synthesize_to_wav(
        self,
        text: str,
        output_path: str | Path,
        speed: float = 1.0,
        pitch: float = 0.0,
        voice: Optional[str] = None,
        use_cache: bool = True,
    ) -> Path:
        raise NotImplementedError


class RHVoiceTTS(SpeechSynthesizer):
    """Thin adapter around RHVoice CLI tools with file-level caching."""
    backend_name = "rhvoice"

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
                try:
                    subprocess.run([aplay, str(wav_path)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except subprocess.CalledProcessError:
                    self.logger.warning("aplay завершился с ошибкой, WAV уже сохранён: %s", wav_path)
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
            _ensure_nonempty_wav(output)
            self.logger.info("Windows SAPI сохранил WAV: %s", output)
        else:
            self._synthesize_cli_to_wav(text=text, output=output, speed=speed, pitch=pitch, voice=voice)
            _ensure_nonempty_wav(output)
            self.logger.info("RHVoice сохранил WAV: %s", output)

        if cache_path and output.exists():
            try:
                shutil.copy2(output, cache_path)
            except Exception:
                self.logger.warning("Не удалось обновить TTS cache: %s", cache_path)
        return output


class PiperTTS(SpeechSynthesizer):
    """Adapter for Piper CLI with speed control and wav cache."""

    backend_name = "piper"

    def __init__(
        self,
        binary: Optional[str] = None,
        model_path: Optional[str] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.logger = logger or setup_logger("tts", "tts.log")
        self.binary = binary or self._discover_binary()
        self.default_model_path = model_path or DEFAULT_PIPER_MODEL_PATH
        self.voice_models = self._load_voice_models(DEFAULT_PIPER_VOICE_MODELS)
        self.cache_enabled = DEFAULT_TTS_CACHE_ENABLED
        self.cache_dir = DEFAULT_TTS_CACHE_DIR
        self._pitch_warning_emitted = False

        if self.cache_enabled:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

        if not self.binary:
            raise FileNotFoundError(
                "Не найден Piper CLI бинарник. Установите piper и задайте PIPER_BIN при необходимости."
            )
        if not self.default_model_path and not self.voice_models:
            raise FileNotFoundError(
                "Не задана модель Piper. Укажите PIPER_MODEL_PATH или PIPER_VOICE_MODELS (JSON map)."
            )
        self.logger.info("Используется Piper бинарник: %s", self.binary)

    @staticmethod
    def _discover_binary() -> Optional[str]:
        candidates = [DEFAULT_PIPER_BIN, "piper"]
        for candidate in candidates:
            if candidate and shutil.which(candidate):
                return candidate
        return None

    @staticmethod
    def _load_voice_models(raw: str) -> dict[str, str]:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        result: dict[str, str] = {}
        for key, value in parsed.items():
            if isinstance(key, str) and isinstance(value, str):
                result[key] = value
        return result

    @staticmethod
    def _normalize_speed(speed: float) -> float:
        return min(2.0, max(0.5, float(speed)))

    @staticmethod
    def _normalize_pitch(pitch: float) -> float:
        return min(12.0, max(-12.0, float(pitch)))

    @staticmethod
    def _speed_to_length_scale(speed: float) -> float:
        safe_speed = max(0.1, speed)
        return min(2.5, max(0.3, 1.0 / safe_speed))

    def _resolve_model_path(self, voice: Optional[str]) -> Path:
        candidate: Optional[str] = None
        if voice:
            if voice in self.voice_models:
                candidate = self.voice_models[voice]
            else:
                candidate = voice
        elif self.default_model_path:
            candidate = self.default_model_path
        elif self.voice_models:
            candidate = next(iter(self.voice_models.values()))

        if not candidate:
            raise FileNotFoundError("Не удалось определить модель Piper (voice/model not set)")
        resolved = Path(candidate).expanduser().resolve()
        if not resolved.exists():
            raise FileNotFoundError(f"Модель Piper не найдена: {resolved}")
        return resolved

    def _cache_key(self, text: str, model_path: Path, speed: float, pitch: float, voice: Optional[str]) -> str:
        payload = {
            "backend": self.backend_name,
            "binary": self.binary or "",
            "model": str(model_path),
            "voice": voice or "",
            "speed": round(speed, 3),
            "pitch": round(pitch, 3),
            "text": text,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _cached_wav_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.wav"

    def _run_piper(self, text: str, output: Path, model_path: Path, speed: float, pitch: float) -> None:
        if pitch != 0.0 and not self._pitch_warning_emitted:
            self.logger.warning("Piper backend не поддерживает pitch напрямую; параметр будет проигнорирован")
            self._pitch_warning_emitted = True

        cmd = [
            self.binary,
            "--model",
            str(model_path),
            "--output_file",
            str(output),
            "--length_scale",
            f"{self._speed_to_length_scale(speed):.3f}",
        ]
        # On Windows, --input_file can hang with some Piper builds.
        # Feed stdin explicitly as UTF-8 to keep Russian text stable.
        result = subprocess.run(
            cmd,
            input=text,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=True,
        )
        if result.stderr:
            self.logger.info("Piper stderr: %s", result.stderr.strip())

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

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav_path = Path(tmp.name)
        try:
            self.synthesize_to_wav(text, wav_path, speed=speed, pitch=pitch, voice=voice, use_cache=use_cache)
            aplay = shutil.which("aplay")
            if aplay:
                try:
                    subprocess.run([aplay, str(wav_path)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except subprocess.CalledProcessError:
                    self.logger.warning("aplay завершился с ошибкой, WAV уже сохранён: %s", wav_path)
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
        model_path = self._resolve_model_path(voice=voice)
        cache_path: Optional[Path] = None

        if use_cache and self.cache_enabled:
            cache_key = self._cache_key(text=text, model_path=model_path, speed=speed, pitch=pitch, voice=voice)
            cache_path = self._cached_wav_path(cache_key)
            if cache_path.exists():
                shutil.copy2(cache_path, output)
                self.logger.info("TTS cache hit: %s", cache_key[:12])
                return output

        self._run_piper(text=text, output=output, model_path=model_path, speed=speed, pitch=pitch)
        _ensure_nonempty_wav(output)
        self.logger.info("Piper сохранил WAV: %s", output)

        if cache_path and output.exists():
            try:
                shutil.copy2(output, cache_path)
            except Exception:
                self.logger.warning("Не удалось обновить TTS cache: %s", cache_path)
        return output


class CachedTTSEngine(SpeechSynthesizer):
    backend_name = "cached"

    def __init__(
        self,
        engine: SpeechSynthesizer,
        cache_dir: Path = DEFAULT_TTS_CACHE_DIR,
        cache_ttl_seconds: int = int(os.environ.get("TTS_CACHE_TTL_SECONDS", "86400")),
        logger: Optional[logging.Logger] = None,
    ):
        self._engine = engine
        self._cache_dir = Path(cache_dir)
        self._cache_ttl_seconds = cache_ttl_seconds
        self.logger = logger or setup_logger("tts", "tts.log")
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    @property
    def engine_id(self) -> str:
        return f"{getattr(self._engine, 'engine_id', self._engine.__class__.__name__)}+cache"

    def _cache_path(self, text: str, speed: float, pitch: float, voice: Optional[str]) -> Path:
        payload = json.dumps(
            {
                "engine": self.engine_id,
                "text": text.strip(),
                "speed": round(float(speed), 3),
                "pitch": round(float(pitch), 3),
                "voice": voice or "",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return self._cache_dir / f"{digest}.wav"

    def _is_fresh(self, path: Path) -> bool:
        if not path.exists():
            return False
        if self._cache_ttl_seconds <= 0:
            return True
        age = time.time() - path.stat().st_mtime
        return age <= self._cache_ttl_seconds

    def _ensure_cached(self, text: str, speed: float, pitch: float, voice: Optional[str]) -> Path:
        key = text.strip()
        if not key:
            raise ValueError("Пустой текст для синтеза")
        cached_path = self._cache_path(key, speed=speed, pitch=pitch, voice=voice)
        if self._is_fresh(cached_path):
            self.logger.info("TTS cache hit: %s", cached_path.name)
            return cached_path
        try:
            self._engine.synthesize_to_wav(key, cached_path, speed=speed, pitch=pitch, voice=voice, use_cache=True)
        except TypeError:
            # Backward compatibility with engines that expose legacy signature.
            self._engine.synthesize_to_wav(key, cached_path)
        self.logger.info("TTS cache miss: %s", cached_path.name)
        return cached_path

    def speak(
        self,
        text: str,
        speed: float = 1.0,
        pitch: float = 0.0,
        voice: Optional[str] = None,
        use_cache: bool = True,
    ) -> None:
        if not text.strip():
            return
        if not use_cache:
            self._engine.speak(text, speed=speed, pitch=pitch, voice=voice, use_cache=False)
            return
        cached_path = self._ensure_cached(text, speed=speed, pitch=pitch, voice=voice)
        aplay = shutil.which("aplay")
        if aplay:
            try:
                subprocess.run([aplay, str(cached_path)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except subprocess.CalledProcessError:
                self.logger.warning("aplay завершился с ошибкой, WAV в кэше доступен: %s", cached_path)
        else:
            self.logger.warning("Аудиоплеер не найден, WAV сохранен: %s", cached_path)

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
        if not use_cache:
            try:
                self._engine.synthesize_to_wav(text, output, speed=speed, pitch=pitch, voice=voice, use_cache=False)
            except TypeError:
                self._engine.synthesize_to_wav(text, output)
            return output
        cached_path = self._ensure_cached(text, speed=speed, pitch=pitch, voice=voice)
        shutil.copy2(cached_path, output)
        return output


class VoskRecognizer(SpeechRecognizer):
    backend_name = "vosk"

    def __init__(self, model_path: str = DEFAULT_VOSK_MODEL_PATH, logger: Optional[logging.Logger] = None):
        if vosk is None:
            raise RuntimeError("Не установлен vosk. Установите: pip install vosk")
        self.model_path = model_path
        self.logger = logger or setup_logger("stt", "stt.log")
        self.mic_vad_rms_threshold = DEFAULT_STT_MIC_VAD_RMS_THRESHOLD
        if not os.path.isdir(model_path):
            raise FileNotFoundError(
                f"Модель Vosk не найдена: {model_path}. Скачайте русскую модель и задайте VOSK_MODEL_PATH."
            )
        self.logger.info("Загрузка модели Vosk: %s", model_path)
        self.model = vosk.Model(model_path)

    def transcribe_from_wav(self, wav_path: str) -> STTResult:
        processed_wav, temp_denoised_path = self._maybe_denoise_wav(wav_path)
        try:
            wf = wave.open(processed_wav, "rb")
        except FileNotFoundError:
            return STTResult(text="", success=False, error=f"Файл не найден: {processed_wav}")
        except wave.Error as exc:
            return STTResult(text="", success=False, error=str(exc))

        try:
            if wf.getnchannels() != 1 or wf.getframerate() != 16000:
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
            text, confidence = self._extract_text_and_confidence(chunks)
            return STTResult(text=text, success=True, confidence=confidence)
        finally:
            wf.close()
            if temp_denoised_path is not None:
                try:
                    temp_denoised_path.unlink()
                except Exception:
                    pass

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
        processed_wav, temp_denoised_path = self._maybe_denoise_wav(wav_path)
        wav_file = Path(processed_wav)
        if not wav_file.exists():
            return STTResult(text="", success=False, error=f"Файл не найден: {processed_wav}")

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
        finally:
            if temp_denoised_path is not None:
                try:
                    temp_denoised_path.unlink()
                except Exception:
                    pass

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
    if resolved_backend == "auto":
        try:
            return PiperTTS(model_path=model_path or DEFAULT_PIPER_MODEL_PATH, logger=logger)
        except Exception:
            return RHVoiceTTS(logger=logger)
    raise ValueError(f"Неподдерживаемый TTS backend: {backend}")


def build_stt_recognizer(backend: str = DEFAULT_STT_BACKEND) -> SpeechRecognizer:
    return create_recognizer(backend=backend)


def build_tts_engine(backend: str = DEFAULT_TTS_BACKEND) -> CachedTTSEngine:
    return CachedTTSEngine(create_tts_engine(backend=backend))


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

    stt_backend = _normalize_stt_backend_name(DEFAULT_STT_BACKEND)
    if stt_backend == "vosk":
        stt_backend_available = os.path.isdir(model_path)
    else:
        stt_backend_available = WhisperModel is not None

    piper_model_exists = False
    if piper_model_path and Path(piper_model_path).expanduser().resolve().exists():
        piper_model_exists = True
    elif piper_models:
        piper_model_exists = any(Path(path).expanduser().resolve().exists() for path in piper_models.values())

    tts_backend = _normalize_tts_backend_name(DEFAULT_TTS_BACKEND)
    if tts_backend in {"rhvoice", "rhvoice_cli"}:
        tts_backend_available = rhvoice_available
    elif tts_backend == "piper":
        tts_backend_available = bool(piper_binary and piper_model_exists)
    else:
        tts_backend_available = False

    return Diagnostics(
        vosk_model_exists=os.path.isdir(model_path),
        rhvoice_binary=rhvoice_binary,
        rhvoice_backend=rhvoice_backend,
        rhvoice_target=rhvoice_target,
        rhvoice_available=rhvoice_available,
        sounddevice_available=sd is not None and (vosk is not None or WhisperModel is not None),
        stt_backend=stt_backend,
        stt_backend_available=stt_backend_available,
        faster_whisper_available=WhisperModel is not None,
        tts_backend=tts_backend,
        tts_backend_available=tts_backend_available,
        piper_binary=piper_binary,
        piper_model_path=piper_model_path,
    )
