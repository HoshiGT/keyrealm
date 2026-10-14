"""百度文心 API Key 验证插件（plugins/llm/ 类别下的独立子插件）。

协议：POST {base}/oauth/2.0/token（client_credentials）。
凭证为 **API Key + Secret Key 两个字段**（旧版把两者拼在一个字段里、用 ``|`` 分隔，仍兼容）。

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
from llm_common import probe_model, probe_requested, quota_insufficient, quota_ok, quota_unknown
from llm_protocols import is_insufficient_balance, resolve_chat_model

NAME = "百度文心"
DEFAULT_BASE_URL = "https://aip.baidubce.com"
PROTOCOL = "wenxin"


def _creds(data: dict) -> tuple[str, str]:
    """取 (client_id, client_secret)。

    新格式是两个独立字段 ``api_key`` / ``secret_key``；旧版把两者拼在一个字段里
    （``API_KEY|SECRET_KEY``），这里继续认——``api_key`` 里出现 ``|`` 就地拆分，
    于是老凭据不改动也仍然可用（用户重填这笔时自然分开即可）。
    """
    api_key = (data.get("api_key") or "").strip()
    secret = (data.get("secret_key") or "").strip()
    if "|" in api_key:
        client_id, _, tail = api_key.partition("|")
        return client_id.strip(), (tail.strip() or secret)
    return api_key, secret


def validate(data: dict) -> list[str]:
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]

    errors: list[str] = []
    client_id, client_secret = _creds(data)
    if not client_id:
        errors.append("API Key 不能为空")
    if not client_secret:
        errors.append("Secret Key 不能为空")
    if not DEFAULT_BASE_URL and not (data.get("base_url") or "").strip():
        errors.append(f"厂商「{NAME}」需要填写 Base URL")
    return errors


def _probe(base_url: str, client_id: str, client_secret: str) -> VerifyResult:
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
    client_id, client_secret = _creds(data)
    if not client_id or not client_secret:
        return VerifyResult(status="invalid", message="API Key / Secret Key 不完整")

    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    if not base_url:
        return VerifyResult(status="error", message=f"厂商「{NAME}」需要填写 Base URL")

    return _probe(base_url, client_id, client_secret)


@guarded
def verify(data: dict) -> VerifyResult:
    """验证 API Key 是否有效。保证不抛出异常。"""
    return _verify(data or {})


# ── 可选操作：简单对话 ──────────────────────────────────
# 模型 → 文心聊天端点后缀。**这是百度协议要求的路由表**（不同模型挂不同 endpoint 后缀，
# 且文心没有 OpenAI 式的 `/models` 列表接口），不是「默认模型」——所以它保留。
_CHAT_ENDPOINTS = {
    "ernie-4.0-8k": "completions_pro",
    "ernie-3.5-8k": "completions",
    "ernie-speed-8k": "ernie-speed-8k",
    "ernie-lite-8k": "ernie-lite-8k",
    "ernie-tiny-8k": "ernie-tiny-8k",
}


def _get_access_token(base_url: str, client_id: str, client_secret: str) -> tuple[Optional[str], Optional[str]]:
    """OAuth 换取 access_token；返回 (token, error)。"""
    if not client_id or not client_secret:
        return None, "API Key / Secret Key 不完整"
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
    """声明可用操作（凭据含完整的 API Key / Secret Key 时才提供简单对话）。"""
    client_id, client_secret = _creds(data or {})
    if not client_id or not client_secret:
        return []
    return [{"id": "chat", "name": "简单对话", "kind": "chat", "model": ""}]


def list_models(data: dict, force: bool = False) -> list:
    """可选模型即本插件的**端点路由表**（文心没有 OpenAI 式的 `/models` 列表接口）。

    路由表由协议定义，非默认模型配置，因此直接返回其键。
    ``force`` 对本插件无缓存效果（不联网取列表），仅为契约一致性而接受。
    """
    return list(_CHAT_ENDPOINTS)


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行操作：简单对话（文心 ERNIE，需先以 OAuth 换 token）。"""
    if action_id != "chat":
        return {"ok": False, "error": "未知操作"}
    data = data or {}
    params = params or {}
    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    model = resolve_chat_model(params.get("model"), lambda: list_models(data)).strip()
    if not model:
        return {"ok": False, "error": "无法确定模型：请先选择模型（本插件模型表为空）"}
    endpoint = _CHAT_ENDPOINTS.get(model)
    if not endpoint:
        return {
            "ok": False,
            "error": f"不支持的文心模型：{model}（支持：{', '.join(_CHAT_ENDPOINTS)}）",
        }

    client_id, client_secret = _creds(data)
    token, err = _get_access_token(base_url, client_id, client_secret)
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


# ── 可选：欠费探测（鉴权格式非通用，因此不使用 make_quota_probe）────────
# 文心需用 API Key / Secret Key 换 access_token，且聊天端点按模型分路径，无法直接复用通用探测。
# 免费档模型（ernie-speed / lite / tiny）不计费，因此探测仅选择付费档。
# 付费档由端点后缀与模型名是否一致推断，不额外维护模型名单。
_PAID_MODELS = tuple(m for m, ep in _CHAT_ENDPOINTS.items() if ep != m)
# 数值错误码 17/18/19 分别表示当日请求量、QPS、总请求量限制，不表示欠费。
# 欠费仅识别千帆字符串错误码（ChargeNotEnough / NoOpenedBilling / OptInRequired）。
_INSUFFICIENT_CODES: frozenset = frozenset()          # 数值码里没有欠费
_INSUFFICIENT_TEXT_CODES = ("chargenotenough", "noopenedbilling", "optinrequired")


def quota_probe(data: dict) -> Optional[dict]:
    """余额未知时发起一次最小对话请求，判断账户是否可用。

    按 ``llm_common`` 的 ``quota_probe`` 契约返回：``None``（未开启探测）/
    ``quota_ok()``（可用）/ ``quota_insufficient()``（欠费）/ ``quota_unknown()``（无法判断）。
    """
    data = data or {}
    if not probe_requested(data):
        return None

    client_id, client_secret = _creds(data)
    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    if not client_id or not client_secret or not base_url:
        return None

    # 指定模型不在端点路由表中时无法探测。
    chosen = probe_model(data)
    if chosen and chosen not in _CHAT_ENDPOINTS:
        return quota_unknown()  # 指定模型不在端点路由表中，无法执行探测
    candidates = [chosen] if chosen else list(_PAID_MODELS)
    candidates = [m for m in candidates if m in _CHAT_ENDPOINTS]
    if not candidates:
        return quota_unknown()

    token, err = _get_access_token(base_url, client_id, client_secret)
    if err or not token:
        return quota_unknown()  # 换 token 失败不构成「欠费」结论（verify 已确认凭据有效）

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
            return quota_unknown()  # 网络问题不构成欠费结论
        payload = safe_json(resp)
        if not isinstance(payload, dict):
            continue
        if "result" in payload:
            return quota_ok()
        code = payload.get("error_code")
        message = str(payload.get("error_msg") or "")
        # 仅欠费字符串码及相关文案触发欠费结论。
        haystack = f"{code} {message}".lower()
        if any(t in haystack for t in _INSUFFICIENT_TEXT_CODES) or is_insufficient_balance(haystack):
            return quota_insufficient()
        # 其余（模型不可用 / 限流 / 参数错误）不下欠费结论，换下一个候选
    return quota_unknown()  # 候选耗尽仍无法判断时返回未知状态
