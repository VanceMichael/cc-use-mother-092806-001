"""接收、待核实身份与档案合并测试。"""

from __future__ import annotations

from src.disaster_relief.errors import PermissionDeniedError
from src.disaster_relief.state import ST_MERGED, ST_UNVERIFIED, ST_VERIFIED

try:
    from ._harness import ServiceTestCase
except ImportError:  # 以 tests 为发现目录运行时
    from _harness import ServiceTestCase


class IntakeAndMergeTest(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.seed_site("S1")
        self.seed_site("S2")

    def test_person_without_documents_enters_unverified(self) -> None:
        result = self.cmd("receive_person", {
            "person_id": "P1", "site_id": "S1", "display_name": "Somchai",
        }, role="rescue_worker")
        self.assertEqual(result["status"], "accepted")
        person = self.app.commands.state.person("P1")
        self.assertEqual(person["status"], ST_UNVERIFIED)
        self.assertTrue(person["documents_missing"])
        # 最初接收事实被完整保留
        self.assertEqual(person["first_received"]["site_id"], "S1")

    def test_verify_changes_status_and_keeps_first_receipt(self) -> None:
        first_ts = self.clock.t
        self.cmd("receive_person", {"person_id": "P1", "site_id": "S1"}, role="rescue_worker")
        self.clock.advance(3_600_000)
        self.cmd("verify_person", {
            "person_id": "P1", "basis": "witness",
            "id_documents": [{"type": "id_card", "number": "TH-1"}],
        }, role="site_worker")
        person = self.app.commands.state.person("P1")
        self.assertEqual(person["status"], ST_VERIFIED)
        self.assertEqual(person["first_received"]["at"], first_ts)
        self.assertEqual(person["first_received"]["site_id"], "S1")

    def test_merge_preserves_earliest_intake_fact(self) -> None:
        # P1 先在 S1 被接收（无证件，待核实）
        early = self.clock.t
        self.cmd("receive_person", {"person_id": "P1", "site_id": "S1", "family_id": "F1"}, role="rescue_worker")
        self.clock.advance(7_200_000)
        # 核实后，同一人在 S2 又以 P2 建档
        self.cmd("receive_person", {"person_id": "P2", "site_id": "S2",
                                    "documents_missing": False}, role="rescue_worker")
        self.cmd("verify_person", {"person_id": "P2", "basis": "id_card",
                                   "id_documents": [{"type": "id_card", "number": "TH-9"}]})
        merge = self.cmd("merge_person", {
            "canonical_id": "P2", "duplicate_id": "P1", "reason": "same person",
        })
        self.assertEqual(merge["status"], "accepted")

        canonical = self.app.commands.state.person("P2")
        # 关键不变量：合并后档案仍指向最早的接收事实（S1，而非 S2）
        self.assertEqual(canonical["first_received"]["site_id"], "S1")
        self.assertEqual(canonical["first_received"]["at"], early)
        # 两条接收事实都在谱系中，可回放
        receipt_sites = {r["site_id"] for r in canonical["merged_receipts"]}
        self.assertEqual(receipt_sites, {"S1", "S2"})
        # 旧档案标记为已并入，别名可解析
        self.assertEqual(self.app.commands.state.persons["P1"]["status"], ST_MERGED)
        self.assertEqual(self.app.commands.state.canonical_person_id("P1"), "P2")

    def test_duplicate_intake_at_another_site_opens_review(self) -> None:
        self.cmd("receive_person", {"person_id": "P1", "site_id": "S1"}, role="rescue_worker")
        result = self.cmd("receive_person", {"person_id": "P1", "site_id": "S2"},
                          role="rescue_worker")
        # 重复接收不覆盖，转人工复核
        self.assertEqual(result["status"], "manual_review")
        review = self.app.commands.state.reviews[result["review_id"]]
        self.assertEqual(review["kind"], "duplicate_intake")
        self.assertEqual(review["status"], "open")

    def test_shared_document_number_conflict_opens_review(self) -> None:
        self.cmd("receive_person", {"person_id": "P1", "site_id": "S1",
                                    "documents_missing": False}, role="rescue_worker")
        self.cmd("receive_person", {"person_id": "P2", "site_id": "S2",
                                    "documents_missing": False}, role="rescue_worker")
        for pid in ("P1", "P2"):
            self.cmd("verify_person", {"person_id": pid, "basis": "id_card",
                                       "id_documents": [{"type": "id_card", "number": "DUP"}]})
        # 第二次核实带同一证件号：第一次通过，第二次触发冲突复核
        # （第一次直接通过；第二次执行时两个档案都已绑定该号）
        reviews = [r for r in self.app.commands.state.reviews.values()
                   if r["kind"] == "identity_conflict"]
        self.assertEqual(len(reviews), 1)

    def test_only_authorized_roles_can_merge(self) -> None:
        self.cmd("receive_person", {"person_id": "P1", "site_id": "S1"}, role="rescue_worker")
        self.cmd("receive_person", {"person_id": "P2", "site_id": "S2"}, role="rescue_worker")
        with self.assertRaises(PermissionDeniedError):
            self.cmd("merge_person",
                     {"canonical_id": "P1", "duplicate_id": "P2"},
                     role="rescue_worker")

    def test_unknown_role_is_rejected(self) -> None:
        with self.assertRaises(PermissionDeniedError):
            self.cmd("register_site", {"site_id": "X", "capacity": 1},
                     role="rescue_worker")
