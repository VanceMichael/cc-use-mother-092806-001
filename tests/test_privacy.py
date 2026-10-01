"""字段级脱敏与权限测试：儿童与医疗信息只让履职岗位看到。"""

from __future__ import annotations

from src.disaster_relief.errors import PermissionDeniedError

try:
    from ._harness import ServiceTestCase
except ImportError:  # 以 tests 为发现目录运行时
    from _harness import ServiceTestCase

HIDDEN = "【受限】"


class PrivacyTest(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.seed_site("S1")
        self.cmd("receive_person", {
            "person_id": "P1", "site_id": "S1", "display_name": "Child",
            "is_minor": True, "approx_age": 9, "contact": "080-111-2222",
        }, role="rescue_worker")
        self.cmd("update_medical_profile", {
            "person_id": "P1", "patient_type": "dialysis", "needs_dialysis": True,
            "medication": "epoetin",
        }, role="medical_worker")
        self.cmd("register_medication_supply", {
            "person_id": "P1", "medication": "epoetin",
            "quantity_per_refill": 2, "supply_days": 7,
        }, role="medical_worker")
        self.cmd("open_trace", {
            "person_id": "P1", "subject_name": "Child",
            "reporter_name": "Aunt", "reporter_contact": "099-9",
            "minor_included": True, "description": "child details…",
        }, role="social_worker")

    def test_site_worker_cannot_see_medical_or_minor_fields(self) -> None:
        view = self.app.queries.person_view("P1", "site_worker")
        self.assertEqual(view["is_minor"], HIDDEN)
        self.assertEqual(view["approx_age"], HIDDEN)
        self.assertEqual(view["contact"], HIDDEN)
        self.assertTrue(view["medical"]["restricted"])
        self.assertTrue(view["medication_supply"]["restricted"])

    def test_medical_worker_sees_medical_but_trace_contact_rules_apply(self) -> None:
        view = self.app.queries.person_view("P1", "medical_worker")
        self.assertEqual(view["medical"]["needs_dialysis"], True)
        self.assertEqual(view["medication_supply"]["medication"], "epoetin")
        self.assertEqual(view["is_minor"], True)  # 医疗岗也是儿童履职岗位

    def test_social_worker_sees_minor_trace_but_not_full_medical(self) -> None:
        trace = self.app.queries.traces(role="social_worker")[0]
        self.assertTrue(trace["minor_included"])
        self.assertNotEqual(trace["description"], HIDDEN)
        person = self.app.queries.person_view("P1", "social_worker")
        # 社会服务可以看儿童信息，但看不到医疗处方
        self.assertEqual(person["is_minor"], True)
        self.assertTrue(person["medical"]["restricted"])

    def test_trace_reporter_contact_hidden_from_site_worker(self) -> None:
        trace = self.app.queries.traces(role="site_worker")[0]
        self.assertEqual(trace["reporter_contact"], HIDDEN)

    def test_site_worker_cannot_write_medical_profile(self) -> None:
        with self.assertRaises(PermissionDeniedError):
            self.cmd("update_medical_profile",
                     {"person_id": "P1", "needs_dialysis": True},
                     role="site_worker")

    def test_rescue_worker_cannot_register_sites(self) -> None:
        with self.assertRaises(PermissionDeniedError):
            self.cmd("register_site", {"site_id": "SX", "capacity": 1},
                     role="rescue_worker")

    def test_due_medication_list_is_masked_for_non_medical_roles(self) -> None:
        self.cmd("receive_person", {"person_id": "P9", "site_id": "S1"},
                 role="rescue_worker")
        self.cmd("register_medication_supply", {
            "person_id": "P9", "medication": "secret-drug",
            "quantity_per_refill": 1, "supply_days": 1,
            "last_refill_at": self.clock.t - 2 * 86_400_000,
        }, role="medical_worker")
        masked = self.app.queries.medication_due(
            "site_worker", now=self.clock.t, lead_ms=0)
        p9 = next(item for item in masked if item["person_id"] == "P9")
        self.assertEqual(p9["medication"], HIDDEN)
        clear = self.app.queries.medication_due(
            "medical_worker", now=self.clock.t, lead_ms=0)
        p9 = next(item for item in clear if item["person_id"] == "P9")
        self.assertEqual(p9["medication"], "secret-drug")
