from __future__ import annotations

import os
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Optional

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from prometheus_client import Counter, Histogram, start_http_server

from app.core.security import InMemoryRateLimiter, RateLimitConfig, require_api_token
from app.core.speech import STTResult, VoskRecognizer


STT_REQUESTS_TOTAL = Counter("stt_requests_total", "Total number of speech-to-text requests")
STT_ERRORS_TOTAL = Counter("stt_errors_total", "Total number of speech-to-text errors")
STT_LATENCY_SECONDS = Histogram(
    "stt_latency_seconds",
    "Latency of speech-to-text requests",
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

recognizer: Optional[VoskRecognizer] = None
MAX_AUDIO_BYTES = int(os.environ.get("MAX_AUDIO_BYTES", str(2 * 1024 * 1024)))
RATE_LIMITER = InMemoryRateLimiter(RateLimitConfig(requests=60, window_seconds=60))


@asynccontextmanager
async def lifespan(app: FastAPI):
    del app
    global recognizer
    recognizer = VoskRecognizer()
    start_http_server(9101)
    yield


app = FastAPI(title="STT Service", version="1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/stt/recognize")
def recognise_audio(
    file: UploadFile = File(...),
    x_client_id: str | None = Header(default=None, alias="X-Client-Id"),
    _: Annotated[None, Depends(require_api_token)] = None,
) -> dict[str, str | bool]:
    STT_REQUESTS_TOTAL.inc()
    if recognizer is None:
        raise HTTPException(status_code=503, detail="Recognizer not initialised")
    client_key = x_client_id or "unknown"
    if not RATE_LIMITER.allow(client_key):
        STT_ERRORS_TOTAL.inc()
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    start_time = time.perf_counter()
    with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
        temp_path = tmp.name
        copied = 0
        while True:
            chunk = file.file.read(64 * 1024)
            if not chunk:
                break
            copied += len(chunk)
            if copied > MAX_AUDIO_BYTES:
                STT_ERRORS_TOTAL.inc()
                raise HTTPException(status_code=413, detail="Audio file is too large")
            tmp.write(chunk)
    try:
        result: STTResult = recognizer.transcribe_from_wav(temp_path)
        if not result.success:
            STT_ERRORS_TOTAL.inc()
            raise HTTPException(status_code=400, detail=result.error or "STT failed")
        return {"text": result.text, "success": True}
    finally:
        STT_LATENCY_SECONDS.observe(time.perf_counter() - start_time)
        try:
            Path(temp_path).unlink()
        except Exception:
            pass
