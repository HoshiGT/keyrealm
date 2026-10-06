"""百度文心 API Key 验证插件（plugins/llm/ 类别下的独立子插件）。

协议：POST {base}/oauth/2.0/token（client_credentials）。
凭证格式为 API_KEY|SECRET_KEY（由子插件自行拆分）。

参考文档：https://cloud.baidu.com/doc/WENXINWORKSHOP/index.html
"""
from __future__ import annotations

import httpx
from typing import Any, Optional

from backend.plugin_sdk import (
    VerifyResult,
    body_snippet,
    guarded,
    make_client,
    safe_json,
    timeout,
)
from llm_common import probe_model, probe_requested, quota_insufficient, quota_ok
from llm_protocols import is_insufficient_balance

NAME = "百度文心"
DEFAULT_BASE_URL = "https://aip.baidubce.com"
PROTOCOL = "wenxin"


def _split_key(api_key: str) -> tuple[str, str]:
    """百度文心凭证格式为 API_KEY|SECRET_KEY。"""
    parts = api_key.split("|", 1)
    return parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")


def validate(data: dict) -> list[str]:
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]

    errors: list[str] = []
    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        errors.append("API Key 不能为空")
    if not DEFAULT_BASE_URL and not (data.get("base_url") or "").strip():
        errors.append(f"厂商「{NAME}」需要填写 Base URL")
    if api_key:
        client_id, client_secret = _split_key(api_key)
        if not client_id or not client_secret:
            errors.append("百度文心凭证格式应为 API_KEY|SECRET_KEY")
    return errors


def _probe(base_url: str, api_key: str) -> VerifyResult:
    client_id, client_secret = _split_key(api_key)
    url = base_url.rstrip("/") + "/oauth/2.0/token"
    with make_client() as client:
        resp = client.post(
            url,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
    status = resp.status_code
    payload = safe_json(resp)
    if isinstance(payload, dict):
        if payload.get("access_token"):
            info: dict[str, Any] = {}
            if payload.get("expires_in") is not None:
                info["expires_in"] = payload["expires_in"]
            return VerifyResult(status="valid", message="凭证有效", account_info=info or None)
        if payload.get("error"):
            detail = payload.get("error_description") or payload.get("error")
            return VerifyResult(status="invalid", message=f"凭证无效: {detail}")
    if status in (401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")
    message = f"无法确认凭证状态 (HTTP {status})"
    snippet = body_snippet(resp)
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def _verify(data: dict) -> VerifyResult:
    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        return VerifyResult(status="invalid", message="API Key 为空")

    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    if not base_url:
        return VerifyResult(status="error", message=f"厂商「{NAME}」需要填写 Base URL")

    return _probe(base_url, api_key)


@guarded
def verify(data: dict) -> VerifyResult:
    """验证 API Key 是否有效。保证不抛出异常。"""
    return _verify(data or {})


# ── 可选操作：简单对话 ──────────────────────────────────
CHAT_MODEL = "ernie-4.0-8k"
# 模型 → 文心聊天端点后缀（不同模型对应不同 endpoint）
_CHAT_ENDPOINTS = {
    "ernie-4.0-8k": "completions_pro",
    "ernie-3.5-8k": "completions",
    "ernie-speed-8k": "ernie-speed-8k",
    "ernie-lite-8k": "ernie-lite-8k",
    "ernie-tiny-8k": "ernie-tiny-8k",
}


def _get_access_token(base_url: str, api_key: str) -> tuple[Optional[str], Optional[str]]:
    """OAuth 换取 access_token；返回 (token, error)。"""
    client_id, client_secret = _split_key(api_key)
    if not client_id or not client_secret:
        return None, "凭证格式应为 API_KEY|SECRET_KEY"
    url = base_url.rstrip("/") + "/oauth/2.0/token"
    try:
        with make_client() as client:
            resp = client.post(
                url,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
            )
    except httpx.TimeoutException:
        return None, f"请求超时（{int(timeout())} 秒）"
    except httpx.HTTPError as exc:
        return None, f"网络请求失败（{type(exc).__name__}）"
    payload = safe_json(resp)
    if isinstance(payload, dict):
        if payload.get("access_token"):
            return payload["access_token"], None
        if payload.get("error"):
            return None, f"鉴权失败: {payload.get('error_description') or payload['error']}"
    return None, f"获取 access_token 失败 (HTTP {resp.status_code})"


def actions(data: dict) -> list[dict]:
    """声明可用操作（凭据含 API Key 时才提供简单对话）。"""
    if not (data or {}).get("api_key"):
        return []
    return [{"id": "chat", "name": "简单对话", "kind": "chat", "model": CHAT_MODEL}]


def list_models(data: dict) -> list:
    """可选模型即本插件内置的端点表（文心没有 OpenAI 式的 /models 列表接口）。"""
    return list(_CHAT_ENDPOINTS)


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行操作：简单对话（文心 ERNIE，需先以 OAuth 换 token）。"""
    if action_id != "chat":
        return {"ok": False, "error": "未知操作"}
    data = data or {}
    params = params or {}
    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    model = (params.get("model") or CHAT_MODEL).strip()
    endpoint = _CHAT_ENDPOINTS.get(model)
    if not endpoint:
        return {
            "ok": False,
            "error": f"不支持的文心模型：{model}（支持：{', '.join(_CHAT_ENDPOINTS)}）",
        }

    token, err = _get_access_token(base_url, (data.get("api_key") or "").strip())
    if err:
        return {"ok": False, "error": err}

    url = f"{base_url.rstrip('/')}/rpc/2.0/ai_custom/v1/wenxinworkshop/chat/{endpoint}"
    try:
        with make_client() as client:
            resp = client.post(
                url,
                params={"access_token": token},
                headers={"Content-Type": "application/json"},
                json={"messages": params.get("messages") or []},
            )
    except httpx.TimeoutException:
        return {"ok": False, "error": f"请求超时（{int(timeout())} 秒）"}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"网络请求失败（{type(exc).__name__}）"}
    payload = safe_json(resp)
    if isinstance(payload, dict):
        if payload.get("result"):
            return {"ok": True, "output": str(payload["result"]).strip()}
        if payload.get("error_msg"):
            return {"ok": False, "error": f"{payload.get('error_code')}: {payload['error_msg']}"}
    return {"ok": False, "error": f"HTTP {resp.status_code}"}


def get_display_summary(data: dict) -> str:
    """凭据列表中的简短展示文本。"""
    data = data or {}
    base_url = data.get("base_url") or DEFAULT_BASE_URL
    return f"{NAME} | {base_url}"


# ── 可选：欠费探测（鉴权非通用格式，故自行实现，不走 make_quota_probe）──────
# 文心既要用 API_KEY|SECRET_KEY 换 access_token、聊天端点又按模型分路径，通用实现拼不出来。
# 免费档模型（ernie-speed / lite / tiny）不计费，欠费时照样能对话、看不出问题，
# 所以自动选型刻意只挑**付费档**（ernie-3.5-8k 最便宜，其次 4.0）。
_PROBE_MODELS = ("ernie-3.5-8k", "ernie-4.0-8k")
# 百度把「额度用尽」放在错误码里（HTTP 200 + error_code）：19 = 总请求量已达上限。
# 17（当日额度）/ 18（QPS）既可能是欠费也可能是限流，故意不下结论，避免误报。
_INSUFFICIENT_CODES = {19}


def quota_probe(data: dict) -> Optional[dict]:
    """余额未知时发一次最小对话请求，实测账户是否可用。

    按 ``llm_common`` 的 ``quota_probe`` 契约返回：``None``（未开启 / 无结论）/
    ``quota_ok()``（实测可用）/ ``quota_insufficient()``（实测欠费）。
    """
    data = data or {}
    if not probe_requested(data):
        return None

    api_key = (data.get("api_key") or "").strip()
    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    if not api_key or not base_url:
        return None

    # 模型：用户指定的优先（须是本插件认识的端点），否则自动挑一个付费档
    chosen = probe_model(data)
    if chosen and chosen not in _CHAT_ENDPOINTS:
        return None  # 指定了不支持的模型，无从探测
    candidates = [chosen] if chosen else list(_PROBE_MODELS)
    candidates = [m for m in candidates if m in _CHAT_ENDPOINTS]
    if not candidates:
        return None

    token, err = _get_access_token(base_url, api_key)
    if err or not token:
        return None  # 换 token 失败不构成「欠费」结论（verify 已确认凭据有效）

    for name in candidates[:2]:
        url = (
            f"{base_url.rstrip('/')}/rpc/2.0/ai_custom/v1/wenxinworkshop/chat/"
            f"{_CHAT_ENDPOINTS[name]}"
        )
        try:
            with make_client() as client:
                resp = client.post(
                    url,
                    params={"access_token": token},
                    headers={"Content-Type": "application/json"},
                    json={"messages": [{"role": "user", "content": "hi"}]},
                )
        except httpx.HTTPError:
            return None  # 网络问题不构成欠费结论
        payload = safe_json(resp)
        if not isinstance(payload, dict):
            continue
        if "result" in payload:
            return quota_ok()
        code = payload.get("error_code")
        message = str(payload.get("error_msg") or "")
        if code in _INSUFFICIENT_CODES or is_insufficient_balance(message) or "欠费" in message:
            return quota_insufficient()
        # 其余（模型不可用 / 限流 / 参数错误）不下结论，换下一个候选
    return None
