"""Azure OpenAI API Key 验证插件（plugins_optin/llm/ 类别下的独立子插件）。

Azure 每个资源有独立 ``endpoint``（``https://<资源名>.openai.azure.com``），鉴权用
``api-key`` 头 + ``api-version`` 查询参数（**非** OpenAI 通用 Bearer 格式）。探针为
``GET {endpoint}/openai/models?api-version={api_version}``。

注意：本插件没有固定默认端点（用户必须填自己的 Azure 资源地址）；因无真实资源端点，
**未做阴性对照**（垃圾 key 行为未实测），判定沿用 Azure 官方约定：401/403 判无效，
其余（400/404/429/未知）一律归「无法确认」，避免把「端点写错」误判成「key 无效」。

参考文档：https://learn.microsoft.com/en-us/rest/api/azureopenai/models/list
"""
from __future__ import annotations

import httpx

from llm_common import VerifyResult, body_snippet, guarded, make_client

NAME = "Azure OpenAI"
DEFAULT_API_VERSION = "2024-10-21"


def _build_url(endpoint: str, api_version: str) -> str:
    """归一 endpoint：``https://<资源名>.openai.azure.com`` → ``.../openai/models?api-version=...``。

    兼容两种写法：只填主机名（自动补 ``/openai``），或已带 ``/openai`` 路径（原样使用）。
    """
    root = (endpoint or "").strip().rstrip("/")
    if not root:
        return ""
    base = root if root.endswith("/openai") else root + "/openai"
    version = (api_version or "").strip() or DEFAULT_API_VERSION
    return f"{base}/models?api-version={version}"


def _interpret(resp: httpx.Response) -> VerifyResult:
    """Azure 的判定：200 判有效，401/403 判无效，429（限流 / 额度耗尽）判有效，
    其余（400/404/未知）无法确认 —— 端点写错与 key 无效无法区分，绝不硬判 invalid。"""
    status = resp.status_code
    snippet = body_snippet(resp)
    if status == 200:
        return VerifyResult(status="valid", message="凭证有效")
    if status in (401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")
    if status == 429:
        # 429 只出现在鉴权之后（RateLimitExceeded / InsufficientQuota）⇒ 密钥有效。
        return VerifyResult(status="valid", message="凭证有效，但触发速率限制或额度耗尽 (HTTP 429)")
    return VerifyResult(
        status="error",
        message=f"无法确认凭证状态 (HTTP {status})" + (f": {snippet}" if snippet else ""),
    )


def validate(data: dict) -> list[str]:
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]
    errors: list[str] = []
    if not (data.get("endpoint") or "").strip():
        errors.append("Endpoint 不能为空（填 https://<资源名>.openai.azure.com）")
    if not (data.get("api_key") or "").strip():
        errors.append("API Key 不能为空")
    return errors


def _verify(data: dict) -> VerifyResult:
    endpoint = (data.get("endpoint") or "").strip()
    if not endpoint:
        return VerifyResult(
            status="error",
            message="缺少 Azure OpenAI Endpoint（填 https://<资源名>.openai.azure.com）",
        )
    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        return VerifyResult(status="invalid", message="API Key 为空")

    url = _build_url(endpoint, (data.get("api_version") or "").strip())
    with make_client() as client:
        resp = client.get(url, headers={"api-key": api_key})
    return _interpret(resp)


@guarded
def verify(data: dict) -> VerifyResult:
    """验证 Azure OpenAI API Key 是否有效。保证不抛出异常。"""
    return _verify(data or {})


def get_display_summary(data: dict) -> str:
    """凭据列表中的简短展示文本（不含密钥）。"""
    data = data or {}
    endpoint = (data.get("endpoint") or "").strip() or "（未填）"
    version = (data.get("api_version") or "").strip() or DEFAULT_API_VERSION
    return f"{NAME} | {endpoint} (api-version {version})"
