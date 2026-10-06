"""腾讯混元（Hunyuan）API Key 验证插件（plugins_optin/llm/ 类别下的独立子插件）。

OpenAI 兼容协议探针 ``GET {base}/models``（Bearer 认证）。该端点真实鉴权：垃圾 key
实测返回 401 ``Incorrect API key provided``，可安全用于验证（不踩「/models 不鉴权」的坑）。

参考文档：https://cloud.tencent.com/document/product/1729/116755
"""
from __future__ import annotations

import httpx

from llm_common import VerifyResult, body_snippet, guarded, make_client

NAME = "腾讯混元"
DEFAULT_BASE_URL = "https://api.hunyuan.cloud.tencent.com/v1"

# 部分厂商把「无效 key」报成 HTTP 400 而非 401/403（如 xAI），按正文关键字识别鉴权失败。
_AUTH_FAIL_KEYWORDS = (
    "incorrect api key", "invalid api key", "api key not valid", "wrong api key",
    "invalid_api_key", "incorrect_api_key", "invalid x-api-key", "unauthorized",
)


def _interpret(resp: httpx.Response) -> VerifyResult:
    """GET /models 的判定：200 判有效；401/403 及 400 鉴权错判无效；
    402（欠费）与 429（限流）都发生在鉴权之后 ⇒ 密钥本身有效；其余无法确认。"""
    status = resp.status_code
    snippet = body_snippet(resp)
    if status == 200:
        return VerifyResult(status="valid", message="凭证有效")
    if status in (401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")
    lowered = snippet.lower()
    if status == 400 and any(k in lowered for k in _AUTH_FAIL_KEYWORDS):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP 400): {snippet}")
    if status == 402:
        return VerifyResult(status="valid", message="凭证有效，但账户欠费或无可用额度 (HTTP 402)")
    if status == 429:
        # 429 只出现在鉴权之后（限流 / 额度耗尽都要求 key 已被接受）⇒ 密钥有效。
        # 口径同 plugins/llm/llm_protocols.py 的 _interpret_simple。
        return VerifyResult(status="valid", message="凭证有效，但触发速率限制 (HTTP 429)")
    return VerifyResult(
        status="error",
        message=f"无法确认凭证状态 (HTTP {status})" + (f": {snippet}" if snippet else ""),
    )


def _probe(base_url: str, api_key: str) -> VerifyResult:
    url = base_url.rstrip("/") + "/models"
    with make_client() as client:
        resp = client.get(url, headers={"Authorization": f"Bearer {api_key}"})
    return _interpret(resp)


def validate(data: dict) -> list[str]:
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]
    errors: list[str] = []
    if not (data.get("api_key") or "").strip():
        errors.append("API Key 不能为空")
    return errors


def _verify(data: dict) -> VerifyResult:
    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        return VerifyResult(status="invalid", message="API Key 为空")
    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    return _probe(base_url, api_key)


@guarded
def verify(data: dict) -> VerifyResult:
    """验证 API Key 是否有效。保证不抛出异常。"""
    return _verify(data or {})


def get_display_summary(data: dict) -> str:
    """凭据列表中的简短展示文本（不含密钥）。"""
    data = data or {}
    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    return f"{NAME} | {base_url}"
