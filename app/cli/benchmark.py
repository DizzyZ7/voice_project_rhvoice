from __future__ import annotations

import argparse
import json
import statistics
import time
import tracemalloc
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from app.commands.registry import (
    COMMAND_CONFIDENCE_THRESHOLD,
    UNKNOWN_COMMAND_REPLY,
    resolve_command_with_score,
    response_text_for_command,
)
from app.core.speech import create_recognizer, create_tts_engine, run_diagnostics


BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_PHRASES_PATH = BASE_DIR / "benchmarks" / "phrases_ru.json"


@dataclass(frozen=True)
class PhraseCase:
    text: str
    expected_command: str | None


@dataclass(frozen=True)
class MetricSummary:
    count: int
    min_ms: float
    mean_ms: float
    p95_ms: float
    max_ms: float
    cpu_load_percent: float
    peak_memory_kib: float


def load_phrase_cases(path: str | Path) -> list[PhraseCase]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [PhraseCase(text=item["text"], expected_command=item.get("expected_command")) for item in raw]


def percentile(sorted_values: list[float], ratio: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * ratio
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def _normalize_text(value: str) -> str:
    return " ".join(value.strip().lower().split())


def tokenize_words(value: str) -> list[str]:
    return _normalize_text(value).split()


def tokenize_chars(value: str) -> list[str]:
    return list("".join(tokenize_words(value)))


def levenshtein_distance(reference: list[str], hypothesis: list[str]) -> int:
    if not reference:
        return len(hypothesis)
    if not hypothesis:
        return len(reference)

    previous = list(range(len(hypothesis) + 1))
    for i, ref_item in enumerate(reference, start=1):
        current = [i]
        for j, hyp_item in enumerate(hypothesis, start=1):
            deletion = previous[j] + 1
            insertion = current[j - 1] + 1
            substitution = previous[j - 1] + (0 if ref_item == hyp_item else 1)
            current.append(min(deletion, insertion, substitution))
        previous = current
    return previous[-1]


def summarize_error_rates(pairs: list[tuple[str, str]]) -> dict[str, float | int]:
    ref_word_total = 0
    hyp_word_total = 0
    word_edits_total = 0
    ref_char_total = 0
    hyp_char_total = 0
    char_edits_total = 0

    for reference_text, hypothesis_text in pairs:
        ref_words = tokenize_words(reference_text)
        hyp_words = tokenize_words(hypothesis_text)
        ref_chars = tokenize_chars(reference_text)
        hyp_chars = tokenize_chars(hypothesis_text)

        ref_word_total += len(ref_words)
        hyp_word_total += len(hyp_words)
        ref_char_total += len(ref_chars)
        hyp_char_total += len(hyp_chars)
        word_edits_total += levenshtein_distance(ref_words, hyp_words)
        char_edits_total += levenshtein_distance(ref_chars, hyp_chars)

    wer = (word_edits_total / ref_word_total * 100) if ref_word_total else 0.0
    cer = (char_edits_total / ref_char_total * 100) if ref_char_total else 0.0
    return {
        "reference_words": ref_word_total,
        "hypothesis_words": hyp_word_total,
        "word_edits": word_edits_total,
        "wer_percent": round(wer, 3),
        "reference_chars": ref_char_total,
        "hypothesis_chars": hyp_char_total,
        "char_edits": char_edits_total,
        "cer_percent": round(cer, 3),
    }


def summarize_measurements(
    samples_ms: list[float],
    process_time_seconds: float,
    wall_time_seconds: float,
    peak_memory_bytes: int,
) -> MetricSummary:
    ordered = sorted(samples_ms)
    cpu_load = (process_time_seconds / wall_time_seconds * 100) if wall_time_seconds > 0 else 0.0
    return MetricSummary(
        count=len(ordered),
        min_ms=round(ordered[0], 3) if ordered else 0.0,
        mean_ms=round(statistics.fmean(ordered), 3) if ordered else 0.0,
        p95_ms=round(percentile(ordered, 0.95), 3) if ordered else 0.0,
        max_ms=round(ordered[-1], 3) if ordered else 0.0,
        cpu_load_percent=round(cpu_load, 3),
        peak_memory_kib=round(peak_memory_bytes / 1024, 3),
    )


def benchmark_callable(iterations: int, call: Callable[[], object]) -> MetricSummary:
    samples_ms: list[float] = []
    tracemalloc.start()
    wall_start = time.perf_counter()
    cpu_start = time.process_time()
    for _ in range(iterations):
        started = time.perf_counter()
        call()
        samples_ms.append((time.perf_counter() - started) * 1000)
    process_time_seconds = time.process_time() - cpu_start
    wall_time_seconds = time.perf_counter() - wall_start
    _, peak_memory_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return summarize_measurements(samples_ms, process_time_seconds, wall_time_seconds, peak_memory_bytes)


def benchmark_phrase_set(cases: list[PhraseCase]) -> dict[str, object]:
    mismatches: list[dict[str, object]] = []
    latencies_ms: list[float] = []
    tracemalloc.start()
    wall_start = time.perf_counter()
    cpu_start = time.process_time()
    for case in cases:
        started = time.perf_counter()
        spec, score = resolve_command_with_score(case.text)
        latencies_ms.append((time.perf_counter() - started) * 1000)
        actual = spec.key if spec and score >= COMMAND_CONFIDENCE_THRESHOLD else None
        if actual != case.expected_command:
            mismatches.append(
                {
                    "text": case.text,
                    "expected_command": case.expected_command,
                    "actual_command": actual,
                    "score": round(score, 3),
                }
            )
    process_time_seconds = time.process_time() - cpu_start
    wall_time_seconds = time.perf_counter() - wall_start
    _, peak_memory_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    accuracy = ((len(cases) - len(mismatches)) / len(cases) * 100) if cases else 0.0
    return {
        "cases_total": len(cases),
        "accuracy_percent": round(accuracy, 2),
        "summary": asdict(summarize_measurements(latencies_ms, process_time_seconds, wall_time_seconds, peak_memory_bytes)),
        "mismatches": mismatches,
    }


def benchmark_tts(messages: list[str], output_dir: Path) -> dict[str, object]:
    try:
        engine = create_tts_engine()
    except (FileNotFoundError, RuntimeError) as exc:
        return {"status": "skipped", "reason": str(exc)}
    target_dir = output_dir / "tts"
    target_dir.mkdir(parents=True, exist_ok=True)
    latencies_ms: list[float] = []
    generated_files: list[str] = []
    tracemalloc.start()
    wall_start = time.perf_counter()
    cpu_start = time.process_time()
    for index, message in enumerate(messages, start=1):
        output_path = target_dir / f"tts_case_{index}.wav"
        started = time.perf_counter()
        engine.synthesize_to_wav(message, output_path)
        latencies_ms.append((time.perf_counter() - started) * 1000)
        generated_files.append(str(output_path))
    process_time_seconds = time.process_time() - cpu_start
    wall_time_seconds = time.perf_counter() - wall_start
    _, peak_memory_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "status": "ok",
        "backend": getattr(engine, "backend_name", engine.__class__.__name__),
        "messages_total": len(messages),
        "summary": asdict(summarize_measurements(latencies_ms, process_time_seconds, wall_time_seconds, peak_memory_bytes)),
        "generated_files": generated_files,
    }


def benchmark_stt(wav_cases: list[dict[str, str]]) -> dict[str, object]:
    diagnostics = run_diagnostics()
    if diagnostics.stt_backend == "vosk" and not diagnostics.vosk_model_exists:
        return {"status": "skipped", "reason": "Vosk model not found"}
    if not diagnostics.stt_backend_available:
        return {"status": "skipped", "reason": f"STT backend '{diagnostics.stt_backend}' is not available"}

    recognizer = create_recognizer()
    latencies_ms: list[float] = []
    mismatches: list[dict[str, str]] = []
    error_pairs: list[tuple[str, str]] = []
    tracemalloc.start()
    wall_start = time.perf_counter()
    cpu_start = time.process_time()
    for item in wav_cases:
        started = time.perf_counter()
        result = recognizer.transcribe_from_wav(item["wav_path"])
        latencies_ms.append((time.perf_counter() - started) * 1000)
        expected = _normalize_text(item["expected_text"])
        actual = _normalize_text(result.text if result.success else "")
        error_pairs.append((expected, actual))
        if actual != expected:
            mismatches.append(
                {
                    "wav_path": item["wav_path"],
                    "expected_text": expected,
                    "actual_text": actual,
                    "error": result.error or "",
                }
            )
    process_time_seconds = time.process_time() - cpu_start
    wall_time_seconds = time.perf_counter() - wall_start
    _, peak_memory_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    accuracy = ((len(wav_cases) - len(mismatches)) / len(wav_cases) * 100) if wav_cases else 0.0
    error_summary = summarize_error_rates(error_pairs)
    return {
        "status": "ok",
        "backend": getattr(recognizer, "backend_name", recognizer.__class__.__name__),
        "cases_total": len(wav_cases),
        "accuracy_percent": round(accuracy, 2),
        "summary": asdict(summarize_measurements(latencies_ms, process_time_seconds, wall_time_seconds, peak_memory_bytes)),
        **error_summary,
        "mismatches": mismatches,
    }


def load_text_lines(path: str | Path) -> list[str]:
    return [line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def load_stt_cases(path: str | Path) -> list[dict[str, str]]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def build_demo_messages(cases: list[PhraseCase]) -> list[str]:
    messages: list[str] = []
    for case in cases:
        if case.expected_command is None:
            messages.append(UNKNOWN_COMMAND_REPLY)
        else:
            messages.append(response_text_for_command(case.expected_command, temperature_value=23))
    return messages


def main() -> None:
    parser = argparse.ArgumentParser(description="Измерения для 2 этапа TTS/STT модуля")
    parser.add_argument("--phrases", default=str(DEFAULT_PHRASES_PATH), help="JSON с тестовыми фразами")
    parser.add_argument("--iterations", type=int, default=200, help="Число итераций для синтетического прогона")
    parser.add_argument("--tts-texts", type=str, help="TXT файл с фразами для реального TTS бенчмарка")
    parser.add_argument("--stt-cases", type=str, help="JSON файл с WAV кейсами для реального STT бенчмарка")
    parser.add_argument("--output", type=str, default=str(BASE_DIR / "reports" / "stage2_measurements.json"))
    args = parser.parse_args()

    phrase_cases = load_phrase_cases(args.phrases)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    router_summary = benchmark_phrase_set(phrase_cases)
    synthetic_summary = benchmark_callable(
        args.iterations,
        lambda: [response_text_for_command(case.expected_command, temperature_value=23) if case.expected_command else UNKNOWN_COMMAND_REPLY for case in phrase_cases],
    )

    report: dict[str, object] = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "phrase_set": router_summary,
        "synthetic_response_pipeline": asdict(synthetic_summary),
        "tts_runtime": {"status": "not_requested"},
        "stt_runtime": {"status": "not_requested"},
    }

    if args.tts_texts:
        report["tts_runtime"] = benchmark_tts(load_text_lines(args.tts_texts), output_path.parent)
    else:
        report["tts_runtime"] = benchmark_tts(build_demo_messages(phrase_cases[:4]), output_path.parent)

    if args.stt_cases:
        report["stt_runtime"] = benchmark_stt(load_stt_cases(args.stt_cases))

    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Отчёт сохранён: {output_path}")


if __name__ == "__main__":
    main()
