"""恢复与巡检。

服务启动以及之后每个周期执行同一套 ``RecoverySweeper.run()``：
- 容量预警：占用达到阈值即预警，回落到阈值以下自动解除（去重键防重复）
- 失联匹配：为仍开放的线索重跑候选评分，高分候选生成待确认提议
- 药品补给：预计断药、在途批次逾期均提醒
- 待确认转运：临近出发仍未确认（透析患者提级）提醒
- 家庭分离：成员分布在多个安置点时提醒社会服务

全部产出仍是普通事件（``alert.raised`` / ``match.proposed`` 等），
因此巡检本身可回放、可审计；崩溃重启后重放账本即接续去重状态。
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from .clock import now_ms
from .matching import score_candidate
from .state import (
    ALERT_RAISED,
    AR_PENDING,
    MATCH_PROPOSED,
    MEDBATCH_EXPECTED,
    ST_MERGED,
    TRACE_OPEN,
    State,
)
from .store import EventStore

DAY_MS = 86_400_000

DEFAULT_THRESHOLDS = {
    "capacity_warn_ratio": 0.9,     # 占用率达到 90% 预警
    "capacity_critical_ratio": 1.0,  # 满员/超员危急
    "medication_lead_ms": 2 * DAY_MS,
    "med_batch_overdue_ms": 6 * 3600_000,  # 预计到货后 6 小时仍未到
    "transport_horizon_ms": 12 * 3600_000,
    "match_score": 40,              # 自动提议阈值（确认始终需要人工）
}

SYSTEM_META = {"role": "system", "actor": "recovery-sweeper"}


class RecoverySweeper:
    def __init__(
        self,
        store: EventStore,
        state: State,
        *,
        clock: Callable[[], int] = now_ms,
        thresholds: dict[str, Any] | None = None,
    ) -> None:
        self.store = store
        self.state = state
        self.clock = clock
        self.t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}

    def run(self) -> dict[str, list[dict[str, Any]]]:
        """执行一轮巡检，返回本轮产生的记录，按类别分组。"""
        produced: dict[str, list[dict[str, Any]]] = {
            "capacity": [], "matching": [], "medication": [],
            "transports": [], "families": [],
        }
        ts = self.clock()
        produced["capacity"].extend(self._sweep_capacity(ts))
        produced["matching"].extend(self._sweep_matching(ts))
        produced["medication"].extend(self._sweep_medication(ts))
        produced["transports"].extend(self._sweep_transports(ts))
        produced["families"].extend(self._sweep_families(ts))
        return produced

    # ------------------------------------------------------------------

    def _raise(
        self, ts: int, dedup_key: str, kind: str, message: str,
        *, severity: str = "warning", subject_type: str = "",
        subject_id: str = "", details: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if self.state.alert_key_exists(dedup_key):
            return None
        alert_id = self.state.next_id("ALERT")
        record = self.store.append(
            "alert.raised",
            {"alert_id": alert_id, "kind": kind, "severity": severity,
             "subject_type": subject_type, "subject_id": subject_id,
             "message": message, "details": details or {}, "dedup_key": dedup_key},
            SYSTEM_META, ts=ts,
        )
        self.state.apply(record)
        return record

    def _resolve(self, ts: int, alert_id: str, note: str = "条件解除") -> dict[str, Any]:
        record = self.store.append(
            "alert.resolved", {"alert_id": alert_id, "note": note}, SYSTEM_META, ts=ts
        )
        self.state.apply(record)
        return record

    def _open_alerts_by_kind(self, kind: str) -> dict[str, str]:
        return {
            a["dedup_key"]: a["id"]
            for a in self.state.alerts.values()
            if a["kind"] == kind and a["status"] == ALERT_RAISED and a.get("dedup_key")
        }

    # ---- 容量 ---------------------------------------------------------

    def _sweep_capacity(self, ts: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        active = self._open_alerts_by_kind("capacity")
        for site in self.state.sites.values():
            if site["status"] != "open":
                key = f"capacity:{site['id']}"
                if key in active:
                    record = self._resolve(ts, active[key], "安置点关闭")
                    out.append(record)
                continue
            occupancy = self.state.site_occupancy(site["id"])
            ratio = occupancy / site["capacity"] if site["capacity"] else 1.0
            key = f"capacity:{site['id']}"
            if ratio >= self.t["capacity_warn_ratio"]:
                severity = "critical" if ratio >= self.t["capacity_critical_ratio"] else "warning"
                record = self._raise(
                    ts, key, "capacity",
                    f"安置点 {site['id']} 占用 {occupancy}/{site['capacity']}",
                    severity=severity, subject_type="site", subject_id=site["id"],
                    details={"occupancy": occupancy, "capacity": site["capacity"],
                             "available": site["capacity"] - occupancy},
                )
                if record:
                    out.append(record)
            elif key in active:
                out.append(self._resolve(ts, active[key]))
        return out

    # ---- 失联匹配 -----------------------------------------------------

    def _sweep_matching(self, ts: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for trace in self.state.traces.values():
            if trace["status"] != TRACE_OPEN:
                continue
            # 已有待确认/已确认提议则不重复提议
            pending = [
                m for m in self.state.matches.values()
                if m["trace_id"] == trace["id"]
                and m["status"] in {MATCH_PROPOSED, "confirmed"}
            ]
            if pending:
                continue
            best = None
            for person in self.state.persons.values():
                if person["status"] == ST_MERGED:
                    continue
                scored = score_candidate(trace, person)
                if scored["score"] >= self.t["match_score"] and (
                    best is None or scored["score"] > best["score"]
                ):
                    best = scored
            if best is None:
                continue
            match_id = self.state.next_id("MATCH")
            record = self.store.append(
                "match.proposed",
                {"match_id": match_id, "trace_id": trace["id"],
                 "candidate_person_id": best["person_id"],
                 "family_id": self.state.persons[best["person_id"]].get("family_id"),
                 "score": best["score"], "signals": best["signals"]},
                SYSTEM_META, ts=ts,
            )
            self.state.apply(record)
            out.append(record)
            alert = self._raise(
                ts, f"matchproposal:{match_id}", "match_proposal",
                f"线索 {trace['id']} 出现高分候选 {best['person_id']}（{best['score']}分），待人工确认",
                severity="info", subject_type="trace", subject_id=trace["id"],
                details={"match_id": match_id, "signals": best["signals"]},
            )
            if alert:
                out.append(alert)
        return out

    # ---- 药品 ---------------------------------------------------------

    def _sweep_medication(self, ts: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        lead = self.t["medication_lead_ms"]
        active = self._open_alerts_by_kind("medication_due")
        for supply in self.state.med_supplies.values():
            last = supply.get("last_refill_at")
            key = f"meddue:{supply['person_id']}"
            if last is None:
                continue
            run_out = last + supply["supply_days"] * DAY_MS
            if run_out <= ts + lead:
                record = self._raise(
                    ts, key, "medication_due",
                    f"人员 {supply['person_id']} 的 {supply['medication']} "
                    f"将于窗口内用完",
                    subject_type="person", subject_id=supply["person_id"],
                    details={"run_out_at": run_out, "medication": supply["medication"]},
                )
                if record:
                    out.append(record)
            elif key in active:
                out.append(self._resolve(ts, active[key], "已完成补给"))

        active_batch = self._open_alerts_by_kind("med_batch_overdue")
        for batch in self.state.med_batches.values():
            key = f"medbatch:{batch['id']}:overdue"
            expected = batch.get("expected_at")
            if batch["status"] == MEDBATCH_EXPECTED and expected and expected + self.t["med_batch_overdue_ms"] <= ts:
                record = self._raise(
                    ts, key, "med_batch_overdue",
                    f"药品批次 {batch['id']}（{batch['medication']}）超过预计到货时间",
                    subject_type="med_batch", subject_id=batch["id"],
                    details={"expected_at": expected},
                )
                if record:
                    out.append(record)
            elif batch["status"] != MEDBATCH_EXPECTED and key in active_batch:
                out.append(self._resolve(ts, active_batch[key], "批次已到货"))
        return out

    # ---- 待确认转运 ---------------------------------------------------

    def _sweep_transports(self, ts: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        horizon = self.t["transport_horizon_ms"]
        active = self._open_alerts_by_kind("transport_pending")
        for transport in self.state.transports.values():
            key = f"transport:{transport['id']}"
            due = transport["status"] == AR_PENDING and (
                transport.get("scheduled_at") is None
                or transport["scheduled_at"] <= ts + horizon
            )
            if due:
                is_dialysis = transport["kind"] == "dialysis"
                record = self._raise(
                    ts, key, "transport_pending",
                    f"{'透析' if is_dialysis else ''}转运 {transport['id']} 仍待确认",
                    severity="critical" if is_dialysis else "warning",
                    subject_type="transport", subject_id=transport["id"],
                    details={"person_id": transport["person_id"], "kind": transport["kind"],
                             "scheduled_at": transport.get("scheduled_at")},
                )
                if record:
                    out.append(record)
            elif key in active:
                out.append(self._resolve(ts, active[key], "转运已确认或结束"))
        return out

    # ---- 家庭分离 -----------------------------------------------------

    def _sweep_families(self, ts: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        active = self._open_alerts_by_kind("family_separated")
        for family in self.state.families.values():
            if family["status"] != "active" or family.get("returned_home_at"):
                if f"familysep:{family['id']}" in active:
                    out.append(self._resolve(ts, active[f"familysep:{family['id']}"]))
                continue
            sites = {
                loc["ref"]
                for pid in family["members"]
                if (loc := self.state.current_location(pid))
                and loc["kind"] in {"site", "admission"}
            }
            key = f"familysep:{family['id']}"
            if len(sites) > 1:
                record = self._raise(
                    ts, key, "family_separated",
                    f"家庭 {family['id']} 成员分布在 {len(sites)} 个安置点",
                    severity="info", subject_type="family", subject_id=family["id"],
                    details={"sites": sorted(sites)},
                )
                if record:
                    out.append(record)
            elif key in active:
                out.append(self._resolve(ts, active[key], "家庭成员已在同一安置点团聚"))
        return out


class RecoveryLoop:
    """后台周期巡检线程；与启动时执行一次巡检接续同一套逻辑。"""

    def __init__(self, target: Callable[[], Any], interval_ms: int) -> None:
        self._target = target
        self.interval = interval_ms / 1000
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="recovery-loop", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self._target()
            except Exception:  # noqa: BLE001 - 巡检异常不能杀死线程
                import logging
                logging.getLogger("disaster_relief").exception("恢复巡检失败")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
