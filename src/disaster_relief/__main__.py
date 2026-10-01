"""服务入口。

用法::

    python -m disaster_relief --ledger data/ledger.jsonl --host 0.0.0.0 --port 8080

启动时重放账本并立即执行一次恢复巡检，随后后台每 60 秒巡检一次。
"""

from __future__ import annotations

import argparse
import logging
import signal

from .app import Application
from .api import make_server


def main() -> None:
    parser = argparse.ArgumentParser(description="灾害转移家庭与药品补给账本后端")
    parser.add_argument("--ledger", default="data/ledger.jsonl", help="事件账本路径")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--sweep-interval-ms", type=int, default=60_000)
    parser.add_argument("--no-startup-sweep", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("disaster_relief")

    app = Application(
        args.ledger,
        sweep_interval_ms=args.sweep_interval_ms,
        run_startup_sweep=not args.no_startup_sweep,
    )
    log.info("账本重放完成，当前序号 %d", app.commands.state.seq)
    app.start_background()
    log.info("恢复巡检已启动（每 %.0f 秒）", args.sweep_interval_ms / 1000)

    server = make_server(app, args.host, args.port)

    def shutdown(_signum: int, _frame: object) -> None:
        log.info("正在停止服务…")
        app.stop_background()
        server.shutdown()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log.info("HTTP API 监听 %s:%d", args.host, args.port)
    try:
        server.serve_forever()
    finally:
        app.stop_background()
        server.server_close()


if __name__ == "__main__":
    main()
