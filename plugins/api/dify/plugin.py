"""Dify 应用 API Key 验证插件（plugins/api/ 类别下的独立子插件）。

只做一件事：**只读**调服务端 ``GET {base_url}/parameters``（「获取应用参数」）。

选它的理由：这是 Dify 服务 API 里少数**既校验 Bearer 令牌、又不产生任何会话或额度消耗**
的读接口——回 200 就等价于「这个 app- 令牌当前可用」，回包还自带该应用的编排形态
（开场白、建议问题、输入表单项、文件上传开关），足以证明它确实挂在某个真实应用上。

自带完整的校验与展示逻辑，只复用 api_common 的通用工具。

参考文档：https://docs.dify.ai/en/api-reference/applications/get-app-parameters
"""
from api_common import VerifyResult, body_snippet, guarded, log, make_client, safe_json

NAME = "Dify"
DEFAULT_BASE_URL = "https://api.dify.ai/v1"
DISPLAY_FIELD = "api_key"

# 401/403 之外的「令牌本身不成立」判定：Dify 用 code=unauthorized / access_denied 表达
INVALID_CODES = {"unauthorized", "access_denied", "invalid_token"}


def _unconfirmed(status: int, snippet: str) -> VerifyResult:
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def _summarize(payload: dict) -> tuple[str, dict]:
    """把 /parameters 的回包压成一行说明 + 一份结构化信息。"""
    info: dict = {}
    parts = ["API Key 有效"]

    form = payload.get("user_input_form")
    if isinstance(form, list) and form:
        info["user_input_form_count"] = len(form)
        parts.append(f"{len(form)} 个输入项")

    questions = payload.get("suggested_questions")
    if isinstance(questions, list) and questions:
        info["suggested_questions_count"] = len(questions)
        parts.append(f"{len(questions)} 条建议问题")

    opening = " ".join(str(payload.get("opening_statement") or "").split())
    if opening:
        info["opening_statement"] = opening[:120]

    file_upload = payload.get("file_upload")
    if isinstance(file_upload, dict) and file_upload.get("enabled") is not None:
        info["file_upload_enabled"] = bool(file_upload.get("enabled"))

    system_parameters = payload.get("system_parameters")
    if isinstance(system_parameters, dict):
        features = sorted(system_parameters.keys())
        if features:
            info["system_parameters"] = features

    return "，".join(parts), info


def _verify_dify(base_url: str, api_key: str) -> VerifyResult:
    url = base_url.rstrip("/") + "/parameters"
    with make_client() as client:
        resp = client.get(url, headers={"Authorization": f"Bearer {api_key}"})

    status = resp.status_code
    payload = safe_json(resp) or {}

    if status == 200:
        message, info = _summarize(payload if isinstance(payload, dict) else {})
        info["base_url"] = base_url
        return VerifyResult(status="valid", message=message, account_info=info)

    code = str(payload.get("code") or "").strip().lower()
    detail = str(payload.get("message") or "").strip()
    if status in (401, 403) or code in INVALID_CODES:
        return VerifyResult(
            status="invalid",
            message=f"API Key 无效（HTTP {status}{'，' + detail if detail else ''}）",
        )
    if status == 404:
        return VerifyResult(
            status="error",
            message=f"Base URL 下找不到 /parameters（HTTP 404），请确认地址是否含版本前缀：{base_url}",
        )
    if status == 429:
        return VerifyResult(status="valid", message="API Key 有效，但触发速率限制 (HTTP 429)")
    return _unconfirmed(status, detail or body_snippet(resp))


def validate(data):
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]

    errors: list[str] = []
    if not (data.get(DISPLAY_FIELD) or "").strip():
        errors.append("API Key 不能为空")

    base_url = (data.get("base_url") or "").strip()
    if base_url and not base_url.startswith(("http://", "https://")):
        errors.append("Base URL 需以 http:// 或 https:// 开头")
    return errors


@guarded
def verify(data):
    """验证 Dify 应用 API Key 是否有效。"""
    data = data or {}

    api_key = (data.get(DISPLAY_FIELD) or "").strip()
    if not api_key:
        return VerifyResult(status="invalid", message="API Key 为空")

    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    log(f"校验 Dify 凭据（{base_url}）")
    return _verify_dify(base_url, api_key)


def get_display_summary(data):
    """凭据列表中的简短展示文本。"""
    data = data or {}
    return f"{NAME} | {(data.get('base_url') or '').strip() or DEFAULT_BASE_URL}"
