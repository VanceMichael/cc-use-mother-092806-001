"""应用装配：把账本、命令、查询和恢复巡检接成一个服务实例。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .clock import now_ms
from .queries import QueryService
from .recovery import DEFAULT_THRESHOLDS, RecoveryLoop, RecoverySweeper
from .service import CommandService
from .store import EventStore


class Application:
    def __init__(
        self,
        ledger_path: str | Path,
        *,
        clock: Callable[[], int] = now_ms,
        sweep_interval_ms: int = 60_000,
        thresholds: dict[str, Any] | None = None,
        run_startup_sweep: bool = True,
    ) -> None:
        self.store = EventStore(ledger_path)
        self.clock = clock
        self.commands = CommandService(self.store, clock=clock)
        self.queries = QueryService(self.store, self.commands.state)
        self.sweeper = RecoverySweeper(
            self.store, self.commands.state, clock=clock,
            thresholds={**DEFAULT_THRESHOLDS, **(thresholds or {})},
        )
        self._loop = RecoveryLoop(self.sweep_once, sweep_interval_ms)
        self._last_sweep: dict[str, list[dict[str, Any]]] = {}
        if run_startup_sweep:  # 重启后立即接续预警/匹配/补给/待确认转运
            self.sweep_once()

    def sweep_once(self) -> dict[str, list[dict[str, Any]]]:
        # 与命令写入共用同一把锁，保证 ID 序列与状态折叠一致
        with self.commands._lock:
            self._last_sweep = self.sweeper.run()
        return self._last_sweep

    def start_background(self) -> None:
        self._loop.start()

    def stop_background(self) -> None:
        self._loop.stop()
