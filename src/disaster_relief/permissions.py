"""基于履职岗位的访问控制。

写入按命令类型授权；读取对敏感字段做脱敏。
儿童信息与医疗信息只对履职岗位可见。
"""

from __future__ import annotations

from typing import Any

from .errors import AuthenticationError, PermissionDeniedError

# 履职岗位
ROLE_RESCUE = "rescue_worker"          # 地方救援队：现场接收、撤离
ROLE_SITE = "site_worker"              # 安置点工作人员：入住、物资
ROLE_COORDINATOR = "provincial_coordinator"  # 省级协调员：全局调度
ROLE_MEDICAL = "medical_worker"        # 医疗与社会服务人员（医疗）
ROLE_SOCIAL = "social_worker"          # 社会服务：儿童保护、寻亲
ROLE_SUPERVISOR = "supervisor"         # 值班主管：复核裁决

ALL_ROLES = frozenset(
    {ROLE_RESCUE, ROLE_SITE, ROLE_COORDINATOR, ROLE_MEDICAL, ROLE_SOCIAL, ROLE_SUPERVISOR}
)

# 医疗敏感信息可见岗位
MEDICAL_ROLES = frozenset({ROLE_MEDICAL, ROLE_COORDINATOR, ROLE_SUPERVISOR})
# 儿童敏感信息可见岗位
MINOR_ROLES = frozenset({ROLE_SOCIAL, ROLE_MEDICAL, ROLE_COORDINATOR, ROLE_SUPERVISOR})
# 联系方式（寻亲举报人等）可见岗位
CONTACT_ROLES = frozenset({ROLE_SOCIAL, ROLE_COORDINATOR, ROLE_SUPERVISOR, ROLE_RESCUE})

# 命令 -> 允许发起的岗位
COMMAND_PERMISSIONS: dict[str, frozenset[str]] = {
    # 家庭与人员
    "register_family": frozenset({ROLE_RESCUE, ROLE_SITE, ROLE_SOCIAL, ROLE_COORDINATOR}),
    "receive_person": frozenset({ROLE_RESCUE, ROLE_SITE, ROLE_SOCIAL}),
    "verify_person": frozenset({ROLE_SITE, ROLE_COORDINATOR, ROLE_SUPERVISOR}),
    "flag_person": frozenset({ROLE_RESCUE, ROLE_SITE, ROLE_SOCIAL, ROLE_MEDICAL}),
    "merge_person": frozenset({ROLE_COORDINATOR, ROLE_SUPERVISOR}),
    "link_family_member": frozenset({ROLE_SOCIAL, ROLE_COORDINATOR, ROLE_SITE}),
    "relink_family": frozenset({ROLE_SUPERVISOR, ROLE_COORDINATOR, ROLE_SOCIAL}),
    # 安置点与撤离
    "register_site": frozenset({ROLE_COORDINATOR}),
    "change_site_capacity": frozenset({ROLE_COORDINATOR, ROLE_SITE}),
    "close_site": frozenset({ROLE_COORDINATOR, ROLE_SITE}),
    "create_evac_batch": frozenset({ROLE_RESCUE, ROLE_COORDINATOR}),
    "set_evac_batch_status": frozenset({ROLE_RESCUE, ROLE_COORDINATOR}),
    # 入住/出院/返家
    "admit_person": frozenset({ROLE_SITE, ROLE_RESCUE}),
    "discharge_person": frozenset({ROLE_SITE}),
    "return_family_home": frozenset({ROLE_COORDINATOR, ROLE_SITE, ROLE_SOCIAL}),
    # 转运
    "create_transport": frozenset({ROLE_MEDICAL, ROLE_RESCUE, ROLE_COORDINATOR}),
    "revise_transport": frozenset({ROLE_MEDICAL, ROLE_COORDINATOR}),
    "confirm_transport": frozenset({ROLE_MEDICAL, ROLE_COORDINATOR, ROLE_RESCUE}),
    "set_transport_status": frozenset({ROLE_MEDICAL, ROLE_RESCUE, ROLE_SITE, ROLE_COORDINATOR}),
    # 医疗
    "update_medical_profile": frozenset({ROLE_MEDICAL}),
    "register_medication_supply": frozenset({ROLE_MEDICAL, ROLE_SITE}),
    "refill_medication": frozenset({ROLE_MEDICAL, ROLE_SITE}),
    "register_med_batch": frozenset({ROLE_MEDICAL, ROLE_COORDINATOR, ROLE_SITE}),
    "receive_med_batch": frozenset({ROLE_MEDICAL, ROLE_SITE}),
    "deplete_med_batch": frozenset({ROLE_MEDICAL, ROLE_SITE}),
    # 物资
    "register_supply_item": frozenset({ROLE_COORDINATOR, ROLE_SITE}),
    "allocate_supplies": frozenset({ROLE_COORDINATOR, ROLE_SITE}),
    "distribute_supplies": frozenset({ROLE_SITE, ROLE_RESCUE}),
    "return_supplies": frozenset({ROLE_SITE}),
    "reissue_supplies": frozenset({ROLE_SITE, ROLE_COORDINATOR}),
    # 寻亲
    "open_trace": frozenset({ROLE_SOCIAL, ROLE_RESCUE, ROLE_SITE, ROLE_COORDINATOR}),
    "propose_match": frozenset({ROLE_SOCIAL, ROLE_COORDINATOR}),
    "confirm_match": frozenset({ROLE_SOCIAL, ROLE_COORDINATOR}),
    "reject_match": frozenset({ROLE_SOCIAL, ROLE_COORDINATOR}),
    "close_trace": frozenset({ROLE_SOCIAL, ROLE_COORDINATOR}),
    # 复核
    "resolve_review": frozenset({ROLE_SUPERVISOR, ROLE_COORDINATOR}),
    # 预警处置
    "acknowledge_alert": frozenset(ALL_ROLES),
    "resolve_alert": frozenset({ROLE_COORDINATOR, ROLE_SUPERVISOR, ROLE_SITE}),
}

# 查询授权：默认任何已认证岗位；医疗/儿童明细在字段级脱敏
SENSITIVE_QUERY_ROLES: dict[str, frozenset[str]] = {}


def require_role(role: str | None) -> str:
    if not role:
        raise AuthenticationError("缺少岗位标识（X-Actor-Role）")
    if role not in ALL_ROLES:
        raise AuthenticationError(f"未知岗位：{role}", details={"role": role})
    return role


def authorize_command(role: str, command: str) -> None:
    allowed = COMMAND_PERMISSIONS.get(command)
    if allowed is None:
        raise PermissionDeniedError(f"未知命令：{command}")
    if role not in allowed:
        raise PermissionDeniedError(
            f"岗位 {role} 无权执行 {command}",
            details={"role": role, "command": command, "allowed": sorted(allowed)},
        )


# ----------------------------------------------------------------------
# 读模型脱敏
# ----------------------------------------------------------------------

_MINOR_KEYS = frozenset({"is_minor", "approx_age"})
_MEDICAL_KEYS = frozenset(
    {"patient_type", "needs_dialysis", "dialysis_frequency_hours", "mobility",
     "medication", "medical", "med_supply", "medication_supply", "preferred_facility_id",
     "medical_priority"}
)
_CONTACT_KEYS = frozenset({"contact", "reporter_contact"})

_HIDDEN = "【受限】"


def can_view_medical(role: str) -> bool:
    return role in MEDICAL_ROLES


def can_view_minor(role: str) -> bool:
    return role in MINOR_ROLES


def redact_person(person: dict[str, Any], role: str) -> dict[str, Any]:
    """按岗位对人员档案做字段级脱敏。"""
    view = {k: v for k, v in person.items() if k != "merged_receipts"}
    if not can_view_minor(role):
        for key in _MINOR_KEYS:
            if key in view:
                view[key] = _HIDDEN
    if role not in CONTACT_ROLES:
        for key in _CONTACT_KEYS:
            if key in view:
                view[key] = _HIDDEN
    # notes 中可能含儿童/医疗线索，非履职岗位折叠
    if role not in (MEDICAL_ROLES | MINOR_ROLES) and "notes" in view:
        view["notes"] = ""
    return view


def redact_medical(profile: dict[str, Any] | None, role: str) -> dict[str, Any] | None:
    if profile is None:
        return None
    if can_view_medical(role):
        return profile
    return {"person_id": profile.get("person_id"), "restricted": True}


def redact_trace(trace: dict[str, Any], role: str) -> dict[str, Any]:
    view = dict(trace)
    if not can_view_minor(role):
        view["minor_included"] = _HIDDEN if trace.get("minor_included") else False
        if view.get("minor_included") == _HIDDEN:
            view["description"] = _HIDDEN
    if role not in CONTACT_ROLES:
        view["reporter_contact"] = _HIDDEN if trace.get("reporter_contact") else ""
    return view


def redact_med_supply(supply: dict[str, Any] | None, role: str) -> dict[str, Any] | None:
    if supply is None:
        return None
    if can_view_medical(role):
        return supply
    return {"person_id": supply.get("person_id"), "restricted": True}
