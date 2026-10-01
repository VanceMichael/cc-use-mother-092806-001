"""人员接收、待核实、档案合并与隐私脱敏。"""

from src.errors import PermissionDeniedError, StateConflictError, ValidationError
from tests.support import ServiceCase


class PersonVerificationTest(ServiceCase):
    def test_no_documents_enters_pending_verification(self) -> None:
        self.seed_shelters()
        result = self.service.receive_person(
            "rescue", shelter_id="SH_BKK", name="玛妮", age=None)
        self.assertEqual(result["status"], "pending_verification")
        person = self.service.get_person("coordinator", result["person_id"])
        self.assertEqual(person["status"], "pending_verification")
        self.assertEqual(person["id_documents"], [])

    def test_with_documents_is_verified_directly(self) -> None:
        self.seed_shelters()
        result = self.service.receive_person(
            "rescue", shelter_id="SH_BKK", name="阿南",
            id_documents=["ID-1001"])
        self.assertEqual(result["status"], "verified")

    def test_verify_without_basis_is_rejected(self) -> None:
        self.seed_shelters()
        pid = self.service.receive_person(
            "rescue", shelter_id="SH_BKK", name="玛妮")["person_id"]
        with self.assertRaises(ValidationError):
            self.service.verify_person("rescue", pid)
        ok = self.service.verify_person("rescue", pid, basis="人工确认")
        self.assertEqual(ok["status"], "verified")

    def test_duplicate_document_opens_manual_review(self) -> None:
        self.seed_shelters()
        p1 = self.service.receive_person(
            "rescue", shelter_id="SH_BKK", name="阿南", id_documents=["ID-1001"])["person_id"]
        p2 = self.service.receive_person(
            "shelter", shelter_id="SH_BKK", name="无名氏")["person_id"]
        result = self.service.verify_person("rescue", p2, id_documents=["ID-1001"])
        self.assertEqual(result["status"], "pending_review")
        review = self.service.state.reviews[result["review_id"]]
        self.assertEqual(review["topic"], "identity_conflict")
        self.assertEqual(review["status"], "open")
        # 协调员复核后可按结论完成核验
        resolved = self.service.resolve_review(
            "coordinator", result["review_id"], resolution="确认为同一证件挂失重领",
            action={"type": "verify"})
        self.assertEqual(resolved["resolution"], "确认为同一证件挂失重领")
        self.assertEqual(self.service.state.people[p2]["status"], "verified")

    def test_unknown_role_denied(self) -> None:
        self.seed_shelters()
        with self.assertRaises(PermissionDeniedError):
            self.service.receive_person("reporter", shelter_id="SH_BKK", name="x")


class ProfileMergeTest(ServiceCase):
    def test_merge_preserves_original_reception_facts(self) -> None:
        self.seed_shelters()
        # 无证件先接收（曼谷），后凭证件在暖武里又建档案
        first = self.service.receive_person(
            "rescue", shelter_id="SH_BKK", name="颂猜")["person_id"]
        second = self.service.receive_person(
            "rescue", shelter_id="SH_NT", name="颂猜", id_documents=["ID-2002"])["person_id"]
        result = self.service.merge_profiles("coordinator", kept_id=second, merged_id=first)
        kept = result["kept_id"]
        facts = result["reception_facts"]
        shelters = {f["shelter_id"] for f in facts}
        self.assertEqual(shelters, {"SH_BKK", "SH_NT"})
        self.assertTrue(any(f["via_merge"] and f["merged_id"] == first for f in facts))
        # 旧编号解析到存活编号，最初接收地点仍可追溯
        self.assertEqual(self.service.state.resolve(first), kept)
        path = [h["shelter_id"] for h in self.service.state.location_history[kept]]
        self.assertIn("SH_BKK", path)
        self.assertIn("SH_NT", path)

    def test_merge_does_not_auto_verify_unverified_profile(self) -> None:
        self.seed_shelters()
        first = self.service.receive_person(
            "rescue", shelter_id="SH_BKK", name="颂猜")["person_id"]
        second = self.service.receive_person(
            "rescue", shelter_id="SH_BKK", name="颂猜", id_documents=["ID-2002"])["person_id"]
        self.service.merge_profiles("coordinator", kept_id=second, merged_id=first)
        self.assertEqual(self.service.state.people[second]["status"], "verified")

    def test_conflicting_names_go_to_review(self) -> None:
        self.seed_shelters()
        a = self.service.receive_person(
            "rescue", shelter_id="SH_BKK", name="甲女士")["person_id"]
        b = self.service.receive_person(
            "rescue", shelter_id="SH_BKK", name="乙先生")["person_id"]
        result = self.service.merge_profiles("coordinator", kept_id=a, merged_id=b)
        self.assertEqual(result["status"], "pending_review")
        review = self.service.state.reviews[result["review_id"]]
        self.assertEqual(review["topic"], "merge_conflict")

    def test_profiles_in_different_families_go_to_review(self) -> None:
        self.seed_shelters()
        a = self.service.receive_person("rescue", shelter_id="SH_BKK", name="同名人")["person_id"]
        b = self.service.receive_person("rescue", shelter_id="SH_BKK", name="同名人")["person_id"]
        self.service.declare_family("rescue", "F1", [{"person_id": a, "relation": "户主"}])
        self.service.declare_family("rescue", "F2", [{"person_id": b, "relation": "户主"}])
        result = self.service.merge_profiles("coordinator", kept_id=a, merged_id=b)
        self.assertEqual(result["status"], "pending_review")

    def test_merge_requires_coordinator_or_welfare(self) -> None:
        self.seed_shelters()
        a = self.service.receive_person("rescue", shelter_id="SH_BKK", name="甲")["person_id"]
        b = self.service.receive_person("rescue", shelter_id="SH_BKK", name="乙")["person_id"]
        with self.assertRaises(PermissionDeniedError):
            self.service.merge_profiles("rescue", kept_id=a, merged_id=b)


class PrivacyTest(ServiceCase):
    def test_child_and_health_fields_are_role_restricted(self) -> None:
        self.seed_shelters()
        child = self.service.receive_person(
            "rescue", shelter_id="SH_BKK", name="小树苗", age=9)["person_id"]
        self.service.record_health(
            "medical", child, condition="哮喘", mobility="自主")
        # 救援队：看不到年龄与健康
        rescue_view = self.service.get_person("rescue", child)
        self.assertNotIn("age", rescue_view)
        self.assertNotIn("is_minor", rescue_view)
        self.assertNotIn("health", rescue_view)
        # 安置点：同样看不到
        shelter_view = self.service.get_person("shelter", child)
        self.assertNotIn("health", shelter_view)
        self.assertNotIn("age", shelter_view)
        # 医疗岗：可见健康与年龄
        med_view = self.service.get_person("medical", child)
        self.assertEqual(med_view["age"], 9)
        self.assertTrue(med_view["is_minor"])
        self.assertEqual(med_view["health"][0]["condition"], "哮喘")
        # 儿童福利岗：可见年龄但看不到健康详情
        welfare_view = self.service.get_person("social_welfare", child)
        self.assertEqual(welfare_view["age"], 9)
        self.assertNotIn("health", welfare_view)

    def test_rescue_cannot_record_health(self) -> None:
        self.seed_shelters()
        pid = self.service.receive_person("rescue", shelter_id="SH_BKK", name="x")["person_id"]
        with self.assertRaises(PermissionDeniedError):
            self.service.record_health("rescue", pid, condition="透析")
