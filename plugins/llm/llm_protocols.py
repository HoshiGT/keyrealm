"""LLM 协议级工具模块（供各厂商子插件复用）。

不放最底层工具（那些在 llm_common.py），只放**协议级、跨厂商完全相同**的能力：

- 校验探针：`probe_openai_models` / `probe_anthropic_models` / `probe_openai_chat`
  （统一经 `_request_with_retry` 发请求：上游 5xx 退避重试，避免把偶发故障误判为失败）
- 简单对话：`chat_completion`（openai / anthropic / gemini 三种协议）
- 欠费探测：`quota_chat_probe`（通用实现）、`is_insufficient_balance`（关键字判定，
  供自建 `quota_probe` 的插件复用）

厂商的默认地址、模型名、余额端点与解析等专属逻辑仍写在各
plugins/llm/<厂商>/plugin.py 内。
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

import httpx

from backend.plugin_sdk import VerifyResult, body_snippet, make_client, safe_json, timeout

# 欠费/余额不足判定关键字（出现在错误正文中）
_INSUFFICIENT_BALANCE_KEYWORDS = (
    "余额不足",
    "充值",
    "insufficient",
    "no balance",
    "out of balance",
    "1113",  # 智谱错误码：余额不足
)


def _anthropic_root(base_url: str) -> str:
    """Anthropic 协议的站点根：``/v1/messages`` 由本模块自己拼，Base URL 自带 ``/v1`` 要去掉。

    各插件默认值不统一——Anthropic 官方是 ``…/v1``，智谱 Anthropic 端点是
    ``…/api/anthropic``；不做归一就会拼出 ``…/v1/v1/messages``。
    """
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3].rstrip("/")
    return root


# ── 简单对话 ─────────────────────────────────────────────

def chat_completion(base_url: str, api_key: str, model: str, messages: list,
                    protocol: str = "openai", max_tokens: int = 512) -> dict:
    """发起一次简单对话。返回 {"ok": bool, "output": str} 或 {"ok": False, "error": str}。

    protocol:
      - "openai"     → POST {base}/chat/completions，Bearer 头
      - "anthropic"  → POST {base}/v1/messages，x-api-key 头
      - "gemini"     → POST {base}/models/{model}:generateContent?key=…（模型名在路径里）
    """
    if not model:
        return {"ok": False, "error": "未指定模型"}
    if not api_key:
        return {"ok": False, "error": "缺少 API Key"}
    root = base_url.rstrip("/")
    messages = messages or []
    try:
        with make_client() as client:
            if protocol == "gemini":
                contents = [
                    {
                        "role": "model" if (m.get("role") == "assistant") else "user",
                        "parts": [{"text": str(m.get("content", ""))}],
                    }
                    for m in messages
                ]
                resp = client.post(
                    f"{root}/models/{model}:generateContent",
                    params={"key": api_key},
                    headers={"Content-Type": "application/json"},
                    json={"contents": contents},
                )
            elif protocol == "anthropic":
                resp = client.post(
                    _anthropic_root(base_url) + "/v1/messages",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "Content-Type": "application/json",
                    },
                    json={"model": model, "max_tokens": max_tokens, "messages": messages},
                )
            else:
                resp = client.post(
                    root + "/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={"model": model, "messages": messages, "max_tokens": max_tokens},
                )
    except httpx.TimeoutException:
        return {"ok": False, "error": f"请求超时（{int(timeout())} 秒）"}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"网络请求失败（{type(exc).__name__}）"}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底
        return {"ok": False, "error": f"执行异常（{type(exc).__name__}）"}

    status = resp.status_code
    payload = safe_json(resp)
    if status == 200 and isinstance(payload, dict):
        if protocol == "gemini":
            candidates = payload.get("candidates")
            if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
                parts = (candidates[0].get("content") or {}).get("parts")
                if isinstance(parts, list):
                    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
                    if text.strip():
                        return {"ok": True, "output": text.strip()}
        elif protocol == "anthropic":
            blocks = payload.get("content")
            if isinstance(blocks, list):
                text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
                if text.strip():
                    return {"ok": True, "output": text.strip()}
        else:
            choices = payload.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                message = choices[0].get("message")
                if isinstance(message, dict) and message.get("content"):
                    return {"ok": True, "output": str(message["content"]).strip()}
        return {"ok": False, "error": "响应中没有可读内容"}
    # Gemini 无效 key 返回 400
    if status in (400, 401, 403):
        snippet = body_snippet(resp)
        detail = f": {snippet}" if snippet else ""
        return {"ok": False, "error": f"鉴权或请求错误 (HTTP {status}){detail}"}
    snippet = body_snippet(resp)
    return {"ok": False, "error": f"HTTP {status}" + (f": {snippet}" if snippet else "")}


# ── 流式简单对话 ─────────────────────────────────────────

def _extract_delta(protocol: str, line: str) -> str:
    """从一行 SSE 数据里取出新增文本（无则返回空串）。"""
    if isinstance(line, bytes):
        line = line.decode("utf-8", "ignore")
    line = line.strip()
    if not line.startswith("data:"):
        return ""
    payload = line[5:].strip()
    if not payload or payload == "[DONE]":
        return ""
    try:
        obj = json.loads(payload)
    except Exception:
        return ""
    if not isinstance(obj, dict):
        return ""

    if protocol == "gemini":
        candidates = obj.get("candidates")
        if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
            parts = (candidates[0].get("content") or {}).get("parts")
            if isinstance(parts, list):
                return "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        return ""
    if protocol == "anthropic":
        if obj.get("type") == "content_block_delta":
            return str((obj.get("delta") or {}).get("text") or "")
        return ""
    choices = obj.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return str((choices[0].get("delta") or {}).get("content") or "")
    return ""


def chat_completion_stream(base_url: str, api_key: str, model: str, messages: list,
                           protocol: str = "openai", max_tokens: int = 512,
                           on_delta=None) -> dict:
    """流式对话：每收到一段文本即调用 `on_delta(text)`。

    返回 {"ok": True, "output": 全文} 或 {"ok": False, "error": ..., "output": 已收到的部分}。
    """
    if not model:
        return {"ok": False, "error": "未指定模型", "output": ""}
    if not api_key:
        return {"ok": False, "error": "缺少 API Key", "output": ""}

    root = base_url.rstrip("/")
    messages = messages or []
    parts: list = []
    emit = on_delta or (lambda _t: None)

    if protocol == "gemini":
        contents = [
            {
                "role": "model" if (m.get("role") == "assistant") else "user",
                "parts": [{"text": str(m.get("content", ""))}],
            }
            for m in messages
        ]
        request = dict(
            method="POST",
            url=f"{root}/models/{model}:streamGenerateContent",
            params={"key": api_key, "alt": "sse"},
            headers={"Content-Type": "application/json"},
            json={"contents": contents},
        )
    elif protocol == "anthropic":
        request = dict(
            method="POST",
            url=_anthropic_root(base_url) + "/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={"model": model, "max_tokens": max_tokens, "messages": messages, "stream": True},
        )
    else:
        request = dict(
            method="POST",
            url=root + "/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "messages": messages, "max_tokens": max_tokens, "stream": True},
        )

    try:
        with make_client() as client:
            with client.stream(**request) as resp:
                if resp.status_code != 200:
                    resp.read()
                    snippet = body_snippet(resp)
                    detail = f": {snippet}" if snippet else ""
                    if resp.status_code in (400, 401, 403):
                        return {"ok": False, "error": f"鉴权或请求错误 (HTTP {resp.status_code}){detail}",
                                "output": "".join(parts)}
                    return {"ok": False, "error": f"HTTP {resp.status_code}{detail}",
                            "output": "".join(parts)}
                for line in resp.iter_lines():
                    text = _extract_delta(protocol, line)
                    if text:
                        parts.append(text)
                        emit(text)
    except httpx.TimeoutException:
        return {"ok": False, "error": f"请求超时（{int(timeout())} 秒）", "output": "".join(parts)}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"网络请求失败（{type(exc).__name__}）", "output": "".join(parts)}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底
        return {"ok": False, "error": f"执行异常（{type(exc).__name__}）", "output": "".join(parts)}

    output = "".join(parts)
    if not output.strip():
        return {"ok": False, "error": "响应中没有可读内容", "output": ""}
    return {"ok": True, "output": output.strip()}


# ── 模型列表（供「简单对话」选择模型）────────────────────
def fetch_models(protocol: str, base_url: str, api_key: str) -> list:
    """获取该厂商可用模型列表（best-effort，失败/无权限返回 []）。

    protocol:
      - "openai" / "openrouter" → GET {base}/models（Bearer）→ data[].id
      - "anthropic"             → GET {base}/models（x-api-key）→ data[].id
      - "gemini"                → GET {base}/models?key=… → models[].name（去掉 "models/" 前缀）
    """
    if not api_key:
        return []
    root = base_url.rstrip("/")
    try:
        with make_client() as client:
            if protocol == "gemini":
                resp = client.get(f"{root}/models", params={"key": api_key})
            elif protocol == "anthropic":
                resp = client.get(
                    root + "/models",
                    params={"limit": 100},
                    headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                )
            else:
                resp = client.get(root + "/models", headers={"Authorization": f"Bearer {api_key}"})
        if resp.status_code != 200:
            return []
        payload = safe_json(resp)
    except Exception:  # noqa: BLE001 - 取列表失败即返回空
        return []
    if not isinstance(payload, dict):
        return []

    names: set = set()
    if protocol == "gemini":
        for item in payload.get("models") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", ""))
            if name.startswith("models/"):
                name = name[len("models/"):]
            if name:
                names.add(name)
    else:
        for item in payload.get("data") or []:
            if isinstance(item, dict) and item.get("id"):
                names.add(str(item["id"]))
    return sorted(names)


# ── 欠费探测（可选项，由应用设置开关控制）───────────────

# 便宜模型的命名线索（命中越靠前越优先）——「默认智能获取测试模型」的挑选依据
_PROBE_MODEL_HINTS = (
    "flash", "lite", "mini", "nano", "small", "haiku", "air", "turbo", "free", "8b", "7b",
)
# 不能用于对话的模型（嵌入 / 重排 / 语音 / 图像等），探测时排除
_PROBE_MODEL_EXCLUDE = (
    "embed", "rerank", "tts", "whisper", "moderation", "audio", "video", "image",
    "dall", "flux", "stable-diffusion", "sd3", "veo", "sora",
)
# 一次探测最多尝试几个候选模型（模型不可用时换下一个）
_PROBE_MAX_CANDIDATES = 3


def _probe_model_candidates(base_url: str, api_key: str, protocol: str) -> list:
    """挑探测用的候选模型：先选名字像「便宜款」的，再兜底其它可对话模型。"""
    models = fetch_models(protocol, base_url, api_key)
    usable = [m for m in models if not any(x in m.lower() for x in _PROBE_MODEL_EXCLUDE)]
    hinted = [m for m in usable if any(h in m.lower() for h in _PROBE_MODEL_HINTS)]
    ordered = hinted + [m for m in usable if m not in hinted]
    return ordered


def quota_chat_probe(base_url: str, api_key: str, protocol: str = "openai",
                     preferred_model: str = "") -> Optional[dict]:
    """发一次最小对话请求，判断账户是否真的可用（欠费探测）。

    只在「凭据有效但拿不到余额」时才有意义：``/v1/models`` 对欠费账户同样返回 200，
    分不清「可用」与「账户欠费」，这里用 ``max_tokens=1`` 的一发请求换一个确定结论。

    返回 ``None``（未发现异常 / 无法判定）或：

    - ``{"account_info": {"chat_probe": "可用"}}``：实测通过；
    - ``{"message": "..."}``：实测发现账户欠费或无可用额度。

    模型选择：``preferred_model`` 优先（用户在设置里指定），否则自动挑一个便宜的；
    候选模型不可用时换下一个，都不成则放弃（不误报）。
    """
    if not base_url or not api_key:
        return None
    candidates = [preferred_model] if preferred_model else _probe_model_candidates(
        base_url, api_key, protocol
    )
    candidates = [c for c in candidates if c][:_PROBE_MAX_CANDIDATES]
    if not candidates:
        return None

    for model in candidates:
        resp = chat_completion(
            base_url, api_key, model,
            [{"role": "user", "content": "hi"}],
            protocol=protocol, max_tokens=1,
        )
        if resp.get("ok"):
            return {"account_info": {"chat_probe": "可用"}}
        error = str(resp.get("error") or "")
        # 402 / 欠费关键字 = 账户没钱（模型本身是能用的，说明请求已到计费环节）
        if "402" in error or is_insufficient_balance(error):
            return {"message": "凭证有效，但账户欠费或无可用额度（已用最小对话实测）"}
        # 其它错误（模型不存在 / 鉴权 / 限流）不构成欠费结论，换下一个候选
    return None


# ── 校验探针 ─────────────────────────────────────────────

# 上游偶发的 5xx：实测智谱 Anthropic 端点在免费模型上约 1/3 概率返回
# 500 Internal Network Failure，同一请求重试即成功——不重试会把好 key 误判为失败。
# 只重试 5xx：429（限流）与 4xx 重试没有意义，反而加重对端负担。
_TRANSIENT_STATUS = (500, 502, 503, 504)
_PROBE_ATTEMPTS = 3


def _request_with_retry(client: httpx.Client, method: str, url: str, **kwargs) -> httpx.Response:
    """发一次请求；上游返回 5xx 时退避重试，最多 ``_PROBE_ATTEMPTS`` 次。"""
    resp = client.request(method, url, **kwargs)
    for attempt in range(1, _PROBE_ATTEMPTS):
        if resp.status_code not in _TRANSIENT_STATUS:
            return resp
        time.sleep(0.4 * attempt)
        resp = client.request(method, url, **kwargs)
    return resp


def probe_openai_models(base_url: str, api_key: str) -> VerifyResult:
    """OpenAI 协议：GET {base}/models，Bearer 认证。"""
    url = base_url.rstrip("/") + "/models"
    with make_client() as client:
        resp = _request_with_retry(client, "GET", url,
                                   headers={"Authorization": f"Bearer {api_key}"})
    return _interpret_simple(resp)


def probe_anthropic_models(base_url: str, api_key: str, verify_path: str = "/models") -> VerifyResult:
    """Anthropic 协议：GET {base}{verify_path}，x-api-key 认证。"""
    url = base_url.rstrip("/") + verify_path
    with make_client() as client:
        resp = _request_with_retry(
            client, "GET", url,
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        )
    return _interpret_simple(resp)


def probe_openai_chat(base_url: str, api_key: str, model: str,
                      timeout: Optional[float] = None) -> VerifyResult:
    """OpenAI 协议：POST {base}/chat/completions（消耗极小额度，可识别欠费）。

    ``timeout`` 为空时用全局超时；显式传入则单次请求用它（用于「连试多个候选
    模型、每个只给短超时」的验证策略）。
    """
    url = base_url.rstrip("/") + "/chat/completions"
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}
    with make_client(request_timeout=timeout) as client:
        resp = _request_with_retry(
            client, "POST", url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body,
        )
    return _interpret_chat(resp)


# ── 响应判定 ─────────────────────────────────────────────

def _interpret_simple(resp: httpx.Response) -> VerifyResult:
    """简单 GET 探针（models 类）的通用判定。"""
    status = resp.status_code
    payload = safe_json(resp)
    if status == 200:
        return VerifyResult(status="valid", message="凭证有效", account_info=_extract_account_info(payload))
    if status in (401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")
    if status == 429:
        return VerifyResult(status="valid", message="凭证有效，但触发速率限制 (HTTP 429)")
    return _unconfirmed(status, body_snippet(resp))


def _interpret_chat(resp: httpx.Response) -> VerifyResult:
    """chat 探针判定：额外识别 429 余额不足（欠费）与速率限制。"""
    status = resp.status_code
    payload = safe_json(resp)
    snippet = body_snippet(resp)
    if status == 200:
        return VerifyResult(status="valid", message="凭证有效", account_info=_extract_account_info(payload))
    if status in (401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")
    if status == 429:
        if is_insufficient_balance(snippet):
            # 欠费不算失效：密钥本身有效，仅账户余额不足
            return VerifyResult(status="valid", message="凭证有效，但账户欠费或无可用资源包")
        return VerifyResult(status="valid", message="凭证有效，但触发速率限制 (HTTP 429)")
    if status == 402:
        # 402 Payment Required：欠费不算失效
        return VerifyResult(status="valid", message="凭证有效，但账户欠费或无可用额度 (HTTP 402)")
    return _unconfirmed(status, snippet)


def _unconfirmed(status: int, snippet: str) -> VerifyResult:
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def is_insufficient_balance(text: str) -> bool:
    """文本里是否含「欠费 / 余额不足」类关键字。

    各协议通用；子插件自己实现 ``quota_probe`` 时直接复用这个判定（另可按需叠加厂商
    专属错误码，如百度文心的 17/18/19）。
    """
    lowered = (text or "").lower()
    return any(kw in lowered for kw in _INSUFFICIENT_BALANCE_KEYWORDS)


def _extract_account_info(payload: Any) -> Optional[dict]:
    """从响应中提取账号信息（organization / org_id）。

    LLM 的「附加信息」只展示余额 / 使用率；余额由各厂商 plugin.py 自行查询后合并。
    """
    if not isinstance(payload, dict):
        return None
    info: dict[str, Any] = {}
    for key in ("organization", "org_id"):
        if payload.get(key):
            info[key] = payload[key]
    return info or None
