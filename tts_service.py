import sys

from app.services import tts_api as _impl

sys.modules[__name__] = _impl
