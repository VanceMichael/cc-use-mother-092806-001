"""家庭分离、团聚与任一时点时间线测试。"""

from __future__ import annotations

try:
    from ._harness import ServiceTestCase
except ImportError:  # 以 tests 为发现目录运行时
    from _harness import ServiceTestCase


def receive(app_test, pid, site, **extra):
    return app_test.cmd("receive_person",
                        {"person_id": pid, "site_id": site, **extra},
                        role="rescue_worker")


class FamilySeparationTest(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.seed_site("S1")
        self.seed_site("S2")

    def test_family_split_across_sites_is_visible_and_alerted(self) -> None:
        # 同一家庭 F1 的成员同时到达两个安置点
        receive(self, "P1", "S1", family_id="F1", family_role="parent")
        receive(self, "P2", "S2", family_id="F1", family_role="child", is_minor=True)

        view = self.app.queries.family_view("F1", "provincial_coordinator")
        self.assertTrue(view["separated"])
        self.assertEqual(view["members_by_site"], {"S1": 1, "S2": 1})

        produced = self.sweep()
        alerts = [r["data"] for r in produced["families"] if r["type"] == "alert.raised"]
        self.assertTrue(any(a["kind"] == "family_separated" for a in alerts))

        # 团聚后下一轮巡检自动解除预警
        self.cmd("admit_person", {"person_id": "P2", "site_id": "S1"}, role="rescue_worker")
        produced = self.sweep()
        resolved = [r for r in produced["families"] if r["type"] == "alert.resolved"]
        self.assertTrue(resolved)
        view = self.app.queries.family_view("F1", "provincial_coordinator")
        self.assertFalse(view["separated"])

    def test_trace_match_and_confirmation_records_reunion_basis(self) -> None:
        receive(self, "P1", "S1", family_id="F1", display_name="Child Name", is_minor=True)
        trace = self.cmd("open_trace", {
            "subject_name": "Child Name",
            "reporter_name": "Parent",
            "reporter_relation": "parent",
            "reporter_contact": "080-000-0000",
            "last_seen_location": "S1",
            "minor_included": True,
        }, role="social_worker")

        proposed = self.cmd("propose_match", {
            "trace_id": trace["trace_id"], "candidate_person_id": "P1",
        }, role="social_worker")
        self.assertGreaterEqual(proposed["events"][0]["data"]["score"], 40)

        confirmed = self.cmd("confirm_match",
                             {"match_id": proposed["match_id"]},
                             role="social_worker")
        self.assertEqual(confirmed["status"], "accepted")

        timeline = self.app.queries.family_timeline("F1", "social_worker")
        self.assertEqual(len(timeline["reunions"]), 1)
        reunion = timeline["reunions"][0]
        self.assertEqual(reunion["person_id"], "P1")
        self.assertEqual(reunion["confirmed_by"], "social_worker")
        self.assertIn("name", reunion["signals"])
        # 时间线事件可回放：包含接收与确认团聚
        types = [e["type"] for e in timeline["events"]]
        self.assertIn("person.received", types)
        self.assertIn("match.confirmed", types)

    def test_confirmed_match_cannot_be_rejected(self) -> None:
        from src.disaster_relief.errors import ConflictError
        receive(self, "P1", "S1", display_name="N")
        trace = self.cmd("open_trace", {"subject_name": "N"}, role="social_worker")
        match = self.cmd("propose_match",
                         {"trace_id": trace["trace_id"], "candidate_person_id": "P1"},
                         role="social_worker")
        self.cmd("confirm_match", {"match_id": match["match_id"]}, role="social_worker")
        with self.assertRaises(ConflictError):
            self.cmd("reject_match", {"match_id": match["match_id"]}, role="social_worker")

    def test_rival_matches_on_same_trace_go_to_manual_review(self) -> None:
        receive(self, "P1", "S1", display_name="N")
        receive(self, "P2", "S2", display_name="N")
        trace = self.cmd("open_trace", {"subject_name": "N"}, role="social_worker")
        m1 = self.cmd("propose_match",
                      {"trace_id": trace["trace_id"], "candidate_person_id": "P1"},
                      role="social_worker")
        m2 = self.cmd("propose_match",
                      {"trace_id": trace["trace_id"], "candidate_person_id": "P2"},
                      role="social_worker")
        self.cmd("confirm_match", {"match_id": m1["match_id"]}, role="social_worker")
        result = self.cmd("confirm_match", {"match_id": m2["match_id"]}, role="social_worker")
        self.assertEqual(result["status"], "manual_review")
        self.assertEqual(result["kind"], "match_conflict")

    def test_timeline_as_of_earlier_seq_shows_no_reunion_yet(self) -> None:
        receive(self, "P1", "S1", family_id="F1", display_name="Child")
        trace = self.cmd("open_trace", {"subject_name": "Child"}, role="social_worker")
        match = self.cmd("propose_match",
                         {"trace_id": trace["trace_id"], "candidate_person_id": "P1"},
                         role="social_worker")
        before_seq = self.app.commands.state.seq
        self.cmd("confirm_match", {"match_id": match["match_id"]}, role="social_worker")

        earlier = self.app.queries.family_timeline("F1", "social_worker", at_seq=before_seq)
        self.assertEqual(earlier["reunions"], [])
        current = self.app.queries.family_timeline("F1", "social_worker")
        self.assertEqual(len(current["reunions"]), 1)

    def test_timeline_as_of_timestamp_replays_without_seq_gap_errors(self) -> None:
        receive(self, "P1", "S1", family_id="F1", display_name="Child")
        t0 = self.clock.t
        trace = self.cmd("open_trace", {"subject_name": "Child"}, role="social_worker")
        match = self.cmd("propose_match",
                         {"trace_id": trace["trace_id"], "candidate_person_id": "P1"},
                         role="social_worker")
        self.clock.advance(1000)
        t1 = self.clock.t
        self.cmd("confirm_match", {"match_id": match["match_id"]}, role="social_worker")
        # 取确认发生之前的时间点：允许跳过事件序号
        view = self.app.queries.family_timeline("F1", "social_worker", at_ts=t1 - 1)
        self.assertEqual(view["reunions"], [])
        # 同一时刻（同 ts 的命令事件整体可见）之后
        view_now = self.app.queries.family_timeline("F1", "social_worker", at_ts=t1 + 1)
        self.assertEqual(len(view_now["reunions"]), 1)
        self.assertGreaterEqual(t0, 0)
