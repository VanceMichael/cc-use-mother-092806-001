"""事件折叠出的领域状态。

``State.apply(record)`` 是唯一的状态变更入口：服务重启后重放账本即可
完整恢复。聚合内同时保留位置历史、合并谱系、回执索引等回放所需结构。
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from .errors import IntegrityError, NotFoundError

_ID_PATTERN = re.compile(r"^([A-Z][A-Z0-9]*)-(\d+)$")

# 人员档案状态
ST_UNVERIFIED = "unverified"          # 无证件，待核实
ST_VERIFIED = "verified"              # 已核实
ST_MERGED = "merged"                  # 已并入主档案，仅保留历史事实

# 转运/安排状态
AR_PENDING = "pending"                # 待确认
AR_CONFIRMED = "confirmed"            # 已确认、尚未完成
AR_IN_PROGRESS = "in_progress"
AR_COMPLETED = "completed"            # 终态
AR_CANCELLED = "cancelled"            # 终态

# 物资发放状态
SUP_ISSUED = "issued"
SUP_RETURNED = "returned"
SUP_REISSUED = "reissued"

# 线索与匹配
TRACE_OPEN = "open"
TRACE_MATCHED = "matched"
TRACE_CLOSED = "closed"
TRACE_CONFLICT = "conflict"
MATCH_PROPOSED = "proposed"
MATCH_CONFIRMED = "confirmed"
MATCH_REJECTED = "rejected"

# 复核
REVIEW_OPEN = "open"
REVIEW_RESOLVED = "resolved"

# 预警
ALERT_RAISED = "raised"
ALERT_ACK = "acknowledged"
ALERT_RESOLVED = "resolved"

# 药品批次
MEDBATCH_EXPECTED = "expected"
MEDBATCH_RECEIVED = "received"
MEDBATCH_DEPLETED = "depleted"

TERMINAL_ARRANGEMENTS = frozenset({AR_COMPLETED, AR_CANCELLED})
OPEN_ARRANGEMENTS = frozenset({AR_PENDING, AR_CONFIRMED, AR_IN_PROGRESS})


class State:
    """从事件流折叠的内存状态。"""

    def __init__(self) -> None:
        self.seq = 0
        self.persons: dict[str, dict[str, Any]] = {}
        self.families: dict[str, dict[str, Any]] = {}
        self.sites: dict[str, dict[str, Any]] = {}
        self.evac_batches: dict[str, dict[str, Any]] = {}
        self.transports: dict[str, dict[str, Any]] = {}
        self.medical: dict[str, dict[str, Any]] = {}
        self.med_batches: dict[str, dict[str, Any]] = {}
        self.med_supplies: dict[str, dict[str, Any]] = {}
        self.supply_items: dict[str, dict[str, Any]] = {}
        self.allocations: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        self.distributions: dict[str, dict[str, Any]] = {}
        self.traces: dict[str, dict[str, Any]] = {}
        self.matches: dict[str, dict[str, Any]] = {}
        self.reviews: dict[str, dict[str, Any]] = {}
        self.alerts: dict[str, dict[str, Any]] = {}
        # 幂等索引：command_id -> 首次决定快照
        self.command_index: dict[str, dict[str, Any]] = {}
        # 每个主体的位置历史：person_id -> [{ts, kind, label, ref}]
        self.location_history: dict[str, list[dict[str, Any]]] = defaultdict(list)
        # 合并谱系：canonical_id -> [被并入的人员 id]
        self.alias_of: dict[str, str] = {}
        self.counters: dict[str, int] = defaultdict(int)
        # 已用于预警去重：key -> alert_id
        self._alert_keys: dict[str, str] = {}

    # ------------------------------------------------------------------
    # 折叠入口
    # ------------------------------------------------------------------

    def apply(self, record: dict[str, Any], *, strict: bool = True) -> None:
        seq = record["seq"]
        if strict and seq != self.seq + 1:
            raise IntegrityError(
                f"状态折叠序号断裂：期望{self.seq + 1}，实际{seq}",
                details={"expected": self.seq + 1, "actual": seq},
            )
        handler = getattr(self, f"_apply_{record['type'].replace('.', '_')}", None)
        if handler is None:
            raise IntegrityError(f"未知事件类型：{record['type']}", details={"seq": seq})
        handler(record["ts"], record["data"], record)
        self.seq = seq
        self._note_ids(record["data"])
        # 幂等回执只登记一次（重复事件不应出现，但重放需保持同一结果）
        cid = record["meta"].get("command_id")
        if cid and cid not in self.command_index:
            self.command_index[cid] = {
                "seq": seq,
                "ts": record["ts"],
                "event_type": record["type"],
                "data": record["data"],
            }

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    def next_id(self, prefix: str) -> str:
        self.counters[prefix] += 1
        return f"{prefix}-{self.counters[prefix]:04d}"

    def _note_ids(self, value: Any) -> None:
        """从事件数据中扫描形如 ``PREFIX-0001`` 的标识，恢复计数器水位。

        重启重放时自动生成 ID 不会与历史标识碰撞。
        """
        if isinstance(value, str):
            match = _ID_PATTERN.match(value)
            if match:
                prefix, number = match.groups()
                self.counters[prefix] = max(self.counters[prefix], int(number))
        elif isinstance(value, dict):
            for nested in value.values():
                self._note_ids(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                self._note_ids(nested)

    def canonical_person_id(self, person_id: str) -> str:
        """沿合并链找到主档案 id。"""
        seen: set[str] = set()
        current = person_id
        while current in self.alias_of and current not in seen:
            seen.add(current)
            current = self.alias_of[current]
        return current

    def alias_ids(self, person_id: str) -> list[str]:
        canonical = self.canonical_person_id(person_id)
        return [pid for pid, target in self.alias_of.items() if target == canonical]

    def person(self, person_id: str) -> dict[str, Any]:
        pid = self.canonical_person_id(person_id)
        if pid not in self.persons:
            raise NotFoundError("人员档案不存在", details={"person_id": person_id})
        return self.persons[pid]

    def family_of(self, person_id: str) -> dict[str, Any] | None:
        pid = self.canonical_person_id(person_id)
        fid = self.persons.get(pid, {}).get("family_id")
        return self.families.get(fid) if fid else None

    def family_members(self, family_id: str) -> list[dict[str, Any]]:
        family = self.families.get(family_id)
        if family is None:
            return []
        return [self.persons[pid] for pid in sorted(family["members"])]

    def _record_location(
        self, ts: int, person_id: str, kind: str, label: str, ref: str
    ) -> None:
        self.location_history[person_id].append(
            {"ts": ts, "kind": kind, "label": label, "ref": ref}
        )

    def current_location(self, person_id: str) -> dict[str, Any] | None:
        history = self.location_history.get(self.canonical_person_id(person_id))
        if not history:
            return None
        return history[-1]

    def _move_member(self, person_id: str, family_id: str, role: str, ts: int, source: str) -> None:
        person = self.persons[person_id]
        old_fid = person.get("family_id")
        if old_fid and old_fid in self.families:
            self.families[old_fid]["members"].pop(person_id, None)
        person["family_id"] = family_id
        self.families[family_id]["members"][person_id] = {
            "role": role,
            "joined_at": ts,
            "source": source,
        }

    def site_occupancy(self, site_id: str) -> int:
        """当前在某安置点的人数（按位置历史最后一条计算）。"""
        count = 0
        for pid in self.persons:
            if self.alias_of.get(pid):
                continue  # 被合并档案不重复计数
            location = self.current_location(pid)
            if location and location["kind"] in {"site", "admission"} and location["ref"] == site_id:
                count += 1
        return count

    def available_capacity(self, site_id: str) -> int:
        site = self.sites[site_id]
        return site["capacity"] - self.site_occupancy(site_id)

    # ------------------------------------------------------------------
    # 人员与家庭
    # ------------------------------------------------------------------

    def _apply_person_received(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        if d["person_id"] in self.persons:
            raise IntegrityError("人员重复登记", details={"person_id": d["person_id"]})
        person = {
            "id": d["person_id"],
            "display_name": d.get("display_name") or "未具名人员",
            "status": ST_UNVERIFIED if d.get("documents_missing", True) else ST_VERIFIED,
            "family_id": d.get("family_id"),
            "documents_missing": d.get("documents_missing", True),
            "id_documents": list(d.get("id_documents", [])),
            "approx_age": d.get("approx_age"),
            "is_minor": bool(d.get("is_minor", False)),
            "contact": d.get("contact"),
            "notes": d.get("notes", ""),
            "created_at": ts,
            "first_received": {
                "at": ts,
                "site_id": d["site_id"],
                "batch_id": d.get("batch_id"),
                "by_role": record["meta"].get("role"),
                "seq": record["seq"],
            },
            "merged_receipts": [],
        }
        self.persons[d["person_id"]] = person
        if d.get("family_id"):
            self._attach_to_family(d["person_id"], d["family_id"], d.get("family_role", "member"), ts, "declared")
        self._record_location(ts, d["person_id"], "site", f"安置点 {d['site_id']}", d["site_id"])

    def _attach_to_family(self, pid: str, fid: str, role: str, ts: int, source: str) -> None:
        family = self.families[fid]
        family["members"][pid] = {"role": role, "joined_at": ts, "source": source}
        self.persons[pid]["family_id"] = fid

    def _apply_family_registered(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        if d["family_id"] in self.families:
            raise IntegrityError("家庭重复登记", details={"family_id": d["family_id"]})
        self.families[d["family_id"]] = {
            "id": d["family_id"],
            "home_province": d.get("home_province"),
            "home_district": d.get("home_district"),
            "contact": d.get("contact"),
            "created_at": ts,
            "status": "active",
            "merged_into": None,
            "members": {},
            "merge_history": [],
        }
        for member in d.get("members", []):
            if member["person_id"] in self.persons:
                self._attach_to_family(
                    member["person_id"], d["family_id"],
                    member.get("role", "member"), ts, "declared",
                )

    def _apply_person_verified(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        person = self.person(d["person_id"])
        person["status"] = ST_VERIFIED
        person["documents_missing"] = False
        person["id_documents"] = list(d.get("id_documents", person["id_documents"]))
        if d.get("display_name"):
            person["display_name"] = d["display_name"]
        person.setdefault("verifications", []).append(
            {"at": ts, "basis": d.get("basis", ""), "by_role": record["meta"].get("role"),
             "evidence_ref": d.get("evidence_ref")}
        )

    def _apply_person_merged(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        canonical_id = d["canonical_id"]
        duplicate_id = d["duplicate_id"]
        canonical = self.person(canonical_id)
        duplicate = self.persons[duplicate_id]
        if canonical_id == duplicate_id:
            raise IntegrityError("不能合并到自身", details={"person_id": canonical_id})
        if duplicate["status"] == ST_MERGED:
            raise IntegrityError("已并入的档案不能再次合并", details={"person_id": duplicate_id})

        # 关键不变量：合并后保留"最初接收事实"——取两者最早的接收记录。
        receipts = [canonical["first_received"], duplicate["first_received"],
                    *canonical.get("merged_receipts", []),
                    *duplicate.get("merged_receipts", [])]
        earliest = min(receipts, key=lambda r: r["at"])
        canonical["first_received"] = dict(earliest)
        canonical["merged_receipts"] = receipts

        # 家庭归并：成员搬到主档案家庭（或反过来挂上）
        dup_family = self.families.get(duplicate.get("family_id") or "")
        can_fid = canonical.get("family_id")
        if dup_family and dup_family["status"] == "active":
            if can_fid and can_fid != dup_family["id"]:
                # 两个活跃家庭：把对方成员逐个并入，记录谱系
                target_family = self.families[can_fid]
                for pid, info in list(dup_family["members"].items()):
                    self._move_member(pid, target_family["id"], info["role"], ts, "person-merge")
                dup_family["status"] = "merged"
                dup_family["merged_into"] = target_family["id"]
                target_family["merge_history"].append(
                    {"family_id": dup_family["id"], "at": ts, "reason": d.get("reason", "")}
                )
            elif not can_fid:
                canonical["family_id"] = dup_family["id"]
                dup_family["members"][canonical_id] = {
                    "role": "member", "joined_at": ts, "source": "person-merge"
                }
            dup_family["members"].pop(duplicate_id, None)

        # 医疗档案归并（敏感信息跟随本人）
        if duplicate_id in self.medical and canonical_id not in self.medical:
            self.medical[canonical_id] = self.medical.pop(duplicate_id)
        elif duplicate_id in self.medical:
            kept = self.medical[canonical_id]
            kept.setdefault("merged_notes", []).append(self.medical.pop(duplicate_id))

        # 未结线索改指主档案
        for trace in self.traces.values():
            if trace["person_id"] == duplicate_id and trace["status"] == TRACE_OPEN:
                trace["person_id"] = canonical_id

        # 位置历史与药品需求挂到主档案
        self.location_history[canonical_id].extend(self.location_history.get(duplicate_id, []))
        self.location_history[canonical_id].sort(key=lambda e: e["ts"])
        self.location_history.pop(duplicate_id, None)
        if duplicate_id in self.med_supplies and canonical_id not in self.med_supplies:
            self.med_supplies[canonical_id] = self.med_supplies.pop(duplicate_id)

        duplicate["status"] = ST_MERGED
        duplicate["merged_into"] = canonical_id
        duplicate["merged_at"] = ts
        self.alias_of[duplicate_id] = canonical_id
        canonical.setdefault("aliases", []).append(duplicate_id)

    def _apply_family_linked(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        person = self.person(d["person_id"])
        family = self.families[d["family_id"]]
        self._move_member(person["id"], family["id"], d.get("role", "member"), ts, "linked")
        if family["status"] != "active":
            raise IntegrityError("不能挂接到已合并家庭", details={"family_id": family["id"]})

    def _apply_family_relinked(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        source = self.families[d["from_family_id"]]
        target = self.families[d["to_family_id"]]
        if source["status"] != "active" or target["status"] != "active":
            raise IntegrityError("家庭重组要求两方均为活跃家庭")
        for pid, info in list(d.get("members", {}).items()):
            self._move_member(pid, target["id"], info if isinstance(info, str) else info.get("role", "member"),
                              ts, "review-relink")
        target["merge_history"].append(
            {"family_id": source["id"], "at": ts, "reason": d.get("reason", ""),
             "partial": True}
        )

    def _apply_person_flagged(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        person = self.person(d["person_id"])
        person.setdefault("flags", []).append(
            {"kind": d["kind"], "note": d.get("note", ""), "at": ts}
        )

    # ------------------------------------------------------------------
    # 安置点与撤离批次
    # ------------------------------------------------------------------

    def _apply_site_registered(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        if d["site_id"] in self.sites:
            raise IntegrityError("安置点重复登记", details={"site_id": d["site_id"]})
        self.sites[d["site_id"]] = {
            "id": d["site_id"],
            "name": d.get("name", d["site_id"]),
            "province": d.get("province", ""),
            "capacity": int(d["capacity"]),
            "medical_capable": bool(d.get("medical_capable", False)),
            "dialysis_capable": bool(d.get("dialysis_capable", False)),
            "status": "open",
            "created_at": ts,
        }

    def _apply_site_capacity_changed(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        self.sites[d["site_id"]]["capacity"] = int(d["capacity"])

    def _apply_site_closed(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        self.sites[d["site_id"]]["status"] = "closed"

    def _apply_evac_batch_created(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        self.evac_batches[d["batch_id"]] = {
            "id": d["batch_id"],
            "origin": d.get("origin", ""),
            "scheduled_at": d.get("scheduled_at"),
            "status": "planned",
            "created_at": ts,
        }

    def _apply_evac_batch_status(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        self.evac_batches[d["batch_id"]]["status"] = d["status"]

    # ------------------------------------------------------------------
    # 接收入院/出院/返家
    # ------------------------------------------------------------------

    def _apply_person_admitted(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        self._record_location(
            ts, d["person_id"], "admission",
            f"入住安置点 {d['site_id']}", d["site_id"]
        )

    def _apply_person_discharged(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        self._record_location(
            ts, d["person_id"], "discharge",
            d.get("note", f"离开安置点 {d.get('site_id', '')}"), d.get("site_id", "")
        )

    def _apply_family_returned_home(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        family = self.families[d["family_id"]]
        for pid in family["members"]:
            self._record_location(ts, pid, "home", "返家", "home")
        family["returned_home_at"] = ts

    # ------------------------------------------------------------------
    # 转运安排（可取消/修改的只允许未完成安排）
    # ------------------------------------------------------------------

    def _apply_transport_created(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        if d["transport_id"] in self.transports:
            raise IntegrityError("转运安排重复", details={"transport_id": d["transport_id"]})
        self.transports[d["transport_id"]] = {
            "id": d["transport_id"],
            "person_id": d["person_id"],
            "kind": d["kind"],
            "batch_id": d.get("batch_id"),
            "origin_site_id": d.get("origin_site_id"),
            "destination_site_id": d.get("destination_site_id"),
            "medical_facility_id": d.get("medical_facility_id"),
            "scheduled_at": d.get("scheduled_at"),
            "status": d.get("status", AR_PENDING),
            "reason": d.get("reason", ""),
            "created_seq": record["seq"],
            "created_at": ts,
            "decided_by": record["meta"].get("role"),
            "updates": [],
            "cancel_reason": None,
            "completed_at": None,
        }

    def _apply_transport_revised(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        transport = self.transports[d["transport_id"]]
        if transport["status"] in TERMINAL_ARRANGEMENTS:
            raise IntegrityError("终态安排不得修改", details={"transport_id": transport["id"]})
        before = {k: transport.get(k) for k in
                  ("destination_site_id", "medical_facility_id", "scheduled_at", "kind", "batch_id")}
        for key in ("destination_site_id", "medical_facility_id", "scheduled_at", "kind", "batch_id"):
            if key in d:
                transport[key] = d[key]
        transport["updates"].append(
            {"at": ts, "before": before, "reason": d.get("reason", ""),
             "by_role": record["meta"].get("role")}
        )

    def _apply_transport_confirmed(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        transport = self.transports[d["transport_id"]]
        if transport["status"] in TERMINAL_ARRANGEMENTS:
            raise IntegrityError("终态安排不得确认", details={"transport_id": transport["id"]})
        transport["status"] = AR_CONFIRMED
        transport["confirmed_at"] = ts

    def _apply_transport_status(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        transport = self.transports[d["transport_id"]]
        new_status = d["status"]
        if transport["status"] in TERMINAL_ARRANGEMENTS:
            raise IntegrityError(
                "终态安排不得变更", details={"transport_id": transport["id"], "status": transport["status"]}
            )
        transport["status"] = new_status
        if new_status == AR_COMPLETED:
            transport["completed_at"] = ts
            dest = transport.get("destination_site_id")
            if dest:
                self._record_location(
                    ts, transport["person_id"], "site",
                    f"转运到达 {dest}", dest
                )
        if new_status == AR_CANCELLED:
            transport["cancel_reason"] = d.get("reason", "")

    # ------------------------------------------------------------------
    # 健康需求与药品
    # ------------------------------------------------------------------

    def _apply_medical_profile_updated(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        pid = d["person_id"]
        profile = self.medical.get(pid)
        if profile is None:
            profile = {"person_id": pid, "history": []}
            self.medical[pid] = profile
        for key in ("patient_type", "needs_dialysis", "dialysis_frequency_hours",
                    "mobility", "medication", "notes", "preferred_facility_id", "priority"):
            if key in d:
                profile[key] = d[key]
        profile["updated_at"] = ts
        profile["history"].append({"at": ts, "by_role": record["meta"].get("role"),
                                   "keys": [k for k in d if k != "person_id"]})

    def _apply_med_supply_registered(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        self.med_supplies[d["person_id"]] = {
            "person_id": d["person_id"],
            "medication": d["medication"],
            "quantity_per_refill": d["quantity_per_refill"],
            "unit": d.get("unit", ""),
            "supply_days": d["supply_days"],
            "site_id": d.get("site_id"),
            "last_refill_at": d.get("last_refill_at"),
            "created_at": ts,
        }

    def _apply_med_refilled(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        supply = self.med_supplies[d["person_id"]]
        supply["last_refill_at"] = ts
        supply["last_refill_quantity"] = d.get("quantity", supply["quantity_per_refill"])
        supply["site_id"] = d.get("site_id", supply.get("site_id"))
        supply.setdefault("refills", []).append(
            {"at": ts, "quantity": d.get("quantity", supply["quantity_per_refill"]),
             "batch_id": d.get("med_batch_id"), "by_role": record["meta"].get("role")}
        )

    def _apply_med_batch_registered(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        self.med_batches[d["med_batch_id"]] = {
            "id": d["med_batch_id"],
            "medication": d["medication"],
            "quantity": d["quantity"],
            "unit": d.get("unit", ""),
            "site_id": d.get("site_id"),
            "expected_at": d.get("expected_at"),
            "status": MEDBATCH_EXPECTED,
            "created_at": ts,
            "received_at": None,
        }

    def _apply_med_batch_received(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        batch = self.med_batches[d["med_batch_id"]]
        batch["status"] = MEDBATCH_RECEIVED
        batch["received_at"] = ts
        if d.get("site_id"):
            batch["site_id"] = d["site_id"]

    def _apply_med_batch_depleted(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        self.med_batches[d["med_batch_id"]]["status"] = MEDBATCH_DEPLETED

    # ------------------------------------------------------------------
    # 物资（守恒：发放、退回、补发均留痕）
    # ------------------------------------------------------------------

    def _apply_supply_item_registered(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        self.supply_items[d["item_id"]] = {
            "id": d["item_id"],
            "name": d["name"],
            "unit": d.get("unit", ""),
            "category": d.get("category", "general"),
        }

    def _apply_supply_allocated(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        bucket = self.allocations[d["site_id"]]
        entry = bucket.setdefault(d["item_id"], {"quantity": 0, "plan_id": d["plan_id"]})
        entry["quantity"] += int(d["quantity"])
        entry["updated_at"] = ts

    def _apply_supplies_distributed(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        if d["distribution_id"] in self.distributions:
            raise IntegrityError("发放回执重复", details={"distribution_id": d["distribution_id"]})
        self.distributions[d["distribution_id"]] = {
            "id": d["distribution_id"],
            "site_id": d["site_id"],
            "person_id": d["person_id"],
            "item_id": d["item_id"],
            "quantity": d["quantity"],
            "unit": d.get("unit", self.supply_items.get(d["item_id"], {}).get("unit", "")),
            "status": SUP_ISSUED,
            "issued_at": ts,
            "issued_by": record["meta"].get("role"),
            "batch_id": d.get("batch_id"),
            "related_id": d.get("related_distribution_id"),
            "reason": d.get("reason", ""),
            "family_id": d.get("family_id"),
        }

    def _apply_supplies_returned(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        original = self.distributions[d["distribution_id"]]
        if original["status"] != SUP_ISSUED:
            raise IntegrityError("只有已发放物资可退回", details={"distribution_id": original["id"]})
        original["status"] = SUP_RETURNED
        original["returned_at"] = ts
        original["return_reason"] = d.get("reason", "")
        original["returned_quantity"] = d.get("quantity", original["quantity"])

    def _apply_supplies_reissued(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        # 补发是一张新的发放单，挂回原单以便追溯
        if d["new_distribution_id"] in self.distributions:
            raise IntegrityError("补发回执重复", details={"distribution_id": d["new_distribution_id"]})
        source = self.distributions[d["original_distribution_id"]]
        self.distributions[d["new_distribution_id"]] = {
            "id": d["new_distribution_id"],
            "site_id": d.get("site_id", source["site_id"]),
            "person_id": d.get("person_id", source["person_id"]),
            "item_id": d.get("item_id", source["item_id"]),
            "quantity": d.get("quantity", source["quantity"]),
            "unit": source["unit"],
            "status": SUP_REISSUED,
            "issued_at": ts,
            "issued_by": record["meta"].get("role"),
            "related_id": source["id"],
            "reason": d.get("reason", "补发"),
            "family_id": source.get("family_id"),
        }

    def allocation_available(self, site_id: str, item_id: str) -> int:
        allocated = self.allocations.get(site_id, {}).get(item_id, {}).get("quantity", 0)
        outstanding = 0
        for dist in self.distributions.values():
            if dist["site_id"] != site_id or dist["item_id"] != item_id:
                continue
            if dist["status"] in {SUP_ISSUED, SUP_REISSUED}:
                outstanding += dist["quantity"]
            elif dist["status"] == SUP_RETURNED:
                # 部分退回时仍在受益人手里的数量
                outstanding += dist["quantity"] - dist.get("returned_quantity", dist["quantity"])
        return allocated - outstanding

    # ------------------------------------------------------------------
    # 寻亲线索与团聚
    # ------------------------------------------------------------------

    def _apply_trace_opened(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        self.traces[d["trace_id"]] = {
            "id": d["trace_id"],
            "person_id": d.get("person_id"),
            "subject_name": d.get("subject_name", ""),
            "reporter_name": d.get("reporter_name", ""),
            "reporter_contact": d.get("reporter_contact", ""),
            "reporter_relation": d.get("reporter_relation", ""),
            "last_seen_location": d.get("last_seen_location", ""),
            "last_seen_at": d.get("last_seen_at"),
            "description": d.get("description", ""),
            "minor_included": bool(d.get("minor_included", False)),
            "status": TRACE_OPEN,
            "conflict_of": None,
            "created_at": ts,
        }

    def _apply_trace_conflict(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        trace = self.traces[d["trace_id"]]
        trace["status"] = TRACE_CONFLICT
        trace["conflict_of"] = d.get("candidate_person_id")
        trace["conflict_note"] = d.get("note", "")

    def _apply_match_proposed(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        self.matches[d["match_id"]] = {
            "id": d["match_id"],
            "trace_id": d["trace_id"],
            "person_id": d["candidate_person_id"],
            "family_id": d.get("family_id"),
            "score": d.get("score", 0),
            "signals": d.get("signals", []),
            "status": MATCH_PROPOSED,
            "created_at": ts,
            "resolved_at": None,
        }

    def _apply_match_confirmed(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        match = self.matches[d["match_id"]]
        match["status"] = MATCH_CONFIRMED
        match["resolved_at"] = ts
        match["confirmed_by"] = record["meta"].get("role")
        match["basis_note"] = d.get("note", "")
        trace = self.traces.get(match["trace_id"])
        if trace:
            trace["status"] = TRACE_MATCHED
            trace["match_id"] = match["id"]
            if match["person_id"] and not trace.get("person_id"):
                trace["person_id"] = match["person_id"]

    def _apply_match_rejected(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        match = self.matches[d["match_id"]]
        match["status"] = MATCH_REJECTED
        match["resolved_at"] = ts
        match["reject_reason"] = d.get("reason", "")

    def _apply_trace_closed(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        self.traces[d["trace_id"]]["status"] = TRACE_CLOSED

    # ------------------------------------------------------------------
    # 人工复核
    # ------------------------------------------------------------------

    def _apply_review_opened(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        self.reviews[d["review_id"]] = {
            "id": d["review_id"],
            "kind": d["kind"],
            "status": REVIEW_OPEN,
            "subject": d.get("subject", {}),
            "payload": d.get("payload", {}),
            "command_id": d.get("command_id"),
            "opened_at": ts,
            "opened_by": record["meta"].get("role"),
            "resolution": None,
            "resolved_at": None,
        }

    def _apply_review_resolved(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        review = self.reviews[d["review_id"]]
        review["status"] = REVIEW_RESOLVED
        review["resolved_at"] = ts
        review["resolution"] = {
            "decision": d["decision"],
            "note": d.get("note", ""),
            "by_role": record["meta"].get("role"),
        }

    # ------------------------------------------------------------------
    # 容量预警 / 补给提醒
    # ------------------------------------------------------------------

    def alert_key_exists(self, key: str) -> bool:
        return key in self._alert_keys

    def _apply_alert_raised(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        alert_id = d["alert_id"]
        self.alerts[alert_id] = {
            "id": alert_id,
            "kind": d["kind"],
            "severity": d.get("severity", "warning"),
            "subject_type": d.get("subject_type", ""),
            "subject_id": d.get("subject_id", ""),
            "message": d.get("message", ""),
            "details": d.get("details", {}),
            "status": ALERT_RAISED,
            "created_at": ts,
            "resolved_at": None,
            "dedup_key": d.get("dedup_key"),
        }
        if d.get("dedup_key"):
            self._alert_keys[d["dedup_key"]] = alert_id

    def _apply_alert_acknowledged(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        self.alerts[d["alert_id"]]["status"] = ALERT_ACK

    def _apply_alert_resolved(self, ts: int, d: dict[str, Any], record: dict[str, Any]) -> None:
        alert = self.alerts[d["alert_id"]]
        alert["status"] = ALERT_RESOLVED
        alert["resolved_at"] = ts
        if alert.get("dedup_key"):
            self._alert_keys.pop(alert["dedup_key"], None)
