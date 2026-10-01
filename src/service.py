"""领域命令服务：校验、决策、发出事件。

所有写操作都以事件落盘，状态由 projection 回放得到。命令层只做
- 权限校验（access）
- 业务规则校验（未完成才可变更、守恒、冲突转复核等）
- 幂等：同一回执/幂等键返回首次决定
- 派生：容量预警、失联线索匹配、药品低存量补给
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from . import access
from .errors import (
    NotFoundError,
    PermissionDeniedError,
    StateConflictError,
    ValidationError,
)
from .journal import EventStore, utc_now
from .projection import (
    CAPACITY_WARN_RATIO,
    MED_RESUPPLY_THRESHOLD,
    State,
    load_state,
    state_at,
)

PENDING_TRANSFER_AGE_HOURS = 24


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


class ReliefService:
    def __init__(self, store: EventStore, *, now: Callable[[], str] = utc_now):
        store.clock = now
        self.store = store
        self.now = now
        self.state: State = load_state(store.events())

    # ================= 安置点与容量 =====================================

    def register_shelter(self, role: str, shelter_id: str, name: str,
                         capacity: int, area: str = "") -> dict:
        access.require(role, "shelter.register")
        if not name or capacity < 0:
            raise ValidationError("安置点名称与容量必填且不能为负")
        if shelter_id in self.state.shelters:
            raise StateConflictError(f"安置点 {shelter_id} 已登记")
        self.store.append("shelter.registered", {
            "shelter_id": shelter_id, "name": name, "area": area, "capacity": capacity})
        self._refresh()
        return {"shelter_id": shelter_id}

    def change_capacity(self, role: str, shelter_id: str, new_capacity: int) -> dict:
        access.require(role, "shelter.capacity")
        self._shelter(shelter_id)
        if new_capacity < 0:
            raise ValidationError("容量不能为负")
        self.store.append("shelter.capacity_changed", {
            "shelter_id": shelter_id, "new_capacity": new_capacity})
        self._refresh()
        self.run_capacity_checks()
        return {"shelter_id": shelter_id, "capacity": new_capacity}

    # ================= 人员接收与核验 ===================================

    def receive_person(self, role: str, *, shelter_id: str, name: str,
                       id_documents: Optional[list[str]] = None,
                       age: Optional[int] = None, family_id: Optional[str] = None,
                       relation: Optional[str] = None, batch_id: Optional[str] = None,
                       idem_key: Optional[str] = None) -> dict:
        access.require(role, "person.receive")
        self._shelter(shelter_id)
        if not name or not name.strip():
            raise ValidationError("姓名不能为空")
        if batch_id and batch_id not in self.state.batches:
            raise NotFoundError(f"撤离批次 {batch_id} 不存在")

        def do() -> dict:
            person_id = _new_id("per")
            is_minor = age is not None and age < 18
            self.store.append("person.received", {
                "person_id": person_id, "shelter_id": shelter_id, "name": name,
                "id_documents": sorted(set(id_documents or [])), "age": age,
                "is_minor": is_minor, "family_id": family_id, "relation": relation,
                "batch_id": batch_id, "received_at": self.now()})
            if batch_id:
                self.store.append("batch.person_assigned",
                                  {"batch_id": batch_id, "person_id": person_id})
            self._refresh()
            self.run_capacity_checks()
            status = "verified" if id_documents else "pending_verification"
            return {"person_id": person_id, "status": status,
                    "reception_fact_preserved": True}

        return self._idem(idem_key, "person.receive", do)

    def verify_person(self, role: str, person_id: str, *,
                      id_documents: Optional[list[str]] = None,
                      basis: str = "证件核验") -> dict:
        access.require(role, "person.verify")
        pid = self._person(person_id)
        if self.state.people[pid]["status"] == "verified":
            return {"person_id": pid, "status": "verified", "already": True}
        # 内容冲突：新核验材料与既有登记的姓名不一致 -> 人工复核
        if id_documents:
            conflicting = self._detect_identity_conflict(pid, id_documents)
            if conflicting:
                review_id = self._open_review(
                    "identity_conflict",
                    f"人员 {pid} 核验材料与既有登记冲突: {'; '.join(conflicting)}",
                    {"person_id": pid, "id_documents": id_documents})
                return {"person_id": pid, "status": "pending_review", "review_id": review_id}
        if not id_documents and basis != "人工确认":
            raise ValidationError("无证件核验需人工确认依据")
        self.store.append("person.verified", {
            "person_id": pid, "id_documents": sorted(set(id_documents or [])), "basis": basis})
        self._refresh()
        return {"person_id": pid, "status": "verified"}

    def _detect_identity_conflict(self, pid: str, docs: list[str]) -> list[str]:
        """证件号已属于另一个档案即冲突（同名同号是合并线索，不算冲突）。"""
        conflicts = []
        owner_by_doc: dict[str, str] = {}
        for other_id, person in self.state.people.items():
            if other_id == pid or person.get("merged_into"):
                continue
            for doc in person.get("id_documents", []):
                owner_by_doc[doc] = other_id
        for doc in docs:
            owner = owner_by_doc.get(doc)
            if owner and owner != pid:
                conflicts.append(f"{doc} 已登记于档案 {owner}")
        return conflicts

    # ================= 家庭关系与档案合并 ===============================

    def declare_family(self, role: str, family_id: str,
                       members: list[dict]) -> dict:
        """声明一户家庭：members 为 [{person_id, relation}]，人员须已接收。"""
        access.require(role, "family.declare")
        for member in members:
            self._person(member["person_id"])
        self.store.append("family.declared", {"family_id": family_id, "members": members})
        self._refresh()
        return {"family_id": family_id, "member_count": len(members)}

    def link_family_member(self, role: str, family_id: str, person_id: str,
                           relation: str = "成员") -> dict:
        access.require(role, "family.link")
        pid = self._person(person_id)
        self.store.append("family.member_linked",
                          {"family_id": family_id, "person_id": pid, "relation": relation})
        self._refresh()
        return {"family_id": family_id, "person_id": pid}

    def merge_profiles(self, role: str, kept_id: str, merged_id: str) -> dict:
        """合并两个档案。最初接收事实原样保留到存活档案。

        内容冲突（姓名、证件指向不同人）不自动合并，转人工复核。
        """
        access.require(role, "profile.merge")
        kept = self._person(kept_id)
        merged = self._person(merged_id)
        if kept == merged:
            raise ValidationError("不能合并同一档案")
        kp, mp = self.state.people[kept], self.state.people[merged]
        conflicts = self._profile_conflicts(kp, mp)
        if conflicts:
            review_id = self._open_review(
                "merge_conflict",
                f"档案 {kept} 与 {merged} 内容冲突，无法自动合并: {'; '.join(conflicts)}",
                {"kept_id": kept, "merged_id": merged, "conflicts": conflicts})
            return {"status": "pending_review", "review_id": review_id}
        self.store.append("profiles.merged", {"kept_id": kept, "merged_id": merged})
        self._refresh()
        facts = self.state.reception_facts.get(kept, [])
        return {"kept_id": kept, "merged_id": merged,
                "reception_facts": facts}

    def _profile_conflicts(self, a: dict, b: dict) -> list[str]:
        conflicts: list[str] = []
        # 同档案已挂的不同证件在核验阶段处理；此处看姓名与家庭归属
        if a.get("name") and b.get("name"):
            na = "".join(a["name"].split())
            nb = "".join(b["name"].split())
            if na != nb and not self._name_compatible(na, nb):
                conflicts.append(f"姓名不一致({a['name']} / {b['name']})")
        fa, fb = a.get("family_id"), b.get("family_id")
        if fa and fb and fa != fb:
            conflicts.append(f"分属不同家庭({fa} / {fb})")
        return conflicts

    @staticmethod
    def _name_compatible(a: str, b: str) -> bool:
        # 单方姓名是另一方的子串（昵称/简称）视为兼容，需人工确认则走复核
        return a in b or b in a

    # ================= 撤离批次 =========================================

    def register_batch(self, role: str, batch_id: str, *,
                       origin_area: str = "", kind: str = "standard",
                       scheduled_ts: Optional[str] = None) -> dict:
        access.require(role, "batch.manage")
        if batch_id in self.state.batches:
            raise StateConflictError(f"批次 {batch_id} 已登记")
        self.store.append("batch.registered", {
            "batch_id": batch_id, "origin_area": origin_area, "kind": kind,
            "scheduled_ts": scheduled_ts or self.now()})
        self._refresh()
        return {"batch_id": batch_id}

    def assign_to_batch(self, role: str, batch_id: str, person_id: str) -> dict:
        access.require(role, "batch.manage")
        if batch_id not in self.state.batches:
            raise NotFoundError(f"批次 {batch_id} 不存在")
        pid = self._person(person_id)
        self.store.append("batch.person_assigned", {"batch_id": batch_id, "person_id": pid})
        self._refresh()
        return {"batch_id": batch_id, "person_id": pid}

    # ================= 安排：转运/出院/返家/调剂 =========================

    def request_arrangement(self, role: str, *, kind: str, person_id: str,
                            from_shelter_id: Optional[str] = None,
                            to_shelter_id: Optional[str] = None,
                            destination: Optional[str] = None,
                            batch_id: Optional[str] = None, reason: str = "",
                            idem_key: Optional[str] = None) -> dict:
        action = "arrangement.medical_transfer" if kind == "medical_transfer" else (
            "arrangement.reallocate" if kind == "reallocate" else "arrangement.transfer")
        if kind in {"discharge", "return_home"}:
            action = "arrangement.discharge" if kind == "discharge" else "arrangement.return_home"
        access.require(role, action)
        pid = self._person(person_id)
        if kind not in {"transfer", "medical_transfer", "discharge", "return_home", "reallocate"}:
            raise ValidationError(f"未知安排类型 {kind}")
        if kind in {"transfer", "medical_transfer", "reallocate"} and not to_shelter_id:
            raise ValidationError("转运/调剂必须给出目标安置点")
        if to_shelter_id:
            self._shelter(to_shelter_id)
        if batch_id and batch_id not in self.state.batches:
            raise NotFoundError(f"批次 {batch_id} 不存在")
        if self.state.open_arrangements_for(pid):
            raise StateConflictError("该人员已有未完成安排，请先处理")

        def do() -> dict:
            arrangement_id = _new_id("arr")
            self.store.append("arrangement.requested", {
                "arrangement_id": arrangement_id, "person_id": pid, "kind": kind,
                "from_shelter_id": from_shelter_id, "to_shelter_id": to_shelter_id,
                "destination": destination, "batch_id": batch_id, "reason": reason,
                "medical": kind == "medical_transfer"})
            self._refresh()
            return {"arrangement_id": arrangement_id, "status": "pending"}

        return self._idem(idem_key, f"arrangement.request.{kind}", do)

    def confirm_arrangement(self, role: str, arrangement_id: str) -> dict:
        access.require(role, "arrangement.confirm")
        a = self._open_arrangement(arrangement_id)
        dest = a.get("to_shelter_id")
        if dest:
            self._shelter(dest)
            # 医疗转运可突破预警线，但不能突破硬容量
            if not a.get("medical") and self._occupancy_at(dest) >= self.state.shelters[dest]["capacity"]:
                raise StateConflictError(f"目标安置点 {dest} 已满，需跨区调剂")
        self.store.append("arrangement.confirmed", {"arrangement_id": arrangement_id})
        self._refresh()
        return {"arrangement_id": arrangement_id, "status": "confirmed"}

    def execute_arrangement(self, role: str, arrangement_id: str) -> dict:
        access.require(role, "arrangement.execute")
        a = self._open_arrangement(arrangement_id)
        if a["status"] != "confirmed":
            raise StateConflictError("安排尚未确认，不能执行")
        dest = a.get("to_shelter_id")
        if dest and self._occupancy_at(dest) >= self.state.shelters[dest]["capacity"]:
            raise StateConflictError(f"目标安置点 {dest} 已满，执行将超容量")
        self.store.append("arrangement.executed",
                          {"arrangement_id": arrangement_id, "executed_ts": self.now()})
        self._refresh()
        self.run_capacity_checks()
        return {"arrangement_id": arrangement_id, "status": "executed"}

    def cancel_arrangement(self, role: str, arrangement_id: str, reason: str = "") -> dict:
        access.require(role, "arrangement.cancel")
        self._open_arrangement(arrangement_id)  # 已完成的安排不能取消
        self.store.append("arrangement.cancelled",
                          {"arrangement_id": arrangement_id, "reason": reason})
        self._refresh()
        self.run_capacity_checks()
        return {"arrangement_id": arrangement_id, "status": "cancelled"}

    # ================= 健康需求 =========================================

    def record_health(self, role: str, person_id: str, *, condition: str,
                      needs_dialysis: bool = False, mobility: str = "自主",
                      notes: str = "") -> dict:
        access.require(role, "health.record")
        pid = self._person(person_id)
        if not condition:
            raise ValidationError("健康状况不能为空")
        self.store.append("health.recorded", {
            "person_id": pid, "condition": condition,
            "needs_dialysis": needs_dialysis, "mobility": mobility, "notes": notes})
        self._refresh()
        return {"person_id": pid, "condition": condition, "needs_dialysis": needs_dialysis}

    # ================= 药品批次与补给 ===================================

    def receive_medication(self, role: str, *, shelter_id: str, medication: str,
                           quantity: int, med_batch_id: Optional[str] = None,
                           expires: Optional[str] = None) -> dict:
        access.require(role, "med.manage")
        self._shelter(shelter_id)
        if quantity <= 0 or not medication:
            raise ValidationError("药品名称与数量必须有效")
        med_batch_id = med_batch_id or _new_id("med")
        self.store.append("medication.batch_received", {
            "med_batch_id": med_batch_id, "shelter_id": shelter_id,
            "medication": medication, "quantity": quantity, "expires": expires})
        self._refresh()
        # 到货自动了结该点该药品的开放补给待办
        for rid, r in list(self.state.resupplies.items()):
            if (r["status"] == "open" and r["shelter_id"] == shelter_id
                    and r["medication"] == medication):
                self.store.append("medication.resupply_resolved",
                                  {"resupply_id": rid, "med_batch_id": med_batch_id})
        self._refresh()
        return {"med_batch_id": med_batch_id, "quantity": quantity}

    def dispense_medication(self, role: str, *, person_id: str, med_batch_id: str,
                            quantity: int, issue_id: Optional[str] = None) -> dict:
        access.require(role, "med.manage")
        pid = self._person(person_id)
        if med_batch_id not in self.state.med_batches:
            raise NotFoundError(f"药品批次 {med_batch_id} 不存在")
        if quantity <= 0:
            raise ValidationError("发药数量必须为正")
        batch = self.state.med_batches[med_batch_id]
        available = batch["quantity"] - batch["dispensed"]
        if quantity > available:
            raise StateConflictError(
                f"批次 {med_batch_id} 可用 {available}，不足 {quantity}")
        issue_id = issue_id or _new_id("iss")
        self.store.append("medication.dispensed", {
            "med_batch_id": med_batch_id, "person_id": pid,
            "quantity": quantity, "issue_id": issue_id})
        self._refresh()
        self.run_medication_checks()
        return {"issue_id": issue_id, "quantity": quantity}

    # ================= 物资批次守恒 =====================================

    def receive_supply(self, role: str, *, shelter_id: str, item: str,
                       quantity: int, supply_batch_id: Optional[str] = None) -> dict:
        access.require(role, "supply.manage")
        self._shelter(shelter_id)
        if quantity <= 0 or not item:
            raise ValidationError("物资名称与数量必须有效")
        supply_batch_id = supply_batch_id or _new_id("sup")
        self.store.append("supply.received", {
            "supply_batch_id": supply_batch_id, "shelter_id": shelter_id,
            "item": item, "quantity": quantity})
        self._refresh()
        return {"supply_batch_id": supply_batch_id, "quantity": quantity}

    def issue_supply(self, role: str, *, person_id: str, supply_batch_id: str,
                     quantity: int, family_id: Optional[str] = None,
                     receipt_key: str, reason: str = "") -> dict:
        """发放物资。receipt_key 是回执编号：重复回执直接返回原决定。"""
        access.require(role, "supply.manage")
        pid = self._person(person_id)
        if supply_batch_id not in self.state.supply_batches:
            raise NotFoundError(f"物资批次 {supply_batch_id} 不存在")
        if quantity <= 0:
            raise ValidationError("发放数量必须为正")
        if not receipt_key:
            raise ValidationError("回执编号不能为空")
        existing = self._issue_by_receipt(receipt_key)
        if existing is not None:
            return {**existing, "duplicate": True}
        batch = self.state.supply_batches[supply_batch_id]
        available = batch["quantity"] - batch["issued"]
        if quantity > available:
            raise StateConflictError(
                f"批次 {supply_batch_id} 可用 {available}，不足 {quantity}")
        issue_id = _new_id("iss")
        self.store.append("supply.issued", {
            "issue_id": issue_id, "supply_batch_id": supply_batch_id,
            "person_id": pid, "quantity": quantity, "family_id": family_id,
            "receipt_key": receipt_key, "reason": reason})
        self._refresh()
        return self.state.issues[issue_id]

    def return_supply(self, role: str, *, original_issue_id: str,
                      quantity: int, reason: str = "") -> dict:
        """退回：冲减已发数量，原发放记录保留不变。"""
        access.require(role, "supply.manage")
        if original_issue_id not in self.state.issues:
            raise NotFoundError(f"原发放记录 {original_issue_id} 不存在")
        original = self.state.issues[original_issue_id]
        if original["kind"] != "supply":
            raise ValidationError("只能退回到普通发放记录")
        if quantity <= 0 or quantity > original["quantity"]:
            raise ValidationError("退回数量须为正且不超过原数量")
        self.store.append("supply.returned", {
            "original_issue_id": original_issue_id, "quantity": quantity, "reason": reason})
        self._refresh()
        return {"original_issue_id": original_issue_id, "returned": quantity}

    def reissue_supply(self, role: str, *, person_id: str, supply_batch_id: str,
                       quantity: int, original_issue_id: Optional[str] = None,
                       family_id: Optional[str] = None, receipt_key: str,
                       reason: str = "") -> dict:
        """补发：原记录不动，从（可能是新的）批次再出一批并关联原记录。"""
        access.require(role, "supply.manage")
        pid = self._person(person_id)
        if supply_batch_id not in self.state.supply_batches:
            raise NotFoundError(f"物资批次 {supply_batch_id} 不存在")
        if not receipt_key:
            raise ValidationError("回执编号不能为空")
        existing = self._issue_by_receipt(receipt_key)
        if existing is not None:
            return {**existing, "duplicate": True}
        if quantity <= 0:
            raise ValidationError("补发数量必须为正")
        if original_issue_id and original_issue_id not in self.state.issues:
            raise NotFoundError(f"原发放记录 {original_issue_id} 不存在")
        batch = self.state.supply_batches[supply_batch_id]
        available = batch["quantity"] - batch["issued"]
        if quantity > available:
            raise StateConflictError(
                f"批次 {supply_batch_id} 可用 {available}，不足 {quantity}")
        issue_id = _new_id("iss")
        self.store.append("supply.reissued", {
            "issue_id": issue_id, "supply_batch_id": supply_batch_id,
            "person_id": pid, "quantity": quantity, "family_id": family_id,
            "original_issue_id": original_issue_id, "reason": reason,
            "receipt_key": receipt_key})
        self._refresh()
        return self.state.issues[issue_id]

    def _issue_by_receipt(self, receipt_key: str) -> Optional[dict]:
        for issue in self.state.issues.values():
            if issue.get("receipt_key") == receipt_key:
                return issue
        return None

    # ================= 寻亲线索与团聚 ===================================

    def open_trace(self, role: str, *, person_id: str,
                   looking_for: Optional[dict] = None, clues: Optional[list[str]] = None,
                   contact: str = "", idem_key: Optional[str] = None) -> dict:
        access.require(role, "trace.open")
        pid = self._person(person_id)

        def do() -> dict:
            trace_id = _new_id("trc")
            self.store.append("trace.opened", {
                "trace_id": trace_id, "person_id": pid,
                "looking_for": looking_for or {}, "clues": clues or [],
                "contact": contact})
            self._refresh()
            self.run_trace_matching()
            return {"trace_id": trace_id, "status": "open"}

        return self._idem(idem_key, "trace.open", do)

    def add_trace_clue(self, role: str, trace_id: str, clue: str) -> dict:
        access.require(role, "trace.open")
        if trace_id not in self.state.traces:
            raise NotFoundError(f"寻亲记录 {trace_id} 不存在")
        if not clue:
            raise ValidationError("线索内容不能为空")
        self.store.append("trace.clue_added", {"trace_id": trace_id, "clue": clue})
        self._refresh()
        self.run_trace_matching()
        return {"trace_id": trace_id}

    def confirm_trace_match(self, role: str, trace_id: str, matched_person_id: str,
                            basis: list[str]) -> dict:
        """人工确认团聚：basis 必须给出依据（线索、家庭关系等），留痕可回放。"""
        access.require(role, "trace.decide")
        if trace_id not in self.state.traces:
            raise NotFoundError(f"寻亲记录 {trace_id} 不存在")
        trace = self.state.traces[trace_id]
        if trace["status"] != "open":
            raise StateConflictError("该寻亲记录已了结")
        matched = self._person(matched_person_id)
        if matched == self.state.resolve(trace["person_id"]):
            raise ValidationError("不能与本人匹配")
        if not basis:
            raise ValidationError("确认团聚必须提供依据")
        self.store.append("trace.match_confirmed", {
            "trace_id": trace_id, "matched_person_id": matched,
            "basis": basis, "confirmed_by": role})
        self._refresh()
        return {"trace_id": trace_id, "matched_person_id": matched, "basis": basis}

    def close_trace(self, role: str, trace_id: str) -> dict:
        access.require(role, "trace.decide")
        if trace_id not in self.state.traces:
            raise NotFoundError(f"寻亲记录 {trace_id} 不存在")
        self.store.append("trace.closed", {"trace_id": trace_id})
        self._refresh()
        return {"trace_id": trace_id, "status": "closed"}

    # ================= 人工复核 =========================================

    def resolve_review(self, role: str, review_id: str, *, resolution: str,
                       action: Optional[dict] = None) -> dict:
        access.require(role, "review.resolve")
        if review_id not in self.state.reviews:
            raise NotFoundError(f"复核单 {review_id} 不存在")
        review = self.state.reviews[review_id]
        if review["status"] != "open":
            raise StateConflictError("复核单已了结")
        # 复核结论可携带后续动作：verify / merge
        if action:
            kind = action.get("type")
            payload = review["payload"]
            if kind == "verify":
                self.store.append("person.verified", {
                    "person_id": payload["person_id"],
                    "id_documents": sorted(set(payload.get("id_documents", []))),
                    "basis": f"人工复核: {resolution}"})
            elif kind == "merge":
                self.store.append("profiles.merged", {
                    "kept_id": payload["kept_id"], "merged_id": payload["merged_id"]})
            else:
                raise ValidationError(f"未知复核动作 {kind}")
        self.store.append("review.resolved", {
            "review_id": review_id, "resolution": resolution, "resolved_by": role})
        self._refresh()
        return {"review_id": review_id, "resolution": resolution}

    def _open_review(self, topic: str, reason: str, payload: dict) -> str:
        review_id = _new_id("rev")
        self.store.append("review.opened", {
            "review_id": review_id, "topic": topic, "reason": reason, "payload": payload})
        self._refresh()
        return review_id

    # ================= 派生检查（重启后由 recover/tick 接续） ============

    def run_capacity_checks(self) -> list[dict]:
        """超过 90% 发预警；回落到阈值以下解除。每点同时只有一条开放预警。"""
        changes: list[dict] = []
        for sid, shelter in self.state.shelters.items():
            occupancy = self._occupancy_at(sid)
            ratio = occupancy / shelter["capacity"] if shelter["capacity"] else 0
            alert = self.state.capacity_alerts.get(sid)
            open_alert = alert and alert["status"] == "open"
            if ratio >= CAPACITY_WARN_RATIO and not open_alert:
                level = "full" if occupancy >= shelter["capacity"] else "near_full"
                alert_id = _new_id("alrt")
                self.store.append("capacity.alert_raised", {
                    "alert_id": alert_id, "shelter_id": sid, "level": level,
                    "occupancy": occupancy, "capacity": shelter["capacity"]})
                changes.append({"shelter_id": sid, "level": level})
            elif ratio < CAPACITY_WARN_RATIO and open_alert:
                self.store.append("capacity.alert_cleared", {"shelter_id": sid})
                changes.append({"shelter_id": sid, "level": "cleared"})
        if changes:
            self._refresh()
        return changes

    def run_medication_checks(self) -> list[dict]:
        """某点某药品可用存量低于阈值即开补给待办（已开放不重复）。"""
        raised: list[dict] = []
        open_pairs = {(r["shelter_id"], r["medication"])
                      for r in self.state.resupplies.values() if r["status"] == "open"}
        needs: dict[tuple[str, str], int] = {}
        for batch in self.state.med_batches.values():
            key = (batch["shelter_id"], batch["medication"])
            needs[key] = needs.get(key, 0) + batch["quantity"] - batch["dispensed"]
        for (sid, med), stock in needs.items():
            if stock < MED_RESUPPLY_THRESHOLD and (sid, med) not in open_pairs:
                resupply_id = _new_id("rsp")
                needed = MED_RESUPPLY_THRESHOLD * 2 - stock
                self.store.append("medication.resupply_raised", {
                    "resupply_id": resupply_id, "shelter_id": sid,
                    "medication": med, "needed_quantity": needed,
                    "stock_remaining": stock})
                raised.append({"resupply_id": resupply_id, "shelter_id": sid,
                               "medication": med, "needed_quantity": needed})
        if raised:
            self._refresh()
        return raised

    def run_trace_matching(self) -> list[dict]:
        """对每条开放寻亲记录扫描全部档案，给出带依据的候选（确认仍需人工）。"""
        suggestions: list[dict] = []
        for trace_id, trace in self.state.traces.items():
            if trace["status"] != "open":
                continue
            seeker = self.state.people.get(self.state.resolve(trace["person_id"]), {})
            target_name = (trace.get("looking_for") or {}).get("name", "")
            target_relation = (trace.get("looking_for") or {}).get("relation", "")
            scored: list[tuple[int, str, list[str]]] = []
            for candidate_id, candidate in self.state.people.items():
                if candidate.get("merged_into"):
                    continue
                if candidate_id == trace["person_id"]:
                    continue
                score, basis = self._score_match(
                    seeker, candidate, target_name, target_relation, trace["clues"])
                if score > 0:
                    scored.append((score, candidate_id, basis))
            scored.sort(key=lambda item: (-item[0], item[1]))
            for score, candidate_id, basis in scored[:5]:
                already = any(s["person_id"] == candidate_id for s in trace["suggestions"])
                if not already:
                    self.store.append("trace.match_suggested", {
                        "trace_id": trace_id, "person_id": candidate_id,
                        "score": score, "basis": basis})
                    suggestions.append({"trace_id": trace_id,
                                        "person_id": candidate_id, "score": score})
        if suggestions:
            self._refresh()
        return suggestions

    def _score_match(self, seeker: dict, candidate: dict,
                     target_name: str, target_relation: str,
                     clues: list[str]) -> tuple[int, list[str]]:
        score = 0
        basis: list[str] = []
        cname = candidate.get("name") or ""
        if target_name and cname and (
                target_name in cname or cname in target_name):
            score += 3
            basis.append(f"姓名吻合: {cname}")
        # 同家庭且称谓相符
        sf, cf = seeker.get("family_id"), candidate.get("family_id")
        if sf and cf and sf == cf:
            score += 4
            relation = self.state.families[sf]["members"].get(candidate.get("person_id"), "")
            basis.append(f"同属家庭 {sf}，登记关系 {relation}")
            if target_relation and target_relation in relation:
                score += 2
                basis.append(f"称谓相符: {target_relation}")
        # 线索文本中出现候选姓名或所在安置点
        for clue in clues:
            if cname and cname in clue:
                score += 2
                basis.append(f"线索提及姓名: {clue}")
            shelter_id = candidate.get("current_shelter")
            if shelter_id:
                shelter = self.state.shelters.get(shelter_id, {})
                if shelter.get("name") and shelter["name"] in clue:
                    score += 1
                    basis.append(f"线索指向安置点 {shelter['name']}: {clue}")
        return score, basis

    def tick(self, role: Optional[str] = None) -> dict:
        """一轮巡检：容量预警、失联匹配、药品补给、超期待确认转运。"""
        if role is not None:
            access.require(role, "ops.tick")
        capacity = self.run_capacity_checks()
        medication = self.run_medication_checks()
        traces = self.run_trace_matching()
        stale_transfers = self._stale_pending_transfers()
        return {"capacity_alerts": capacity, "medication_resupplies": medication,
                "trace_suggestions": traces, "stale_pending_transfers": stale_transfers,
                "checked_ts": self.now()}

    def _stale_pending_transfers(self) -> list[dict]:
        now = _parse_ts(self.now())
        stale = []
        for a in self.state.arrangements.values():
            if a["status"] != "pending":
                continue
            age_h = (now - _parse_ts(a["requested_ts"])).total_seconds() / 3600
            if age_h >= PENDING_TRANSFER_AGE_HOURS:
                stale.append({"arrangement_id": a["arrangement_id"],
                              "person_id": a["person_id"], "kind": a["kind"],
                              "age_hours": round(age_h, 1)})
        return stale

    def recover(self, role: Optional[str] = None) -> dict:
        """重启接续：事件已从磁盘回放，汇总所有待办供值班人员接续。"""
        if role is not None:
            access.require(role, "ops.recover")
        tick = self.tick()
        open_alerts = [{"shelter_id": s, **{k: v for k, v in a.items() if k != "shelter_id"}}
                       for s, a in self.state.capacity_alerts.items() if a["status"] == "open"]
        open_resupplies = [r for r in self.state.resupplies.values() if r["status"] == "open"]
        open_traces = [{"trace_id": t["trace_id"], "person_id": t["person_id"],
                        "suggestions": t["suggestions"]}
                       for t in self.state.traces.values() if t["status"] == "open"]
        pending = [{"arrangement_id": a["arrangement_id"], "person_id": a["person_id"],
                    "kind": a["kind"], "status": a["status"],
                    "to_shelter_id": a.get("to_shelter_id")}
                   for a in self.state.arrangements.values()
                   if a["status"] in {"pending", "confirmed"}]
        open_reviews = [r for r in self.state.reviews.values() if r["status"] == "open"]
        return {"replayed_events": self.state.seq, "open_capacity_alerts": open_alerts,
                "open_medication_resupplies": open_resupplies,
                "open_traces": open_traces, "pending_arrangements": pending,
                "open_reviews": open_reviews, "tick": tick}

    # ================= 查询（按岗位脱敏） ===============================

    def get_person(self, role: str, person_id: str) -> dict:
        access.require(role, "person.view")
        pid = self._person(person_id)
        return access.redact_person(role, self.state.people[pid])

    def family_timeline(self, role: str, family_id: str, ts: Optional[str] = None) -> dict:
        """说明一个家庭在任一时点的去向、领用记录与重新团聚依据。"""
        access.require(role, "trace.view")
        events = list(self.store.events())
        state = state_at(events, ts or self.now())
        if family_id not in state.families:
            raise NotFoundError(f"家庭 {family_id} 不存在")
        members = []
        for pid, relation in state.families[family_id]["members"].items():
            person = state.people.get(pid)
            if person is None:
                continue
            locations = [h for h in state.location_history.get(pid, []) if h["ts"] <= (ts or self.now())]
            current = locations[-1] if locations else None
            issues = [i for i in state.issue_history.get(pid, []) if i["ts"] <= (ts or self.now())]
            members.append({
                "person_id": pid,
                "relation": relation,
                "person": access.redact_person(role, person),
                "current_location": current,
                "location_path": locations,
                "issues": issues,
                "reception_facts": state.reception_facts.get(pid, []),
            })
        # 团聚依据：该家庭成员作为寻亲对象被确认的记录
        reunions = []
        member_ids = set(state.families[family_id]["members"])
        for trace in state.traces.values():
            if trace["status"] == "matched" and trace.get("matched_person_id") in member_ids:
                reunions.append({"trace_id": trace["trace_id"],
                                 "seeker_id": trace["person_id"],
                                 "matched_person_id": trace["matched_person_id"],
                                 "basis": trace["basis"],
                                 "confirmed_ts": trace.get("confirmed_ts")})
        return {"family_id": family_id, "as_of": ts or self.now(),
                "members": members, "reunions": reunions}

    # ================= 内部工具 =========================================

    def _idem(self, key: Optional[str], topic: str, do: Callable[[], dict]) -> dict:
        if key and key in self.state.idem:
            return {**self.state.idem[key]["response"], "idempotent_replay": True}
        result = do()
        if key:
            self.store.append("idempotency.seen",
                              {"key": key, "topic": topic, "response": result})
            self._refresh()
        return result

    def _refresh(self) -> None:
        self.state = load_state(self.store.events())

    def _person(self, person_id: str) -> str:
        pid = self.state.resolve(person_id)
        if pid not in self.state.people:
            raise NotFoundError(f"人员 {person_id} 不存在")
        return pid

    def _shelter(self, shelter_id: str) -> dict:
        shelter = self.state.shelters.get(shelter_id)
        if shelter is None:
            raise NotFoundError(f"安置点 {shelter_id} 不存在")
        return shelter

    def _occupancy_at(self, shelter_id: str) -> int:
        return sum(1 for p in self.state.people.values()
                   if p.get("current_shelter") == shelter_id and not p.get("merged_into"))

    def _open_arrangement(self, arrangement_id: str) -> dict:
        a = self.state.arrangements.get(arrangement_id)
        if a is None:
            raise NotFoundError(f"安排 {arrangement_id} 不存在")
        if a["status"] not in {"pending", "confirmed"}:
            raise StateConflictError(
                f"安排已处于 {a['status']}，只能调整尚未完成的安排")
        return a
