from __future__ import annotations

import io
import queue
import types
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from app.commands.registry import STOP_REPLY, UNKNOWN_COMMAND_REPLY
from app.commands.runtime import parse_and_execute
from app.core.security import InMemoryRateLimiter, RateLimitConfig, extract_bearer_token
from app.core.speech import RHVoiceTTS, setup_logger
from app.services import orchestrator_api, stt_api, tts_api
from app.ui.voice_command_gui import VoiceCommandGUI

logger = setup_logger("tests", "tests.log")
BASE_DIR = Path(__file__).resolve().parents[1]


def test_rhvoice_command_build():
    with mock.patch("app.core.speech.shutil.which") as which_mock, mock.patch("app.core.speech.subprocess.run") as run_mock:
        which_mock.side_effect = lambda x: x if x in {"RHVoice-test", "aplay"} else None
        tts = RHVoiceTTS(logger=logger)
        tts.speak("Проверка синтеза")
        assert run_mock.called


def test_command_routing():
    class FakeTTS:
        def __init__(self):
            self.messages = []

        def speak(self, text: str):
            self.messages.append(text)

    tts = FakeTTS()
    assert parse_and_execute("включи свет", tts) is True
    assert tts.messages[-1] == "Готово"
    assert parse_and_execute("выключи свет", tts) is True
    assert tts.messages[-1] == "Готово"
    assert parse_and_execute("какая температура", tts) is True
    assert tts.messages[-1] == "Сейчас температура 23.5 градусов"
    assert parse_and_execute("неизвестная команда", tts) is True
    assert tts.messages[-1] == UNKNOWN_COMMAND_REPLY
    assert parse_and_execute("стоп", tts) is False
    assert tts.messages[-1] == STOP_REPLY


def test_tts_path_is_confined_to_output_dir():
    original_dir = tts_api.TTS_OUTPUT_DIR
    original_engine = tts_api.tts_engine
    fake_engine = mock.Mock()
    try:
        tts_api.TTS_OUTPUT_DIR = (BASE_DIR / "tmp-tts-output").resolve()
        tts_api.tts_engine = fake_engine

        response = tts_api.generate(tts_api.TTSRequest(text="тест", save_to_file="nested/out.wav"))
        assert Path(response["file"]).is_relative_to(tts_api.TTS_OUTPUT_DIR)
        fake_engine.synthesize_to_wav.assert_called_once()

        try:
            tts_api.generate(tts_api.TTSRequest(text="тест", save_to_file="../../escape.wav"))
        except HTTPException as exc:
            assert exc.status_code == 400
        else:
            raise AssertionError("Path traversal must be rejected")
    finally:
        tts_api.TTS_OUTPUT_DIR = original_dir
        tts_api.tts_engine = original_engine


def test_stt_service_streams_upload_to_disk():
    fake_recognizer = mock.Mock()
    fake_recognizer.transcribe_from_wav.return_value = stt_api.STTResult(text="тест", success=True)

    upload = types.SimpleNamespace(file=io.BytesIO(b"wav-data"))
    original_recognizer = stt_api.recognizer
    try:
        stt_api.recognizer = fake_recognizer
        response = stt_api.recognise_audio(upload)
    finally:
        stt_api.recognizer = original_recognizer

    assert response == {"text": "тест", "success": True}
    fake_recognizer.transcribe_from_wav.assert_called_once()


def test_gui_restores_buttons_after_voice_stop():
    class FakeVar:
        def __init__(self):
            self.value = None

        def set(self, value):
            self.value = value

    class FakeButton:
        def __init__(self):
            self.state = None

        def config(self, state):
            self.state = state

    class FakeRoot:
        def after(self, delay, callback):
            self.delay = delay
            self.callback = callback

    gui = VoiceCommandGUI.__new__(VoiceCommandGUI)
    gui.root = FakeRoot()
    gui.queue = queue.Queue()
    gui.status_var = FakeVar()
    gui.start_btn = FakeButton()
    gui.stop_btn = FakeButton()
    gui.append_log = mock.Mock()
    gui.queue.put("Получена голосовая команда остановки")

    gui._poll_queue()

    assert gui.status_var.value == "Получена голосовая команда остановки"
    assert gui.start_btn.state == "normal"
    assert gui.stop_btn.state == "disabled"


def test_orchestrator_forwards_file_object_without_buffering_entire_upload():
    class NoReadFile(io.BytesIO):
        def read(self, *args, **kwargs):
            raise AssertionError("process_audio must not buffer the whole upload via read()")

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    upload_file = NoReadFile(b"wav-data")
    upload = types.SimpleNamespace(filename="sample.wav", file=upload_file, content_type="audio/wav")

    with (
        mock.patch("app.services.orchestrator_api.http_client.post") as post_mock,
        mock.patch("app.services.orchestrator_api.publish_command") as publish_mock,
    ):
        post_mock.side_effect = [FakeResponse({"text": "неизвестная команда"}), FakeResponse({"status": "ok"})]

        response = orchestrator_api.process_audio(upload)

    first_call = post_mock.call_args_list[0]
    forwarded_stream = first_call.kwargs["files"]["file"][1]
    assert getattr(forwarded_stream, "_raw", None) is upload_file
    assert response == {"text": "неизвестная команда", "command": "unknown", "status": "unknown"}
    publish_mock.assert_not_called()


def test_security_helpers():
    assert extract_bearer_token("Bearer token-123") == "token-123"
    assert extract_bearer_token("Basic token-123") is None

    limiter = InMemoryRateLimiter(RateLimitConfig(requests=2, window_seconds=60))
    assert limiter.allow("client-a")
    assert limiter.allow("client-a")
    assert not limiter.allow("client-a")
