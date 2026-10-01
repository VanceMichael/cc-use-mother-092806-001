"""追加式事件日志。

每条记录是一行 JSON：{"seq": 序号, "type": 事件类型, "ts": 时间, "data": {...}}。
服务重启时逐条回放即可重建全部状态。崩溃可能只写坏最后一行，
打开日志时会检测并截掉不完整的尾部，保证后续追加不破坏既有记录。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from collections.abc import Callable

from .errors import ValidationError


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventStore:
    def __init__(self, path: str | Path, *, clock: Callable[[], str] = utc_now):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._lock = threading.Lock()
        self._seq = 0
        self._repair_torn_tail()

    # ---- 读取 ----------------------------------------------------------

    def events(self) -> Iterator[dict[str, Any]]:
        """按序号顺序产出全部事件。"""
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                yield json.loads(line)

    @property
    def latest_seq(self) -> int:
        return self._seq

    # ---- 写入 ----------------------------------------------------------

    def append(self, event_type: str, data: dict[str, Any], *, ts: str | None = None) -> dict[str, Any]:
        if not event_type or not isinstance(data, dict):
            raise ValidationError("事件类型与数据不能为空")
        with self._lock:
            event = {
                "seq": self._seq + 1,
                "type": event_type,
                "ts": ts or self.clock(),
                "data": data,
            }
            line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
            self._seq = event["seq"]
            return event

    # ---- 崩溃尾部修复 --------------------------------------------------

    def _repair_torn_tail(self) -> None:
        """校验既有日志：最后一行写坏（崩溃中断）时截掉它。"""
        if not self.path.exists():
            return
        good_offset = 0
        offset = 0
        last_good_seq = 0
        torn = False
        with self.path.open("rb") as handle:
            for raw in handle:
                try:
                    event = json.loads(raw.decode("utf-8"))
                    last_good_seq = int(event["seq"])
                except (ValueError, KeyError, TypeError):
                    torn = True
                    break
                offset += len(raw)
                good_offset = offset
        self._seq = last_good_seq
        if torn:
            with self.path.open("r+b") as handle:
                handle.truncate(good_offset)
