from __future__ import annotations

import json
import sys
import types
from pathlib import Path

from app.cli.stt_noise_benchmark import mix_with_noise
from app.integrations.runtime import IntegrationRuntime, ModbusAdapter


def test_integration_runtime_gpio_mapping(tmp_path: Path):
    mapping_path = tmp_path / "map.json"
    mapping_path.write_text(
        json.dumps(
            {
                "topics": {
                    "factory/light/on": {
                        "handler": "gpio",
                        "pin": 17,
                        "value": 1,
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    runtime = IntegrationRuntime(mapping_path=mapping_path, dry_run=True)
    result = runtime.execute_topic("factory/light/on", "1")
    assert result.ok is True
    assert result.handler == "gpio"


def test_integration_runtime_missing_mapping():
    runtime = IntegrationRuntime(mapping_path=Path("not_existing_mapping.json"), dry_run=True)
    result = runtime.execute_topic("factory/unknown", "1")
    assert result.ok is False


def test_mix_with_noise_changes_signal():
    clean = [1000] * 1600
    noise = [500] * 1600
    mixed = mix_with_noise(clean, noise, snr_db=10.0, rnd=__import__("random").Random(42))
    assert len(mixed) == len(clean)
    assert mixed != clean


def test_modbus_adapter_reports_missing_dependency(monkeypatch):
    monkeypatch.setitem(sys.modules, "pymodbus", None)
    monkeypatch.setitem(sys.modules, "pymodbus.client", None)
    adapter = ModbusAdapter(dry_run=False)
    result = adapter.write_coil(host="127.0.0.1", port=502, unit=1, address=1, value=True)
    assert result.ok is False
    assert result.handler == "modbus"
    assert "pymodbus unavailable" in (result.detail or "")


def test_modbus_adapter_real_path_with_mocked_client(monkeypatch):
    class FakeResponse:
        def isError(self):
            return False

    class FakeClient:
        def __init__(self, host: str, port: int, timeout: float):
            self.host = host
            self.port = port
            self.timeout = timeout
            self.closed = False

        def connect(self):
            return True

        def write_coil(self, address: int, value: bool, slave: int):
            assert address == 7
            assert value is True
            assert slave == 2
            return FakeResponse()

        def close(self):
            self.closed = True

    pymodbus_module = types.ModuleType("pymodbus")
    client_module = types.ModuleType("pymodbus.client")
    client_module.ModbusTcpClient = FakeClient
    monkeypatch.setitem(sys.modules, "pymodbus", pymodbus_module)
    monkeypatch.setitem(sys.modules, "pymodbus.client", client_module)

    adapter = ModbusAdapter(dry_run=False)
    result = adapter.write_coil(host="127.0.0.1", port=1502, unit=2, address=7, value=True)
    assert result.ok is True
    assert result.handler == "modbus"
