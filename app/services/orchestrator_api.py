from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from typing import Annotated

import requests
from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from paho.mqtt.publish import single as mqtt_publish
from prometheus_client import Counter, Histogram, start_http_server
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.commands.registry import (
    COMMAND_CONFIDENCE_THRESHOLD,
    UNKNOWN_COMMAND_REPLY,
    resolve_command_with_score,
    response_text_for_command,
)
from app.core.security import InMemoryRateLimiter, RateLimitConfig, require_api_token

STT_URL = os.environ.get("STT_URL", "http://stt-service:8000/stt/recognize")
TTS_URL = os.environ.get("TTS_URL", "http://tts-service:8001/tts/generate")
MQTT_HOST = os.environ.get("MQTT_HOST", "mqtt-broker")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MAX_AUDIO_BYTES = int(os.environ.get("MAX_AUDIO_BYTES", str(2 * 1024 * 1024)))
RATE_LIMITER = InMemoryRateLimiter(RateLimitConfig(requests=60, window_seconds=60))
UPSTREAM_TIMEOUT_SECONDS = float(os.environ.get("UPSTREAM_TIMEOUT_SECONDS", "15"))
VOICE_API_TOKEN = os.environ.get("VOICE_API_TOKEN", "dev-token-change-me")

ORC_REQUESTS_TOTAL = Counter("orc_requests_total", "Total /process requests")
ORC_ERRORS_TOTAL = Counter("orc_errors_total", "Total orchestrator errors")
ORC_COMMANDS_TOTAL = Counter(
    "orc_commands_total",
    "Total commands executed",
    labelnames=("command",),
)
ORC_STT_LATENCY_SECONDS = Histogram(
    "orc_stt_latency_seconds",
    "Latency of STT service calls",
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
ORC_TTS_LATENCY_SECONDS = Histogram(
    "orc_tts_latency_seconds",
    "Latency of TTS service calls",
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    del app
    start_http_server(9103)
    yield


app = FastAPI(title="Orchestrator Service", version="1.0", lifespan=lifespan)
http_client = requests.Session()
http_client.mount(
    "http://",
    HTTPAdapter(
        max_retries=Retry(
            total=2,
            backoff_factor=0.2,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset({"POST"}),
        )
    ),
)
http_client.mount(
    "https://",
    HTTPAdapter(
        max_retries=Retry(
            total=2,
            backoff_factor=0.2,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset({"POST"}),
        )
    ),
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def publish_command(topic: str, payload: str | None = None) -> None:
    auth = None
    if os.environ.get("MQTT_USERNAME"):
        auth = {"username": os.environ["MQTT_USERNAME"], "password": os.environ.get("MQTT_PASSWORD", "")}
    tls = None
    if os.environ.get("MQTT_TLS", "").lower() in {"1", "true", "yes", "on"}:
        tls = {
            "ca_certs": os.environ.get("MQTT_TLS_CA_CERT"),
            "certfile": os.environ.get("MQTT_TLS_CERTFILE"),
            "keyfile": os.environ.get("MQTT_TLS_KEYFILE"),
        }
        tls = {k: v for k, v in tls.items() if v}
    mqtt_publish(topic, payload or "1", hostname=MQTT_HOST, port=MQTT_PORT, auth=auth, tls=tls)


class LimitedReader:
    def __init__(self, raw, limit_bytes: int):
        self._raw = raw
        self._limit_bytes = limit_bytes
        self._read_bytes = 0

    def read(self, size: int = -1) -> bytes:
        data = self._raw.read(size)
        self._read_bytes += len(data)
        if self._read_bytes > self._limit_bytes:
            raise HTTPException(status_code=413, detail="Audio file is too large")
        return data

    def __getattr__(self, name):
        return getattr(self._raw, name)


@app.post("/process")
def process_audio(
    file: UploadFile = File(...),
    x_client_id: str | None = Header(default=None, alias="X-Client-Id"),
    _: Annotated[None, Depends(require_api_token)] = None,
) -> dict[str, str]:
    ORC_REQUESTS_TOTAL.inc()
    client_key = x_client_id or "unknown"
    if not RATE_LIMITER.allow(client_key):
        ORC_ERRORS_TOTAL.inc()
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    stt_start = time.perf_counter()
    try:
        file.file.seek(0)
        limited_stream = LimitedReader(file.file, MAX_AUDIO_BYTES)
        files = {"file": (file.filename or "audio.wav", limited_stream, file.content_type or "application/octet-stream")}
        headers = {"Authorization": f"Bearer {VOICE_API_TOKEN}"}
        stt_response = http_client.post(STT_URL, files=files, timeout=UPSTREAM_TIMEOUT_SECONDS, headers=headers)
        stt_response.raise_for_status()
    except Exception as exc:
        ORC_ERRORS_TOTAL.inc()
        raise HTTPException(status_code=502, detail=f"STT service error: {exc}")
    finally:
        ORC_STT_LATENCY_SECONDS.observe(time.perf_counter() - stt_start)

    stt_json = stt_response.json()
    recognised_text: str = stt_json.get("text", "").strip().lower()
    spec, confidence = resolve_command_with_score(recognised_text)
    if spec and confidence >= COMMAND_CONFIDENCE_THRESHOLD:
        ORC_COMMANDS_TOTAL.labels(command=spec.key).inc()
        try:
            publish_command(spec.mqtt_topic)
        except Exception as exc:
            ORC_ERRORS_TOTAL.inc()
            raise HTTPException(status_code=502, detail=f"MQTT publish error: {exc}")

        tts_start = time.perf_counter()
        try:
            response_text = response_text_for_command(spec.key, temperature_value=23)
            headers = {"Authorization": f"Bearer {VOICE_API_TOKEN}"}
            tts_response = http_client.post(
                TTS_URL,
                json={"text": response_text},
                timeout=UPSTREAM_TIMEOUT_SECONDS,
                headers=headers,
            )
            tts_response.raise_for_status()
        except Exception as exc:
            ORC_ERRORS_TOTAL.inc()
            raise HTTPException(status_code=502, detail=f"TTS service error: {exc}")
        finally:
            ORC_TTS_LATENCY_SECONDS.observe(time.perf_counter() - tts_start)
        return {"text": recognised_text, "command": spec.key, "status": "ok"}

    tts_start = time.perf_counter()
    try:
        headers = {"Authorization": f"Bearer {VOICE_API_TOKEN}"}
        tts_response = http_client.post(
            TTS_URL,
            json={"text": UNKNOWN_COMMAND_REPLY},
            timeout=UPSTREAM_TIMEOUT_SECONDS,
            headers=headers,
        )
        tts_response.raise_for_status()
    except Exception as exc:
        ORC_ERRORS_TOTAL.inc()
        raise HTTPException(status_code=502, detail=f"TTS service error: {exc}")
    finally:
        ORC_TTS_LATENCY_SECONDS.observe(time.perf_counter() - tts_start)
    return {"text": recognised_text, "command": "unknown", "status": "unknown"}
