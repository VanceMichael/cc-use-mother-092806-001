"""容量预警、药品补给、待确认转运与重启恢复测试。"""

from __future__ import annotations

try:
    from ._harness import ServiceTestCase
except ImportError:  # 以 tests 为发现目录运行时
    from _harness import ServiceTestCase

DAY = 86_400_000


class RecoveryTest(ServiceTestCase):
    def test_capacity_alert_raises_then_resolves_when_space_freed(self) -> None:
        self.seed_site("S1", capacity=2)
        for pid in ("P1", "P2"):
            self.cmd("receive_person", {"person_id": pid, "site_id": "S1"},
                     role="rescue_worker")
        produced = self.sweep()
        raised = [r for r in produced["capacity"] if r["type"] == "alert.raised"]
        self.assertTrue(raised)
        self.assertEqual(raised[0]["data"]["severity"], "critical")

        # 同一状态重复巡检不重复预警
        again = self.sweep()
        self.assertFalse([r for r in again["capacity"] if r["type"] == "alert.raised"])

        # 一人出院腾出容量后自动解除
        self.cmd("discharge_person", {"person_id": "P2"}, role="site_worker")
        produced = self.sweep()
        self.assertTrue(any(r["type"] == "alert.resolved" for r in produced["capacity"]))

    def test_recovery_alerts_resume_after_restart(self) -> None:
        self.seed_site("S1", capacity=1)
        self.cmd("receive_person", {"person_id": "P1", "site_id": "S1"},
                 role="rescue_worker")
        # 重启并在启动时巡检：应补出容量危急预警
        app2 = self.restart_app(sweep=True)
        alerts = [a for a in app2.commands.state.alerts.values() if a["kind"] == "capacity"]
        self.assertEqual(len(alerts), 1)
        self.assertIn(app2.commands.state.alerts[alerts[0]["id"]]["status"],
                      {"raised", "acknowledged"})

    def test_medication_due_alert_and_resolved_after_refill(self) -> None:
        self.seed_site("S1")
        self.cmd("receive_person", {"person_id": "P1", "site_id": "S1"},
                 role="rescue_worker")
        # 7 天用量，上次补给在 6 天前——2 天预警窗口内应提醒
        self.cmd("register_medication_supply", {
            "person_id": "P1", "medication": "降压药",
            "quantity_per_refill": 14, "supply_days": 7,
            "last_refill_at": self.clock.t - 6 * DAY,
        }, role="medical_worker")
        produced = self.sweep()
        self.assertTrue(any(
            r["type"] == "alert.raised" and r["data"]["kind"] == "medication_due"
            for r in produced["medication"]
        ))
        due = self.app.queries.medication_due(
            "medical_worker", now=self.clock.t, lead_ms=2 * DAY)
        self.assertEqual(len(due), 1)

        # 完成补给后下一轮解除
        self.cmd("refill_medication", {"person_id": "P1"}, role="medical_worker")
        produced = self.sweep()
        self.assertTrue(any(r["type"] == "alert.resolved" for r in produced["medication"]))

    def test_overdue_med_batch_alert_resolves_on_receipt(self) -> None:
        self.seed_site("S1")
        self.cmd("register_med_batch", {
            "med_batch_id": "MB1", "medication": "透析液", "quantity": 100,
            "expected_at": self.clock.t - 12 * 3600_000,
        }, role="medical_worker")
        produced = self.sweep()
        self.assertTrue(any(
            r["data"].get("kind") == "med_batch_overdue"
            for r in produced["medication"] if r["type"] == "alert.raised"
        ))
        self.cmd("receive_med_batch", {"med_batch_id": "MB1"}, role="medical_worker")
        produced = self.sweep()
        self.assertTrue(any(r["type"] == "alert.resolved" for r in produced["medication"]))

    def test_pending_dialysis_transport_is_critical_alert(self) -> None:
        self.seed_site("S1")
        self.seed_site("S2", dialysis=True)
        self.cmd("receive_person", {"person_id": "P1", "site_id": "S1"},
                 role="rescue_worker")
        self.cmd("update_medical_profile",
                 {"person_id": "P1", "needs_dialysis": True}, role="medical_worker")
        self.cmd("create_transport", {
            "person_id": "P1", "kind": "dialysis", "destination_site_id": "S2",
            "scheduled_at": self.clock.t + 3600_000,
        }, role="medical_worker")
        produced = self.sweep()
        raised = [r["data"] for r in produced["transports"] if r["type"] == "alert.raised"]
        self.assertEqual(len(raised), 1)
        self.assertEqual(raised[0]["severity"], "critical")

        # 确认后解除
        transport_id = self.app.commands.state.transports and next(
            iter(self.app.commands.state.transports))
        self.cmd("confirm_transport", {"transport_id": transport_id}, role="medical_worker")
        produced = self.sweep()
        self.assertTrue(any(r["type"] == "alert.resolved" for r in produced["transports"]))

    def test_open_trace_gets_auto_proposal_then_no_duplicate(self) -> None:
        self.seed_site("S1")
        self.cmd("receive_person", {
            "person_id": "P1", "site_id": "S1", "display_name": "Wanted Child",
        }, role="rescue_worker")
        self.cmd("open_trace", {
            "subject_name": "Wanted Child", "last_seen_location": "S1",
        }, role="social_worker")
        first = self.sweep()
        proposals = [r for r in first["matching"] if r["type"] == "match.proposed"]
        self.assertEqual(len(proposals), 1)
        # 下一轮不重复提议（已有待确认提议）
        second = self.sweep()
        self.assertFalse([r for r in second["matching"] if r["type"] == "match.proposed"])
        # 自动提议仍需人工确认，未自动成团
        self.assertEqual(
            self.app.commands.state.matches[proposals[0]["data"]["match_id"]]["status"],
            "proposed",
        )

    def test_full_state_rebuilds_from_ledger_with_same_seqs(self) -> None:
        self.seed_site("S1")
        self.cmd("receive_person", {"person_id": "P1", "site_id": "S1"},
                 role="rescue_worker")
        self.cmd("receive_person", {"person_id": "P2", "site_id": "S1",
                                    "family_id": "F1"}, role="rescue_worker")
        before = self.app.commands.state.seq
        app2 = self.restart_app()
        self.assertEqual(app2.commands.state.seq, before)
        self.assertEqual(app2.commands.state.person("P1")["first_received"]["site_id"], "S1")
        # 自动 ID 水位恢复：重启后再生成转运号不会与历史撞号
        first_auto = self.cmd("create_transport", {
            "person_id": "P1", "kind": "general", "destination_site_id": "S1",
        }, role="medical_worker")
        app3 = self.restart_app()
        second_auto = app3.commands.execute("create_transport", {
            "person_id": "P1", "kind": "general", "destination_site_id": "S1",
        }, role="medical_worker")
        self.assertNotEqual(first_auto["transport_id"], second_auto["transport_id"])
        self.assertGreater(
            int(second_auto["transport_id"].split("-")[1]),
            int(first_auto["transport_id"].split("-")[1]),
        )
