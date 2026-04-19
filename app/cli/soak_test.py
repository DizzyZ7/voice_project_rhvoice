from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import requests


@dataclass(frozen=True)
class SoakStats:
    total_requests: int
    success_requests: int
    failed_requests: int
    min_latency_ms: float
    mean_latency_ms: float
    p95_latency_ms: float
    max_latency_ms: float


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


def summarize(latencies_ms: list[float], failures: int) -> SoakStats:
    ordered = sorted(latencies_ms)
    total = len(latencies_ms)
    mean = (sum(ordered) / total) if total else 0.0
    return SoakStats(
        total_requests=total,
        success_requests=total - failures,
        failed_requests=failures,
        min_latency_ms=round(ordered[0], 3) if ordered else 0.0,
        mean_latency_ms=round(mean, 3),
        p95_latency_ms=round(percentile(ordered, 0.95), 3),
        max_latency_ms=round(ordered[-1], 3) if ordered else 0.0,
    )


def run_soak(
    orchestrator_url: str,
    audio_path: Path,
    token: str,
    duration_minutes: int,
    requests_per_minute: int,
    timeout_seconds: float,
) -> SoakStats:
    interval_seconds = 60.0 / max(1, requests_per_minute)
    deadline = time.monotonic() + duration_minutes * 60
    latencies_ms: list[float] = []
    failures = 0
    session = requests.Session()
    session.trust_env = False
    headers = {"Authorization": f"Bearer {token}"}

    while time.monotonic() < deadline:
        started = time.perf_counter()
        ok = False
        try:
            with audio_path.open("rb") as fh:
                response = session.post(
                    orchestrator_url,
                    headers=headers,
                    files={"file": (audio_path.name, fh, "audio/wav")},
                    timeout=timeout_seconds,
                )
            ok = response.status_code == 200
        except Exception:
            ok = False
        elapsed = (time.perf_counter() - started) * 1000
        latencies_ms.append(elapsed)
        if not ok:
            failures += 1
        sleep_seconds = interval_seconds - (time.perf_counter() - started)
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    return summarize(latencies_ms, failures)


def main() -> None:
    parser = argparse.ArgumentParser(description="Long-running /process soak test for orchestrator service")
    parser.add_argument("--url", default="http://127.0.0.1:8002/process")
    parser.add_argument("--audio", required=True, help="Path to WAV mono 16kHz file")
    parser.add_argument("--token", default="dev-token-change-me")
    parser.add_argument("--minutes", type=int, default=5)
    parser.add_argument("--rpm", type=int, default=10, help="Requests per minute")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--output", default="reports/soak_report.json")
    args = parser.parse_args()

    stats = run_soak(
        orchestrator_url=args.url,
        audio_path=Path(args.audio),
        token=args.token,
        duration_minutes=args.minutes,
        requests_per_minute=args.rpm,
        timeout_seconds=args.timeout,
    )

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(asdict(stats), ensure_ascii=False, indent=2), encoding="utf-8")
    print(output_path)


if __name__ == "__main__":
    main()
