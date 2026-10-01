"""读模型与时间线。

所有查询都可以"按任一时点"回答：把账本重放到指定序号/时间戳得到
当时的 ``State``，再组织成值班视图。重放是只读的，不触碰主状态。
"""

from __future__ import annotations

from typing import Any

from .errors import NotFoundError
from .permissions import (
    redact_medical,
    redact_med_supply,
    redact_person,
    redact_trace,
)
from .state import MATCH_CONFIRMED, State
from .store import EventStore


class QueryService:
    def __init__(self, store: EventStore, state: State) -> None:
        self.store = store
        self.state = state

    # ------------------------------------------------------------------
    # 时点回放
    # ------------------------------------------------------------------

    def state_at(self, *, seq: int | None = None, ts: int | None = None) -> State:
        """重放事件得到历史状态：seq/ts 同时给出时取更严格者。"""
        snapshot = State()
        for record in self.store.records():
            if seq is not None and record["seq"] > seq:
                break
            if ts is not None and record["ts"] > ts:
                continue
            # 按 ts 过滤会跳过中间事件：历史快照允许序号间隔
            snapshot.apply(record, strict=ts is None)
        return snapshot

    # ------------------------------------------------------------------
    # 人员与家庭
    # ------------------------------------------------------------------

    def person_view(self, person_id: str, role: str) -> dict[str, Any]:
        pid = self.state.canonical_person_id(person_id)
        person = self.state.person(person_id)
        view = redact_person(person, role)
        location = self.state.current_location(pid)
        family = self.state.family_of(pid)
        view.update({
            "canonical_id": pid,
            "aliases": list(person.get("aliases", [])),
            "location": location,
            "family_id": family["id"] if family else None,
            "medical": redact_medical(self.state.medical.get(pid), role),
            "medication_supply": redact_med_supply(self.state.med_supplies.get(pid), role),
        })
        open_transports = [
            self._transport_brief(t) for t in self.state.transports.values()
            if t["person_id"] == pid and t["status"] not in {"completed", "cancelled"}
        ]
        view["open_transports"] = open_transports
        view["first_received"] = person["first_received"]
        return view

    def family_view(self, family_id: str, role: str) -> dict[str, Any]:
        family = self.state.families.get(family_id)
        if family is None:
            raise NotFoundError("家庭不存在", details={"family_id": family_id})
        members = []
        for member in self.state.family_members(family_id):
            view = redact_person(member, role)
            view["location"] = self.state.current_location(member["id"])
            view["family_role"] = family["members"][member["id"]]["role"]
            if role in {"medical_worker", "provincial_coordinator", "supervisor"}:
                profile = self.state.medical.get(member["id"])
                view["medical_summary"] = None if profile is None else {
                    k: profile.get(k) for k in
                    ("patient_type", "needs_dialysis", "mobility", "priority")
                }
            members.append(view)
        sites: dict[str, int] = {}
        for member in members:
            loc = member.get("location")
            if loc and loc["kind"] in {"site", "admission"}:
                sites[loc["ref"]] = sites.get(loc["ref"], 0) + 1
        return {
            "family_id": family["id"],
            "status": family["status"],
            "merged_into": family.get("merged_into"),
            "home_province": family.get("home_province"),
            "home_district": family.get("home_district"),
            "members": members,
            "members_by_site": sites,
            "separated": len(sites) > 1,
            "returned_home_at": family.get("returned_home_at"),
            "merge_history": family.get("merge_history", []),
            "as_of_seq": self.state.seq,
        }

    def families(self) -> list[dict[str, Any]]:
        result = []
        for family in self.state.families.values():
            sites = {
                (loc or {}).get("ref")
                for pid in family["members"]
                for loc in [self.state.current_location(pid)]
                if loc and loc["kind"] in {"site", "admission"}
            }
            result.append({
                "family_id": family["id"],
                "status": family["status"],
                "member_count": len(family["members"]),
                "sites": sorted(s for s in sites if s),
                "separated": len(sites) > 1,
                "returned_home_at": family.get("returned_home_at"),
            })
        return result

    # ------------------------------------------------------------------
    # 家庭时间线：去向 / 领用 / 团聚依据（可按任一时点）
    # ------------------------------------------------------------------

    def family_timeline(
        self,
        family_id: str,
        role: str,
        *,
        at_seq: int | None = None,
        at_ts: int | None = None,
    ) -> dict[str, Any]:
        historical = at_seq is not None or at_ts is not None
        state = self.state_at(seq=at_seq, ts=at_ts) if historical else self.state
        family = state.families.get(family_id)
        if family is None:
            # 家庭可能尚未建立：尝试在全量历史中定位（便于查询极早时点）
            raise NotFoundError("该时点家庭不存在或尚未登记",
                                details={"family_id": family_id})

        member_ids = sorted(family["members"])
        # 当时已并入本家庭成员的档案也算入
        alias_by_canonical: dict[str, list[str]] = {}
        for alias, canonical in state.alias_of.items():
            if canonical in member_ids:
                alias_by_canonical.setdefault(canonical, []).append(alias)
        all_ids = set(member_ids) | {a for aliases in alias_by_canonical.values() for a in aliases}

        members_out = []
        for pid in member_ids:
            person = state.persons[pid]
            path = [dict(e) for e in state.location_history.get(pid, [])]
            # 并入档案携带的接收事实（合并前的去向）
            for alias in alias_by_canonical.get(pid, []):
                path.extend(state.location_history.get(alias, []))
            path.sort(key=lambda e: e["ts"])
            distributions = sorted(
                (self._distribution_brief(d, role) for d in state.distributions.values()
                 if d["person_id"] == pid or d["person_id"] in alias_by_canonical.get(pid, [])),
                key=lambda d: d["issued_at"],
            )
            member_view = redact_person(person, role)
            member_view.update({
                "location": path[-1] if path else None,
                "location_path": path,
                "distributions": distributions,
                "first_received": person["first_received"],
                "aliases": alias_by_canonical.get(pid, []),
            })
            members_out.append(member_view)

        events: list[dict[str, Any]] = []
        # 接收 / 合并 / 转运 / 发放 / 团聚都来自事件本身，逐条可回放
        for record in self.store.records():
            if at_seq is not None and record["seq"] > at_seq:
                break
            if at_ts is not None and record["ts"] > at_ts:
                continue
            data = record["data"]
            etype = record["type"]
            touched = self._event_concerns(etype, data, all_ids, family_id, state)
            if touched:
                events.append({
                    "seq": record["seq"], "ts": record["ts"], "type": etype,
                    "summary": self._summarize(etype, data, state),
                    "refs": touched,
                })

        reunions = []
        for match in state.matches.values():
            if match["status"] != MATCH_CONFIRMED:
                continue
            trace = state.traces.get(match["trace_id"])
            if trace and (match["person_id"] in all_ids or family_id == match.get("family_id")):
                reunions.append({
                    "match_id": match["id"],
                    "trace_id": match["trace_id"],
                    "person_id": match["person_id"],
                    "score": match["score"],
                    "signals": match["signals"],
                    "confirmed_by": match.get("confirmed_by"),
                    "confirmed_at": match["resolved_at"],
                    "basis_note": match.get("basis_note", ""),
                    "reporter_relation": trace.get("reporter_relation", ""),
                    "reporter_name": trace.get("reporter_name", ""),
                })

        return {
            "family_id": family["id"],
            "as_of_seq": state.seq,
            "as_of_ts": at_ts,
            "members": members_out,
            "reunions": sorted(reunions, key=lambda r: r["confirmed_at"] or 0),
            "merge_history": family.get("merge_history", []),
            "events": events,
            "returned_home_at": family.get("returned_home_at"),
        }

    @staticmethod
    def _event_concerns(
        etype: str,
        data: dict[str, Any],
        member_ids: set[str],
        family_id: str,
        state: State,
    ) -> list[str]:
        refs: list[str] = []
        pid = data.get("person_id")
        if pid and (pid in member_ids or state.canonical_person_id(pid) in member_ids):
            refs.append(pid)
        if etype == "family.returned_home" and data.get("family_id") == family_id:
            refs.append(family_id)
        if etype == "person.merged":
            if data.get("canonical_id") in member_ids or data.get("duplicate_id") in member_ids:
                refs.extend([data.get("canonical_id"), data.get("duplicate_id")])
        if etype in {"supplies.distributed", "supplies.returned", "supplies.reissued"}:
            if data.get("family_id") == family_id:
                refs.append(family_id)
            if etype == "supplies.reissued":
                source = state.distributions.get(data.get("original_distribution_id"))
                if source and source["person_id"] in member_ids:
                    refs.append(source["person_id"])
        if etype.startswith("transport."):
            transport = state.transports.get(data.get("transport_id", ""))
            if transport and transport["person_id"] in member_ids:
                refs.append(transport["person_id"])
        if etype == "match.confirmed":
            match = state.matches.get(data.get("match_id", ""))
            if match and (match["person_id"] in member_ids or match.get("family_id") == family_id):
                refs.append(match["person_id"])
        return refs

    @staticmethod
    def _summarize(etype: str, data: dict[str, Any], state: State) -> str:
        if etype == "person.received":
            return f"在 {data['site_id']} 接收（批次 {data.get('batch_id') or '无'}）"
        if etype == "person.admitted":
            return f"入住 {data['site_id']}"
        if etype == "person.discharged":
            return f"离开安置点 {data.get('site_id') or ''}"
        if etype == "person.verified":
            return "身份核实通过"
        if etype == "person.merged":
            return f"档案 {data['duplicate_id']} 并入 {data['canonical_id']}（最初接收事实保留）"
        if etype == "family.returned_home":
            return "全家返家"
        if etype == "family.linked":
            return f"成员挂接到家庭 {data['family_id']}（{data.get('role')}）"
        if etype == "transport.created":
            return f"建立{data['kind']}转运：{data.get('origin_site_id') or '?'} → " \
                   f"{data.get('destination_site_id') or data.get('medical_facility_id')}"
        if etype == "transport.revised":
            return f"转运 {data['transport_id']} 改派（{data.get('reason', '')}）"
        if etype == "transport.confirmed":
            return f"转运 {data['transport_id']} 已确认"
        if etype == "transport.status":
            return f"转运 {data['transport_id']} 状态变更为 {data['status']}"
        if etype == "supplies.distributed":
            return f"发放 {data['item_id']} ×{data['quantity']}"
        if etype == "supplies.returned":
            return f"退回 {data.get('quantity')} 件（{data.get('reason', '')}）"
        if etype == "supplies.reissued":
            return f"补发 {data['item_id']} ×{data['quantity']}，原单 {data['original_distribution_id']}"
        if etype == "match.confirmed":
            match = state.matches.get(data["match_id"], {})
            return f"确认团聚，依据信号：{', '.join(match.get('signals', [])) or '人工确认'}"
        return etype

    @staticmethod
    def _distribution_brief(dist: dict[str, Any], role: str) -> dict[str, Any]:
        return {
            "distribution_id": dist["id"],
            "item_id": dist["item_id"],
            "quantity": dist["quantity"],
            "unit": dist.get("unit", ""),
            "status": dist["status"],
            "issued_at": dist["issued_at"],
            "returned_at": dist.get("returned_at"),
            "returned_quantity": dist.get("returned_quantity"),
            "related_id": dist.get("related_id"),
            "reason": dist.get("reason", ""),
        }

    # ------------------------------------------------------------------
    # 安置点 / 转运 / 线索 / 复核 / 预警
    # ------------------------------------------------------------------

    def sites_overview(self) -> list[dict[str, Any]]:
        result = []
        for site in self.state.sites.values():
            occupancy = self.state.site_occupancy(site["id"])
            result.append({
                "site_id": site["id"],
                "name": site["name"],
                "province": site["province"],
                "capacity": site["capacity"],
                "occupancy": occupancy,
                "available": site["capacity"] - occupancy,
                "utilization": round(occupancy / site["capacity"], 3) if site["capacity"] else None,
                "status": site["status"],
                "medical_capable": site["medical_capable"],
                "dialysis_capable": site["dialysis_capable"],
            })
        return result

    def site_view(self, site_id: str) -> dict[str, Any]:
        site = self.state.sites.get(site_id)
        if site is None:
            raise NotFoundError("安置点不存在", details={"site_id": site_id})
        occupancy = self.state.site_occupancy(site_id)
        supplies = []
        for item_id, bucket in self.state.allocations.get(site_id, {}).items():
            supplies.append({
                "item_id": item_id,
                "allocated": bucket["quantity"],
                "available": self.state.allocation_available(site_id, item_id),
            })
        return {
            "site": site,
            "occupancy": occupancy,
            "available": site["capacity"] - occupancy,
            "supplies": supplies,
        }

    @staticmethod
    def _transport_brief(t: dict[str, Any]) -> dict[str, Any]:
        return {k: t.get(k) for k in (
            "id", "person_id", "kind", "status", "batch_id", "origin_site_id",
            "destination_site_id", "medical_facility_id", "scheduled_at",
            "created_at", "completed_at", "cancel_reason",
        )}

    def transports(self, status: str | None = None) -> list[dict[str, Any]]:
        items = self.state.transports.values()
        if status:
            items = (t for t in items if t["status"] == status)
        return [self._transport_brief(t) for t in items]

    def traces(self, status: str | None = None, role: str = "provincial_coordinator") -> list[dict[str, Any]]:
        items = self.state.traces.values()
        if status:
            items = (t for t in items if t["status"] == status)
        return [redact_trace(t, role) for t in items]

    def trace_view(self, trace_id: str, role: str) -> dict[str, Any]:
        trace = self.state.traces.get(trace_id)
        if trace is None:
            raise NotFoundError("寻亲线索不存在", details={"trace_id": trace_id})
        view = redact_trace(trace, role)
        view["matches"] = [
            m for mid, m in sorted(self.state.matches.items())
            if m["trace_id"] == trace_id
        ]
        return view

    def reviews(self, status: str | None = None) -> list[dict[str, Any]]:
        items = self.state.reviews.values()
        if status:
            items = (r for r in items if r["status"] == status)
        return list(items)

    def alerts(self, status: str | None = None) -> list[dict[str, Any]]:
        items = self.state.alerts.values()
        if status:
            items = (a for a in items if a["status"] == status)
        return list(items)

    def med_batches(self, status: str | None = None) -> list[dict[str, Any]]:
        items = self.state.med_batches.values()
        if status:
            items = (b for b in items if b["status"] == status)
        return list(items)

    def medication_due(self, role: str, *, now: int, lead_ms: int) -> list[dict[str, Any]]:
        """预计在 lead_ms 内断药的人员（医疗信息，按岗脱敏）。"""
        due = []
        for supply in self.state.med_supplies.values():
            last = supply.get("last_refill_at")
            if last is None:
                continue
            run_out = last + supply["supply_days"] * 86_400_000
            if run_out <= now + lead_ms:
                item = {
                    "person_id": supply["person_id"],
                    "run_out_at": run_out,
                    "medication": supply["medication"],
                }
                if role not in {"medical_worker", "provincial_coordinator", "supervisor"}:
                    item["medication"] = "【受限】"
                due.append(item)
        return sorted(due, key=lambda x: x["run_out_at"])

    def person_distributions(self, person_id: str) -> list[dict[str, Any]]:
        pid = self.state.canonical_person_id(person_id)
        return [
            self._distribution_brief(d, "provincial_coordinator")
            for d in self.state.distributions.values()
            if d["person_id"] == pid
        ]
