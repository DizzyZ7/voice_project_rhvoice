#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate benchmark quality gates")
    parser.add_argument("--report", required=True, help="Path to benchmark report JSON")
    parser.add_argument("--min-phrase-accuracy", type=float, default=95.0)
    parser.add_argument("--max-tts-p95-ms", type=float, default=2000.0)
    parser.add_argument("--max-stt-wer-percent", type=float, default=25.0)
    args = parser.parse_args()

    report_path = Path(args.report)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    failures: list[str] = []

    phrase_accuracy = float(report.get("phrase_set", {}).get("accuracy_percent", 0.0))
    if phrase_accuracy < args.min_phrase_accuracy:
        failures.append(
            f"phrase_set.accuracy_percent={phrase_accuracy:.3f} < minimum {args.min_phrase_accuracy:.3f}"
        )

    tts_runtime = report.get("tts_runtime", {})
    tts_status = str(tts_runtime.get("status", "unknown"))
    if tts_status == "ok":
        tts_p95 = float(tts_runtime.get("summary", {}).get("p95_ms", 0.0))
        if tts_p95 > args.max_tts_p95_ms:
            failures.append(f"tts_runtime.summary.p95_ms={tts_p95:.3f} > maximum {args.max_tts_p95_ms:.3f}")
    else:
        print(f"[SKIP] TTS gate skipped, status={tts_status}")

    stt_runtime = report.get("stt_runtime", {})
    stt_status = str(stt_runtime.get("status", "unknown"))
    if stt_status == "ok":
        stt_wer = float(stt_runtime.get("wer_percent", 0.0))
        if stt_wer > args.max_stt_wer_percent:
            failures.append(f"stt_runtime.wer_percent={stt_wer:.3f} > maximum {args.max_stt_wer_percent:.3f}")
    else:
        print(f"[SKIP] STT WER gate skipped, status={stt_status}")

    if failures:
        print("Quality gates failed:")
        for item in failures:
            print(f"- {item}")
        return 1

    print("Quality gates passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
