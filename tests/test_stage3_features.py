from __future__ import annotations

from pathlib import Path

from app.cli.soak_test import summarize
from app.cli.benchmark import word_error_rate
from app.core.speech import CachedTTSEngine, SimpleEnergyVAD, choose_vosk_model_path
from app.services import orchestrator_api


def test_word_error_rate_basics():
    assert word_error_rate("какая температура", "какая температура") == 0.0
    assert word_error_rate("какая температура", "какой температура") == 0.5


def test_simple_energy_vad():
    vad = SimpleEnergyVAD(rms_threshold=200, min_speech_ratio=0.1, frame_ms=20)
    silence = b"\x00\x00" * 16000
    speech_like = b"\xff\x7f" * 16000
    assert not vad.has_speech(silence, 16000)
    assert vad.has_speech(speech_like, 16000)


def test_cached_tts_engine_reuses_cached_wav(tmp_path: Path):
    class FakeEngine:
        engine_id = "fake-tts"

        def __init__(self):
            self.calls = 0

        def synthesize_to_wav(self, text: str, output_path: str | Path):
            self.calls += 1
            Path(output_path).write_bytes(f"wav:{text}".encode("utf-8"))
            return Path(output_path)

    fake = FakeEngine()
    cached = CachedTTSEngine(fake, cache_dir=tmp_path / "cache", cache_ttl_seconds=3600)
    out1 = tmp_path / "out1.wav"
    out2 = tmp_path / "out2.wav"

    cached.synthesize_to_wav("тест", out1)
    cached.synthesize_to_wav("тест", out2)

    assert fake.calls == 1
    assert out1.read_bytes() == out2.read_bytes()


def test_orchestrator_local_transport_dispatch():
    original_transport = orchestrator_api.COMMAND_TRANSPORT
    try:
        orchestrator_api.COMMAND_TRANSPORT = "local"
        orchestrator_api.LOCAL_COMMAND_EVENTS.clear()
        orchestrator_api.dispatch_command("factory/light/on", "1")
        assert orchestrator_api.LOCAL_COMMAND_EVENTS == [{"topic": "factory/light/on", "payload": "1"}]
    finally:
        orchestrator_api.COMMAND_TRANSPORT = original_transport


def test_orchestrator_mqtt_fallback_to_local_on_dispatch_failure():
    original_transport = orchestrator_api.COMMAND_TRANSPORT
    original_fail_open = orchestrator_api.ORC_DISPATCH_FAIL_OPEN
    original_strict = orchestrator_api.INTEGRATION_STRICT
    try:
        orchestrator_api.COMMAND_TRANSPORT = "mqtt"
        orchestrator_api.ORC_DISPATCH_FAIL_OPEN = True
        orchestrator_api.INTEGRATION_STRICT = False
        orchestrator_api.LOCAL_COMMAND_EVENTS.clear()
        with __import__("unittest").mock.patch(
            "app.services.orchestrator_api.publish_command",
            side_effect=OSError("getaddrinfo failed"),
        ):
            orchestrator_api.dispatch_command("factory/light/on", "1")
        assert orchestrator_api.LOCAL_COMMAND_EVENTS == [{"topic": "factory/light/on", "payload": "1"}]
    finally:
        orchestrator_api.COMMAND_TRANSPORT = original_transport
        orchestrator_api.ORC_DISPATCH_FAIL_OPEN = original_fail_open
        orchestrator_api.INTEGRATION_STRICT = original_strict


def test_alert_raise_and_ack_flow():
    orchestrator_api.reset_state_for_tests()
    created = orchestrator_api.raise_alert(orchestrator_api.AlertRaiseRequest(message="Тест тревоги", timeout_seconds=10))
    alert_id = created["alert_id"]

    pending = orchestrator_api.list_pending_alerts()
    assert any(item["alert_id"] == alert_id for item in pending["alerts"])

    ack = orchestrator_api.acknowledge_alert(alert_id, orchestrator_api.AlertAckRequest(operator_id="op-1"))
    assert ack["status"] == "acknowledged"


def test_orchestrator_http_client_disables_env_by_default():
    client = orchestrator_api.build_http_client()
    assert client.trust_env is False


def test_choose_vosk_model_path_prefers_models_folder(tmp_path: Path):
    model_path = tmp_path / "models" / "vosk-model-small-ru-0.22"
    model_path.mkdir(parents=True)
    selected = choose_vosk_model_path(tmp_path, None)
    assert selected == str(model_path)


def test_alert_escalates_after_timeout():
    orchestrator_api.reset_state_for_tests()
    created = orchestrator_api.raise_alert(orchestrator_api.AlertRaiseRequest(message="timeout test", timeout_seconds=1))
    alert_id = created["alert_id"]
    alert = orchestrator_api.ALERTS[alert_id]
    alert.created_at -= 2
    orchestrator_api._sweep_alerts()
    assert orchestrator_api.ALERTS[alert_id].escalated is True


def test_soak_summary_fields():
    stats = summarize([10.0, 20.0, 30.0], failures=1)
    assert stats.total_requests == 3
    assert stats.success_requests == 2
    assert stats.failed_requests == 1
    assert stats.p95_latency_ms >= 28.0


def test_alert_raise_idempotency_key_reuses_response():
    orchestrator_api.reset_state_for_tests()
    response_1 = orchestrator_api.raise_alert(
        orchestrator_api.AlertRaiseRequest(message="idempotent alert", timeout_seconds=10),
        idempotency_key="alert-key-1",
    )
    response_2 = orchestrator_api.raise_alert(
        orchestrator_api.AlertRaiseRequest(message="idempotent alert changed", timeout_seconds=20),
        idempotency_key="alert-key-1",
    )
    assert response_1 == response_2
    assert len(orchestrator_api.ALERTS) == 1
