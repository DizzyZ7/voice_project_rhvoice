from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_MAP_PATH = Path(os.environ.get("INTEGRATION_MAP_PATH", str(BASE_DIR / "config" / "integration_map.example.json")))
DEFAULT_DRY_RUN = os.environ.get("INTEGRATION_DRY_RUN", "1").strip().lower() in {"1", "true", "yes", "on"}

logger = logging.getLogger("integrations")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    logger.addHandler(logging.StreamHandler())


@dataclass(frozen=True)
class IntegrationResult:
    ok: bool
    handler: str | None = None
    detail: str | None = None


class GPIOAdapter:
    def __init__(self, dry_run: bool = DEFAULT_DRY_RUN):
        self.dry_run = dry_run

    def set_pin(self, pin: int, value: int) -> IntegrationResult:
        if value not in {0, 1}:
            return IntegrationResult(ok=False, handler="gpio", detail="Value must be 0 or 1")
        if self.dry_run:
            logger.info("GPIO dry-run: pin=%s value=%s", pin, value)
            return IntegrationResult(ok=True, handler="gpio", detail="dry-run")
        # Preferred path on modern Linux: libgpiod python bindings.
        try:
            import gpiod  # type: ignore

            chip_name = os.environ.get("GPIO_CHIP", "gpiochip0")
            with gpiod.Chip(chip_name) as chip:
                line = chip.get_line(pin)
                line.request(consumer="voice_project", type=gpiod.LINE_REQ_DIR_OUT, default_vals=[value])
                line.set_value(value)
                line.release()
            return IntegrationResult(ok=True, handler="gpio", detail=f"gpiod pin {pin}={value}")
        except Exception:
            pass
        gpio_root = Path("/sys/class/gpio")
        if not gpio_root.exists():
            return IntegrationResult(ok=False, handler="gpio", detail="GPIO not available: gpiod and sysfs missing")
        try:
            gpio_path = gpio_root / f"gpio{pin}"
            if not gpio_path.exists():
                (gpio_root / "export").write_text(str(pin), encoding="ascii")
            (gpio_path / "direction").write_text("out", encoding="ascii")
            (gpio_path / "value").write_text(str(value), encoding="ascii")
            return IntegrationResult(ok=True, handler="gpio", detail=f"pin {pin}={value}")
        except Exception as exc:
            return IntegrationResult(ok=False, handler="gpio", detail=str(exc))


class ModbusAdapter:
    def __init__(self, dry_run: bool = DEFAULT_DRY_RUN):
        self.dry_run = dry_run

    def write_coil(self, host: str, port: int, unit: int, address: int, value: bool) -> IntegrationResult:
        if self.dry_run:
            logger.info(
                "Modbus dry-run: host=%s port=%s address=%s value=%s",
                host,
                port,
                address,
                int(value),
            )
            return IntegrationResult(ok=True, handler="modbus", detail="dry-run")
        try:
            from pymodbus.client import ModbusTcpClient  # type: ignore
        except Exception as exc:
            return IntegrationResult(
                ok=False,
                handler="modbus",
                detail=f"pymodbus unavailable: {exc}",
            )
        try:
            client = ModbusTcpClient(host=host, port=port, timeout=2.0)
            if not client.connect():
                return IntegrationResult(ok=False, handler="modbus", detail="connection failed")
            result = client.write_coil(address=address, value=value, slave=unit)
            client.close()
            if hasattr(result, "isError") and result.isError():
                return IntegrationResult(ok=False, handler="modbus", detail=f"modbus error: {result}")
            return IntegrationResult(ok=True, handler="modbus", detail=f"coil {address}={int(value)}")
        except Exception as exc:
            return IntegrationResult(ok=False, handler="modbus", detail=str(exc))


class IntegrationRuntime:
    def __init__(self, mapping_path: Path = DEFAULT_MAP_PATH, dry_run: bool = DEFAULT_DRY_RUN):
        self.mapping_path = Path(mapping_path)
        self.dry_run = dry_run
        self.gpio = GPIOAdapter(dry_run=dry_run)
        self.modbus = ModbusAdapter(dry_run=dry_run)
        self._mapping = self._load_mapping()

    def _load_mapping(self) -> dict[str, dict[str, Any]]:
        if not self.mapping_path.exists():
            logger.warning("Integration mapping file not found: %s", self.mapping_path)
            return {}
        try:
            payload = json.loads(self.mapping_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("Failed to load integration mapping: %s", exc)
            return {}
        raw = payload.get("topics", {})
        if not isinstance(raw, dict):
            return {}
        return {str(topic): spec for topic, spec in raw.items() if isinstance(spec, dict)}

    def execute_topic(self, topic: str, payload: str | None = None) -> IntegrationResult:
        spec = self._mapping.get(topic)
        if spec is None:
            return IntegrationResult(ok=False, detail=f"No integration mapping for topic: {topic}")
        handler = str(spec.get("handler", "")).strip().lower()
        if handler == "gpio":
            pin = int(spec.get("pin"))
            value = int(spec.get("value", 1 if (payload or "1") not in {"0", "false", "off"} else 0))
            return self.gpio.set_pin(pin, value)
        if handler == "modbus":
            host = str(spec.get("host", "127.0.0.1"))
            port = int(spec.get("port", 502))
            unit = int(spec.get("unit", 1))
            address = int(spec.get("address"))
            value = bool(int(spec.get("value", 1 if (payload or "1") not in {"0", "false", "off"} else 0)))
            return self.modbus.write_coil(host=host, port=port, unit=unit, address=address, value=value)
        return IntegrationResult(ok=False, detail=f"Unsupported integration handler: {handler}")
