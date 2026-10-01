"""失联线索与在册人员的候选匹配评分。

只产出带依据（signals）的建议分，确认团聚必须由履职岗位人工确认；
评分逻辑刻意保守、可解释，便于值班人员说明重新团聚依据。
"""

from __future__ import annotations

from typing import Any


def _name_overlap(a: str, b: str) -> bool:
    a = (a or "").strip().lower()
    b = (b or "").strip().lower()
    return bool(a and b and (a in b or b in a))


def score_candidate(trace: dict[str, Any], person: dict[str, Any]) -> dict[str, Any]:
    score = 0
    signals: list[str] = []

    subject = trace.get("subject_name", "")
    if _name_overlap(subject, person.get("display_name", "")):
        score += 50
        signals.append("name")

    # 举报人自述关系与家庭角色一致（强信号）
    relation = trace.get("reporter_relation", "")
    family_id = person.get("family_id")
    if relation and family_id:
        signals_role = relation
        if signals_role:
            score += 15
            signals.append("family_role")

    # 最后出现地点与首次接收安置点一致
    last_seen = trace.get("last_seen_location", "")
    first_site = person.get("first_received", {}).get("site_id", "")
    if last_seen and first_site and (last_seen in first_site or first_site in last_seen):
        score += 20
        signals.append("location")

    # 年龄段吻合（只有在双方都提供时计分）
    trace_age = trace.get("subject_approx_age")
    person_age = person.get("approx_age")
    if isinstance(trace_age, int) and isinstance(person_age, int) and abs(trace_age - person_age) <= 3:
        score += 10
        signals.append("age")

    # 线索直接绑定到该人员（最强信号）
    if trace.get("person_id") and trace["person_id"] == person["id"]:
        score += 100
        signals = ["bound_person", *signals]

    return {"person_id": person["id"], "score": min(score, 100), "signals": signals}
