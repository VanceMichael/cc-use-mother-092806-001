"""撤离批次、安排状态机、容量预警与跨区调剂。"""

from src.errors import PermissionDeniedError, StateConflictError
from tests.support import ServiceCase


class ArrangementTest(ServiceCase):
    def test_full_lifecycle_pending_confirmed_executed(self) -> None:
        self.seed_shelters()
        pid = self.service.receive_person(
            "rescue", shelter_id="SH_BKK", name="阿南", id_documents=["ID-1"])["person_id"]
        arr = self.service.request_arrangement(
            "rescue", kind="transfer", person_id=pid,
            from_shelter_id="SH_BKK", to_shelter_id="SH_NT", reason="家属在暖武里")
        self.assertEqual(arr["status"], "pending")
        self.service.confirm_arrangement("coordinator", arr["arrangement_id"])
        self.service.execute_arrangement("rescue", arr["arrangement_id"])
        self.assertEqual(self.service.state.people[pid]["current_shelter"], "SH_NT")

    def test_only_open_arrangements_can_change(self) -> None:
        self.seed_shelters()
        pid = self.service.receive_person(
            "rescue", shelter_id="SH_BKK", name="阿南")["person_id"]
        arr = self.service.request_arrangement(
            "rescue", kind="transfer", person_id=pid, to_shelter_id="SH_NT")
        self.service.confirm_arrangement("coordinator", arr["arrangement_id"])
        self.service.execute_arrangement("rescue", arr["arrangement_id"])
        # 已执行不能再取消
        with self.assertRaises(StateConflictError):
            self.service.cancel_arrangement("rescue", arr["arrangement_id"])

    def test_cancel_pending_frees_person_for_new_plan(self) -> None:
        self.seed_shelters()
        pid = self.service.receive_person(
            "rescue", shelter_id="SH_BKK", name="阿南")["person_id"]
        arr = self.service.request_arrangement(
            "rescue", kind="transfer", person_id=pid, to_shelter_id="SH_NT")
        self.service.cancel_arrangement("rescue", arr["arrangement_id"], reason="改期")
        again = self.service.request_arrangement(
            "coordinator", kind="return_home", person_id=pid, reason="水退")
        self.assertEqual(again["status"], "pending")

    def test_cannot_open_two_arrangements_at_once(self) -> None:
        self.seed_shelters()
        pid = self.service.receive_person("rescue", shelter_id="SH_BKK", name="阿南")["person_id"]
        self.service.request_arrangement(
            "rescue", kind="transfer", person_id=pid, to_shelter_id="SH_NT")
        with self.assertRaises(StateConflictError):
            self.service.request_arrangement(
                "rescue", kind="transfer", person_id=pid, to_shelter_id="SH_NT")

    def test_medical_transfer_is_medical_role_only(self) -> None:
        self.seed_shelters()
        pid = self.service.receive_person("rescue", shelter_id="SH_BKK", name="患者")["person_id"]
        with self.assertRaises(PermissionDeniedError):
            self.service.request_arrangement(
                "rescue", kind="medical_transfer", person_id=pid,
                to_shelter_id="SH_NT", reason="透析")
        med = self.service.request_arrangement(
            "medical", kind="medical_transfer", person_id=pid,
            to_shelter_id="SH_NT", reason="透析")
        self.assertEqual(med["status"], "pending")
        self.assertTrue(self.service.state.arrangements[med["arrangement_id"]]["medical"])

    def test_discharge_and_return_home_clear_shelter(self) -> None:
        self.seed_shelters()
        pid = self.service.receive_person("medical", shelter_id="SH_BKK",
                                          name="出院者", id_documents=["ID-9"])["person_id"]
        arr = self.service.request_arrangement(
            "medical", kind="discharge", person_id=pid, from_shelter_id="SH_BKK",
            destination="是里拉医院")
        self.service.confirm_arrangement("medical", arr["arrangement_id"])
        self.service.execute_arrangement("medical", arr["arrangement_id"])
        self.assertIsNone(self.service.state.people[pid]["current_shelter"])


class CapacityTest(ServiceCase):
    def _fill(self, shelter_id: str, n: int) -> list[str]:
        ids = []
        for i in range(n):
            ids.append(self.service.receive_person(
                "rescue", shelter_id=shelter_id, name=f"群众{i}",
                id_documents=[f"ID-{shelter_id}-{i}"])["person_id"])
        return ids

    def test_warning_at_90_percent_and_clear_after_exit(self) -> None:
        self.seed_shelters()
        self._fill("SH_BKK", 9)  # 9/10 = 90%
        alert = self.service.state.capacity_alerts["SH_BKK"]
        self.assertEqual(alert["status"], "open")
        self.assertEqual(alert["level"], "near_full")
        # 一人转出后回落，预警解除
        pid = next(
            p["person_id"] for p in self.service.state.people.values()
            if p.get("current_shelter") == "SH_BKK" and not p.get("merged_into"))
        arr = self.service.request_arrangement(
            "rescue", kind="transfer", person_id=pid, to_shelter_id="SH_NT")
        self.service.confirm_arrangement("coordinator", arr["arrangement_id"])
        self.service.execute_arrangement("rescue", arr["arrangement_id"])
        self.assertEqual(
            self.service.state.capacity_alerts["SH_BKK"]["status"], "cleared")

    def test_hard_capacity_blocks_standard_but_not_medical(self) -> None:
        self.seed_shelters()
        self._fill("SH_NT", 10)  # 满员
        pid = self.service.receive_person(
            "rescue", shelter_id="SH_BKK", name="透析患者")["person_id"]
        # 普通转运不能进满员点
        std = self.service.request_arrangement(
            "rescue", kind="transfer", person_id=pid, to_shelter_id="SH_NT")
        with self.assertRaises(StateConflictError):
            self.service.confirm_arrangement("coordinator", std["arrangement_id"])
        # 医疗转运由医疗岗发起，可确认（但执行若仍满员会被硬容量拦截）
        self.service.cancel_arrangement("rescue", std["arrangement_id"])
        med = self.service.request_arrangement(
            "medical", kind="medical_transfer", person_id=pid,
            to_shelter_id="SH_NT", reason="透析")
        self.service.confirm_arrangement("medical", med["arrangement_id"])
        with self.assertRaises(StateConflictError):
            self.service.execute_arrangement("medical", med["arrangement_id"])

    def test_reallocate_is_coordinator_only(self) -> None:
        self.seed_shelters()
        pid = self.service.receive_person("rescue", shelter_id="SH_BKK", name="调剂对象")["person_id"]
        with self.assertRaises(Exception):
            self.service.request_arrangement(
                "rescue", kind="reallocate", person_id=pid, to_shelter_id="SH_NT")
        arr = self.service.request_arrangement(
            "coordinator", kind="reallocate", person_id=pid, to_shelter_id="SH_NT")
        self.assertEqual(arr["status"], "pending")


class BatchTest(ServiceCase):
    def test_batch_assignment_tracks_members(self) -> None:
        self.seed_shelters()
        self.service.register_batch("rescue", "B-001", origin_area="巴吞他尼", kind="standard")
        pid = self.service.receive_person(
            "rescue", shelter_id="SH_BKK", name="成员甲", batch_id="B-001")["person_id"]
        self.assertIn(pid, self.service.state.batches["B-001"]["person_ids"])
        self.assertEqual(self.service.state.people[pid]["batch_id"], "B-001")
