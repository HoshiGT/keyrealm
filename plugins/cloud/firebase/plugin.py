"""Firebase Admin 密钥验证插件（plugins/cloud/ 类别下的独立子插件）。

Firebase 的「服务账号私钥」（控制台导出的 ``*-firebase-adminsdk-*.json``）在协议层
就是一份**普通 Google 服务账号**，所以换 token 这一步与 ``cloud_gcp`` 完全一致，
共用同目录的 ``google_sa`` 模块。

判定同样分两步、全部只读：

1. **换 token**（决定有效性）：换得到 ⇒ 密钥当前有效；Google 回 ``invalid_grant`` 且
   描述为 ``account not found`` / ``Invalid JWT Signature`` ⇒ 服务账号已删或私钥已轮换
   ⇒ 无效（项目被删的 adminsdk 密钥就是这一种，实测回包即 ``Invalid grant: account not found``）。

2. **探 Firebase 项目**（只作补充信息，不参与判定）：带 token 调 Firebase Management
   ``GET /v1beta1/projects/<project_id>``，回 200 就给出项目状态与 RTDB / Storage 实例名。
   ``cloud_gcp`` 探的是 Resource Manager，这里换成了 Firebase 自己的接口，这也是两个插件
   唯一有意义的差别——同一份密钥若只挂在 Firebase 上，只有这一步能看出项目还在不在。

字段按「一行一项」拆平（邮箱 / 私钥 / 密钥 ID / 项目 ID），不接收整段 JSON。

仅从框架层 backend.plugin_sdk 复用通用工具。

参考文档：https://firebase.google.com/docs/projects/api/reference/rest/v1beta1/projects/get
参考文档：https://developers.google.com/identity/protocols/oauth2/service-account
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

NAME = "Firebase 管理密钥"
REQUIRED = (("client_email", "服务账号邮箱"), ("private_key", "私钥（PEM）"))
DISPLAY_FIELD = "client_email"

FIREBASE_URL = "https://firebase.googleapis.com/v1beta1/projects/{project_id}"

# 从 Firebase 项目对象里挑出值得写进 account_info 的资源线索
RESOURCE_KEYS = ("realtimeDatabaseInstance", "storageBucket", "hostingSite", "locationId")


def _identity(sa: dict) -> dict:
    """从归一化后的字段里摘出可展示的身份信息（不含私钥）。"""
    info = {}
    for key in ("client_email", "project_id", "private_key_id"):
        value = str(sa.get(key) or "").strip()
        if value:
            info[key] = value
    return info


def _probe_project(sa: dict, token: str) -> tuple[dict, str]:
    """探一次 Firebase 项目，返回 ``(补充信息, 说明后缀)``；失败只降级为说明文本。"""
    project_id = str(sa.get("project_id") or "").strip()
    if not project_id:
        return {}, "（未填 project_id，未探测 Firebase 项目）"
    try:
        status, payload = get_json(FIREBASE_URL.format(project_id=project_id), token)
    except Exception as exc:  # noqa: BLE001 - 补充探测失败不影响有效性结论
        return {}, f"（Firebase 项目探测失败：{type(exc).__name__}）"

    if status == 200:
        info = {}
        state = str(payload.get("state") or "").strip()
        if state:
            info["firebase_state"] = state
        resources = payload.get("resources")
        if isinstance(resources, dict):
            for key in RESOURCE_KEYS:
                value = str(resources.get(key) or "").strip()
                if value:
                    info[key] = value
        suffix = f"，Firebase 项目 {project_id}"
        if state:
            suffix += f"（{state}）"
        return info, suffix
    if status == 403:
        return {}, "（无 Firebase Management 权限，项目信息不可见）"
    if status == 404:
        return {}, f"（Firebase 项目 {project_id} 不存在或已删除，不影响密钥有效性）"
    return {}, f"（Firebase 项目探测未通过：HTTP {status}）"


@guarded
def verify(data) -> VerifyResult:
    try:
        sa = normalize_sa(data)
    except GoogleSAError as exc:
        # 本地就没法把这组字段用起来（缺项 / 邮箱不像 / 私钥解析不了）⇒ 无法认证
        return VerifyResult(status="invalid", message=str(exc))

    info = _identity(sa)
    log(f"校验 Firebase 密钥 {info.get('client_email', '?')}")
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
            message=f"密钥有效：{info.get('client_email', '')}{suffix}",
            account_info=info,
        )

    kind, message = classify_token_error(status, payload, snippet)
    return VerifyResult(status=kind, message=message, account_info=dict(info))


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
