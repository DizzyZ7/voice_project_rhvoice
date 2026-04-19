from __future__ import annotations

import argparse
import json
import random
import tempfile
import wave
from dataclasses import asdict, dataclass
from pathlib import Path

from app.cli.benchmark import word_error_rate
from app.core.speech import STTResult, build_stt_recognizer


def _read_wav_mono_16k(path: Path) -> list[int]:
    with wave.open(str(path), "rb") as wf:
        if wf.getnchannels() != 1 or wf.getframerate() != 16000 or wf.getsampwidth() != 2:
            raise ValueError(f"WAV must be mono 16kHz PCM16: {path}")
        frames = wf.readframes(wf.getnframes())
    samples: list[int] = []
    for offset in range(0, len(frames), 2):
        samples.append(int.from_bytes(frames[offset : offset + 2], byteorder="little", signed=True))
    return samples


def _write_wav_mono_16k(path: Path, samples: list[int]) -> None:
    raw = bytearray()
    for sample in samples:
        value = max(-32768, min(32767, int(sample)))
        raw += int(value).to_bytes(2, byteorder="little", signed=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(bytes(raw))


def _rms(samples: list[int]) -> float:
    if not samples:
        return 0.0
    return (sum(float(s * s) for s in samples) / len(samples)) ** 0.5


def mix_with_noise(clean: list[int], noise: list[int], snr_db: float, rnd: random.Random) -> list[int]:
    if not clean:
        return []
    if not noise:
        return clean[:]
    if len(noise) < len(clean):
        repeats = (len(clean) // len(noise)) + 1
        noise = (noise * repeats)[: len(clean)]
    elif len(noise) > len(clean):
        start = rnd.randint(0, len(noise) - len(clean))
        noise = noise[start : start + len(clean)]

    clean_rms = _rms(clean)
    noise_rms = _rms(noise)
    if clean_rms == 0.0 or noise_rms == 0.0:
        return clean[:]
    target_noise_rms = clean_rms / (10 ** (snr_db / 20.0))
    scale = target_noise_rms / noise_rms
    mixed = [int(clean[i] + noise[i] * scale) for i in range(len(clean))]
    return [max(-32768, min(32767, value)) for value in mixed]


@dataclass(frozen=True)
class NoiseCaseResult:
    wav_path: str
    noise_path: str
    snr_db: float
    expected_text: str
    actual_text: str
    success: bool
    error: str
    wer: float


def run_noise_benchmark(
    stt_backend: str,
    stt_cases: list[dict[str, str]],
    noise_paths: list[Path],
    snr_values: list[float],
    seed: int,
) -> dict[str, object]:
    recognizer = build_stt_recognizer(stt_backend)
    rnd = random.Random(seed)
    results: list[NoiseCaseResult] = []
    noise_samples = [_read_wav_mono_16k(path) for path in noise_paths]

    for case_index, case in enumerate(stt_cases):
        clean_samples = _read_wav_mono_16k(Path(case["wav_path"]))
        expected_text = case["expected_text"].strip().lower()
        for snr_db in snr_values:
            noise_idx = (case_index + int(snr_db * 10)) % len(noise_paths)
            noisy_samples = mix_with_noise(clean_samples, noise_samples[noise_idx], snr_db, rnd)
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                noisy_path = Path(tmp.name)
            try:
                _write_wav_mono_16k(noisy_path, noisy_samples)
                stt_result: STTResult = recognizer.transcribe_from_wav(str(noisy_path))
                actual_text = stt_result.text.strip().lower() if stt_result.success else ""
                wer = word_error_rate(expected_text, actual_text)
                results.append(
                    NoiseCaseResult(
                        wav_path=case["wav_path"],
                        noise_path=str(noise_paths[noise_idx]),
                        snr_db=snr_db,
                        expected_text=expected_text,
                        actual_text=actual_text,
                        success=stt_result.success,
                        error=stt_result.error or "",
                        wer=round(wer, 4),
                    )
                )
            finally:
                noisy_path.unlink(missing_ok=True)

    total_words = 0
    total_edits = 0.0
    for item in results:
        words = [token for token in item.expected_text.split() if token]
        if not words:
            continue
        total_words += len(words)
        total_edits += item.wer * len(words)

    avg_wer = (total_edits / total_words) if total_words else 0.0
    return {
        "backend": stt_backend,
        "snr_values_db": snr_values,
        "cases_total": len(results),
        "wer_percent": round(avg_wer * 100, 2),
        "results": [asdict(item) for item in results],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="STT noise benchmark with WER on noisy mixes")
    parser.add_argument("--backend", default="vosk", help="STT backend name (vosk|faster_whisper)")
    parser.add_argument("--stt-cases", required=True, help="JSON list: [{wav_path, expected_text}]")
    parser.add_argument("--noise-dir", required=True, help="Directory with mono 16kHz PCM16 noise WAV tracks")
    parser.add_argument("--snr", default="20,10,5", help="Comma-separated SNR values in dB")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", default="reports/stt_noise_benchmark.json")
    args = parser.parse_args()

    stt_cases = json.loads(Path(args.stt_cases).read_text(encoding="utf-8"))
    snr_values = [float(value.strip()) for value in args.snr.split(",") if value.strip()]
    noise_dir = Path(args.noise_dir)
    noise_paths = sorted(noise_dir.glob("*.wav"))
    if not noise_paths:
        raise FileNotFoundError(f"No .wav files in noise dir: {noise_dir}")

    report = run_noise_benchmark(
        stt_backend=args.backend,
        stt_cases=stt_cases,
        noise_paths=noise_paths,
        snr_values=snr_values,
        seed=args.seed,
    )

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output_path)


if __name__ == "__main__":
    main()
