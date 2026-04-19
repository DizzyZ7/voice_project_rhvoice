from __future__ import annotations

import json
import os
import time
import logging
import threading
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated

import requests
from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from pydantic import BaseModel, Field
from paho.mqtt.publish import single as mqtt_publish
from prometheus_client import Counter, Gauge, Histogram, start_http_server
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
COMMAND_TRANSPORT = os.environ.get("COMMAND_TRANSPORT", "mqtt").strip().lower()
MAX_AUDIO_BYTES = int(os.environ.get("MAX_AUDIO_BYTES", str(2 * 1024 * 1024)))
RATE_LIMITER = InMemoryRateLimiter(RateLimitConfig(requests=60, window_seconds=60))
UPSTREAM_TIMEOUT_SECONDS = float(os.environ.get("UPSTREAM_TIMEOUT_SECONDS", "15"))
VOICE_API_TOKEN = os.environ.get("VOICE_API_TOKEN", "dev-token-change-me")
ALERT_ACK_TIMEOUT_SECONDS = int(os.environ.get("ALERT_ACK_TIMEOUT_SECONDS", "30"))
ALERT_MAX_ESCALATION_LEVEL = int(os.environ.get("ALERT_MAX_ESCALATION_LEVEL", "2"))
ALERT_TOPIC_PREFIX = os.environ.get("ALERT_TOPIC_PREFIX", "factory/alerts").strip().strip("/")
logger = logging.getLogger("orchestrator")

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

ALERTS_TOTAL = Counter("orc_alerts_total", "Total created alerts")
ALERTS_ACTIVE = Gauge("orc_alerts_active", "Current active alerts")
ALERT_ESCALATIONS_TOTAL = Counter("orc_alert_escalations_total", "Total alert escalations")

ALERT_STATE_NEW = "NEW"
ALERT_STATE_ANNOUNCED = "ANNOUNCED"
ALERT_STATE_ACKED = "ACKED"
ALERT_STATE_ESCALATED = "ESCALATED"
ALERT_STATE_CLOSED = "CLOSED"
ALERT_ACTIVE_STATES = {ALERT_STATE_NEW, ALERT_STATE_ANNOUNCED, ALERT_STATE_ESCALATED}


@dataclass
class AlertRecord:
    alert_id: str
    message: str
    severity: str
    source: str
    state: str
    created_at: float
    updated_at: float
    ack_timeout_seconds: int
    ack_deadline_at: float | None = None
    announced_at: float | None = None
    acknowledged_at: float | None = None
    acknowledged_by: str | None = None
    escalated_at: float | None = None
    escalation_level: int = 0
    closed_at: float | None = None
    closed_by: str | None = None
    close_reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "alert_id": self.alert_id,
            "message": self.message,
            "severity": self.severity,
            "source": self.source,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "ack_timeout_seconds": self.ack_timeout_seconds,
            "ack_deadline_at": self.ack_deadline_at,
            "announced_at": self.announced_at,
            "acknowledged_at": self.acknowledged_at,
            "acknowledged_by": self.acknowledged_by,
            "escalated_at": self.escalated_at,
            "escalation_level": self.escalation_level,
            "closed_at": self.closed_at,
            "closed_by": self.closed_by,
            "close_reason": self.close_reason,
        }


class AlertCreateRequest(BaseModel):
    message: str = Field(min_length=1, max_length=600)
    severity: str = Field(default="high", max_length=32)
    source: str = Field(default="ggs-simulator", max_length=64)
    ack_timeout_seconds: int | None = Field(default=None, ge=5, le=3600)
    auto_announce: bool = True


class AlertAckRequest(BaseModel):
    operator_id: str = Field(min_length=1, max_length=64)


class AlertCloseRequest(BaseModel):
    operator_id: str = Field(min_length=1, max_length=64)
    reason: str | None = Field(default=None, max_length=200)


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
alerts_store: dict[str, AlertRecord] = {}
alerts_lock = threading.Lock()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def publish_command(topic: str, payload: str | None = None) -> None:
    if COMMAND_TRANSPORT == "local":
        logger.info("Local transport: command=%s payload=%s", topic, payload or "1")
        return
    if COMMAND_TRANSPORT != "mqtt":
        raise RuntimeError(f"Unsupported COMMAND_TRANSPORT: {COMMAND_TRANSPORT}")

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


def _current_time() -> float:
    return time.time()


def _update_active_alert_metric() -> None:
    with alerts_lock:
        active = sum(1 for item in alerts_store.values() if item.state in ALERT_ACTIVE_STATES)
    ALERTS_ACTIVE.set(active)


def _publish_alert_event(event: str, alert: AlertRecord) -> None:
    topic = f"{ALERT_TOPIC_PREFIX}/{event}"
    payload = json.dumps(
        {
            "alert_id": alert.alert_id,
            "state": alert.state,
            "severity": alert.severity,
            "message": alert.message,
            "source": alert.source,
            "escalation_level": alert.escalation_level,
            "timestamp": _current_time(),
        },
        ensure_ascii=False,
    )
    publish_command(topic, payload=payload)


def _emit_tts_message(text: str, strict: bool = True) -> None:
    tts_start = time.perf_counter()
    try:
        headers = {"Authorization": f"Bearer {VOICE_API_TOKEN}"}
        response = http_client.post(
            TTS_URL,
            json={"text": text},
            timeout=UPSTREAM_TIMEOUT_SECONDS,
            headers=headers,
        )
        response.raise_for_status()
    except Exception as exc:
        ORC_ERRORS_TOTAL.inc()
        if strict:
            raise HTTPException(status_code=502, detail=f"TTS service error: {exc}")
        logger.warning("TTS announcement skipped due to error: %s", exc)
    finally:
        ORC_TTS_LATENCY_SECONDS.observe(time.perf_counter() - tts_start)


def _build_ack_prompt(alert: AlertRecord) -> str:
    return f"Тревога. {alert.message}. Требуется подтверждение оператора."


def _announce_alert(alert: AlertRecord, announce: bool = True, strict_tts: bool = False) -> None:
    _publish_alert_event("announced", alert)
    if announce:
        _emit_tts_message(_build_ack_prompt(alert), strict=strict_tts)


def _refresh_alerts(now: float | None = None) -> list[AlertRecord]:
    check_time = now or _current_time()
    escalated: list[AlertRecord] = []
    with alerts_lock:
        for alert in alerts_store.values():
            if alert.state not in {ALERT_STATE_ANNOUNCED, ALERT_STATE_ESCALATED}:
                continue
            if not alert.ack_deadline_at or check_time < alert.ack_deadline_at:
                continue
            if alert.escalation_level >= ALERT_MAX_ESCALATION_LEVEL:
                alert.ack_deadline_at = None
                alert.updated_at = check_time
                continue

            alert.escalation_level += 1
            alert.state = ALERT_STATE_ESCALATED
            alert.escalated_at = check_time
            alert.updated_at = check_time
            alert.ack_deadline_at = check_time + alert.ack_timeout_seconds
            escalated.append(alert)

    for alert in escalated:
        ALERT_ESCALATIONS_TOTAL.inc()
        try:
            _publish_alert_event("escalated", alert)
            _emit_tts_message(
                f"Эскалация тревоги уровня {alert.escalation_level}. {alert.message}. Подтвердите получение.",
                strict=False,
            )
        except Exception as exc:
            logger.warning("Не удалось отправить событие эскалации %s: %s", alert.alert_id, exc)
    _update_active_alert_metric()
    return escalated


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
) -> dict[str, str | float | None]:
    _refresh_alerts()
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
    except HTTPException:
        raise
    except Exception as exc:
        ORC_ERRORS_TOTAL.inc()
        raise HTTPException(status_code=502, detail=f"STT service error: {exc}")
    finally:
        ORC_STT_LATENCY_SECONDS.observe(time.perf_counter() - stt_start)

    try:
        stt_json = stt_response.json()
    except ValueError as exc:
        ORC_ERRORS_TOTAL.inc()
        raise HTTPException(status_code=502, detail=f"STT service returned invalid JSON: {exc}")
    recognised_text: str = stt_json.get("text", "").strip().lower()
    stt_confidence = stt_json.get("confidence")
    spec, confidence = resolve_command_with_score(recognised_text)
    if spec and confidence >= COMMAND_CONFIDENCE_THRESHOLD:
        ORC_COMMANDS_TOTAL.labels(command=spec.key).inc()
        try:
            publish_command(spec.mqtt_topic)
        except Exception as exc:
            ORC_ERRORS_TOTAL.inc()
            raise HTTPException(status_code=502, detail=f"MQTT publish error: {exc}")

        response_text = response_text_for_command(spec.key, temperature_value=23)
        _emit_tts_message(response_text, strict=True)
        return {"text": recognised_text, "command": spec.key, "status": "ok", "confidence": stt_confidence}

    _emit_tts_message(UNKNOWN_COMMAND_REPLY, strict=True)
    return {"text": recognised_text, "command": "unknown", "status": "unknown", "confidence": stt_confidence}


@app.post("/alerts/trigger")
def trigger_alert(
    request: AlertCreateRequest,
    _: Annotated[None, Depends(require_api_token)] = None,
) -> dict[str, object]:
    _refresh_alerts()
    now = _current_time()
    ack_timeout = request.ack_timeout_seconds or ALERT_ACK_TIMEOUT_SECONDS
    alert = AlertRecord(
        alert_id=uuid.uuid4().hex,
        message=request.message.strip(),
        severity=request.severity.strip().lower() or "high",
        source=request.source.strip() or "ggs-simulator",
        state=ALERT_STATE_ANNOUNCED if request.auto_announce else ALERT_STATE_NEW,
        created_at=now,
        updated_at=now,
        ack_timeout_seconds=ack_timeout,
        announced_at=now if request.auto_announce else None,
        ack_deadline_at=(now + ack_timeout) if request.auto_announce else None,
    )
    with alerts_lock:
        alerts_store[alert.alert_id] = alert
    ALERTS_TOTAL.inc()
    _publish_alert_event("created", alert)
    if request.auto_announce:
        _announce_alert(alert, announce=True, strict_tts=False)
    _update_active_alert_metric()
    return alert.to_dict()


@app.post("/alerts/{alert_id}/ack")
def acknowledge_alert(
    alert_id: str,
    request: AlertAckRequest,
    _: Annotated[None, Depends(require_api_token)] = None,
) -> dict[str, object]:
    _refresh_alerts()
    now = _current_time()
    with alerts_lock:
        alert = alerts_store.get(alert_id)
        if alert is None:
            raise HTTPException(status_code=404, detail=f"Alert not found: {alert_id}")
        if alert.state == ALERT_STATE_CLOSED:
            raise HTTPException(status_code=409, detail=f"Alert already closed: {alert_id}")
        alert.state = ALERT_STATE_ACKED
        alert.acknowledged_by = request.operator_id.strip()
        alert.acknowledged_at = now
        alert.ack_deadline_at = None
        alert.updated_at = now

    _publish_alert_event("acknowledged", alert)
    _emit_tts_message(f"Тревога {alert.alert_id} подтверждена оператором {alert.acknowledged_by}.", strict=False)
    _update_active_alert_metric()
    return alert.to_dict()


@app.post("/alerts/{alert_id}/close")
def close_alert(
    alert_id: str,
    request: AlertCloseRequest,
    _: Annotated[None, Depends(require_api_token)] = None,
) -> dict[str, object]:
    _refresh_alerts()
    now = _current_time()
    with alerts_lock:
        alert = alerts_store.get(alert_id)
        if alert is None:
            raise HTTPException(status_code=404, detail=f"Alert not found: {alert_id}")
        if alert.state == ALERT_STATE_CLOSED:
            return alert.to_dict()
        alert.state = ALERT_STATE_CLOSED
        alert.closed_by = request.operator_id.strip()
        alert.closed_at = now
        alert.close_reason = request.reason
        alert.ack_deadline_at = None
        alert.updated_at = now

    _publish_alert_event("closed", alert)
    _emit_tts_message(f"Тревога {alert.alert_id} закрыта оператором {alert.closed_by}.", strict=False)
    _update_active_alert_metric()
    return alert.to_dict()


@app.get("/alerts/{alert_id}")
def get_alert(
    alert_id: str,
    _: Annotated[None, Depends(require_api_token)] = None,
) -> dict[str, object]:
    _refresh_alerts()
    with alerts_lock:
        alert = alerts_store.get(alert_id)
        if alert is None:
            raise HTTPException(status_code=404, detail=f"Alert not found: {alert_id}")
        return alert.to_dict()


@app.get("/alerts")
def list_alerts(
    state: str | None = None,
    _: Annotated[None, Depends(require_api_token)] = None,
) -> dict[str, object]:
    _refresh_alerts()
    desired_state = state.strip().upper() if state else None
    with alerts_lock:
        items = [item.to_dict() for item in alerts_store.values()]
    if desired_state:
        items = [item for item in items if item["state"] == desired_state]
    items.sort(key=lambda item: float(item["created_at"]), reverse=True)
    return {"count": len(items), "items": items}
