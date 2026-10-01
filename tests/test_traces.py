"""寻亲线索、自动匹配建议、人工确认团聚及依据留痕。"""

from src.errors import StateConflictError, ValidationError
from tests.support import ServiceCase


class TraceTest(ServiceCase):
    def _separated_family(self) -> tuple[str, str, str]:
        bkk, nt = self.seed_shelters()
        # 母亲在曼谷，孩子先被送到暖武里，家庭关系已申报
        mother = self.service.receive_person(
            "rescue", shelter_id=bkk, name="婉帕", id_documents=["ID-MOM"])["person_id"]
        child = self.service.receive_person(
            "rescue", shelter_id=nt, name="小树", age=9)["person_id"]
        fid = "F-001"
        self.service.declare_family(
            "rescue", fid,
            [{"person_id": mother, "relation": "母亲"},
             {"person_id": child, "relation": "子女"}])
        return mother, child, fid

    def test_clues_and_family_relation_produce_suggestion(self) -> None:
        mother, child, _ = self._separated_family()
        trace = self.service.open_trace(
            "social_welfare", person_id=mother,
            looking_for={"name": "小树", "relation": "子女"},
            clues=["孩子在暖武里安置点"])
        suggestions = self.service.state.traces[trace["trace_id"]]["suggestions"]
        self.assertTrue(any(s["person_id"] == child for s in suggestions))
        best = max(suggestions, key=lambda s: s["score"])
        self.assertEqual(best["person_id"], child)
        self.assertTrue(any("家庭" in b or "姓名" in b for b in best["basis"]))

    def test_confirmation_requires_basis_and_records_it(self) -> None:
        mother, child, fid = self._separated_family()
        trace = self.service.open_trace(
            "social_welfare", person_id=mother,
            looking_for={"name": "小树", "relation": "子女"},
            clues=["孩子在暖武里安置点"])
        with self.assertRaises(ValidationError):
            self.service.confirm_trace_match(
                "social_welfare", trace["trace_id"], child, basis=[])
        result = self.service.confirm_trace_match(
            "coordinator", trace["trace_id"], child,
            basis=["同属家庭 F-001", "母亲照片辨认", "暖武里安置点登记吻合"])
        self.assertEqual(result["matched_person_id"], child)
        stored = self.service.state.traces[trace["trace_id"]]
        self.assertEqual(stored["status"], "matched")
        self.assertEqual(len(stored["basis"]), 3)
        # 家庭时点视图可说明团聚依据
        timeline = self.service.family_timeline("coordinator", fid)
        self.assertEqual(len(timeline["reunions"]), 1)
        self.assertEqual(timeline["reunions"][0]["matched_person_id"], child)

    def test_rescue_cannot_confirm_match(self) -> None:
        from src.errors import PermissionDeniedError
        mother, child, _ = self._separated_family()
        trace = self.service.open_trace(
            "rescue", person_id=mother, looking_for={"name": "小树"})
        with self.assertRaises(PermissionDeniedError):
            self.service.confirm_trace_match(
                "rescue", trace["trace_id"], child, basis=["x"])

    def test_closed_trace_cannot_confirm_again(self) -> None:
        mother, child, _ = self._separated_family()
        trace = self.service.open_trace(
            "social_welfare", person_id=mother, looking_for={"name": "小树"})
        self.service.confirm_trace_match(
            "social_welfare", trace["trace_id"], child, basis=["家庭关系"])
        with self.assertRaises(StateConflictError):
            self.service.confirm_trace_match(
                "social_welfare", trace["trace_id"], child, basis=["再确认"])
