from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel
from prometheus_client import Counter, Histogram, start_http_server

from app.core.security import InMemoryRateLimiter, RateLimitConfig, require_api_token
from app.core.speech import SpeechSynthesizer, create_tts_engine


TTS_REQUESTS_TOTAL = Counter("tts_requests_total", "Total number of text-to-speech requests")
TTS_ERRORS_TOTAL = Counter("tts_errors_total", "Total number of text-to-speech errors")
TTS_LATENCY_SECONDS = Histogram(
    "tts_latency_seconds",
    "Latency of text-to-speech requests",
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)


class TTSRequest(BaseModel):
    text: str
    save_to_file: Optional[str] = None
    speed: float = 1.0
    pitch: float = 0.0
    voice: Optional[str] = None
    use_cache: bool = True


tts_engine: Optional[SpeechSynthesizer] = None
TTS_OUTPUT_DIR = Path(os.environ.get("TTS_OUTPUT_DIR", "/tmp/tts-output")).resolve()
MAX_TTS_TEXT_LENGTH = int(os.environ.get("MAX_TTS_TEXT_LENGTH", "400"))
RATE_LIMITER = InMemoryRateLimiter(RateLimitConfig(requests=60, window_seconds=60))


@asynccontextmanager
async def lifespan(app: FastAPI):
    del app
    global tts_engine
    tts_engine = create_tts_engine()
    start_http_server(9102)
    yield


app = FastAPI(title="TTS Service", version="1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def resolve_output_path(raw_path: str) -> Path:
    candidate = Path(raw_path)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (TTS_OUTPUT_DIR / candidate).resolve()

    try:
        resolved.relative_to(TTS_OUTPUT_DIR)
    except ValueError:
        raise HTTPException(status_code=400, detail="save_to_file must stay within the configured output directory")
    return resolved


@app.post("/tts/generate")
def generate(
    request: TTSRequest,
    x_client_id: str | None = Header(default=None, alias="X-Client-Id"),
    _: Annotated[None, Depends(require_api_token)] = None,
) -> dict[str, str]:
    TTS_REQUESTS_TOTAL.inc()
    if tts_engine is None:
        raise HTTPException(status_code=503, detail="TTS engine not initialised")
    client_key = x_client_id or "unknown"
    if not RATE_LIMITER.allow(client_key):
        TTS_ERRORS_TOTAL.inc()
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    if not request.text or not request.text.strip():
        TTS_ERRORS_TOTAL.inc()
        raise HTTPException(status_code=400, detail="Text is empty")
    if len(request.text) > MAX_TTS_TEXT_LENGTH:
        TTS_ERRORS_TOTAL.inc()
        raise HTTPException(status_code=413, detail="Text is too long")
    start_time = time.perf_counter()
    try:
        if request.save_to_file:
            path = resolve_output_path(request.save_to_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            tts_engine.synthesize_to_wav(
                request.text,
                path,
                speed=request.speed,
                pitch=request.pitch,
                voice=request.voice,
                use_cache=request.use_cache,
            )
            return {"status": "ok", "file": str(path)}
        tts_engine.speak(
            request.text,
            speed=request.speed,
            pitch=request.pitch,
            voice=request.voice,
            use_cache=request.use_cache,
        )
        return {"status": "ok"}
    except HTTPException:
        raise
    except Exception as exc:
        TTS_ERRORS_TOTAL.inc()
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        TTS_LATENCY_SECONDS.observe(time.perf_counter() - start_time)
