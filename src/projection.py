"""事件回放后的领域状态投影与查询。

State 只负责 *应用事实*：所有状态变化都来自事件，命令层（service.py）
负责校验与决策。这样同一批事件回放任意次都得到相同状态，时点查询
只需在指定时间点截断事件流重新投影。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Optional

CAPACITY_WARN_RATIO = 0.9
MED_RESUPPLY_THRESHOLD = 5  # 某安置点某药品存量低于此值触发补给待办


class State:
    def __init__(self) -> None:
        # 人员与家庭
        self.people: dict[str, dict] = {}
        self.families: dict[str, dict] = {}
        # 安置点与撤离批次
        self.shelters: dict[str, dict] = {}
        self.batches: dict[str, dict] = {}
        # 安排（转运/出院/返家/跨区调剂）
        self.arrangements: dict[str, dict] = {}
        # 健康与药品
        self.med_batches: dict[str, dict] = {}
        self.resupplies: dict[str, Any] = {}
        # 物资
        self.supply_batches: dict[str, dict] = {}
        self.issues: dict[str, dict] = {}
        # 寻亲
        self.traces: dict[str, dict] = {}
        # 人工复核
        self.reviews: dict[str, dict] = {}
        # 容量预警
        self.capacity_alerts: dict[str, dict] = {}  # shelter_id -> alert
        # 幂等键 -> 首次决定摘要
        self.idem: dict[str, dict] = {}
        # 留痕历史
        self.location_history: dict[str, list[dict]] = defaultdict(list)
        self.issue_history: dict[str, list[dict]] = defaultdict(list)  # person_id -> 领用
        self.reception_facts: dict[str, list[dict]] = defaultdict(list)  # 档案 -> 接收事实（含合并来源）
        self.seq = 0

    # ---- 工具 ----------------------------------------------------------

    def resolve(self, person_id: str) -> str:
        """档案合并后，旧编号解析到存活编号。"""
        seen = set()
        while person_id in self.people and self.people[person_id].get("merged_into"):
            if person_id in seen:
                break
            seen.add(person_id)
            person_id = self.people[person_id]["merged_into"]
        return person_id

    def family_members(self, family_id: str) -> dict[str, str]:
        family = self.families.get(family_id)
        return dict(family["members"]) if family else {}

    def occupancy(self, shelter_id: str) -> int:
        return sum(1 for p in self.people.values()
                   if p.get("current_shelter") == shelter_id and not p.get("merged_into"))

    def med_stock(self, shelter_id: str, medication: str) -> int:
        total = 0
        for batch in self.med_batches.values():
            if batch["shelter_id"] == shelter_id and batch["medication"] == medication:
                total += batch["quantity"] - batch["dispensed"]
        return total

    def open_arrangements_for(self, person_id: str) -> list[dict]:
        pid = self.resolve(person_id)
        return [a for a in self.arrangements.values()
                if self.resolve(a["person_id"]) == pid and a["status"] in {"pending", "confirmed"}]

    # ---- 事件应用 ------------------------------------------------------

    def apply(self, event: dict) -> None:
        seq = event["seq"]
        ts = event["ts"]
        data = event.get("data", {})
        handler = getattr(self, f"_on_{event['type'].replace('.', '_')}", None)
        if handler is None:
            raise ValueError(f"未知事件类型: {event['type']}")
        handler(seq, ts, data)
        self.seq = seq

    def _on_person_received(self, seq: int, ts: str, d: dict) -> None:
        pid = d["person_id"]
        person = self.people.setdefault(pid, {})
        person.update({
            "person_id": pid,
            "name": d.get("name"),
            "status": "pending_verification" if not d.get("id_documents") else "verified",
            "id_documents": list(d.get("id_documents", [])),
            "age": d.get("age"),
            "is_minor": bool(d.get("is_minor", False)),
            "received_at": d.get("received_at", ts),
            "received_shelter": d["shelter_id"],
            "current_shelter": d["shelter_id"],
            "batch_id": d.get("batch_id"),
            "family_id": d.get("family_id"),
            "relation": d.get("relation"),
            "health": person.get("health", []),
        })
        fact = {"person_id": pid, "received_at": person["received_at"],
                "shelter_id": person["received_shelter"], "via_merge": False}
        self.reception_facts[pid].append(fact)
        self.location_history[pid].append(
            {"ts": person["received_at"], "shelter_id": d["shelter_id"],
             "reason": "received", "arrangement_id": None})
        if d.get("family_id"):
            self._link_family(d["family_id"], pid, d.get("relation", "成员"), ts)

    def _on_person_verified(self, seq: int, ts: str, d: dict) -> None:
        pid = self.resolve(d["person_id"])
        person = self.people[pid]
        person["status"] = "verified"
        if d.get("id_documents"):
            merged = {*person.get("id_documents", []), *d["id_documents"]}
            person["id_documents"] = sorted(merged)
        person["verified_at"] = ts
        person["verify_basis"] = d.get("basis", "证件核验")

    def _link_family(self, family_id: str, pid: str, relation: str, ts: str) -> None:
        family = self.families.setdefault(family_id, {"family_id": family_id, "members": {}, "created_ts": ts})
        family["members"].setdefault(pid, relation)
        person = self.people.get(pid)
        if person is not None:
            person["family_id"] = family_id

    def _on_family_member_linked(self, seq: int, ts: str, d: dict) -> None:
        self._link_family(d["family_id"], self.resolve(d["person_id"]), d.get("relation", "成员"), ts)

    def _on_family_declared(self, seq: int, ts: str, d: dict) -> None:
        family = self.families.setdefault(d["family_id"],
                                          {"family_id": d["family_id"], "members": {}, "created_ts": ts})
        for member in d.get("members", []):
            mid = self.resolve(member["person_id"])
            family["members"].setdefault(mid, member.get("relation", "成员"))
            if mid in self.people:
                self.people[mid]["family_id"] = d["family_id"]

    def _on_profiles_merged(self, seq: int, ts: str, d: dict) -> None:
        kept = self.resolve(d["kept_id"])
        merged = d["merged_id"]
        if kept == merged:
            return
        kept_person = self.people[kept]
        merged_person = self.people.get(merged, {})
        # 最初接收事实原样保留：把被合并档案的接收记录搬到存活档案下
        for fact in self.reception_facts.get(merged, []):
            self.reception_facts[kept].append({**fact, "via_merge": True, "merged_id": merged})
        # 家庭关系合并：把旧编号在家庭成员表中替换为存活编号
        family_id = kept_person.get("family_id") or merged_person.get("family_id")
        if family_id:
            family = self.families.setdefault(family_id, {"family_id": family_id, "members": {}, "created_ts": ts})
            relation = family["members"].pop(merged, kept_person.get("relation") or merged_person.get("relation", "成员"))
            family["members"].setdefault(kept, relation)
            kept_person["family_id"] = family_id
            merged_person["family_id"] = family_id
        # 证件信息互补，但无证件状态不被自动提升为已核实，必须走核验
        docs = {*kept_person.get("id_documents", []), *merged_person.get("id_documents", [])}
        kept_person["id_documents"] = sorted(docs)
        if merged_person.get("health"):
            kept_person["health"] = [*kept_person.get("health", []), *merged_person["health"]]
        if merged_person.get("status") == "verified" and kept_person["status"] == "pending_verification":
            kept_person["status"] = "pending_verification"  # 合并不代替核验
        merged_person["merged_into"] = kept
        merged_person["merged_at"] = ts
        # 历史轨迹与领用记录归并到存活档案，时点查询不丢事实
        if merged in self.location_history:
            self.location_history[kept].extend(self.location_history.pop(merged))
            self.location_history[kept].sort(key=lambda h: h["ts"])
        if merged in self.issue_history:
            merged_records = self.issue_history.pop(merged)
            for record in merged_records:
                record["person_id"] = kept
            self.issue_history[kept].extend(merged_records)
            self.issue_history[kept].sort(key=lambda h: h["ts"])

    def _on_shelter_registered(self, seq: int, ts: str, d: dict) -> None:
        self.shelters[d["shelter_id"]] = {
            "shelter_id": d["shelter_id"], "name": d["name"], "area": d.get("area", ""),
            "capacity": int(d["capacity"]), "created_ts": ts,
        }

    def _on_shelter_capacity_changed(self, seq: int, ts: str, d: dict) -> None:
        self.shelters[d["shelter_id"]]["capacity"] = int(d["new_capacity"])

    def _on_batch_registered(self, seq: int, ts: str, d: dict) -> None:
        self.batches[d["batch_id"]] = {
            "batch_id": d["batch_id"], "origin_area": d.get("origin_area", ""),
            "kind": d.get("kind", "standard"), "scheduled_ts": d.get("scheduled_ts"),
            "person_ids": [], "created_ts": ts,
        }

    def _on_batch_person_assigned(self, seq: int, ts: str, d: dict) -> None:
        pid = self.resolve(d["person_id"])
        batch = self.batches[d["batch_id"]]
        if pid not in batch["person_ids"]:
            batch["person_ids"].append(pid)
        if pid in self.people:
            self.people[pid]["batch_id"] = d["batch_id"]

    def _on_arrangement_requested(self, seq: int, ts: str, d: dict) -> None:
        self.arrangements[d["arrangement_id"]] = {
            "arrangement_id": d["arrangement_id"], "person_id": d["person_id"],
            "kind": d["kind"], "from_shelter_id": d.get("from_shelter_id"),
            "to_shelter_id": d.get("to_shelter_id"), "destination": d.get("destination"),
            "batch_id": d.get("batch_id"), "reason": d.get("reason", ""),
            "medical": d.get("medical", False),
            "status": "pending", "requested_ts": ts, "history": [{"ts": ts, "status": "pending"}],
        }

    def _on_arrangement_confirmed(self, seq: int, ts: str, d: dict) -> None:
        a = self.arrangements[d["arrangement_id"]]
        a["status"] = "confirmed"
        a["confirmed_ts"] = ts
        a["history"].append({"ts": ts, "status": "confirmed"})

    def _on_arrangement_executed(self, seq: int, ts: str, d: dict) -> None:
        a = self.arrangements[d["arrangement_id"]]
        a["status"] = "executed"
        a["executed_ts"] = d.get("executed_ts", ts)
        a["history"].append({"ts": a["executed_ts"], "status": "executed"})
        pid = self.resolve(a["person_id"])
        dest = a.get("to_shelter_id")
        if a["kind"] in {"discharge", "return_home"} or dest is None:
            if pid in self.people:
                self.people[pid]["current_shelter"] = None
        else:
            if pid in self.people:
                self.people[pid]["current_shelter"] = dest
        if pid in self.people:
            self.location_history[pid].append(
                {"ts": a["executed_ts"], "shelter_id": dest, "reason": a["kind"],
                 "arrangement_id": a["arrangement_id"]})

    def _on_arrangement_cancelled(self, seq: int, ts: str, d: dict) -> None:
        a = self.arrangements[d["arrangement_id"]]
        a["status"] = "cancelled"
        a["cancel_ts"] = ts
        a["cancel_reason"] = d.get("reason", "")
        a["history"].append({"ts": ts, "status": "cancelled"})

    def _on_health_recorded(self, seq: int, ts: str, d: dict) -> None:
        pid = self.resolve(d["person_id"])
        record = {k: v for k, v in d.items() if k != "person_id"}
        record["recorded_ts"] = ts
        self.people[pid].setdefault("health", []).append(record)

    def _on_medication_batch_received(self, seq: int, ts: str, d: dict) -> None:
        self.med_batches[d["med_batch_id"]] = {
            "med_batch_id": d["med_batch_id"], "medication": d["medication"],
            "quantity": int(d["quantity"]), "dispensed": 0,
            "shelter_id": d["shelter_id"], "received_ts": ts,
            "expires": d.get("expires"),
        }

    def _on_medication_dispensed(self, seq: int, ts: str, d: dict) -> None:
        batch = self.med_batches[d["med_batch_id"]]
        batch["dispensed"] += int(d["quantity"])
        pid = self.resolve(d["person_id"])
        self.issue_history[pid].append({
            "ts": ts, "kind": "medication", "med_batch_id": batch["med_batch_id"],
            "medication": batch["medication"], "quantity": d["quantity"],
            "issue_id": d.get("issue_id"),
        })

    def _on_medication_resupply_raised(self, seq: int, ts: str, d: dict) -> None:
        self.resupplies[d["resupply_id"]] = {
            "resupply_id": d["resupply_id"], "shelter_id": d["shelter_id"],
            "medication": d["medication"], "needed_quantity": d["needed_quantity"],
            "status": "open", "raised_ts": ts,
        }

    def _on_medication_resupply_resolved(self, seq: int, ts: str, d: dict) -> None:
        r = self.resupplies[d["resupply_id"]]
        r["status"] = "resolved"
        r["resolved_ts"] = ts

    def _on_supply_received(self, seq: int, ts: str, d: dict) -> None:
        self.supply_batches[d["supply_batch_id"]] = {
            "supply_batch_id": d["supply_batch_id"], "item": d["item"],
            "quantity": int(d["quantity"]), "issued": 0,
            "shelter_id": d["shelter_id"], "received_ts": ts,
        }

    def _on_supply_issued(self, seq: int, ts: str, d: dict) -> None:
        batch = self.supply_batches[d["supply_batch_id"]]
        batch["issued"] += int(d["quantity"])
        record = {
            "issue_id": d["issue_id"], "ts": ts, "kind": "supply",
            "supply_batch_id": batch["supply_batch_id"], "item": batch["item"],
            "quantity": d["quantity"], "person_id": self.resolve(d["person_id"]),
            "family_id": d.get("family_id"), "receipt_key": d.get("receipt_key"),
            "adjusts": None,
        }
        self.issues[d["issue_id"]] = record
        self.issue_history[record["person_id"]].append(record)

    def _on_supply_returned(self, seq: int, ts: str, d: dict) -> None:
        original = self.issues[d["original_issue_id"]]
        batch = self.supply_batches[original["supply_batch_id"]]
        batch["issued"] -= int(d["quantity"])
        pid = original["person_id"]
        record = {
            "issue_id": d.get("issue_id") or f"ret-{d['original_issue_id']}-{seq}",
            "ts": ts, "kind": "return", "supply_batch_id": batch["supply_batch_id"],
            "item": batch["item"], "quantity": -int(d["quantity"]), "person_id": pid,
            "family_id": original.get("family_id"), "reason": d.get("reason", ""),
            "adjusts": d["original_issue_id"],
        }
        self.issues[record["issue_id"]] = record
        self.issue_history[pid].append(record)

    def _on_supply_reissued(self, seq: int, ts: str, d: dict) -> None:
        # 补发：不动原发放记录，从指定批次再出一批并关联原记录
        batch = self.supply_batches[d["supply_batch_id"]]
        batch["issued"] += int(d["quantity"])
        pid = self.resolve(d["person_id"])
        record = {
            "issue_id": d["issue_id"], "ts": ts, "kind": "reissue",
            "supply_batch_id": batch["supply_batch_id"], "item": batch["item"],
            "quantity": d["quantity"], "person_id": pid,
            "family_id": d.get("family_id"), "reason": d.get("reason", ""),
            "adjusts": d.get("original_issue_id"), "receipt_key": d.get("receipt_key"),
        }
        self.issues[d["issue_id"]] = record
        self.issue_history[pid].append(record)

    def _on_trace_opened(self, seq: int, ts: str, d: dict) -> None:
        self.traces[d["trace_id"]] = {
            "trace_id": d["trace_id"], "person_id": self.resolve(d["person_id"]),
            "looking_for": d.get("looking_for", {}), "clues": list(d.get("clues", [])),
            "contact": d.get("contact", ""), "status": "open",
            "opened_ts": ts, "suggestions": [], "matched_person_id": None, "basis": [],
        }

    def _on_trace_clue_added(self, seq: int, ts: str, d: dict) -> None:
        trace = self.traces[d["trace_id"]]
        clue = d.get("clue", "")
        if clue and clue not in trace["clues"]:
            trace["clues"].append(clue)

    def _on_trace_match_suggested(self, seq: int, ts: str, d: dict) -> None:
        trace = self.traces[d["trace_id"]]
        if not any(s["person_id"] == d["person_id"] for s in trace["suggestions"]):
            trace["suggestions"].append(
                {"person_id": d["person_id"], "score": d.get("score", 0),
                 "basis": d.get("basis", []), "suggested_ts": ts})

    def _on_trace_match_confirmed(self, seq: int, ts: str, d: dict) -> None:
        trace = self.traces[d["trace_id"]]
        trace["status"] = "matched"
        trace["matched_person_id"] = self.resolve(d["matched_person_id"])
        trace["basis"] = d.get("basis", [])
        trace["confirmed_by"] = d.get("confirmed_by", "")
        trace["confirmed_ts"] = ts

    def _on_trace_closed(self, seq: int, ts: str, d: dict) -> None:
        trace = self.traces[d["trace_id"]]
        if trace["status"] != "matched":
            trace["status"] = "closed"
        trace["closed_ts"] = ts

    def _on_review_opened(self, seq: int, ts: str, d: dict) -> None:
        self.reviews[d["review_id"]] = {
            "review_id": d["review_id"], "topic": d["topic"],
            "reason": d.get("reason", ""), "payload": d.get("payload", {}),
            "status": "open", "opened_ts": ts,
        }

    def _on_review_resolved(self, seq: int, ts: str, d: dict) -> None:
        review = self.reviews[d["review_id"]]
        review["status"] = "resolved"
        review["resolution"] = d.get("resolution", "")
        review["resolved_by"] = d.get("resolved_by", "")
        review["resolved_ts"] = ts

    def _on_capacity_alert_raised(self, seq: int, ts: str, d: dict) -> None:
        self.capacity_alerts[d["shelter_id"]] = {
            "alert_id": d["alert_id"], "shelter_id": d["shelter_id"],
            "level": d["level"], "occupancy": d["occupancy"], "capacity": d["capacity"],
            "status": "open", "raised_ts": ts,
        }

    def _on_capacity_alert_cleared(self, seq: int, ts: str, d: dict) -> None:
        alert = self.capacity_alerts.get(d["shelter_id"])
        if alert:
            alert["status"] = "cleared"
            alert["cleared_ts"] = ts

    def _on_idempotency_seen(self, seq: int, ts: str, d: dict) -> None:
        self.idem[d["key"]] = {"response": d.get("response", {}), "topic": d.get("topic", "")}


def load_state(events: Iterable[dict]) -> State:
    state = State()
    for event in events:
        state.apply(event)
    return state


def state_at(events: Iterable[dict], ts: str) -> State:
    """重放到 ts（含）为止的事件，用于任一时点查询。"""
    state = State()
    for event in events:
        if event["ts"] <= ts:
            state.apply(event)
    return state
