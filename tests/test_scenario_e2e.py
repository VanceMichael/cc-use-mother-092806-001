"""端到端剧本：一家人被分到不同安置点，透析患者走医疗流程，
无证件成员待核实后合并档案，最终寻亲团聚，全程可回放、重启可接续。"""

from tests.support import ServiceCase


class EndToEndScenarioTest(ServiceCase):
    def test_family_separation_dialysis_and_reunion(self) -> None:
        svc = self.service
        bkk, nt = self.seed_shelters()

        # 1) 父亲凭证件带女儿在曼谷登记；母亲与祖母在暖武里，母亲证件被水冲走
        father = svc.receive_person(
            "rescue", shelter_id=bkk, name="巴育", id_documents=["ID-FATHER"])["person_id"]
        daughter = svc.receive_person(
            "rescue", shelter_id=bkk, name="小莲", age=11)["person_id"]
        mother_b = svc.receive_person(
            "rescue", shelter_id=nt, name="玛莱")["person_id"]  # 无证件
        grandma = svc.receive_person(
            "rescue", shelter_id=nt, name="奶奶", age=67,
            id_documents=["ID-GRANDMA"])["person_id"]

        family_id = "F-BANYEN"
        svc.declare_family("social_welfare", family_id, [
            {"person_id": father, "relation": "父亲"},
            {"person_id": daughter, "relation": "女儿"},
            {"person_id": mother_b, "relation": "母亲"},
            {"person_id": grandma, "relation": "祖母"},
        ])
        self.assertEqual(svc.state.people[mother_b]["status"], "pending_verification")

        # 2) 祖母是透析患者：健康记录由医疗岗登记，走医疗转运到有透析能力的点
        svc.record_health("medical", grandma, condition="尿毒症",
                          needs_dialysis=True, mobility="轮椅",
                          notes="每周二四六透析")
        svc.register_batch("rescue", "B-MED-1", kind="medical")
        svc.assign_to_batch("rescue", "B-MED-1", grandma)
        med_arr = svc.request_arrangement(
            "medical", kind="medical_transfer", person_id=grandma,
            from_shelter_id=nt, to_shelter_id=bkk, batch_id="B-MED-1",
            reason="曼谷透析床位")
        svc.confirm_arrangement("medical", med_arr["arrangement_id"])
        svc.execute_arrangement("medical", med_arr["arrangement_id"])
        self.assertEqual(svc.state.people[grandma]["current_shelter"], bkk)

        # 3) 普通物资与药品走不同流程
        water = svc.receive_supply(
            "shelter", shelter_id=nt, item="饮用水", quantity=50)["supply_batch_id"]
        issue = svc.issue_supply(
            "shelter", person_id=mother_b, supply_batch_id=water, quantity=6,
            family_id=family_id, receipt_key="RCP-0001")
        med = svc.receive_medication(
            "medical", shelter_id=bkk, medication="透析护理包",
            quantity=8)["med_batch_id"]
        svc.dispense_medication(
            "medical", person_id=grandma, med_batch_id=med, quantity=6)
        # 低存量自动产生补给待办
        self.assertTrue(any(r["medication"] == "透析护理包"
                            for r in svc.state.resupplies.values()))

        # 4) 补发与退回：运输遗失一包水，原记录不动，补发新批次
        new_water = svc.receive_supply(
            "shelter", shelter_id=nt, item="饮用水", quantity=20)["supply_batch_id"]
        reissue = svc.reissue_supply(
            "shelter", person_id=mother_b, supply_batch_id=new_water, quantity=6,
            original_issue_id=issue["issue_id"], receipt_key="RCP-0002",
            reason="首批运输遗失")
        self.assertEqual(reissue["adjusts"], issue["issue_id"])

        # 5) 母亲补办证件后在另一档案登记，冲突复核后合并，首接事实保留
        mother_verified = svc.receive_person(
            "shelter", shelter_id=bkk, name="玛莱",
            id_documents=["ID-MOTHER"])["person_id"]
        merge = svc.merge_profiles(
            "coordinator", kept_id=mother_verified, merged_id=mother_b)
        mother = merge["kept_id"]
        first_shelters = {f["shelter_id"] for f in merge["reception_facts"]}
        self.assertEqual(first_shelters, {bkk, nt})
        # 合并后家庭关系仍完整
        self.assertEqual(
            len(svc.state.families[family_id]["members"]), 4)
        self.assertIn(mother, svc.state.families[family_id]["members"])

        # 6) 女儿寻母：线索 + 同家庭关系自动给候选，福利岗人工确认并留依据
        trace = svc.open_trace(
            "social_welfare", person_id=daughter,
            looking_for={"name": "玛莱", "relation": "母亲"},
            clues=["妈妈可能转到曼谷安置点"])
        suggestions = svc.state.traces[trace["trace_id"]]["suggestions"]
        self.assertTrue(any(s["person_id"] == mother for s in suggestions))
        svc.confirm_trace_match(
            "social_welfare", trace["trace_id"], mother,
            basis=["同属家庭 F-BANYEN，登记关系母亲",
                   "补办证件 ID-MOTHER 与本人陈述一致",
                   "女儿照片辨认"])

        # 7) 把母亲转运回曼谷与女儿团聚（旧安排均已完成，不影响既成事实）
        to_bkk = svc.request_arrangement(
            "rescue", kind="transfer", person_id=mother,
            from_shelter_id=nt, to_shelter_id=bkk)
        svc.confirm_arrangement("coordinator", to_bkk["arrangement_id"])
        svc.execute_arrangement("rescue", to_bkk["arrangement_id"])

        # 8) 重启：待办接续，家庭时点视图可说明去向、领用与团聚依据
        restarted = self.new_service()
        report = restarted.recover()
        self.assertEqual(report["replayed_events"], svc.state.seq)
        self.assertTrue(any(
            r["medication"] == "透析护理包"
            for r in report["open_medication_resupplies"]))

        timeline = restarted.family_timeline("coordinator", family_id)
        self.assertEqual(len(timeline["members"]), 4)
        located = {m["relation"]: m["current_location"]["shelter_id"]
                   for m in timeline["members"]}
        self.assertEqual(located["父亲"], bkk)
        self.assertEqual(located["母亲"], bkk)
        self.assertEqual(located["祖母"], bkk)  # 医疗转运到达
        self.assertEqual(located["女儿"], bkk)

        # 领用记录：母亲有发放+补发；祖母有药品发放
        by_relation = {m["relation"]: m for m in timeline["members"]}
        mother_kinds = {r["kind"] for r in by_relation["母亲"]["issues"]}
        self.assertEqual(mother_kinds, {"supply", "reissue"})
        grandma_kinds = {r["kind"] for r in by_relation["祖母"]["issues"]}
        self.assertEqual(grandma_kinds, {"medication"})

        # 团聚依据完整可回放
        self.assertEqual(len(timeline["reunions"]), 1)
        self.assertEqual(timeline["reunions"][0]["matched_person_id"], mother)
        self.assertGreaterEqual(len(timeline["reunions"][0]["basis"]), 3)

        # 首接事实：母亲档案记录了暖武里的最初接收
        mother_facts = by_relation["母亲"]["reception_facts"]
        self.assertTrue(any(f["via_merge"] for f in mother_facts))
        self.assertEqual({f["shelter_id"] for f in mother_facts}, {bkk, nt})

        # 儿童信息对救援队不可见，但福利岗可见
        rescue_view = restarted.family_timeline("rescue", family_id)
        daughter_view = next(
            m for m in rescue_view["members"] if m["relation"] == "女儿")
        self.assertNotIn("age", daughter_view["person"])
        welfare_view = restarted.family_timeline("social_welfare", family_id)
        daughter_w = next(
            m for m in welfare_view["members"] if m["relation"] == "女儿")
        self.assertEqual(daughter_w["person"]["age"], 11)
