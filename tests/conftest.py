from __future__ import annotations

import sys
import types


if "sounddevice" not in sys.modules:
    sys.modules["sounddevice"] = types.SimpleNamespace(RawInputStream=None, sleep=lambda ms: None)

if "vosk" not in sys.modules:
    class DummyRecognizer:
        def __init__(self, *args, **kwargs):
            pass

        def AcceptWaveform(self, data):
            return False

        def Result(self):
            return "{}"

        def FinalResult(self):
            return '{"text":"тест"}'

    sys.modules["vosk"] = types.SimpleNamespace(Model=lambda path: object(), KaldiRecognizer=DummyRecognizer)
