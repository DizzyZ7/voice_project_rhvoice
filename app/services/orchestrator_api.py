from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
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
from app.integrations import IntegrationRuntime

STT_URL = os.environ.get("STT_URL", "http://stt-service:8000/stt/recognize")
TTS_URL = os.environ.get("TTS_URL", "http://tts-service:8001/tts/generate")
MQTT_HOST = os.environ.get("MQTT_HOST", "mqtt-broker")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
COMMAND_TRANSPORT = os.environ.get("COMMAND_TRANSPORT", "mqtt").strip().lower()
MAX_AUDIO_BYTES = int(os.environ.get("MAX_AUDIO_BYTES", str(2 * 1024 * 1024)))
RATE_LIMITER = InMemoryRateLimiter(RateLimitConfig(requests=60, window_seconds=60))
UPSTREAM_TIMEOUT_SECONDS = float(os.environ.get("UPSTREAM_TIMEOUT_SECONDS", "15"))
VOICE_API_TOKEN = os.environ.get("VOICE_API_TOKEN", "dev-token-change-me")
ALERT_DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("ALERT_DEFAULT_TIMEOUT_SECONDS", "30"))
ORC_HTTP_TRUST_ENV = os.environ.get("ORC_HTTP_TRUST_ENV", "").strip().lower() in {"1", "true", "yes", "on"}
BASE_DIR = Path(__file__).resolve().parents[2]
ORC_DB_PATH = Path(os.environ.get("ORC_DB_PATH", str(BASE_DIR / "data" / "orchestrator.db"))).resolve()
ORC_IDEMPOTENCY_TTL_SECONDS = int(os.environ.get("ORC_IDEMPOTENCY_TTL_SECONDS", "3600"))
INTEGRATION_STRICT = os.environ.get("INTEGRATION_STRICT", "").strip().lower() in {"1", "true", "yes", "on"}

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
ORC_ESCALATIONS_TOTAL = Counter("orc_escalations_total", "Total alert escalations")

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
    _load_alerts_from_db()
    start_http_server(9103)
    yield


app = FastAPI(title="Orchestrator Service", version="1.0", lifespan=lifespan)


def build_http_client(trust_env: bool = ORC_HTTP_TRUST_ENV) -> requests.Session:
    client = requests.Session()
    client.trust_env = trust_env
    adapter = HTTPAdapter(
        max_retries=Retry(
            total=2,
            backoff_factor=0.2,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset({"POST"}),
        )
    )
    client.mount("http://", adapter)
    client.mount("https://", adapter)
    return client


http_client = build_http_client()


@dataclass
class AlertState:
    alert_id: str
    message: str
    created_at: float
    timeout_seconds: int
    acknowledged: bool = False
    acknowledged_by: str | None = None
    escalated: bool = False


class AlertRaiseRequest(BaseModel):
    message: str
    timeout_seconds: int = ALERT_DEFAULT_TIMEOUT_SECONDS


ALERTS: dict[str, AlertState] = {}
ALERT_LOCK = threading.Lock()
DB_LOCK = threading.Lock()
LOCAL_COMMAND_EVENTS: list[dict[str, str]] = []
_ALERT_COUNTER = 0
integration_runtime = IntegrationRuntime()


def _db_connect() -> sqlite3.Connection:
    ORC_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(ORC_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_storage() -> None:
    with DB_LOCK:
        with _db_connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS alerts (
                    alert_id TEXT PRIMARY KEY,
                    message TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    timeout_seconds INTEGER NOT NULL,
                    acknowledged INTEGER NOT NULL,
                    acknowledged_by TEXT,
                    escalated INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS idempotency (
                    scope TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (scope, idempotency_key)
                )
                """
            )
            conn.commit()


def _row_to_alert(row: sqlite3.Row) -> AlertState:
    return AlertState(
        alert_id=row["alert_id"],
        message=row["message"],
        created_at=float(row["created_at"]),
        timeout_seconds=int(row["timeout_seconds"]),
        acknowledged=bool(row["acknowledged"]),
        acknowledged_by=row["acknowledged_by"],
        escalated=bool(row["escalated"]),
    )


def _load_alerts_from_db() -> None:
    global _ALERT_COUNTER
    _init_storage()
    with DB_LOCK:
        with _db_connect() as conn:
            rows = conn.execute("SELECT * FROM alerts").fetchall()
    alerts = {_row_to_alert(row).alert_id: _row_to_alert(row) for row in rows}
    with ALERT_LOCK:
        ALERTS.clear()
        ALERTS.update(alerts)
        max_id = 0
        for alert_id in ALERTS:
            if alert_id.startswith("alert-"):
                tail = alert_id.split("-", 1)[1]
                if tail.isdigit():
                    max_id = max(max_id, int(tail))
        _ALERT_COUNTER = max_id


def _persist_alert(alert: AlertState) -> None:
    with DB_LOCK:
        with _db_connect() as conn:
            conn.execute(
                """
                INSERT INTO alerts (alert_id, message, created_at, timeout_seconds, acknowledged, acknowledged_by, escalated)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(alert_id) DO UPDATE SET
                    message=excluded.message,
                    created_at=excluded.created_at,
                    timeout_seconds=excluded.timeout_seconds,
                    acknowledged=excluded.acknowledged,
                    acknowledged_by=excluded.acknowledged_by,
                    escalated=excluded.escalated
                """,
                (
                    alert.alert_id,
                    alert.message,
                    alert.created_at,
                    alert.timeout_seconds,
                    int(alert.acknowledged),
                    alert.acknowledged_by,
                    int(alert.escalated),
                ),
            )
            conn.commit()


def _get_idempotent_response(scope: str, idempotency_key: str) -> dict[str, str | int] | None:
    now = time.time()
    with DB_LOCK:
        with _db_connect() as conn:
            row = conn.execute(
                "SELECT response_json, created_at FROM idempotency WHERE scope=? AND idempotency_key=?",
                (scope, idempotency_key),
            ).fetchone()
            if row is None:
                return None
            age = now - float(row["created_at"])
            if age > ORC_IDEMPOTENCY_TTL_SECONDS:
                conn.execute("DELETE FROM idempotency WHERE scope=? AND idempotency_key=?", (scope, idempotency_key))
                conn.commit()
                return None
            return json.loads(row["response_json"])


def _save_idempotent_response(scope: str, idempotency_key: str, response: dict[str, str | int]) -> None:
    with DB_LOCK:
        with _db_connect() as conn:
            conn.execute(
                """
                INSERT INTO idempotency (scope, idempotency_key, response_json, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(scope, idempotency_key) DO UPDATE SET
                    response_json=excluded.response_json,
                    created_at=excluded.created_at
                """,
                (scope, idempotency_key, json.dumps(response, ensure_ascii=False), time.time()),
            )
            conn.commit()


def _normalize_idempotency_key(value: object) -> str | None:
    if isinstance(value, str):
        key = value.strip()
        return key or None
    return None


def reset_state_for_tests() -> None:
    global _ALERT_COUNTER
    _init_storage()
    with DB_LOCK:
        with _db_connect() as conn:
            conn.execute("DELETE FROM alerts")
            conn.execute("DELETE FROM idempotency")
            conn.commit()
    with ALERT_LOCK:
        ALERTS.clear()
        _ALERT_COUNTER = 0
    LOCAL_COMMAND_EVENTS.clear()


def _next_alert_id() -> str:
    global _ALERT_COUNTER
    _ALERT_COUNTER += 1
    return f"alert-{_ALERT_COUNTER}"


def _sweep_alerts() -> None:
    now = time.time()
    escalated_ids: list[str] = []
    with ALERT_LOCK:
        for alert in ALERTS.values():
            if alert.acknowledged or alert.escalated:
                continue
            if now - alert.created_at >= alert.timeout_seconds:
                alert.escalated = True
                escalated_ids.append(alert.alert_id)
    for alert_id in escalated_ids:
        _persist_alert(ALERTS[alert_id])
        ORC_ESCALATIONS_TOTAL.inc()


_load_alerts_from_db()


@app.get("/health")
async def health() -> dict[str, str]:
    _sweep_alerts()
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


def dispatch_command(topic: str, payload: str | None = None) -> None:
    if COMMAND_TRANSPORT == "mqtt":
        publish_command(topic, payload)
        return
    if COMMAND_TRANSPORT == "local":
        LOCAL_COMMAND_EVENTS.append({"topic": topic, "payload": payload or "1"})
        result = integration_runtime.execute_topic(topic, payload)
        if not result.ok and INTEGRATION_STRICT:
            raise RuntimeError(result.detail or "Integration dispatch failed")
        return
    raise RuntimeError(f"Unsupported COMMAND_TRANSPORT: {COMMAND_TRANSPORT}")


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
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    _: Annotated[None, Depends(require_api_token)] = None,
) -> dict[str, object]:
    _sweep_alerts()
    idempotency_key = _normalize_idempotency_key(idempotency_key)
    ORC_REQUESTS_TOTAL.inc()
    client_key = x_client_id or "unknown"
    if not RATE_LIMITER.allow(client_key):
        ORC_ERRORS_TOTAL.inc()
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    if idempotency_key:
        cached = _get_idempotent_response("process_audio", idempotency_key)
        if cached is not None:
            return cached

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
    if not isinstance(stt_confidence, (float, int)):
        stt_confidence = None
    spec, confidence = resolve_command_with_score(recognised_text)
    if spec and confidence >= COMMAND_CONFIDENCE_THRESHOLD:
        ORC_COMMANDS_TOTAL.labels(command=spec.key).inc()
        try:
            dispatch_command(spec.mqtt_topic)
        except Exception as exc:
            ORC_ERRORS_TOTAL.inc()
            raise HTTPException(status_code=502, detail=f"Command dispatch error: {exc}")

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
        response_payload = {"text": recognised_text, "command": spec.key, "status": "ok"}
        if idempotency_key:
            _save_idempotent_response("process_audio", idempotency_key, response_payload)
        return response_payload

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
    response_payload = {"text": recognised_text, "command": "unknown", "status": "unknown", "confidence": stt_confidence}
    if idempotency_key:
        _save_idempotent_response("process_audio", idempotency_key, response_payload)
    return response_payload


@app.post("/alerts/raise")
def raise_alert(
    request: AlertRaiseRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    _: Annotated[None, Depends(require_api_token)] = None,
) -> dict[str, str | int]:
    _sweep_alerts()
    idempotency_key = _normalize_idempotency_key(idempotency_key)
    if idempotency_key:
        cached = _get_idempotent_response("alerts_raise", idempotency_key)
        if cached is not None:
            return cached
    message = request.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Alert message is empty")
    timeout_seconds = max(1, request.timeout_seconds)
    with ALERT_LOCK:
        alert_id = _next_alert_id()
        created = AlertState(
            alert_id=alert_id,
            message=message,
            created_at=time.time(),
            timeout_seconds=timeout_seconds,
        )
        ALERTS[alert_id] = created
    _persist_alert(created)
    response_payload: dict[str, str | int] = {"alert_id": alert_id, "status": "pending", "timeout_seconds": timeout_seconds}
    if idempotency_key:
        _save_idempotent_response("alerts_raise", idempotency_key, response_payload)
    return response_payload


@app.post("/alerts/{alert_id}/ack")
def acknowledge_alert(
    alert_id: str,
    request: AlertAckRequest,
    _: Annotated[None, Depends(require_api_token)] = None,
) -> dict[str, str]:
    _sweep_alerts()
    persisted_alert: AlertState | None = None
    with ALERT_LOCK:
        alert = ALERTS.get(alert_id)
        if alert is None:
            raise HTTPException(status_code=404, detail="Alert not found")
        if alert.escalated:
            raise HTTPException(status_code=409, detail="Alert already escalated")
        alert.acknowledged = True
        alert.acknowledged_by = request.operator_id.strip() or "unknown"
        persisted_alert = alert
    if persisted_alert is not None:
        _persist_alert(persisted_alert)
    return {"alert_id": alert_id, "status": "acknowledged"}


@app.get("/alerts/pending")
def list_pending_alerts(
    _: Annotated[None, Depends(require_api_token)] = None,
) -> dict[str, list[dict[str, str | int | bool | float | None]]]:
    _sweep_alerts()
    with ALERT_LOCK:
        payload = [
            {
                "alert_id": alert.alert_id,
                "message": alert.message,
                "created_at": alert.created_at,
                "timeout_seconds": alert.timeout_seconds,
                "acknowledged": alert.acknowledged,
                "acknowledged_by": alert.acknowledged_by,
                "escalated": alert.escalated,
            }
            for alert in ALERTS.values()
            if not alert.acknowledged
        ]
    return {"alerts": payload}
