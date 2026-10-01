"""测试辅助：构造临时日志与可控时钟的服务。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.journal import EventStore
from src.service import ReliefService


class ServiceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.clock = FakeClock("2026-10-01T08:00:00+00:00")
        self.service = ReliefService(EventStore(self.dir / "events.log"), now=self.clock)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def new_service(self):
        """模拟重启：从同一份日志重建服务。"""
        return ReliefService(EventStore(self.dir / "events.log"), now=self.clock)

    # ---- 场景搭建快捷方法 ----------------------------------------------

    def seed_shelters(self) -> tuple[str, str]:
        self.service.register_shelter("coordinator", "SH_BKK", "曼谷廊曼安置点", capacity=10, area="曼谷")
        self.service.register_shelter("coordinator", "SH_NT", "暖武里安置点", capacity=10, area="暖武里")
        return "SH_BKK", "SH_NT"


class FakeClock:
    def __init__(self, start: str):
        self.value = start

    def __call__(self) -> str:
        return self.value

    def advance(self, hours: float = 0, **kwargs: float) -> None:
        from datetime import datetime, timedelta
        dt = datetime.fromisoformat(self.value)
        delta = timedelta(hours=hours, **kwargs)
        self.value = (dt + delta).isoformat()
