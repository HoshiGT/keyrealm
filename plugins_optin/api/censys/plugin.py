"""Censys 密钥验证插件。

- 端点: GET https://search.censys.io/api/v2/hosts/search?q=8.8.8.8&per_page=1
- 鉴权: HTTP Basic Auth (api_id / api_secret)
- 判定依据: 200 视为有效; 401 视为无效; 403/429/404 视为不确定。

重要说明(迁移):
- 本插件面向的是旧版 Search API 凭据(API ID + Secret)。
- Censys 已于 2026 年 9 月 30 日关闭旧版 account 端点(GET /api/v2/account 实测返回 404),
  但 hosts/search 仍可正常鉴权(无效凭据返回 401), 故改用该只读查询作为验证探针。
- Censys 官方已迁移到 Platform API(改用 PAT, 形如 censys_ex_..., 走 Bearer), 旧版 ID/Secret 形态正在逐步停用;
  若用户持有的是新版 PAT, 本插件不适用, 需另行支持。
参考文档：https://docs.censys.com/docs/ls-api
"""

from api_common import VerifyResult, guarded, make_client, body_snippet

NAME = "Censys"
SEARCH_URL = "https://search.censys.io/api/v2/hosts/search"


def _mask_secret(value):
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}***{value[-4:]}"


def _unconfirmed(status, snippet):
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def validate(data):
    if not data:
        return ["配置数据为空"]
    errors = []
    if not (data.get("api_id") or "").strip():
        errors.append("API ID 不能为空")
    if not (data.get("api_secret") or "").strip():
        errors.append("API Secret 不能为空")
    return errors


def _verify(data):
    api_id = (data.get("api_id") or "").strip()
    api_secret = (data.get("api_secret") or "").strip()
    with make_client() as client:
        resp = client.get(
            SEARCH_URL,
            params={"q": "8.8.8.8", "per_page": 1},
            auth=(api_id, api_secret),
        )
    status = resp.status_code
    if status == 200:
        return VerifyResult(status="valid", message="凭证有效")
    if status == 401:
        return VerifyResult(status="invalid", message="凭证无效 (HTTP 401)")
    return _unconfirmed(status, body_snippet(resp))


@guarded
def verify(data):
    data = data or {}
    if not (data.get("api_id") or "").strip() or not (data.get("api_secret") or "").strip():
        return VerifyResult(status="invalid", message="API ID 或 API Secret 为空")
    return _verify(data)


def get_display_summary(data):
    data = data or {}
    api_id = (data.get("api_id") or "").strip()
    return f"{NAME} · {api_id}" if api_id else NAME
