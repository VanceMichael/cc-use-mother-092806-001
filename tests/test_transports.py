"""转运安排、透析患者流程与终态不可变测试。"""

from __future__ import annotations

from src.disaster_relief.errors import ConflictError
from src.disaster_relief.state import (
    AR_CANCELLED,
    AR_COMPLETED,
    AR_CONFIRMED,
    AR_PENDING,
)

try:
    from ._harness import ServiceTestCase
except ImportError:  # 以 tests 为发现目录运行时
    from _harness import ServiceTestCase


class TransportTest(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.seed_site("S1")
        self.seed_site("S2", dialysis=True)
        self.seed_site("S3", dialysis=False)
        self.cmd("receive_person", {"person_id": "P1", "site_id": "S1",
                                    "family_id": "F1", "family_role": "parent"},
                 role="rescue_worker")
        self.cmd("receive_person", {"person_id": "P2", "site_id": "S1",
                                    "family_id": "F1", "family_role": "child"},
                 role="rescue_worker")
        # P1 为透析患者
        self.cmd("update_medical_profile", {
            "person_id": "P1", "patient_type": "dialysis", "needs_dialysis": True,
        }, role="medical_worker")

    def _create(self, person="P1", dest="S2", **extra):
        extra.setdefault("kind", "general")
        return self.cmd("create_transport", {
            "person_id": person,
            "destination_site_id": dest, "scheduled_at": self.clock.t + 3_600_000,
            **extra,
        }, role="medical_worker")

    def test_dialysis_patient_cannot_go_to_non_dialysis_site(self) -> None:
        with self.assertRaises(ConflictError):
            self._create("P1", "S3")

    def test_dialysis_patient_may_go_to_capable_facility(self) -> None:
        result = self._create("P1", "S2")
        # 普通转运请求在识别为透析患者后自动升级为透析转运
        self.assertEqual(result["events"][0]["data"]["kind"], "dialysis")

    def test_dialysis_patient_may_go_to_medical_facility(self) -> None:
        result = self._create("P1", None, medical_facility_id="HOSP-9")
        self.assertEqual(result["events"][0]["data"]["medical_facility_id"], "HOSP-9")

    def test_pending_transport_can_be_revised_and_confirmed(self) -> None:
        created = self._create("P1", "S2")
        tid = created["transport_id"]
        self.assertEqual(self.app.commands.state.transports[tid]["status"], AR_PENDING)

        # 改派到另一个透析点：允许
        self.seed_site("S4", dialysis=True)
        revised = self.cmd("revise_transport", {
            "transport_id": tid, "destination_site_id": "S4",
        }, role="provincial_coordinator")
        self.assertEqual(revised["status"], "accepted")
        self.assertEqual(self.app.commands.state.transports[tid]["destination_site_id"], "S4")

        self.cmd("confirm_transport", {"transport_id": tid}, role="medical_worker")
        self.assertEqual(self.app.commands.state.transports[tid]["status"], AR_CONFIRMED)

    def test_revising_dialysis_to_non_capable_site_is_rejected(self) -> None:
        created = self._create("P1", "S2")
        with self.assertRaises(ConflictError):
            self.cmd("revise_transport", {
                "transport_id": created["transport_id"], "destination_site_id": "S3",
            }, role="provincial_coordinator")

    def test_completed_transport_cannot_change_only_new_one(self) -> None:
        created = self._create("P2", "S2", kind="general")
        tid = created["transport_id"]
        self.cmd("confirm_transport", {"transport_id": tid}, role="medical_worker")
        self.cmd("set_transport_status",
                 {"transport_id": tid, "status": "in_progress"},
                 role="medical_worker")
        self.cmd("set_transport_status",
                 {"transport_id": tid, "status": AR_COMPLETED},
                 role="medical_worker")
        # 已完成：修改/取消/再确认全部拒绝
        for command, payload in (
            ("revise_transport", {"transport_id": tid, "destination_site_id": "S1"}),
            ("set_transport_status", {"transport_id": tid, "status": AR_CANCELLED}),
            ("confirm_transport", {"transport_id": tid}),
        ):
            with self.assertRaises(ConflictError):
                self.cmd(command, payload, role="medical_worker")
        # 只能新建安排
        again = self.cmd("create_transport", {
            "person_id": "P2", "kind": "general", "destination_site_id": "S1",
        }, role="medical_worker")
        self.assertEqual(again["status"], "accepted")

    def test_return_home_cancels_only_open_arrangements(self) -> None:
        open_t = self._create("P1", "S2")
        done_t = self._create("P2", "S2", kind="general")
        self.cmd("set_transport_status",
                 {"transport_id": done_t["transport_id"], "status": AR_COMPLETED},
                 role="medical_worker")

        self.cmd("return_family_home", {"family_id": "F1"}, role="provincial_coordinator")
        self.assertEqual(
            self.app.commands.state.transports[open_t["transport_id"]]["status"],
            AR_CANCELLED,
        )
        # 已完成安排保持终态事实不变
        self.assertEqual(
            self.app.commands.state.transports[done_t["transport_id"]]["status"],
            AR_COMPLETED,
        )
        # 取消后不能复活
        with self.assertRaises(ConflictError):
            self.cmd("confirm_transport",
                     {"transport_id": open_t["transport_id"]},
                     role="medical_worker")

    def test_cross_region_reassignment_is_a_revision_of_open_arrangement(self) -> None:
        created = self._create("P2", "S2", kind="general")
        self.cmd("confirm_transport", {"transport_id": created["transport_id"]},
                 role="medical_worker")
        # 跨区调剂：已确认但未完成仍可改派
        self.seed_site("S5")
        self.cmd("revise_transport", {
            "transport_id": created["transport_id"],
            "destination_site_id": "S5", "reason": "跨区容量调剂",
        }, role="provincial_coordinator")
        transport = self.app.commands.state.transports[created["transport_id"]]
        self.assertEqual(transport["destination_site_id"], "S5")
        self.assertEqual(transport["updates"][0]["reason"], "跨区容量调剂")
        self.assertEqual(transport["status"], AR_CONFIRMED)
