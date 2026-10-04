"""GitHub 令牌验证插件（plugins/dev/ 类别下的独立子插件）。"""
from __future__ import annotations

from typing import Any

from dev_common import (
    VerifyResult,
    body_snippet,
    guarded,
    make_client,
    safe_json,
)

NAME = "GitHub"
TOKEN_KEY = "token"
ENDPOINT = "https://api.github.com/user"


def _mask(secret: str) -> str:
    """脱敏展示：保留前 4 位，其余以 * 代替。"""
    secret = secret or ""
    if len(secret) <= 4:
        return "*" * len(secret)
    return secret[:4] + "*" * 6


def _unconfirmed(status: int, snippet: str) -> VerifyResult:
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def validate(data) -> list[str]:
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]

    errors: list[str] = []
    token = (data.get(TOKEN_KEY) or "").strip()
    if not token:
        errors.append("Token 不能为空")
    return errors


def _verify_github(token: str) -> VerifyResult:
    """GitHub 令牌验证：GET /user（Personal Access Token / OAuth Token 通用）。"""
    with make_client() as client:
        resp = client.get(
            ENDPOINT,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "CredentialVault",
            },
        )
    status = resp.status_code
    payload = safe_json(resp)

    if status == 200:
        info: dict[str, Any] = {}
        if isinstance(payload, dict):
            for key in ("login", "name", "type"):
                value = payload.get(key)
                if value:
                    info[key] = value
        # OAuth / fine-grained 令牌会回传授权范围，便于确认权限面
        scopes = resp.headers.get("X-OAuth-Scopes")
        if scopes:
            info["scopes"] = scopes
        return VerifyResult(status="valid", message="凭证有效", account_info=info or None)

    if status == 401:
        return VerifyResult(status="invalid", message="凭证无效 (HTTP 401)")

    if status == 403:
        # 403 多为触发 API 速率限制；剩余配额为 0 时给出更明确的提示
        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining == "0":
            return VerifyResult(
                status="error",
                message="请求被 GitHub 限流（API 速率限制已用尽），暂时无法确认凭证状态",
            )
        return VerifyResult(
            status="error",
            message=f"请求被拒绝 (HTTP 403)，可能触发限流或令牌权限不足: {body_snippet(resp)}",
        )

    return _unconfirmed(status, body_snippet(resp))


@guarded
def verify(data) -> VerifyResult:
    data = data or {}
    token = (data.get(TOKEN_KEY) or "").strip()
    if not token:
        return VerifyResult(status="invalid", message="Token 为空")
    return _verify_github(token)


# ── 可选操作：查看账号信息 / 我的仓库 ───────────────────

def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "CredentialVault",
    }


def actions(data: dict) -> list[dict]:
    """声明可用操作（凭据含 token 时才提供）。"""
    data = data or {}
    if not (data.get(TOKEN_KEY) or "").strip():
        return []
    return [
        {
            "id": "account_info",
            "name": "查看账号信息",
            "kind": "form",
            "submit": "查询",
            "fields": [],
        },
        {
            "id": "list_repos",
            "name": "查看我的仓库",
            "kind": "form",
            "submit": "查询",
            "fields": [
                {"key": "per_page", "label": "条数", "type": "text", "default": "20",
                 "placeholder": "1-100，默认 20"},
            ],
        },
    ]


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行操作：查看 GitHub 账号信息 / 仓库列表（只读，绝不回显 token）。"""
    data = data or {}
    params = params or {}
    token = (data.get(TOKEN_KEY) or "").strip()
    if not token:
        return {"ok": False, "error": "缺少 Token"}
    try:
        if action_id == "account_info":
            with make_client() as client:
                resp = client.get(ENDPOINT, headers=_headers(token))
            if resp.status_code == 401:
                return {"ok": False, "error": "凭证无效 (HTTP 401)"}
            if resp.status_code != 200:
                return {"ok": False, "error": f"请求失败 (HTTP {resp.status_code})"}
            payload = safe_json(resp) or {}
            scopes = resp.headers.get("X-OAuth-Scopes") or ""
            lines = [
                f"登录名：{payload.get('login') or '-'}",
                f"名称：{payload.get('name') or '-'}",
                f"类型：{payload.get('type') or '-'}",
                f"权限范围：{scopes or '（未回传，可能为 fine-grained token）'}",
            ]
            return {"ok": True, "output": "\n".join(lines)}

        if action_id == "list_repos":
            try:
                per_page = int((params.get("per_page") or "20").strip())
            except (TypeError, ValueError):
                per_page = 20
            per_page = max(1, min(per_page, 100))
            with make_client() as client:
                resp = client.get(
                    f"{ENDPOINT}/repos",
                    params={"per_page": per_page, "sort": "updated"},
                    headers=_headers(token),
                )
            if resp.status_code == 401:
                return {"ok": False, "error": "凭证无效 (HTTP 401)"}
            if resp.status_code != 200:
                return {"ok": False, "error": f"请求失败 (HTTP {resp.status_code})"}
            try:
                repos = resp.json()
            except Exception:  # noqa: BLE001 - 非 JSON 响应按空列表处理
                repos = None
            repos = repos if isinstance(repos, list) else []
            lines = [f"共 {len(repos)} 个仓库：", ""]
            for repo in repos:
                if not isinstance(repo, dict):
                    continue
                lines.append(
                    f"{repo.get('full_name') or '-'}  private={bool(repo.get('private'))}  "
                    f"updated={repo.get('updated_at') or '-'}"
                )
            return {"ok": True, "output": "\n".join(lines)}

        return {"ok": False, "error": "未知操作"}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底
        return {"ok": False, "error": f"执行异常：{type(exc).__name__}"}


def get_display_summary(data) -> str:
    """凭据列表中的简短展示文本。"""
    data = data or {}
    secret = (data.get(TOKEN_KEY) or "").strip()
    if not secret:
        return NAME
    return f"{NAME} | {_mask(secret)}"
