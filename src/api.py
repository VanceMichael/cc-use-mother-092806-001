"""标准库 HTTP API。

路由风格 POST /<resource>/<action>，岗位取自 X-Role 头，
幂等键取自 X-Idempotency-Key 头（也可放 body）。
服务单例持有事件日志，所有请求共用一把锁保证决策原子。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .errors import PermissionDeniedError, ReliefError
from .journal import EventStore
from .service import ReliefService

# (method, path) -> (service 方法名, 必填字段, 可从路径提取的字段)
ROUTES: dict[tuple[str, str], tuple[str, list[str], list[str]]] = {
    ("POST", "/shelters/register"): ("register_shelter", ["shelter_id", "name", "capacity"], []),
    ("POST", "/shelters/capacity"): ("change_capacity", ["shelter_id", "new_capacity"], []),

    ("POST", "/people/receive"): ("receive_person", ["shelter_id", "name"], []),
    ("POST", "/people/verify"): ("verify_person", ["person_id"], []),
    ("POST", "/people/get"): ("get_person", ["person_id"], []),

    ("POST", "/families/declare"): ("declare_family", ["family_id", "members"], []),
    ("POST", "/families/link"): ("link_family_member", ["family_id", "person_id"], []),

    ("POST", "/profiles/merge"): ("merge_profiles", ["kept_id", "merged_id"], []),

    ("POST", "/batches/register"): ("register_batch", ["batch_id"], []),
    ("POST", "/batches/assign"): ("assign_to_batch", ["batch_id", "person_id"], []),

    ("POST", "/arrangements/request"): ("request_arrangement", ["kind", "person_id"], []),
    ("POST", "/arrangements/confirm"): ("confirm_arrangement", ["arrangement_id"], []),
    ("POST", "/arrangements/execute"): ("execute_arrangement", ["arrangement_id"], []),
    ("POST", "/arrangements/cancel"): ("cancel_arrangement", ["arrangement_id"], []),

    ("POST", "/health/record"): ("record_health", ["person_id", "condition"], []),

    ("POST", "/medication/receive"): ("receive_medication", ["shelter_id", "medication", "quantity"], []),
    ("POST", "/medication/dispense"): ("dispense_medication", ["person_id", "med_batch_id", "quantity"], []),

    ("POST", "/supplies/receive"): ("receive_supply", ["shelter_id", "item", "quantity"], []),
    ("POST", "/supplies/issue"): ("issue_supply", ["person_id", "supply_batch_id", "quantity", "receipt_key"], []),
    ("POST", "/supplies/return"): ("return_supply", ["original_issue_id", "quantity"], []),
    ("POST", "/supplies/reissue"): ("reissue_supply", ["person_id", "supply_batch_id", "quantity", "receipt_key"], []),

    ("POST", "/traces/open"): ("open_trace", ["person_id"], []),
    ("POST", "/traces/clue"): ("add_trace_clue", ["trace_id", "clue"], []),
    ("POST", "/traces/confirm"): ("confirm_trace_match", ["trace_id", "matched_person_id", "basis"], []),
    ("POST", "/traces/close"): ("close_trace", ["trace_id"], []),

    ("POST", "/reviews/resolve"): ("resolve_review", ["review_id", "resolution"], []),

    ("POST", "/ops/tick"): ("tick", [], []),
    ("POST", "/ops/recover"): ("recover", [], []),
}

FAMILY_TIMELINE = ("POST", "/families/timeline")


class ApiState:
    def __init__(self, journal_path: str):
        self.service = ReliefService(EventStore(journal_path))
        self.lock = threading.Lock()


def make_handler(state: ApiState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ReliefLedger/1.0"

        def log_message(self, *_args: Any) -> None:
            return

        def _send(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            self._send(405, {"error": "method_not_allowed", "message": "请使用 POST"})

        def do_POST(self) -> None:  # noqa: N802
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                payload = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise ReliefError("请求体必须是 JSON 对象", code="invalid_request")
                role = self.headers.get("X-Role") or payload.pop("role", None)
                if not role:
                    raise PermissionDeniedError("缺少 X-Role 岗位头")
                header_idem = self.headers.get("X-Idempotency-Key")
                if header_idem and "idem_key" not in payload:
                    payload["idem_key"] = header_idem
                status, result = self._dispatch(role, self.path, payload)
                self._send(status, result)
            except ReliefError as exc:
                self._send(exc.status, exc.to_dict())
            except json.JSONDecodeError:
                self._send(400, {"error": "invalid_request", "message": "JSON 无法解析"})
            except Exception as exc:  # noqa: BLE001
                self._send(500, {"error": "internal_error", "message": str(exc)})

        def _dispatch(self, role: str, path: str, payload: dict) -> tuple[int, Any]:
            # 家庭时点查询：family_id 必填，as_of 可选
            if (self.command, path) == FAMILY_TIMELINE:
                family_id = payload.get("family_id")
                if not family_id:
                    raise ReliefError("family_id 必填", code="invalid_request")
                with state.lock:
                    return 200, state.service.family_timeline(
                        role, family_id, ts=payload.get("as_of"))

            route = ROUTES.get((self.command, path))
            if route is None:
                raise ReliefError(f"未知路径 {path}", code="not_found", status=404)
            method_name, required, _ = route
            for field in required:
                if field not in payload:
                    raise ReliefError(f"缺少必填字段 {field}", code="invalid_request")
            method: Callable[..., Any] = getattr(state.service, method_name)
            kwargs = {k: v for k, v in payload.items() if k != "role"}
            with state.lock:
                return 200, method(role, **kwargs)

    return Handler


def serve(journal_path: str, host: str = "127.0.0.1", port: int = 8080) -> None:
    state = ApiState(journal_path)
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    print(f"灾害安置账本服务已启动: http://{host}:{port}  日志={journal_path}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
