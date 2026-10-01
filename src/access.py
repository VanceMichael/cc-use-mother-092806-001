"""基于履职岗位的访问控制与字段脱敏。

角色：
- coordinator 省级救灾协调员：跨区调剂、合并档案、人工复核
- rescue 地方救援队：接收、撤离批次、转运执行
- shelter 安置点工作人员：容量、入住登记、普通物资
- medical 医疗与社会服务人员：健康需求、透析转运、药品
- social_welfare 儿童与社会福利岗位：儿童信息、寻亲团聚
"""

from __future__ import annotations

from .errors import PermissionDeniedError

COORDINATOR = "coordinator"
RESCUE = "rescue"
SHELTER = "shelter"
MEDICAL = "medical"
SOCIAL_WELFARE = "social_welfare"

ROLES = frozenset({COORDINATOR, RESCUE, SHELTER, MEDICAL, SOCIAL_WELFARE})

# 动作 -> 允许履职的岗位
PERMISSIONS: dict[str, frozenset[str]] = {
    "shelter.register": frozenset({COORDINATOR, SHELTER}),
    "shelter.capacity": frozenset({COORDINATOR}),
    "person.receive": frozenset({RESCUE, SHELTER, SOCIAL_WELFARE, MEDICAL}),
    "person.verify": frozenset({COORDINATOR, RESCUE, SOCIAL_WELFARE}),
    "family.declare": frozenset({RESCUE, SOCIAL_WELFARE, COORDINATOR}),
    "family.link": frozenset({SOCIAL_WELFARE, COORDINATOR, RESCUE}),
    "profile.merge": frozenset({COORDINATOR, SOCIAL_WELFARE}),
    "batch.manage": frozenset({RESCUE}),
    "arrangement.transfer": frozenset({COORDINATOR, RESCUE}),
    "arrangement.medical_transfer": frozenset({MEDICAL}),
    "arrangement.reallocate": frozenset({COORDINATOR}),
    "arrangement.discharge": frozenset({MEDICAL, SHELTER}),
    "arrangement.return_home": frozenset({SHELTER, COORDINATOR}),
    "arrangement.confirm": frozenset({COORDINATOR, MEDICAL, SHELTER}),
    "arrangement.execute": frozenset({RESCUE, SHELTER, MEDICAL}),
    "arrangement.cancel": frozenset({COORDINATOR, RESCUE, SHELTER, MEDICAL}),
    "health.record": frozenset({MEDICAL}),
    "health.view": frozenset({MEDICAL}),
    "med.manage": frozenset({MEDICAL}),
    "supply.manage": frozenset({SHELTER}),
    "trace.open": frozenset({RESCUE, SHELTER, SOCIAL_WELFARE, COORDINATOR, MEDICAL}),
    "trace.view": frozenset({RESCUE, SHELTER, SOCIAL_WELFARE, COORDINATOR, MEDICAL}),
    "trace.decide": frozenset({SOCIAL_WELFARE, COORDINATOR}),
    "review.resolve": frozenset({COORDINATOR}),
    "ops.tick": frozenset({COORDINATOR, RESCUE, SHELTER, MEDICAL, SOCIAL_WELFARE}),
    "ops.recover": frozenset({COORDINATOR}),
    "person.view": frozenset({COORDINATOR, RESCUE, SHELTER, MEDICAL, SOCIAL_WELFARE}),
    "child.view": frozenset({SOCIAL_WELFARE, MEDICAL}),
}


def require(role: str, action: str) -> None:
    if role not in ROLES:
        raise PermissionDeniedError(f"未知岗位: {role}")
    if role not in PERMISSIONS.get(action, frozenset()):
        raise PermissionDeniedError(f"岗位 {role} 无权执行 {action}")


def can(role: str, action: str) -> bool:
    return role in PERMISSIONS.get(action, frozenset())


def redact_person(role: str, person: dict) -> dict:
    """按岗位脱敏：医疗信息仅 medical 可见，儿童信息仅福利/医疗岗位可见。"""
    view = {k: v for k, v in person.items() if k not in {"health", "is_minor", "age"}}
    if can(role, "health.view"):
        view["health"] = person.get("health", [])
    if can(role, "child.view"):
        view["age"] = person.get("age")
        view["is_minor"] = person.get("is_minor", False)
    return view
