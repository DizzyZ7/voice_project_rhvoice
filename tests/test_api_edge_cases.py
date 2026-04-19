from __future__ import annotations

import io
import types
from unittest import mock

import pytest
from fastapi import HTTPException

from app.core import security
from app.services import orchestrator_api, stt_api, tts_api


class DummyResponse:
    def __init__(self, payload: dict[str, object]):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


def test_require_api_token_accepts_bearer_and_x_api_key(monkeypatch):
    monkeypatch.setattr(security, "AUTH_DISABLED", False)
    monkeypatch.setattr(security, "API_TOKEN", "secret-token")

    security.require_api_token(authorization="Bearer secret-token", x_api_key=None)
    security.require_api_token(authorization=None, x_api_key="secret-token")


def test_require_api_token_rejects_invalid_credentials(monkeypatch):
    monkeypatch.setattr(security, "AUTH_DISABLED", False)
    monkeypatch.setattr(security, "API_TOKEN", "secret-token")

    with pytest.raises(HTTPException) as exc:
        security.require_api_token(authorization="Bearer wrong", x_api_key=None)

    assert exc.value.status_code == 401
    assert exc.value.headers == {"WWW-Authenticate": "Bearer"}


def test_require_api_token_bypasses_when_auth_disabled(monkeypatch):
    monkeypatch.setattr(security, "AUTH_DISABLED", True)

    security.require_api_token(authorization=None, x_api_key=None)


def test_rate_limiter_drops_old_entries(monkeypatch):
    limiter = security.InMemoryRateLimiter(security.RateLimitConfig(requests=2, window_seconds=5))
    moments = iter([100.0, 101.0, 102.0, 106.0])
    monkeypatch.setattr(security.time, "monotonic", lambda: next(moments))

    assert limiter.allow("client")
    assert limiter.allow("client")
    assert not limiter.allow("client")
    assert limiter.allow("client")


def test_request_client_key_resolves_host_or_unknown():
    req = types.SimpleNamespace(client=types.SimpleNamespace(host="127.0.0.1"))

    assert security.request_client_key(req) == "127.0.0.1"
    assert security.request_client_key(types.SimpleNamespace(client=None)) == "unknown"
    assert security.request_client_key(None) == "unknown"


def test_tts_generate_rejects_empty_text(monkeypatch):
    fake_engine = mock.Mock()
    monkeypatch.setattr(tts_api, "tts_engine", fake_engine)
    monkeypatch.setattr(tts_api, "RATE_LIMITER", types.SimpleNamespace(allow=lambda _: True))

    with pytest.raises(HTTPException) as exc:
        tts_api.generate(tts_api.TTSRequest(text="   "))

    assert exc.value.status_code == 400
    fake_engine.speak.assert_not_called()


def test_tts_generate_rejects_too_long_text(monkeypatch):
    fake_engine = mock.Mock()
    monkeypatch.setattr(tts_api, "tts_engine", fake_engine)
    monkeypatch.setattr(tts_api, "RATE_LIMITER", types.SimpleNamespace(allow=lambda _: True))
    monkeypatch.setattr(tts_api, "MAX_TTS_TEXT_LENGTH", 4)

    with pytest.raises(HTTPException) as exc:
        tts_api.generate(tts_api.TTSRequest(text="слишком длинный"))

    assert exc.value.status_code == 413


def test_tts_generate_speak_mode(monkeypatch):
    fake_engine = mock.Mock()
    monkeypatch.setattr(tts_api, "tts_engine", fake_engine)
    monkeypatch.setattr(tts_api, "RATE_LIMITER", types.SimpleNamespace(allow=lambda _: True))

    response = tts_api.generate(tts_api.TTSRequest(text="Привет", speed=1.1, pitch=2.0, voice="anna", use_cache=False))

    assert response == {"status": "ok"}
    fake_engine.speak.assert_called_once_with("Привет", speed=1.1, pitch=2.0, voice="anna", use_cache=False)


def test_stt_recognise_audio_returns_503_without_recognizer(monkeypatch):
    monkeypatch.setattr(stt_api, "recognizer", None)

    upload = types.SimpleNamespace(file=io.BytesIO(b"wav"))
    with pytest.raises(HTTPException) as exc:
        stt_api.recognise_audio(upload)

    assert exc.value.status_code == 503


def test_stt_recognise_audio_returns_429_on_rate_limit(monkeypatch):
    monkeypatch.setattr(stt_api, "recognizer", mock.Mock())
    monkeypatch.setattr(stt_api, "RATE_LIMITER", types.SimpleNamespace(allow=lambda _: False))

    upload = types.SimpleNamespace(file=io.BytesIO(b"wav"))
    with pytest.raises(HTTPException) as exc:
        stt_api.recognise_audio(upload)

    assert exc.value.status_code == 429


def test_stt_recognise_audio_maps_failed_stt_result_to_400(monkeypatch):
    fake_recognizer = mock.Mock()
    fake_recognizer.transcribe_from_wav.return_value = stt_api.STTResult(text="", success=False, error="decode failed")
    monkeypatch.setattr(stt_api, "recognizer", fake_recognizer)
    monkeypatch.setattr(stt_api, "RATE_LIMITER", types.SimpleNamespace(allow=lambda _: True))

    upload = types.SimpleNamespace(file=io.BytesIO(b"wav"))
    with pytest.raises(HTTPException) as exc:
        stt_api.recognise_audio(upload)

    assert exc.value.status_code == 400
    assert "decode failed" in exc.value.detail


def test_limited_reader_enforces_payload_limit():
    reader = orchestrator_api.LimitedReader(io.BytesIO(b"abc"), limit_bytes=2)

    assert reader.read(1) == b"a"
    with pytest.raises(HTTPException) as exc:
        reader.read()

    assert exc.value.status_code == 413


def test_orchestrator_process_audio_known_command(monkeypatch):
    upload = types.SimpleNamespace(filename="sample.wav", file=io.BytesIO(b"wav-data"), content_type="audio/wav")
    stt_payload = {"text": "включи свет", "confidence": 0.95}

    monkeypatch.setattr(orchestrator_api, "RATE_LIMITER", types.SimpleNamespace(allow=lambda _: True))
    with (
        mock.patch("app.services.orchestrator_api._refresh_alerts") as refresh_mock,
        mock.patch("app.services.orchestrator_api.http_client.post", return_value=DummyResponse(stt_payload)) as post_mock,
        mock.patch("app.services.orchestrator_api.publish_command") as publish_mock,
        mock.patch("app.services.orchestrator_api._emit_tts_message") as tts_mock,
    ):
        response = orchestrator_api.process_audio(upload)

    assert response == {
        "text": "включи свет",
        "command": "turn_on_light",
        "status": "ok",
        "confidence": 0.95,
    }
    refresh_mock.assert_called_once()
    post_mock.assert_called_once()
    publish_mock.assert_called_once_with("factory/light/on")
    tts_mock.assert_called_once_with("Готово", strict=True)
