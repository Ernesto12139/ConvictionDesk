"""Clock boundary for production time and deterministic runs."""

from collections.abc import Callable
from datetime import datetime, timezone


Clock = Callable[[], datetime]


def system_utc_now() -> datetime:
    return datetime.now(timezone.utc)
