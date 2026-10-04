"""华泰证券（涨乐）凭证插件（plugins/exchange/ 类别下的独立子插件）。

注意：华泰证券未开放公开的联网账户校验接口，verify 仅返回占位提示，
不做任何网络请求。华泰涨乐（华泰证券）未开放面向个人开发者的公开
行情/账户校验 API，无法在不接入其私有网关（需机构签约与专用 SDK）
的情况下联网验证密钥，因此这里只做“凭证已保存”的占位结论。
"""
from exchange_common import VerifyResult, guarded

SERVICE_NAME = "华泰证券（涨乐）"


def _mask(value: str) -> str:
    """仅展示密钥前缀，避免在列表/日志中回显完整凭据。"""
    value = (value or "").strip()
    if len(value) <= 8:
        return "***" if value else ""
    return f"{value[:4]}***{value[-4:]}"


def validate(data) -> list[str]:
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]
    errors: list[str] = []
    if not (data.get("api_key") or "").strip():
        errors.append("API Key 不能为空")
    return errors


@guarded
def verify(data) -> VerifyResult:
    """华泰证券：暂无公开可用的联网校验接口，仅保存。"""
    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        return VerifyResult(status="invalid", message="API Key 为空")
    return VerifyResult(
        status="error",
        message="该券商暂无公开联网校验接口，仅保存",
    )


def get_display_summary(data) -> str:
    """凭据列表中的简短展示文本（不回显完整密钥）。"""
    data = data or {}
    api_key = _mask(data.get("api_key") or "")
    if not api_key:
        return SERVICE_NAME
    return f"{SERVICE_NAME} | {api_key}（仅保存）"
