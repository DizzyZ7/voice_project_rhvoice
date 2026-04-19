#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


def run_command(command: list[str]) -> None:
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(command)}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run regular WER regressions and append history")
    parser.add_argument("--python", default=sys.executable, help="Python executable")
    parser.add_argument("--stt-cases", required=True, help="Path to benchmark STT cases JSON")
    parser.add_argument("--noise-dir", help="Optional path to noise wav directory for stt_noise_benchmark")
    parser.add_argument("--snr", default="20,10,5")
    parser.add_argument("--backend", default="vosk")
    parser.add_argument("--history", default="reports/wer_history.jsonl")
    parser.add_argument("--bench-output", default="reports/stt_measurements.json")
    parser.add_argument("--noise-output", default="reports/stt_noise_benchmark.json")
    args = parser.parse_args()

    bench_output = Path(args.bench_output)
    bench_output.parent.mkdir(parents=True, exist_ok=True)

    run_command(
        [
            args.python,
            "-m",
            "app.cli.benchmark",
            "--stt-cases",
            args.stt_cases,
            "--output",
            str(bench_output),
        ]
    )
    bench_report = read_json(bench_output)
    stt_runtime = bench_report.get("stt_runtime", {})
    if not isinstance(stt_runtime, dict):
        raise RuntimeError("Invalid stt_runtime in benchmark report")

    record: dict[str, object] = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "backend": args.backend,
        "stt_cases": args.stt_cases,
        "stt_status": stt_runtime.get("status"),
        "stt_wer_percent": stt_runtime.get("wer_percent"),
        "stt_accuracy_percent": stt_runtime.get("accuracy_percent"),
    }

    if args.noise_dir:
        noise_output = Path(args.noise_output)
        run_command(
            [
                args.python,
                "-m",
                "app.cli.stt_noise_benchmark",
                "--backend",
                args.backend,
                "--stt-cases",
                args.stt_cases,
                "--noise-dir",
                args.noise_dir,
                "--snr",
                args.snr,
                "--output",
                str(noise_output),
            ]
        )
        noise_report = read_json(noise_output)
        record["noise_wer_percent"] = noise_report.get("wer_percent")
        record["noise_cases_total"] = noise_report.get("cases_total")

    history_path = Path(args.history)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
