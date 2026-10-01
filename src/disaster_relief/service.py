"""命令服务：账本的唯一写入入口。

职责：
- 岗位授权（见 permissions）
- 命令校验与业务规则
- 幂等：带 ``command_id`` 的重复回执原样返回首次决定
- 内容冲突：不擅自裁决，登记 ``review.opened`` 转人工复核
- 每条决定落为只追加事件；多事件命令共享同一 command_id
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from .clock import now_ms
from .errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from .matching import score_candidate
from .permissions import authorize_command
from .state import (
    AR_CANCELLED,
    AR_COMPLETED,
    AR_CONFIRMED,
    AR_PENDING,
    MATCH_CONFIRMED,
    MATCH_REJECTED,
    MEDBATCH_RECEIVED,
    SUP_ISSUED,
    ST_MERGED,
    TERMINAL_ARRANGEMENTS,
    TRACE_MATCHED,
    State,
)
from .store import EventStore


class ManualReview(Exception):
    """命令内容与既有事实冲突，需人工复核。"""

    def __init__(self, kind: str, subject: dict[str, Any], payload: dict[str, Any],
                 *, trace_id: str | None = None, note: str = "") -> None:
        super().__init__(kind)
        self.kind = kind
        self.subject = subject
        self.payload = payload
        self.trace_id = trace_id
        self.note = note


def _require(payload: dict[str, Any], *keys: str) -> None:
    missing = [key for key in keys if payload.get(key) in (None, "")]
    if missing:
        raise ValidationError("缺少必填字段", details={"missing": missing})


def _int(payload: dict[str, Any], key: str, *, minimum: int | None = None) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValidationError(f"字段 {key} 必须是整数", details={"field": key})
    if minimum is not None and value < minimum:
        raise ValidationError(f"字段 {key} 不得小于 {minimum}", details={"field": key})
    return value


class CommandService:
    def __init__(self, store: EventStore, *, clock: Callable[[], int] = now_ms) -> None:
        self.store = store
        self.state = State()
        self.store.replay_into(self.state)
        self.clock = clock
        self._lock = threading.RLock()
        self._handlers: dict[str, Callable[[dict[str, Any]], list[tuple[str, dict[str, Any]]]]] = {
            name.removeprefix("_cmd_"): getattr(self, name)
            for name in dir(self)
            if name.startswith("_cmd_")
        }

    # ==================================================================
    # 执行入口
    # ==================================================================

    def execute(
        self,
        command: str,
        payload: dict[str, Any] | None = None,
        *,
        role: str,
        command_id: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        payload = dict(payload or {})
        authorize_command(role, command)  # 重复回执也要先认证岗位
        with self._lock:
            if command_id and command_id in self.state.command_index:
                return self._replay_decision(command_id)

            handler = self._handlers.get(command)
            if handler is None:
                raise ValidationError(f"未知命令：{command}")
            ts = self.clock()
            meta = {"role": role}
            if actor:
                meta["actor"] = actor
            if command_id:
                meta["command_id"] = command_id

            try:
                events = handler(payload)
            except ManualReview as review:
                return self._open_review(review, meta, ts)

            records = []
            for event_type, data in events:
                record = self.store.append(event_type, data, meta, ts=ts)
                self.state.apply(record)
                records.append(record)
            result: dict[str, Any] = {
                "status": "accepted",
                "command": command,
                "command_id": command_id,
                "seq": records[-1]["seq"],
                "events": [{"type": r["type"], "seq": r["seq"], "data": r["data"]} for r in records],
            }
            # 命令最关心的主键直接透出
            if len(records) == 1:
                data = records[-1]["data"]
                for key in ("family_id", "person_id", "site_id", "transport_id", "trace_id",
                            "match_id", "review_id", "distribution_id", "alert_id",
                            "med_batch_id", "batch_id"):
                    if key in data:
                        result[key] = data[key]
            return result

    def _replay_decision(self, command_id: str) -> dict[str, Any]:
        """重复回执：返回最初的决定，不产生新事件。"""
        original = self.state.command_index[command_id]
        if original["event_type"] == "review.opened":
            return {
                "status": "manual_review",
                "review_id": original["data"]["review_id"],
                "kind": original["data"]["kind"],
                "replayed": True,
                "seq": original["seq"],
            }
        return {
            "status": "accepted",
            "replayed": True,
            "command_id": command_id,
            "seq": original["seq"],
            "events": [{"type": original["event_type"], "seq": original["seq"],
                        "data": original["data"]}],
        }

    def _open_review(self, review: ManualReview, meta: dict[str, Any], ts: int) -> dict[str, Any]:
        review_id = review.payload.get("review_id") or self.state.next_id("REV")
        events: list[tuple[str, dict[str, Any]]] = [(
            "review.opened",
            {
                "review_id": review_id,
                "kind": review.kind,
                "subject": review.subject,
                "payload": {k: v for k, v in review.payload.items() if k != "review_id"},
                "command_id": meta.get("command_id"),
            },
        )]
        if review.trace_id:
            events.append((
                "trace.conflict",
                {"trace_id": review.trace_id,
                 "candidate_person_id": review.subject.get("person_id"),
                 "note": review.note or review.kind},
            ))
        records = []
        for event_type, data in events:
            record = self.store.append(event_type, data, meta, ts=ts)
            self.state.apply(record)
            records.append(record)
        return {
            "status": "manual_review",
            "review_id": review_id,
            "kind": review.kind,
            "message": review.note or "内容冲突，已转人工复核",
            "seq": records[-1]["seq"],
        }

    # ==================================================================
    # 家庭与人员
    # ==================================================================

    def _cmd_register_family(self, p: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        _require(p, "family_id")
        if p["family_id"] in self.state.families:
            raise ConflictError("家庭已登记", details={"family_id": p["family_id"]})
        data = {
            "family_id": p["family_id"],
            "home_province": p.get("home_province", ""),
            "home_district": p.get("home_district", ""),
            "contact": p.get("contact"),
            "members": p.get("members", []),
        }
        for member in data["members"]:
            if member.get("person_id") not in self.state.persons:
                raise ValidationError(
                    "成员尚未接收", details={"person_id": member.get("person_id")}
                )
        return [("family.registered", data)]

    def _cmd_receive_person(self, p: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        _require(p, "person_id", "site_id")
        if p["site_id"] not in self.state.sites:
            raise NotFoundError("安置点不存在", details={"site_id": p["site_id"]})
        if self.state.sites[p["site_id"]]["status"] != "open":
            raise ConflictError("安置点已关闭，无法接收", details={"site_id": p["site_id"]})
        person_id = p["person_id"]
        if person_id in self.state.persons:
            # 同一人在另一安置点被再次登记——不能静默覆盖，转复核
            existing = self.state.persons[person_id]
            raise ManualReview(
                "duplicate_intake",
                {"person_id": person_id},
                {"person_id": person_id,
                 "existing_site": existing["first_received"]["site_id"],
                 "new_site": p["site_id"],
                 "display_name": p.get("display_name", "")},
                note="同一人员标识在不同接收点重复出现",
            )

        events: list[tuple[str, dict[str, Any]]] = []
        family_id = p.get("family_id")
        if family_id and family_id not in self.state.families:
            # 家庭在不同安置点同时到达：首个接收点建档，后续点直接挂接
            events.append((
                "family.registered",
                {"family_id": family_id,
                 "home_province": p.get("home_province", ""),
                 "home_district": p.get("home_district", ""),
                 "contact": p.get("family_contact"),
                 "members": []},
            ))
        events.append((
            "person.received",
            {
                "person_id": person_id,
                "site_id": p["site_id"],
                "batch_id": p.get("batch_id"),
                "family_id": family_id,
                "family_role": p.get("family_role", "member"),
                "display_name": p.get("display_name", ""),
                "documents_missing": p.get("documents_missing", True),
                "id_documents": p.get("id_documents", []),
                "approx_age": p.get("approx_age"),
                "is_minor": p.get("is_minor", False),
                "contact": p.get("contact"),
                "notes": p.get("notes", ""),
            },
        ))
        return events

    def _cmd_verify_person(self, p: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        _require(p, "person_id", "basis")
        person = self.state.person(p["person_id"])
        documents = p.get("id_documents", [])
        # 证件号已绑定到另一个已核实档案：内容冲突，不替换、不合并，转复核
        if documents:
            claimed = {d.get("number") for d in documents if isinstance(d, dict)}
            for other_id, other in self.state.persons.items():
                if other_id == person["id"] or other["status"] == ST_MERGED:
                    continue
                existing = {d.get("number") for d in other.get("id_documents", [])
                            if isinstance(d, dict)}
                if claimed & existing:
                    raise ManualReview(
                        "identity_conflict",
                        {"person_id": person["id"], "other_person_id": other_id},
                        {"person_id": person["id"], "other_person_id": other_id,
                         "shared_document_numbers": sorted(claimed & existing)},
                        note="证件号与另一档案冲突",
                    )
        return [(
            "person.verified",
            {"person_id": person["id"], "id_documents": documents,
             "basis": p["basis"], "evidence_ref": p.get("evidence_ref"),
             "display_name": p.get("display_name")},
        )]

    def _cmd_merge_person(self, p: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        _require(p, "canonical_id", "duplicate_id")
        canonical = self.state.person(p["canonical_id"])
        duplicate = self.state.person(p["duplicate_id"])
        if canonical["id"] == duplicate["id"]:
            raise ValidationError("不能合并同一档案")
        if duplicate["status"] == ST_MERGED:
            raise ConflictError("该档案已并入其他档案", details={"person_id": duplicate["id"]})

        fam_a = self.state.families.get(canonical.get("family_id") or "")
        fam_b = self.state.families.get(duplicate.get("family_id") or "")
        # 两个档案各属不同活跃家庭，且双方家庭都还有其他成员——关系归属冲突
        if (fam_a and fam_b and fam_a["id"] != fam_b["id"]
                and fam_a["status"] == "active" and fam_b["status"] == "active"
                and len(fam_a["members"]) > 1 and len(fam_b["members"]) > 1):
            raise ManualReview(
                "family_conflict",
                {"person_id": canonical["id"], "other_person_id": duplicate["id"],
                 "family_a": fam_a["id"], "family_b": fam_b["id"]},
                {"canonical_id": canonical["id"], "duplicate_id": duplicate["id"],
                 "family_a": fam_a["id"], "family_b": fam_b["id"],
                 "reason": p.get("reason", "")},
                note="两个档案分属成员完整的不同家庭",
            )
        return [(
            "person.merged",
            {"canonical_id": canonical["id"], "duplicate_id": duplicate["id"],
             "reason": p.get("reason", ""), "basis": p.get("basis", "")},
        )]

    def _cmd_link_family_member(self, p: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        _require(p, "person_id", "family_id", "relation")
        person = self.state.person(p["person_id"])
        if p["family_id"] not in self.state.families:
            raise NotFoundError("家庭不存在", details={"family_id": p["family_id"]})
        old_fid = person.get("family_id")
        if old_fid == p["family_id"]:
            raise ConflictError("人员已在该家庭中", details={"family_id": old_fid})
        return [(
            "family.linked",
            {"person_id": person["id"], "family_id": p["family_id"],
             "role": p["relation"]},
        )]

    def _cmd_relink_family(self, p: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        _require(p, "from_family_id", "to_family_id", "members")
        if p["from_family_id"] not in self.state.families or p["to_family_id"] not in self.state.families:
            raise NotFoundError("家庭不存在")
        for pid in p["members"]:
            if pid not in self.state.persons:
                raise ValidationError("成员不存在", details={"person_id": pid})
        return [(
            "family.relinked",
            {"from_family_id": p["from_family_id"], "to_family_id": p["to_family_id"],
             "members": p["members"], "reason": p.get("reason", "复核裁决")},
        )]

    def _cmd_flag_person(self, p: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        _require(p, "person_id", "kind")
        self.state.person(p["person_id"])
        return [("person.flagged",
                 {"person_id": p["person_id"], "kind": p["kind"], "note": p.get("note", "")})]

    # ==================================================================
    # 安置点与撤离批次
    # ==================================================================

    def _cmd_register_site(self, p: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        _require(p, "site_id", "capacity")
        capacity = _int(p, "capacity", minimum=0)
        if p["site_id"] in self.state.sites:
            raise ConflictError("安置点已登记", details={"site_id": p["site_id"]})
        return [(
            "site.registered",
            {"site_id": p["site_id"], "name": p.get("name", p["site_id"]),
             "province": p.get("province", ""), "capacity": capacity,
             "medical_capable": p.get("medical_capable", False),
             "dialysis_capable": p.get("dialysis_capable", False)},
        )]

    def _cmd_change_site_capacity(self, p: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        _require(p, "site_id", "capacity")
        if p["site_id"] not in self.state.sites:
            raise NotFoundError("安置点不存在", details={"site_id": p["site_id"]})
        capacity = _int(p, "capacity", minimum=0)
        return [("site.capacity_changed",
                 {"site_id": p["site_id"], "capacity": capacity})]

    def _cmd_close_site(self, p: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        _require(p, "site_id")
        if p["site_id"] not in self.state.sites:
            raise NotFoundError("安置点不存在", details={"site_id": p["site_id"]})
        return [("site.closed", {"site_id": p["site_id"]})]

    def _cmd_create_evac_batch(self, p: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        _require(p, "batch_id")
        if p["batch_id"] in self.state.evac_batches:
            raise ConflictError("撤离批次已存在", details={"batch_id": p["batch_id"]})
        return [(
            "evac.batch_created",
            {"batch_id": p["batch_id"], "origin": p.get("origin", ""),
             "scheduled_at": p.get("scheduled_at")},
        )]

    def _cmd_set_evac_batch_status(self, p: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        _require(p, "batch_id", "status")
        if p["batch_id"] not in self.state.evac_batches:
            raise NotFoundError("撤离批次不存在", details={"batch_id": p["batch_id"]})
        if p["status"] not in {"planned", "boarding", "departed", "arrived", "cancelled"}:
            raise ValidationError("批次状态非法", details={"status": p["status"]})
        return [("evac.batch_status", {"batch_id": p["batch_id"], "status": p["status"]})]

    # ==================================================================
    # 入住 / 出院 / 返家
    # ==================================================================

    def _cmd_admit_person(self, p: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        _require(p, "person_id", "site_id")
        person = self.state.person(p["person_id"])
        site = self.state.sites.get(p["site_id"])
        if site is None:
            raise NotFoundError("安置点不存在", details={"site_id": p["site_id"]})
        if site["status"] != "open":
            raise ConflictError("安置点已关闭", details={"site_id": p["site_id"]})
        if self.state.available_capacity(p["site_id"]) <= 0:
            raise ConflictError("安置点容量已满", details={"site_id": p["site_id"]})
        return [("person.admitted", {"person_id": person["id"], "site_id": p["site_id"]})]

    def _cmd_discharge_person(self, p: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        _require(p, "person_id")
        person = self.state.person(p["person_id"])
        site_id = p.get("site_id") or (self.state.current_location(person["id"]) or {}).get("ref")
        return [("person.discharged",
                 {"person_id": person["id"], "site_id": site_id, "note": p.get("note", "")})]

    def _cmd_return_family_home(self, p: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        _require(p, "family_id")
        family = self.state.families.get(p["family_id"])
        if family is None:
            raise NotFoundError("家庭不存在", details={"family_id": p["family_id"]})
        if family.get("returned_home_at"):
            raise ConflictError("该家庭已登记返家", details={"family_id": family["id"]})
        events: list[tuple[str, dict[str, Any]]] = [
            ("family.returned_home", {"family_id": family["id"], "note": p.get("reason", "")})
        ]
        # 返家只取消尚未完成的转运安排；已完成的留作历史事实
        for transport in self.state.transports.values():
            if transport["status"] not in TERMINAL_ARRANGEMENTS:
                member_ids = set(family["members"])
                if transport["person_id"] in member_ids:
                    events.append((
                        "transport.status",
                        {"transport_id": transport["id"], "status": AR_CANCELLED,
                         "reason": "家庭返家，未完成转运自动取消"},
                    ))
        return events

    # ==================================================================
    # 转运
    # ==================================================================

    def _cmd_create_transport(self, p: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        _require(p, "person_id", "kind")
        person = self.state.person(p["person_id"])
        kind = p["kind"]
        if kind not in {"general", "medical", "dialysis"}:
            raise ValidationError("转运类型非法", details={"kind": kind})
        dest_site_id = p.get("destination_site_id")
        facility_id = p.get("medical_facility_id")
        if not dest_site_id and not facility_id:
            raise ValidationError("必须指定目的地安置点或医疗机构")
        if dest_site_id and dest_site_id not in self.state.sites:
            raise NotFoundError("目的地安置点不存在", details={"site_id": dest_site_id})

        profile = self.state.medical.get(person["id"])
        needs_dialysis = bool(profile and profile.get("needs_dialysis"))
        if kind == "dialysis" or needs_dialysis:
            if not facility_id and not (
                dest_site_id and self.state.sites[dest_site_id].get("dialysis_capable")
            ):
                raise ConflictError(
                    "透析患者只能转运至具备透析能力的机构",
                    details={"person_id": person["id"]},
                )
        if dest_site_id and self.state.sites[dest_site_id]["status"] != "open":
            raise ConflictError("目的地安置点已关闭", details={"site_id": dest_site_id})

        transport_id = p.get("transport_id") or self.state.next_id("TR")
        data = {
            "transport_id": transport_id,
            "person_id": person["id"],
            "kind": "dialysis" if needs_dialysis and kind == "general" else kind,
            "batch_id": p.get("batch_id"),
            "origin_site_id": p.get("origin_site_id"),
            "destination_site_id": dest_site_id,
            "medical_facility_id": facility_id,
            "scheduled_at": p.get("scheduled_at"),
            "reason": p.get("reason", ""),
            "status": p.get("status", AR_PENDING),
        }
        if data["status"] not in {AR_PENDING, AR_CONFIRMED}:
            raise ValidationError("新建转运只能是待确认或已确认")
        return [("transport.created", data)]

    def _cmd_revise_transport(self, p: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        _require(p, "transport_id")
        transport = self.state.transports.get(p["transport_id"])
        if transport is None:
            raise NotFoundError("转运安排不存在", details={"transport_id": p["transport_id"]})
        if transport["status"] in TERMINAL_ARRANGEMENTS:
            raise ConflictError("转运已结束，不能修改；请新建安排",
                                details={"transport_id": transport["id"],
                                         "status": transport["status"]})
        dest = p.get("destination_site_id", transport.get("destination_site_id"))
        if dest and dest in self.state.sites:
            person_id = transport["person_id"]
            profile = self.state.medical.get(person_id)
            if transport["kind"] == "dialysis" or (profile and profile.get("needs_dialysis")):
                facility = p.get("medical_facility_id", transport.get("medical_facility_id"))
                if not facility and not self.state.sites[dest].get("dialysis_capable"):
                    raise ConflictError("透析患者改派目的地必须具备透析能力")
        return [(
            "transport.revised",
            {"transport_id": transport["id"],
             "destination_site_id": p.get("destination_site_id"),
             "medical_facility_id": p.get("medical_facility_id"),
             "scheduled_at": p.get("scheduled_at"),
             "kind": p.get("kind"),
             "batch_id": p.get("batch_id"),
             "reason": p.get("reason", "")},
        )]

    def _cmd_confirm_transport(self, p: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        _require(p, "transport_id")
        transport = self.state.transports.get(p["transport_id"])
        if transport is None:
            raise NotFoundError("转运安排不存在", details={"transport_id": p["transport_id"]})
        if transport["status"] in TERMINAL_ARRANGEMENTS:
            raise ConflictError("转运已结束，不能确认", details={"transport_id": transport["id"]})
        return [("transport.confirmed", {"transport_id": transport["id"]})]

    def _cmd_set_transport_status(self, p: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        _require(p, "transport_id", "status")
        transport = self.state.transports.get(p["transport_id"])
        if transport is None:
            raise NotFoundError("转运安排不存在", details={"transport_id": p["transport_id"]})
        allowed = {AR_PENDING, AR_CONFIRMED, "in_progress", AR_COMPLETED, AR_CANCELLED}
        if p["status"] not in allowed:
            raise ValidationError("转运状态非法", details={"status": p["status"]})
        if transport["status"] in TERMINAL_ARRANGEMENTS:
            raise ConflictError("转运已结束，状态不可变更",
                                details={"transport_id": transport["id"],
                                         "status": transport["status"]})
        return [("transport.status",
                 {"transport_id": transport["id"], "status": p["status"],
                  "reason": p.get("reason", "")})]

    # ==================================================================
    # 健康需求与药品
    # ==================================================================

    def _cmd_update_medical_profile(self, p: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        _require(p, "person_id")
        person = self.state.person(p["person_id"])
        data = {"person_id": person["id"]}
        for key in ("patient_type", "needs_dialysis", "dialysis_frequency_hours", "mobility",
                    "medication", "notes", "preferred_facility_id", "priority"):
            if key in p:
                data[key] = p[key]
        return [("medical.profile_updated", data)]

    def _cmd_register_medication_supply(self, p: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        _require(p, "person_id", "medication", "quantity_per_refill", "supply_days")
        person = self.state.person(p["person_id"])
        _int(p, "quantity_per_refill", minimum=1)
        _int(p, "supply_days", minimum=1)
        if person["id"] in self.state.med_supplies:
            raise ConflictError("药品补给档案已存在", details={"person_id": person["id"]})
        return [(
            "med.supply_registered",
            {"person_id": person["id"], "medication": p["medication"],
             "quantity_per_refill": p["quantity_per_refill"], "unit": p.get("unit", ""),
             "supply_days": p["supply_days"], "site_id": p.get("site_id"),
             "last_refill_at": p.get("last_refill_at")},
        )]

    def _cmd_refill_medication(self, p: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        _require(p, "person_id")
        person = self.state.person(p["person_id"])
        supply = self.state.med_supplies.get(person["id"])
        if supply is None:
            raise NotFoundError("药品补给档案不存在", details={"person_id": person["id"]})
        batch_id = p.get("med_batch_id")
        if batch_id and batch_id not in self.state.med_batches:
            raise NotFoundError("药品批次不存在", details={"med_batch_id": batch_id})
        if batch_id and self.state.med_batches[batch_id]["status"] != MEDBATCH_RECEIVED:
            raise ConflictError("药品批次尚未到货", details={"med_batch_id": batch_id})
        return [(
            "med.refilled",
            {"person_id": person["id"], "med_batch_id": batch_id,
             "quantity": p.get("quantity", supply["quantity_per_refill"]),
             "site_id": p.get("site_id")},
        )]

    def _cmd_register_med_batch(self, p: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        _require(p, "med_batch_id", "medication", "quantity")
        _int(p, "quantity", minimum=0)
        if p["med_batch_id"] in self.state.med_batches:
            raise ConflictError("药品批次已登记", details={"med_batch_id": p["med_batch_id"]})
        return [(
            "med.batch_registered",
            {"med_batch_id": p["med_batch_id"], "medication": p["medication"],
             "quantity": p["quantity"], "unit": p.get("unit", ""),
             "site_id": p.get("site_id"), "expected_at": p.get("expected_at")},
        )]

    def _cmd_receive_med_batch(self, p: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        _require(p, "med_batch_id")
        if p["med_batch_id"] not in self.state.med_batches:
            raise NotFoundError("药品批次不存在", details={"med_batch_id": p["med_batch_id"]})
        return [("med.batch_received",
                 {"med_batch_id": p["med_batch_id"], "site_id": p.get("site_id")})]

    def _cmd_deplete_med_batch(self, p: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        _require(p, "med_batch_id")
        if p["med_batch_id"] not in self.state.med_batches:
            raise NotFoundError("药品批次不存在", details={"med_batch_id": p["med_batch_id"]})
        return [("med.batch_depleted", {"med_batch_id": p["med_batch_id"]})]

    # ==================================================================
    # 物资
    # ==================================================================

    def _cmd_register_supply_item(self, p: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        _require(p, "item_id", "name")
        if p["item_id"] in self.state.supply_items:
            raise ConflictError("物资品类已登记", details={"item_id": p["item_id"]})
        return [("supply.item_registered",
                 {"item_id": p["item_id"], "name": p["name"], "unit": p.get("unit", ""),
                  "category": p.get("category", "general")})]

    def _cmd_allocate_supplies(self, p: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        _require(p, "site_id", "item_id", "quantity")
        if p["site_id"] not in self.state.sites:
            raise NotFoundError("安置点不存在", details={"site_id": p["site_id"]})
        if p["item_id"] not in self.state.supply_items:
            raise NotFoundError("物资品类不存在", details={"item_id": p["item_id"]})
        quantity = _int(p, "quantity", minimum=1)
        plan_id = p.get("plan_id") or self.state.next_id("PLAN")
        return [("supply.allocated",
                 {"site_id": p["site_id"], "item_id": p["item_id"],
                  "quantity": quantity, "plan_id": plan_id})]

    def _cmd_distribute_supplies(self, p: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        _require(p, "site_id", "person_id", "item_id", "quantity")
        person = self.state.person(p["person_id"])
        if p["site_id"] not in self.state.sites:
            raise NotFoundError("安置点不存在", details={"site_id": p["site_id"]})
        if p["item_id"] not in self.state.supply_items:
            raise NotFoundError("物资品类不存在", details={"item_id": p["item_id"]})
        quantity = _int(p, "quantity", minimum=1)
        available = self.state.allocation_available(p["site_id"], p["item_id"])
        if quantity > available:
            raise ConflictError(
                "可分配库存不足",
                details={"site_id": p["site_id"], "item_id": p["item_id"],
                         "requested": quantity, "available": available},
            )
        dist_id = p.get("distribution_id") or self.state.next_id("DIST")
        family = self.state.family_of(person["id"])
        return [(
            "supplies.distributed",
            {"distribution_id": dist_id, "site_id": p["site_id"],
             "person_id": person["id"], "item_id": p["item_id"], "quantity": quantity,
             "unit": self.state.supply_items[p["item_id"]]["unit"],
             "batch_id": p.get("batch_id"),
             "related_distribution_id": p.get("related_distribution_id"),
             "reason": p.get("reason", ""),
             "family_id": family["id"] if family else None},
        )]

    def _cmd_return_supplies(self, p: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        _require(p, "distribution_id")
        dist = self.state.distributions.get(p["distribution_id"])
        if dist is None:
            raise NotFoundError("发放记录不存在", details={"distribution_id": p["distribution_id"]})
        if dist["status"] != SUP_ISSUED:
            raise ConflictError(
                "只有已发放状态可退回；补发请走补发流程",
                details={"distribution_id": dist["id"], "status": dist["status"]},
            )
        quantity = p.get("quantity", dist["quantity"])
        if not isinstance(quantity, int) or quantity <= 0 or quantity > dist["quantity"]:
            raise ValidationError("退回数量非法", details={"quantity": quantity})
        return [("supplies.returned",
                 {"distribution_id": dist["id"], "quantity": quantity,
                  "reason": p.get("reason", "")})]

    def _cmd_reissue_supplies(self, p: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        _require(p, "original_distribution_id")
        source = self.state.distributions.get(p["original_distribution_id"])
        if source is None:
            raise NotFoundError("原发放记录不存在",
                                details={"distribution_id": p["original_distribution_id"]})
        quantity = p.get("quantity", source["quantity"])
        if not isinstance(quantity, int) or quantity <= 0:
            raise ValidationError("补发数量非法", details={"quantity": quantity})
        site_id = p.get("site_id", source["site_id"])
        available = self.state.allocation_available(site_id, source["item_id"])
        if quantity > available:
            raise ConflictError(
                "可分配库存不足，无法补发",
                details={"site_id": site_id, "item_id": source["item_id"],
                         "requested": quantity, "available": available},
            )
        new_id = p.get("new_distribution_id") or self.state.next_id("DIST")
        return [(
            "supplies.reissued",
            {"new_distribution_id": new_id,
             "original_distribution_id": source["id"],
             "site_id": site_id, "person_id": p.get("person_id", source["person_id"]),
             "item_id": source["item_id"], "quantity": quantity,
             "reason": p.get("reason", "补发")},
        )]

    # ==================================================================
    # 寻亲
    # ==================================================================

    def _cmd_open_trace(self, p: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        trace_id = p.get("trace_id") or self.state.next_id("TRACE")
        person_id = p.get("person_id")
        if person_id:
            self.state.person(person_id)
        data = {
            "trace_id": trace_id,
            "person_id": person_id,
            "subject_name": p.get("subject_name", ""),
            "reporter_name": p.get("reporter_name", ""),
            "reporter_contact": p.get("reporter_contact", ""),
            "reporter_relation": p.get("reporter_relation", ""),
            "last_seen_location": p.get("last_seen_location", ""),
            "last_seen_at": p.get("last_seen_at"),
            "description": p.get("description", ""),
            "minor_included": p.get("minor_included", False),
        }
        return [("trace.opened", data)]

    def _cmd_propose_match(self, p: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        _require(p, "trace_id")
        trace = self.state.traces.get(p["trace_id"])
        if trace is None:
            raise NotFoundError("寻亲线索不存在", details={"trace_id": p["trace_id"]})
        if trace["status"] == TRACE_MATCHED:
            raise ConflictError("线索已有确认团聚", details={"trace_id": trace["id"]})
        candidate_id = p.get("candidate_person_id")
        if candidate_id:
            candidate = self.state.persons.get(self.state.canonical_person_id(candidate_id))
            if candidate is None:
                raise NotFoundError("候选档案不存在", details={"person_id": candidate_id})
            scored = score_candidate(trace, candidate)
        else:
            scored = self._best_candidate(trace)
            if scored is None:
                raise NotFoundError("未找到候选档案")
        match_id = p.get("match_id") or self.state.next_id("MATCH")
        return [(
            "match.proposed",
            {"match_id": match_id, "trace_id": trace["id"],
             "candidate_person_id": scored["person_id"],
             "family_id": self.state.persons[scored["person_id"]].get("family_id"),
             "score": scored["score"], "signals": scored["signals"]},
        )]

    def _best_candidate(self, trace: dict[str, Any]) -> dict[str, Any] | None:
        best: dict[str, Any] | None = None
        for person in self.state.persons.values():
            if person["status"] == ST_MERGED:
                continue
            scored = score_candidate(trace, person)
            if scored["score"] > 0 and (best is None or scored["score"] > best["score"]):
                best = scored
        return best

    def _cmd_confirm_match(self, p: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        _require(p, "match_id")
        match = self.state.matches.get(p["match_id"])
        if match is None:
            raise NotFoundError("匹配提议不存在", details={"match_id": p["match_id"]})
        if match["status"] == MATCH_CONFIRMED:
            raise ConflictError("匹配已确认", details={"match_id": match["id"]})
        if match["status"] == MATCH_REJECTED:
            raise ConflictError("匹配已驳回", details={"match_id": match["id"]})
        trace = self.state.traces[match["trace_id"]]
        # 线索在此期间已被另一匹配确认：内容冲突，转复核
        if trace["status"] == TRACE_MATCHED and trace.get("match_id") != match["id"]:
            raise ManualReview(
                "match_conflict",
                {"trace_id": trace["id"], "match_id": match["id"],
                 "confirmed_match_id": trace.get("match_id")},
                {"trace_id": trace["id"], "match_id": match["id"],
                 "confirmed_match_id": trace.get("match_id"),
                 "candidate_person_id": match["person_id"]},
                trace_id=trace["id"],
                note="线索已确认另一匹配",
            )
        return [("match.confirmed",
                 {"match_id": match["id"], "note": p.get("note", "")})]

    def _cmd_reject_match(self, p: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        _require(p, "match_id")
        match = self.state.matches.get(p["match_id"])
        if match is None:
            raise NotFoundError("匹配提议不存在", details={"match_id": p["match_id"]})
        if match["status"] in {MATCH_CONFIRMED, MATCH_REJECTED}:
            raise ConflictError("匹配已定论", details={"match_id": match["id"]})
        return [("match.rejected",
                 {"match_id": match["id"], "reason": p.get("reason", "")})]

    def _cmd_close_trace(self, p: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        _require(p, "trace_id")
        if p["trace_id"] not in self.state.traces:
            raise NotFoundError("寻亲线索不存在", details={"trace_id": p["trace_id"]})
        return [("trace.closed", {"trace_id": p["trace_id"], "reason": p.get("reason", "")})]

    # ==================================================================
    # 复核与预警
    # ==================================================================

    def _cmd_resolve_review(self, p: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        _require(p, "review_id", "decision")
        review = self.state.reviews.get(p["review_id"])
        if review is None:
            raise NotFoundError("复核单不存在", details={"review_id": p["review_id"]})
        if review["status"] != "open":
            raise ConflictError("复核单已裁决", details={"review_id": review["id"]})
        if p["decision"] not in {"accept", "reject", "relink", "merge", "manual_followup"}:
            raise ValidationError("裁决结果非法", details={"decision": p["decision"]})
        return [("review.resolved",
                 {"review_id": review["id"], "decision": p["decision"],
                  "note": p.get("note", "")})]

    def _cmd_acknowledge_alert(self, p: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        _require(p, "alert_id")
        if p["alert_id"] not in self.state.alerts:
            raise NotFoundError("预警不存在", details={"alert_id": p["alert_id"]})
        return [("alert.acknowledged", {"alert_id": p["alert_id"]})]

    def _cmd_resolve_alert(self, p: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        _require(p, "alert_id")
        if p["alert_id"] not in self.state.alerts:
            raise NotFoundError("预警不存在", details={"alert_id": p["alert_id"]})
        return [("alert.resolved",
                 {"alert_id": p["alert_id"], "note": p.get("note", "")})]
