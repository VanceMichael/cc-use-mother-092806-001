"""时间来源与时间线工具。

业务时间一律使用毫秒级 Unix 时间戳整数，便于按任一时点回放。
"""

from __future__ import annotations

import time
from datetime import datetime, timezone


def now_ms() -> int:
    return int(time.time() * 1000)


def to_iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
