"""服务重启接续、时点回放与日志崩溃修复。"""

from src.journal import EventStore
from src.service import ReliefService
from tests.support import ServiceCase


class RecoveryTest(ServiceCase):
    def test_recover_surfaces_all_open_work(self) -> None:
        bkk, nt = self.seed_shelters()
        # 容量预警：安置点 10 人容量接收 9 人
        for i in range(9):
            self.service.receive_person(
                "rescue", shelter_id="SH_BKK", name=f"群众{i}",
                id_documents=[f"ID-{i}"])
        # 待确认转运
        patient = self.service.receive_person(
            "medical", shelter_id="SH_BKK", name="透析患者", id_documents=["ID-P"])["person_id"]
        arr = self.service.request_arrangement(
            "medical", kind="medical_transfer", person_id=patient,
            to_shelter_id="SH_NT", reason="定点透析")
        self.clock.advance(hours=25)  # 超过 24 小时未确认
        # 药品低存量补给
        med = self.service.receive_medication(
            "medical", shelter_id="SH_BKK", medication="透析护理包", quantity=6)["med_batch_id"]
        self.service.dispense_medication(
            "medical", person_id=patient, med_batch_id=med, quantity=2)
        # 开放寻亲
        self.service.open_trace(
            "social_welfare", person_id=patient,
            looking_for={"name": "家属"}, clues=["曼谷"])

        # ---- 模拟重启 ----
        restarted = self.new_service()
        report = restarted.recover()
        self.assertEqual(report["replayed_events"], self.service.state.seq)
        self.assertTrue(any(a["shelter_id"] == "SH_BKK"
                            for a in report["open_capacity_alerts"]))
        pending_ids = {a["arrangement_id"] for a in report["pending_arrangements"]}
        self.assertIn(arr["arrangement_id"], pending_ids)
        stale = {s["arrangement_id"] for s in report["tick"]["stale_pending_transfers"]}
        self.assertIn(arr["arrangement_id"], stale)
        meds = {r["medication"] for r in report["open_medication_resupplies"]}
        self.assertIn("透析护理包", meds)
        self.assertEqual(len(report["open_traces"]), 1)

        # 重启后仍可接续操作：确认并执行转运
        restarted.confirm_arrangement("medical", arr["arrangement_id"])
        restarted.execute_arrangement("medical", arr["arrangement_id"])
        self.assertEqual(restarted.state.resolve(patient) and
                         restarted.state.people[patient]["current_shelter"], "SH_NT")

    def test_tick_raises_new_resupply_after_restart(self) -> None:
        bkk, _ = self.seed_shelters()
        med = self.service.receive_medication(
            "medical", shelter_id="SH_BKK", medication="胰岛素", quantity=4)["med_batch_id"]
        pid = self.service.receive_person(
            "medical", shelter_id="SH_BKK", name="糖友", id_documents=["ID-DM"])["person_id"]
        self.service.dispense_medication(
            "medical", person_id=pid, med_batch_id=med, quantity=1)  # 剩 3
        # 补给待办在发药时已自动产生；重启后巡检不应重复
        restarted = self.new_service()
        tick = restarted.tick()
        self.assertEqual(tick["medication_resupplies"], [])
        open_rs = [r for r in restarted.state.resupplies.values() if r["status"] == "open"]
        self.assertEqual(len(open_rs), 1)


class PointInTimeTest(ServiceCase):
    def test_family_location_and_issues_at_any_time(self) -> None:
        bkk, nt = self.seed_shelters()
        pid = self.service.receive_person(
            "rescue", shelter_id=bkk, name="家长", id_documents=["ID-H"])["person_id"]
        fid = "F-T"
        self.service.declare_family(
            "rescue", fid, [{"person_id": pid, "relation": "户主"}])
        supply = self.service.receive_supply(
            "shelter", shelter_id=bkk, item="毛毯", quantity=5)["supply_batch_id"]
        self.service.issue_supply(
            "shelter", person_id=pid, supply_batch_id=supply, quantity=1,
            family_id=fid, receipt_key="RCP-T1")
        t_after_issue = self.clock.value
        self.clock.advance(hours=6)
        arr = self.service.request_arrangement(
            "rescue", kind="transfer", person_id=pid, to_shelter_id=nt)
        self.service.confirm_arrangement("coordinator", arr["arrangement_id"])
        self.service.execute_arrangement("rescue", arr["arrangement_id"])
        t_after_move = self.clock.value

        # 当前时点：人在暖武里，领用记录仍在
        now_view = self.service.family_timeline("coordinator", fid)
        member = now_view["members"][0]
        self.assertEqual(member["current_location"]["shelter_id"], "SH_NT")
        self.assertEqual(len(member["issues"]), 1)
        # 路径包含两个安置点
        path = [h["shelter_id"] for h in member["location_path"]]
        self.assertEqual(path, ["SH_BKK", "SH_NT"])

        # 转移前的时点：人还在曼谷，领用记录已存在
        past = self.service.family_timeline("coordinator", fid, ts=t_after_issue)
        past_member = past["members"][0]
        self.assertEqual(past_member["current_location"]["shelter_id"], "SH_BKK")
        self.assertEqual(len(past_member["issues"]), 1)
        self.assertIsNone(past_member["current_location"]["arrangement_id"])

        # 更早（家庭尚未登记）：查询返回未找到
        from src.errors import NotFoundError
        with self.assertRaises(NotFoundError):
            self.service.family_timeline(
                "coordinator", fid, ts="2026-10-01T07:59:00+00:00")

        # as_of 字符串边界校验
        self.assertEqual(past["as_of"], t_after_issue)
        self.assertLess(t_after_issue, t_after_move)


class JournalTest(ServiceCase):
    def test_torn_tail_is_truncated_on_open(self) -> None:
        path = self.dir / "events.log"
        store = EventStore(path)
        store.append("shelter.registered", {"shelter_id": "S1", "name": "一", "capacity": 1})
        store.append("shelter.registered", {"shelter_id": "S2", "name": "二", "capacity": 1})
        good_count = sum(1 for _ in store.events())
        # 模拟崩溃：写入半截 JSON
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"seq": 3, "type": "broken')
        reopened = EventStore(path)
        self.assertEqual(sum(1 for _ in reopened.events()), good_count)
        self.assertEqual(reopened.latest_seq, 2)
        # 修复后可继续追加且序号连续
        event = reopened.append("shelter.registered",
                                {"shelter_id": "S3", "name": "三", "capacity": 1})
        self.assertEqual(event["seq"], 3)

    def test_replay_is_deterministic(self) -> None:
        self.seed_shelters()
        pid = self.service.receive_person(
            "rescue", shelter_id="SH_BKK", name="同一人", id_documents=["ID-Z"])["person_id"]
        s1 = self.new_service().state
        s2 = self.new_service().state
        self.assertEqual(s1.people[pid]["name"], s2.people[pid]["name"])
        self.assertEqual(s1.seq, s2.seq)
