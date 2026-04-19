from __future__ import annotations

from pathlib import Path

from app.cli.benchmark import (
    benchmark_phrase_set,
    levenshtein_distance,
    load_phrase_cases,
    summarize_error_rates,
    summarize_measurements,
)


BASE_DIR = Path(__file__).resolve().parents[1]


def test_load_phrase_cases():
    cases = load_phrase_cases(BASE_DIR / "benchmarks" / "phrases_ru.json")
    assert len(cases) >= 10
    assert any(case.expected_command == "turn_on_light" for case in cases)
    assert any(case.expected_command is None for case in cases)


def test_summarize_measurements():
    summary = summarize_measurements(
        [10.0, 20.0, 30.0, 40.0],
        process_time_seconds=0.05,
        wall_time_seconds=0.2,
        peak_memory_bytes=4096,
    )
    assert summary.count == 4
    assert summary.min_ms == 10.0
    assert summary.max_ms == 40.0
    assert summary.mean_ms == 25.0
    assert summary.p95_ms >= 38.0
    assert summary.cpu_load_percent == 25.0
    assert summary.peak_memory_kib == 4.0


def test_benchmark_phrase_set_has_full_accuracy_on_control_data():
    report = benchmark_phrase_set(load_phrase_cases(BASE_DIR / "benchmarks" / "phrases_ru.json"))
    assert report["cases_total"] >= 10
    assert report["accuracy_percent"] == 100.0
    assert report["mismatches"] == []


def test_benchmark_phrase_set_has_full_accuracy_on_long_phrases():
    report = benchmark_phrase_set(load_phrase_cases(BASE_DIR / "benchmarks" / "phrases_long_ru.json"))
    assert report["cases_total"] >= 5
    assert report["accuracy_percent"] == 100.0
    assert report["mismatches"] == []
