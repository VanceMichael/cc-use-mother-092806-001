"""基于标准库 ``http.server`` 的 JSON HTTP API。

无第三方依赖。约定：
- ``X-Actor-Role`` 必填（履职岗位）
- ``X-Command-Id`` 可选，携带同一值的重复命令返回首次决定
- POST ``/commands/<name>`` 执行命令
- GET ``/queries/...`` 只读查询
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .app import Application
from .clock import to_iso
from .errors import DomainError
from .permissions import require_role

_JSON_ENCODING = "application/json; charset=utf-8"


def _json_default(value: Any) -> Any:
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"不可序列化的类型：{type(value)!r}")


def _enrich(value: Any) -> Any:
    """输出中把毫秒时间戳附上 ISO 形式，便于值班人员阅读。"""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            out[key] = _enrich(item)
            if key.endswith("_at") and isinstance(item, int):
                out[key + "_iso"] = to_iso(item)
        return out
    if isinstance(value, list):
        return [_enrich(item) for item in value]
    return value


class ApiHandler(BaseHTTPRequestHandler):
    app: Application  # 由 make_server 注入到类属性

    server_version = "DisasterReliefLedger/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静日志
        return

    # ------------------------------------------------------------------

    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(_enrich(body), ensure_ascii=False, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", _JSON_ENCODING)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            from .errors import ValidationError
            raise ValidationError("请求体不是合法 JSON")
        if not isinstance(body, dict):
            from .errors import ValidationError
            raise ValidationError("请求体必须是 JSON 对象")
        return body

    def _role(self) -> str:
        return require_role(self.headers.get("X-Actor-Role"))

    # ------------------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802
        try:
            path = urlsplit(self.path).path
            if not path.startswith("/commands/"):
                self._send_json(404, {"error": "not_found", "message": f"未知路径：{path}"})
                return
            command = path.removeprefix("/commands/")
            role = self._role()
            payload = self._read_body()
            command_id = self.headers.get("X-Command-Id") or payload.pop("_command_id", None)
            actor = self.headers.get("X-Actor-Id")
            result = self.app.commands.execute(
                command, payload, role=role,
                command_id=command_id, actor=actor,
            )
            status = 202 if result.get("status") == "manual_review" else 201
            if result.get("replayed"):
                status = 200
            self._send_json(status, result)
        except DomainError as exc:
            self._send_json(exc.http_status, exc.to_dict())
        except Exception as exc:  # noqa: BLE001
            self._send_json(500, {"error": "internal_error", "message": str(exc)})

    def do_GET(self) -> None:  # noqa: N802
        try:
            split = urlsplit(self.path)
            path, query = split.path, parse_qs(split.query)
            role = self._role()
            # 与命令写入互斥，避免读到多事件命令应用到一半的中间态
            with self.app.commands._lock:
                body = self._route_get(path, query, role)
            self._send_json(200, body)
        except DomainError as exc:
            self._send_json(exc.http_status, exc.to_dict())
        except Exception as exc:  # noqa: BLE001
            self._send_json(500, {"error": "internal_error", "message": str(exc)})

    # ------------------------------------------------------------------

    @staticmethod
    def _one(query: dict[str, list[str]], key: str, default: str | None = None) -> str | None:
        values = query.get(key)
        return values[0] if values else default

    @staticmethod
    def _int_param(query: dict[str, list[str]], key: str) -> int | None:
        raw = ApiHandler._one(query, key)
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            from .errors import ValidationError
            raise ValidationError(f"参数 {key} 必须是整数", details={"parameter": key})

    def _route_get(
        self, path: str, query: dict[str, list[str]], role: str
    ) -> dict[str, Any]:
        q = self.app.queries
        segments = [s for s in path.split("/") if s]

        if path == "/health":
            return {"status": "ok", "seq": self.app.commands.state.seq}

        if path == "/queries/families":
            return {"families": q.families()}
        if len(segments) == 3 and segments[:2] == ["queries", "families"]:
            family_id = segments[2]
            return q.family_view(family_id, role)
        if len(segments) == 4 and segments[:2] == ["queries", "families"] and segments[3] == "timeline":
            return q.family_timeline(
                segments[2], role,
                at_seq=self._int_param(query, "at_seq"),
                at_ts=self._int_param(query, "at_ts"),
            )

        if path == "/queries/persons":
            from .errors import NotFoundError
            person_id = self._one(query, "person_id", "")
            if not person_id:
                from .errors import ValidationError
                raise ValidationError("需要 person_id 参数")
            return q.person_view(person_id, role)

        if path == "/queries/sites":
            return {"sites": q.sites_overview()}
        if len(segments) == 3 and segments[:2] == ["queries", "sites"]:
            return q.site_view(segments[2])

        if path == "/queries/transports":
            return {"transports": q.transports(self._one(query, "status"))}
        if path == "/queries/traces":
            return {"traces": q.traces(self._one(query, "status"), role)}
        if len(segments) == 3 and segments[:2] == ["queries", "traces"]:
            return q.trace_view(segments[2], role)
        if path == "/queries/reviews":
            return {"reviews": q.reviews(self._one(query, "status"))}
        if path == "/queries/alerts":
            return {"alerts": q.alerts(self._one(query, "status"))}
        if path == "/queries/med-batches":
            return {"med_batches": q.med_batches(self._one(query, "status"))}
        if path == "/queries/medication-due":
            lead = self._int_param(query, "lead_ms") or 2 * 86_400_000
            return {"due": q.medication_due(role, now=self.app.clock(), lead_ms=lead)}
        if len(segments) == 4 and segments[:2] == ["queries", "persons"] and segments[3] == "distributions":
            return {"distributions": q.person_distributions(segments[2])}

        if path == "/queries/events":
            # 原始事件流含医疗与儿童敏感字段，仅全局履职岗位可检视
            if role not in {"provincial_coordinator", "supervisor"}:
                from .errors import PermissionDeniedError
                raise PermissionDeniedError("原始事件流仅协调员或值班主管可检视")
            # 账本只读检视
            after = self._int_param(query, "after_seq") or 0
            limit = self._int_param(query, "limit") or 200
            records = self.app.store.records()[after:after + limit]
            return {"events": records, "count": len(records)}

        from .errors import NotFoundError
        raise NotFoundError(f"未知查询：{path}")


def make_server(app: Application, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    handler = type("BoundApiHandler", (ApiHandler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    # 命令写入在服务层已加锁；查询直接读，事件追加是锁内原子替换
    server.allow_reuse_address = True
    return server
