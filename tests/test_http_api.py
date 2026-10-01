"""HTTP API 端到端测试：真实端口、标准库客户端。"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

from src.disaster_relief.api import make_server

try:
    from ._harness import ServiceTestCase
except ImportError:  # 以 tests 为发现目录运行时
    from _harness import ServiceTestCase


class ApiClient:
    def __init__(self, base_url: str, role: str = "provincial_coordinator") -> None:
        self.base_url = base_url
        self.role = role

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        role: str | None = None,
        command_id: str | None = None,
    ):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.base_url + path, data=data, method=method,
            headers={"Content-Type": "application/json",
                     "X-Actor-Role": role or self.role},
        )
        if command_id:
            req.add_header("X-Command-Id", command_id)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class HttpApiTest(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.server = make_server(self.app, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.api = ApiClient(f"http://127.0.0.1:{self.port}")

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        super().tearDown()

    def test_health(self) -> None:
        status, body = self.api.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_missing_role_is_unauthorized(self) -> None:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/health")
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("应当拒绝无岗位请求")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 401)

    def test_command_and_query_roundtrip(self) -> None:
        status, body = self.api.request("POST", "/commands/register_site", {
            "site_id": "S1", "capacity": 10,
        })
        self.assertEqual(status, 201)

        status, body = self.api.request(
            "POST", "/commands/receive_person",
            {"person_id": "P1", "site_id": "S1", "is_minor": True, "approx_age": 8},
            role="rescue_worker",
        )
        self.assertEqual(status, 201)

        status, body = self.api.request("GET", "/queries/sites")
        self.assertEqual(status, 200)
        site = next(s for s in body["sites"] if s["site_id"] == "S1")
        self.assertEqual(site["occupancy"], 1)

        # 脱敏：安置点工作人员看不到儿童字段
        status, body = self.api.request(
            "GET", "/queries/persons?person_id=P1", role="site_worker")
        self.assertEqual(body["is_minor"], "【受限】")

    def test_idempotent_command_via_header(self) -> None:
        self.api.request("POST", "/commands/register_site", {"site_id": "S1", "capacity": 5})
        self.api.request("POST", "/commands/register_supply_item",
                         {"item_id": "RICE", "name": "米"})
        self.api.request("POST", "/commands/receive_person",
                         {"person_id": "P1", "site_id": "S1"}, role="rescue_worker")
        self.api.request("POST", "/commands/allocate_supplies",
                         {"site_id": "S1", "item_id": "RICE", "quantity": 5})
        dist_payload = {"site_id": "S1", "person_id": "P1", "item_id": "RICE", "quantity": 1}
        s1, b1 = self.api.request("POST", "/commands/distribute_supplies", dist_payload,
                                  role="site_worker", command_id="RCPT-1")
        s2, b2 = self.api.request("POST", "/commands/distribute_supplies", dist_payload,
                                  role="site_worker", command_id="RCPT-1")
        self.assertEqual(s2, 200)
        self.assertTrue(b2["replayed"])
        self.assertEqual(b1["seq"], b2["seq"])

    def test_conflict_returns_409_and_review_returns_202(self) -> None:
        self.api.request("POST", "/commands/register_site", {"site_id": "S1", "capacity": 1})
        self.api.request("POST", "/commands/register_site", {"site_id": "S2", "capacity": 1})
        self.api.request("POST", "/commands/receive_person",
                         {"person_id": "P1", "site_id": "S1"}, role="rescue_worker")
        # 容量冲突
        self.api.request("POST", "/commands/receive_person",
                         {"person_id": "P2", "site_id": "S1"}, role="rescue_worker")
        status, _ = self.api.request("POST", "/commands/admit_person",
                                     {"person_id": "P2", "site_id": "S1"},
                                     role="rescue_worker")
        # receive 已占容量；admit 再次占用时容量满 -> 409
        self.assertEqual(status, 409)

        # 重复接收 -> 202 人工复核
        status, body = self.api.request("POST", "/commands/receive_person",
                                        {"person_id": "P1", "site_id": "S2"},
                                        role="rescue_worker")
        self.assertEqual(status, 202)
        self.assertEqual(body["status"], "manual_review")

    def test_family_timeline_endpoint(self) -> None:
        self.api.request("POST", "/commands/register_site", {"site_id": "S1", "capacity": 10})
        self.api.request("POST", "/commands/receive_person",
                         {"person_id": "P1", "site_id": "S1", "family_id": "F1"},
                         role="rescue_worker")
        status, body = self.api.request("GET", "/queries/families/F1/timeline")
        self.assertEqual(status, 200)
        self.assertEqual(body["family_id"], "F1")
        self.assertTrue(any(e["type"] == "person.received" for e in body["events"]))

    def test_raw_events_restricted_to_coordinator_and_supervisor(self) -> None:
        status, body = self.api.request("GET", "/queries/events", role="site_worker")
        self.assertEqual(status, 403)
        status, body = self.api.request("GET", "/queries/events")
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 0)
