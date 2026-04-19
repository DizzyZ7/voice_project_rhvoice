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


def test_limited_reader_enforces_payload_limit():
    reader = orchestrator_api.LimitedReader(io.BytesIO(b"abc"), limit_bytes=2)

    assert reader.read(1) == b"a"
    with pytest.raises(HTTPException) as exc:
        reader.read()

    assert exc.value.status_code == 413


def test_orchestrator_process_audio_known_command(monkeypatch):
    upload = types.SimpleNamespace(filename="sample.wav", file=io.BytesIO(b"wav-data"), content_type="audio/wav")
    stt_payload = {"text": "включи свет"}

    monkeypatch.setattr(orchestrator_api, "RATE_LIMITER", types.SimpleNamespace(allow=lambda _: True))
    with (
        mock.patch("app.services.orchestrator_api.http_client.post") as post_mock,
        mock.patch("app.services.orchestrator_api.dispatch_command") as dispatch_mock,
    ):
        post_mock.side_effect = [DummyResponse(stt_payload), DummyResponse({"status": "ok"})]
        response = orchestrator_api.process_audio(upload)

    assert response == {
        "text": "включи свет",
        "command": "turn_on_light",
        "status": "ok",
    }
    assert post_mock.call_count == 2
    dispatch_mock.assert_called_once_with("factory/light/on")
