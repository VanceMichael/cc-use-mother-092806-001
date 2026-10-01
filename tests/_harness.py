"""测试辅助：可控时钟与临时账本应用。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from src.disaster_relief.app import Application


class FakeClock:
    def __init__(self, start: int = 1_759_000_000_000) -> None:
        self.t = start

    def __call__(self) -> int:
        return self.t

    def advance(self, ms: int) -> None:
        self.t += ms

    def set(self, ms: int) -> None:
        self.t = ms


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.clock = FakeClock()
        self.app = Application(
            self.dir / "ledger.jsonl",
            clock=self.clock,
            run_startup_sweep=False,
            sweep_interval_ms=3_600_000,
        )
        self.calls = 0

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def cmd(
        self,
        name: str,
        payload: dict[str, Any] | None = None,
        *,
        role: str = "provincial_coordinator",
        command_id: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        self.calls += 1
        return self.app.commands.execute(
            name, payload, role=role, command_id=command_id, actor=actor
        )

    def restart_app(self, *, sweep: bool = False) -> Application:
        """模拟服务重启：从同一账本重建。"""
        return Application(
            self.dir / "ledger.jsonl",
            clock=self.clock,
            run_startup_sweep=sweep,
            sweep_interval_ms=3_600_000,
        )

    def seed_site(
        self,
        site_id: str = "S1",
        *,
        capacity: int = 20,
        dialysis: bool = False,
        medical: bool = False,
    ) -> None:
        self.cmd("register_site", {
            "site_id": site_id, "name": site_id, "capacity": capacity,
            "dialysis_capable": dialysis, "medical_capable": medical,
        })

    def sweep(self) -> dict[str, list[dict[str, Any]]]:
        return self.app.sweep_once()
