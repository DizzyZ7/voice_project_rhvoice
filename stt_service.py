import sys

from app.services import stt_api as _impl

sys.modules[__name__] = _impl
