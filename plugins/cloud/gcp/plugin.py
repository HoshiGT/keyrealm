"""GCP 服务账号验证插件（plugins/cloud/ 类别下的独立子插件）。

判定两步，全部只读：

1. **换 token**（决定有效性）：邮箱 + 私钥签 RS256 JWT 到 token 端点换 access_token。
   换得到 ⇒ 有效；回 ``invalid_grant`` + ``account not found`` / ``Invalid JWT Signature``
   ⇒ 服务账号已删或私钥已轮换 ⇒ 无效。

2. **探项目**（只作补充信息，不参与判定）：带 token 调 Resource Manager
   ``GET /v1/projects/<project_id>``，200 时顺带给出项目号与生命周期状态。
   需额外权限，403/404 一律**不影响**第 1 步结论。

字段按「一行一项」拆平（邮箱 / 私钥 / 密钥 ID / 项目 ID），不接收整段 JSON；只有邮箱与
私钥必需，密钥 ID 只进 JWT 头、项目 ID 只用于第 2 步。协议实现复用同目录 ``google_sa``。

参考文档：https://developers.google.com/identity/protocols/oauth2/service-account
参考文档：https://docs.cloud.google.com/resource-manager/reference/rest/v1/projects/get
"""
from __future__ import annotations

from backend.plugin_sdk import VerifyResult, guarded, log
from google_sa import (
    GoogleSAError,
    classify_token_error,
    get_json,
    normalize_sa,
    token_for,
)

NAME = "GCP 服务账号"
REQUIRED = (("client_email", "服务账号邮箱"), ("private_key", "私钥（PEM）"))
DISPLAY_FIELD = "client_email"

CRM_URL = "https://cloudresourcemanager.googleapis.com/v1/projects/{project_id}"


def _identity(sa: dict) -> dict:
    """从归一化后的字段里摘出可展示的身份信息（不含私钥）。"""
    info = {}
    for key in ("client_email", "project_id", "private_key_id"):
        value = str(sa.get(key) or "").strip()
        if value:
            info[key] = value
    return info


def _probe_project(sa: dict, token: str) -> tuple[dict, str]:
    """探一次 GCP 项目，返回 ``(补充信息, 说明后缀)``；失败只降级为说明文本。"""
    project_id = str(sa.get("project_id") or "").strip()
    if not project_id:
        return {}, "（未填 project_id，未探测项目权限）"
    try:
        status, payload = get_json(CRM_URL.format(project_id=project_id), token)
    except Exception as exc:  # noqa: BLE001 - 补充探测失败不影响有效性结论
        return {}, f"（项目探测失败：{type(exc).__name__}）"

    if status == 200:
        info = {"crm_status": "ACTIVE" if payload.get("lifecycleState") == "ACTIVE" else payload.get("lifecycleState") or ""}
        for src, dst in (("projectNumber", "project_number"), ("name", "project_name")):
            value = str(payload.get(src) or "").strip()
            if value:
                info[dst] = value
        suffix = f"，项目 {project_id}"
        if info.get("lifecycleState"):
            suffix += f"（{info['lifecycleState']}）"
        return info, suffix
    if status == 403:
        return {}, "（无 Resource Manager 权限，项目信息不可见）"
    if status == 404:
        return {}, f"（项目 {project_id} 不存在或已删除，不影响凭据有效性）"
    return {}, f"（项目探测未通过：HTTP {status}）"


@guarded
def verify(data) -> VerifyResult:
    try:
        sa = normalize_sa(data)
    except GoogleSAError as exc:
        # 本地就没法把这组字段用起来（缺项 / 邮箱不像 / 私钥解析不了）⇒ 无法认证
        return VerifyResult(status="invalid", message=str(exc))

    info = _identity(sa)
    log(f"校验 GCP 服务账号 {info.get('client_email', '?')}")
    try:
        status, payload, snippet = token_for(sa)
    except GoogleSAError as exc:
        return VerifyResult(status="invalid", message=str(exc))

    if status == 200 and str(payload.get("access_token") or "").strip():
        extra, suffix = _probe_project(sa, payload["access_token"])
        info.update(extra)
        if payload.get("expires_in"):
            info["token_expires_in"] = payload["expires_in"]
        return VerifyResult(
            status="valid",
            message=f"服务账号有效：{info.get('client_email', '')}{suffix}",
            account_info=info,
        )

    kind, message = classify_token_error(status, payload, snippet)
    result_info = dict(info)
    return VerifyResult(status=kind, message=message, account_info=result_info)


def validate(data) -> list[str]:
    try:
        normalize_sa(data)
    except GoogleSAError as exc:
        return [str(exc)]
    return []


def get_display_summary(data) -> str:
    """凭据列表里的简短展示：只取邮箱，绝不回显私钥。"""
    email = str((data or {}).get(DISPLAY_FIELD) or "").strip()
    return f"{NAME} | {email}" if email else NAME
