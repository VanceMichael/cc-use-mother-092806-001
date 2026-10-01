"""物资批次守恒、重复回执、退回补发；药品批次与补给待办。"""

from src.errors import StateConflictError, ValidationError
from tests.support import ServiceCase


class SupplyConservationTest(ServiceCase):
    def _ready(self) -> tuple[str, str]:
        bkk, _ = self.seed_shelters()
        pid = self.service.receive_person(
            "shelter", shelter_id=bkk, name="户主", id_documents=["ID-1"])["person_id"]
        batch = self.service.receive_supply(
            "shelter", shelter_id=bkk, item="饮用水", quantity=20)["supply_batch_id"]
        return pid, batch

    def test_issue_consumes_stock(self) -> None:
        pid, batch = self._ready()
        issue = self.service.issue_supply(
            "shelter", person_id=pid, supply_batch_id=batch,
            quantity=6, receipt_key="RCP-001", family_id="F-1")
        self.assertEqual(issue["kind"], "supply")
        stock = self.service.state.supply_batches[batch]
        self.assertEqual(stock["issued"], 6)
        self.assertEqual(stock["quantity"] - stock["issued"], 14)

    def test_duplicate_receipt_returns_original_decision(self) -> None:
        pid, batch = self._ready()
        first = self.service.issue_supply(
            "shelter", person_id=pid, supply_batch_id=batch,
            quantity=3, receipt_key="RCP-DUP")
        second = self.service.issue_supply(
            "shelter", person_id=pid, supply_batch_id=batch,
            quantity=99, receipt_key="RCP-DUP")  # 数量不同也以原决定为准
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["issue_id"], first["issue_id"])
        self.assertEqual(second["quantity"], 3)
        # 库存只扣了一次
        self.assertEqual(self.service.state.supply_batches[batch]["issued"], 3)

    def test_issue_beyond_stock_rejected(self) -> None:
        pid, batch = self._ready()
        with self.assertRaises(StateConflictError):
            self.service.issue_supply(
                "shelter", person_id=pid, supply_batch_id=batch,
                quantity=21, receipt_key="RCP-X")

    def test_return_reverses_issued_without_deleting_original(self) -> None:
        pid, batch = self._ready()
        issue = self.service.issue_supply(
            "shelter", person_id=pid, supply_batch_id=batch,
            quantity=8, receipt_key="RCP-010", family_id="F-1")
        self.service.return_supply(
            "shelter", original_issue_id=issue["issue_id"],
            quantity=3, reason="家庭已转移至他点")
        stock = self.service.state.supply_batches[batch]
        self.assertEqual(stock["issued"], 5)
        # 原发放记录原样保留
        self.assertEqual(self.service.state.issues[issue["issue_id"]]["quantity"], 8)
        records = [r for r in self.service.state.issue_history[pid]
                   if r["supply_batch_id"] == batch]
        kinds = [(r["kind"], r["quantity"]) for r in records]
        self.assertIn(("supply", 8), kinds)
        self.assertIn(("return", -3), kinds)

    def test_return_cannot_exceed_original(self) -> None:
        pid, batch = self._ready()
        issue = self.service.issue_supply(
            "shelter", person_id=pid, supply_batch_id=batch,
            quantity=2, receipt_key="RCP-011")
        with self.assertRaises(ValidationError):
            self.service.return_supply(
                "shelter", original_issue_id=issue["issue_id"], quantity=5)

    def test_reissue_adds_new_record_linked_to_original(self) -> None:
        pid, batch = self._ready()
        first = self.service.issue_supply(
            "shelter", person_id=pid, supply_batch_id=batch,
            quantity=4, receipt_key="RCP-020")
        # 补发时原批次已空，从新批次出
        new_batch = self.service.receive_supply(
            "shelter", shelter_id="SH_BKK", item="饮用水", quantity=10)["supply_batch_id"]
        again = self.service.reissue_supply(
            "shelter", person_id=pid, supply_batch_id=new_batch, quantity=4,
            original_issue_id=first["issue_id"], receipt_key="RCP-021",
            reason="原物资在转运中遗失")
        self.assertEqual(again["kind"], "reissue")
        self.assertEqual(again["adjusts"], first["issue_id"])
        # 原记录不动
        self.assertEqual(self.service.state.issues[first["issue_id"]]["quantity"], 4)
        self.assertEqual(self.service.state.supply_batches[batch]["issued"], 4)
        self.assertEqual(self.service.state.supply_batches[new_batch]["issued"], 4)

    def test_idempotency_key_replays_first_decision(self) -> None:
        bkk, _ = self.seed_shelters()
        r1 = self.service.receive_person(
            "rescue", shelter_id=bkk, name="某人", idem_key="IDEM-RECV-1")
        r2 = self.service.receive_person(
            "rescue", shelter_id=bkk, name="某人", idem_key="IDEM-RECV-1")
        self.assertTrue(r2.get("idempotent_replay"))
        self.assertEqual(r1["person_id"], r2["person_id"])
        self.assertEqual(len(self.service.state.people), 1)


class MedicationTest(ServiceCase):
    def _shelter_with_dialysis_patient(self) -> tuple[str, str]:
        bkk, _ = self.seed_shelters()
        pid = self.service.receive_person(
            "medical", shelter_id=bkk, name="透析患者", id_documents=["ID-M"])["person_id"]
        self.service.record_health(
            "medical", pid, condition="慢性肾病", needs_dialysis=True, mobility="轮椅")
        return bkk, pid

    def test_dispense_consumes_med_batch(self) -> None:
        bkk, pid = self._shelter_with_dialysis_patient()
        med = self.service.receive_medication(
            "medical", shelter_id=bkk, medication="降压药", quantity=10)["med_batch_id"]
        issue = self.service.dispense_medication(
            "medical", person_id=pid, med_batch_id=med, quantity=2)
        self.assertTrue(issue["issue_id"].startswith("iss_"))
        self.assertEqual(self.service.state.med_batches[med]["dispensed"], 2)

    def test_dispense_beyond_stock_rejected(self) -> None:
        bkk, pid = self._shelter_with_dialysis_patient()
        med = self.service.receive_medication(
            "medical", shelter_id=bkk, medication="降压药", quantity=2)["med_batch_id"]
        with self.assertRaises(StateConflictError):
            self.service.dispense_medication(
                "medical", person_id=pid, med_batch_id=med, quantity=3)

    def test_low_stock_raises_resupply_and_arrival_resolves(self) -> None:
        bkk, pid = self._shelter_with_dialysis_patient()
        med = self.service.receive_medication(
            "medical", shelter_id=bkk, medication="透析护理包", quantity=6)["med_batch_id"]
        self.service.dispense_medication(
            "medical", person_id=pid, med_batch_id=med, quantity=2)  # 剩 4 < 5
        open_rs = [r for r in self.service.state.resupplies.values() if r["status"] == "open"]
        self.assertEqual(len(open_rs), 1)
        self.assertEqual(open_rs[0]["medication"], "透析护理包")
        # 再次发药到剩 2，不重复开待办
        self.service.dispense_medication(
            "medical", person_id=pid, med_batch_id=med, quantity=2)
        open_rs2 = [r for r in self.service.state.resupplies.values() if r["status"] == "open"]
        self.assertEqual(len(open_rs2), 1)
        # 补给到货，自动了结
        self.service.receive_medication(
            "medical", shelter_id=bkk, medication="透析护理包", quantity=20)
        self.assertEqual(
            self.service.state.resupplies[open_rs[0]["resupply_id"]]["status"], "resolved")
        # 了结后若又低于阈值，允许开新待办
        # （发掉新批次使存量再次低于阈值）
        new_batch_id = next(
            b for b, v in self.service.state.med_batches.items()
            if v["medication"] == "透析护理包" and b != med)
        self.service.dispense_medication(
            "medical", person_id=pid, med_batch_id=new_batch_id, quantity=18)
        open_rs3 = [r for r in self.service.state.resupplies.values() if r["status"] == "open"]
        self.assertEqual(len(open_rs3), 1)

    def test_rescue_cannot_manage_medication(self) -> None:
        from src.errors import PermissionDeniedError
        self.seed_shelters()
        with self.assertRaises(PermissionDeniedError):
            self.service.receive_medication(
                "rescue", shelter_id="SH_BKK", medication="x", quantity=1)
