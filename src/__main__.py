"""启动入口：python3 -m src --journal data/relief.log --port 8080"""

from __future__ import annotations

import argparse

from .api import serve


def main() -> None:
    parser = argparse.ArgumentParser(description="灾害转移家庭与药品补给账本服务")
    parser.add_argument("--journal", default="data/relief.log", help="事件日志路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    serve(args.journal, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
