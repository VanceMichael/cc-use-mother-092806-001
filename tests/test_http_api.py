"""HTTP API 集成测试：真实起线程服务器，走 JSON over HTTP。"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from src.api import make_handler, ApiState
from src.journal import EventStore


class ApiClient:
    def __init__(self, base: str):
        self.base = base

    def call(self, path: str, payload: dict, role: str | None = None,
             idem_key: str | None = None):
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if role:
            headers["X-Role"] = role
        if idem_key:
            headers["X-Idempotency-Key"] = idem_key
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode("utf-8"),
            headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp_ctx = __import__("tempfile").TemporaryDirectory()
        tmp = cls._tmp_ctx.name
        store = EventStore(f"{tmp}/api.log")
        cls.api_state = ApiState(f"{tmp}/api.log")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                        make_handler(cls.api_state))
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.client = ApiClient(f"http://127.0.0.1:{cls.port}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls._tmp_ctx.cleanup()

    def test_full_flow_over_http(self) -> None:
        c = self.client
        status, shelter = c.call("/shelters/register", {
            "shelter_id": "SH1", "name": "廊曼", "capacity": 4}, "coordinator")
        self.assertEqual(status, 200)

        status, person = c.call("/people/receive", {
            "shelter_id": "SH1", "name": "阿南"}, "rescue")
        self.assertEqual(status, 200)
        self.assertEqual(person["status"], "pending_verification")
        pid = person["person_id"]

        # 缺岗位头 -> 403
        status, err = c.call("/people/get", {"person_id": pid})
        self.assertEqual(status, 403)
        self.assertEqual(err["error"], "forbidden")

        # 缺字段 -> 400
        status, err = c.call("/people/receive", {"shelter_id": "SH1"}, "rescue")
        self.assertEqual(status, 400)
        self.assertEqual(err["error"], "invalid_request")

        # 权限：救援队不能登记安置点
        status, err = c.call("/shelters/register", {
            "shelter_id": "SH2", "name": "他处", "capacity": 1}, "rescue")
        self.assertEqual(status, 403)

        # 未知路由 -> 404
        status, err = c.call("/nope", {}, "coordinator")
        self.assertEqual(status, 404)

    def test_idempotency_header_and_duplicate_receipt(self) -> None:
        c = self.client
        c.call("/shelters/register", {
            "shelter_id": "SHI", "name": "点", "capacity": 20}, "coordinator")
        s1, r1 = c.call("/people/receive", {
            "shelter_id": "SHI", "name": "玛妮"}, "rescue", idem_key="K-1")
        s2, r2 = c.call("/people/receive", {
            "shelter_id": "SHI", "name": "玛妮"}, "rescue", idem_key="K-1")
        self.assertEqual((s1, s2), (200, 200))
        self.assertEqual(r1["person_id"], r2["person_id"])
        self.assertTrue(r2["idempotent_replay"])

        # 物资重复回执
        _, batch = c.call("/supplies/receive", {
            "shelter_id": "SHI", "item": "水", "quantity": 10}, "shelter")
        _, i1 = c.call("/supplies/issue", {
            "person_id": r1["person_id"], "supply_batch_id": batch["supply_batch_id"],
            "quantity": 2, "receipt_key": "RR-1"}, "shelter")
        _, i2 = c.call("/supplies/issue", {
            "person_id": r1["person_id"], "supply_batch_id": batch["supply_batch_id"],
            "quantity": 2, "receipt_key": "RR-1"}, "shelter")
        self.assertEqual(i1["issue_id"], i2["issue_id"])
        self.assertTrue(i2["duplicate"])

        # 儿童与医疗脱敏经 API 生效
        _, child = c.call("/people/receive", {
            "shelter_id": "SHI", "name": "小童", "age": 8}, "social_welfare")
        _, med_ok = c.call("/health/record", {
            "person_id": child["person_id"], "condition": "哮喘"}, "medical")
        self.assertEqual(med_ok["condition"], "哮喘")
        _, denied = c.call("/health/record", {
            "person_id": child["person_id"], "condition": "哮喘"}, "rescue")
        self.assertEqual(denied["error"], "forbidden")

        s_rescue, view_rescue = c.call("/people/get", {
            "person_id": child["person_id"]}, "rescue")
        self.assertNotIn("health", view_rescue)
        _, view_med = c.call("/people/get", {
            "person_id": child["person_id"]}, "medical")
        self.assertIn("health", view_med)

    def test_recover_endpoint_after_restart(self) -> None:
        import tempfile
        tmp = tempfile.mkdtemp()
        state1 = ApiState(f"{tmp}/restart.log")
        from src.service import ReliefService
        svc = state1.service
        svc.register_shelter("coordinator", "S", "点", capacity=2)
        for i in range(2):
            svc.receive_person("rescue", shelter_id="S", name=f"人{i}",
                               id_documents=[f"ID-{i}"])
        # 同进程重建（等价重启）
        state2 = ApiState(f"{tmp}/restart.log")
        alerts = state2.service.recover()["open_capacity_alerts"]
        self.assertTrue(any(a["shelter_id"] == "S" for a in alerts))
