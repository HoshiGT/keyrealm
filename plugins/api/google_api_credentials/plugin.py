"""Google API 凭据验证插件（plugins/api/ 类别下的独立子插件）。

参考文档：https://developers.google.com/identity/protocols/oauth2 （OAuth 客户端密钥）
          https://firebase.google.com/docs/reference/rest/auth （Identity Toolkit / API Key）

一个插件装两类**应用级密钥**，由 `kind` 选择器区分，两者的校验方式完全不同：

- ``google_api_key``（``AIza…``）：只读调 Identity Toolkit 的 ``getProjectConfig``。
  Google 对这个接口只做 API Key 校验，所以回包能干净地区分三种情况：
    200                     → 键有效，还能顺带拿到它所属的 Firebase 项目与授权域；
    400 ``API key not valid`` → 键不成立（唯一能判无效的信号）；
    403                     → **键已被识别**（Google 是先认键、再查项目配额与限制），
                              只是这个 API 未在该项目启用、或键受 API/来源限制 ⇒ 仍然有效。
  429 同理：能触发配额说明键是真的。
- ``google_oauth_secret``（``GOCSPX-…``）：走 ``oauth2.googleapis.com/token``，故意用一个
  不存在的授权码。Google 先校验客户端凭据、再校验授权码，于是：
    ``invalid_grant``   → 凭据对已被接受，只是授权码无效 ⇒ 密钥有效；
    ``invalid_client``  → 客户端凭据不成立 ⇒ 无效（但若描述是 "not found"，那是 **Client ID**
                          没被识别，不能算到密钥头上，归为「无法确认」）；
  客户端凭据是**成对**校验的，所以这个种类必须同时给出配对的 Client ID，缺了只能报「无法确认」。

全部只读，且两个接口都不会产生任何资源变更（授权码是现场编的假值）。
仅从框架层 backend.plugin_sdk 复用通用工具。
"""
from __future__ import annotations

import re

from backend.plugin_sdk import (
    VerifyResult,
    body_snippet,
    guarded,
    log,
    make_client,
    mask_secret,
    safe_json,
)

NAME = "Google API 凭据"
DISPLAY_FIELD = "secret"

IDENTITYTOOLKIT_URL = "https://identitytoolkit.googleapis.com/v1/projects"
TOKEN_URL = "https://oauth2.googleapis.com/token"

KIND_API_KEY = "google_api_key"
KIND_OAUTH_SECRET = "google_oauth_secret"
KIND_LABELS = {KIND_API_KEY: "Google API Key", KIND_OAUTH_SECRET: "Google OAuth 客户端密钥"}

# 403 的正文里常带 "…has not been used in project <项目号>…"，顺手把这个号摘出来
_PROJECT_RE = re.compile(r"project\s+(\d{6,})")

_DETAIL_LIMIT = 200


def _short(text: str, limit: int = _DETAIL_LIMIT) -> str:
    """Google 的 403 文案很长（还带 console 链接），截断到一行内可读的长度。"""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _kind(data: dict) -> str:
    kind = str(data.get("kind") or "").strip()
    return kind if kind in KIND_LABELS else KIND_API_KEY


# ── Google API Key ──────────────────────────────────────

def _verify_api_key(api_key: str) -> VerifyResult:
    log(f"校验 Google API Key {mask_secret(api_key)}")
    with make_client() as client:
        resp = client.get(IDENTITYTOOLKIT_URL, params={"key": api_key})

    payload = safe_json(resp) or {}
    if resp.status_code == 200:
        info: dict = {"kind": KIND_API_KEY}
        project = str(payload.get("projectId") or "").strip()
        if project:
            info["project_id"] = project
        domains = payload.get("authorizedDomains")
        if isinstance(domains, list) and domains:
            info["authorized_domains"] = [str(d) for d in domains][:10]
        message = "API Key 有效"
        if project:
            message += f"：Firebase 项目 {project}"
        if isinstance(domains, list) and domains:
            message += f"（授权域 {len(domains)} 个）"
        return VerifyResult(status="valid", message=message, account_info=info)

    envelope = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    detail = _short(str(envelope.get("message") or "").strip() or body_snippet(resp))
    reasons = {
        str(item.get("reason") or "")
        for item in (envelope.get("details") or [])
        if isinstance(item, dict)
    }

    # 唯一能判无效的信号：Google 明确说这把键不成立
    if resp.status_code == 400 or "API_KEY_INVALID" in reasons or "not valid" in detail.lower():
        return VerifyResult(status="invalid", message=f"API Key 无效：{detail}")

    if resp.status_code in (401, 403):
        # 403 说明键已被识别，只是被项目/接口限制挡住 —— 键本身有效
        info = {"kind": KIND_API_KEY}
        found = _PROJECT_RE.search(detail)
        if found:
            info["project_number"] = found.group(1)
        if "has not been used in project" in detail:
            note = "该 API 未在项目启用"
        elif "blocked" in detail.lower():
            note = "密钥受 API / 来源限制"
        else:
            note = "请求被项目或接口策略拒绝"
        return VerifyResult(
            status="valid",
            message=f"API Key 有效（{note}）：{detail}",
            account_info=info,
        )

    if resp.status_code == 429:
        return VerifyResult(
            status="valid",
            message="API Key 有效（触发配额限制 HTTP 429）",
            account_info={"kind": KIND_API_KEY},
        )

    return VerifyResult(
        status="error",
        message=f"无法确认 API Key 状态 (HTTP {resp.status_code})：{detail}",
    )


# ── Google OAuth 客户端密钥 ──────────────────────────────

def _verify_oauth_secret(client_secret: str, client_id: str) -> VerifyResult:
    if not client_id:
        return VerifyResult(
            status="error",
            message="校验 OAuth 客户端密钥必须同时填写配对的 Client ID："
                    "Google 对客户端凭据是成对校验的，单凭密钥无法判定",
        )

    log(f"校验 Google OAuth 客户端密钥 {mask_secret(client_secret)}")
    with make_client() as client:
        resp = client.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "authorization_code",
                "code": "vault-probe",  # 现场编的假授权码，只用来把流程推到「凭据已校验」那一步
            },
        )

    payload = safe_json(resp) or {}
    error = str(payload.get("error") or "").strip()
    description = str(payload.get("error_description") or "").strip()
    detail = f"{error}: {description}" if description else (error or body_snippet(resp))

    if error == "invalid_grant":
        # 客户端凭据已通过校验，卡住的是那个假授权码
        return VerifyResult(
            status="valid",
            message=f"OAuth 客户端密钥有效（凭据对已被接受，仅授权码无效）：{detail}",
        )
    if error == "invalid_client":
        if "not found" in description.lower():
            return VerifyResult(
                status="error",
                message=f"Client ID 未被 Google 识别，无法判断密钥是否有效：{detail}",
            )
        return VerifyResult(status="invalid", message=f"OAuth 客户端密钥无效：{detail}")
    return VerifyResult(
        status="error",
        message=f"无法确认 OAuth 客户端密钥 (HTTP {resp.status_code})：{detail}",
    )


# ── 插件接口 ────────────────────────────────────────────

@guarded
def verify(data) -> VerifyResult:
    data = data or {}
    secret = str(data.get(DISPLAY_FIELD) or "").strip()
    if not secret:
        return VerifyResult(status="invalid", message="缺少必填字段: 密钥值")

    if _kind(data) == KIND_OAUTH_SECRET:
        return _verify_oauth_secret(secret, str(data.get("client_id") or "").strip())
    return _verify_api_key(secret)


def validate(data) -> list[str]:
    if not data:
        return ["配置数据为空"]
    if not str(data.get(DISPLAY_FIELD) or "").strip():
        return ["密钥值不能为空"]
    return []


def get_display_summary(data) -> str:
    """列表展示只用种类名，不回显密钥本身。"""
    return f"{NAME} | {KIND_LABELS[_kind(data or {})]}"
