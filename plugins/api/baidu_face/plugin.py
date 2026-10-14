"""百度人脸（人脸识别）插件（plugins/api/ 类别）。

认证走**百度 AI 开放平台**的 client_credentials OAuth：

    POST {base}/oauth/2.0/token
        grant_type=client_credentials & client_id=<API Key> & client_secret=<Secret Key>
        → access_token（之后的接口以 `?access_token=` 携带）

凭证为 **API Key + Secret Key 两个独立字段**（与「百度文心」同一套平台认证）。
校验取**只读**的「人脸库分组列表」接口——不需要传图片，空的人脸库也算连通。

参考文档：https://ai.baidu.com/ai-doc/FACE/ek37c1qiz
"""
from __future__ import annotations

import httpx
from typing import Optional

from backend.plugin_sdk import VerifyResult, guarded, make_client, safe_json, timeout

NAME = "百度人脸"
DEFAULT_BASE_URL = "https://aip.baidubce.com"
_GROUP_LIST_PATH = "/rest/2.0/face/v3/faceset/group/getlist"

# 数值错误码：token 无效 / 过期 → 凭据有问题；配额类（17/18/19）说明**凭据有效**、只是当下受限。
_TOKEN_BAD_CODES = {110, 111}
_QUOTA_CODES = {17, 18, 19}


def _creds(data: dict) -> tuple[str, str]:
    """取 (client_id, client_secret)——插件只认当前格式：两个独立字段。"""
    return (data.get("api_key") or "").strip(), (data.get("secret_key") or "").strip()


def _base(data: dict) -> str:
    return (data.get("base_url") or "").strip() or DEFAULT_BASE_URL


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
    return errors


def _get_access_token(base_url: str, client_id: str,
                      client_secret: str) -> tuple[Optional[str], Optional[str]]:
    """OAuth 换取 access_token；返回 (token, error)。"""
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
            return str(payload["access_token"]), None
        if payload.get("error"):
            return None, f"鉴权失败：{payload.get('error_description') or payload['error']}"
    return None, f"换取 access_token 失败（HTTP {resp.status_code}）"


def _fetch_groups(base_url: str, token: str) -> tuple[Optional[dict], Optional[str]]:
    """只读拉一次人脸库分组列表；返回 (payload, error)。"""
    url = base_url.rstrip("/") + _GROUP_LIST_PATH
    try:
        with make_client() as client:
            resp = client.post(
                url,
                params={"access_token": token},
                headers={"Content-Type": "application/json"},
                json={"start": 0, "length": 1},
            )
    except httpx.TimeoutException:
        return None, f"请求超时（{int(timeout())} 秒）"
    except httpx.HTTPError as exc:
        return None, f"网络请求失败（{type(exc).__name__}）"
    payload = safe_json(resp)
    if isinstance(payload, dict):
        return payload, None
    return None, f"接口返回非 JSON（HTTP {resp.status_code}）"


def _verify(data: dict) -> VerifyResult:
    client_id, client_secret = _creds(data)
    if not client_id or not client_secret:
        return VerifyResult(status="invalid", message="API Key / Secret Key 不完整")

    base_url = _base(data)
    token, err = _get_access_token(base_url, client_id, client_secret)
    if err:
        return VerifyResult(status="invalid" if "鉴权失败" in err else "error", message=err)

    payload, err = _fetch_groups(base_url, token or "")
    if err:
        return VerifyResult(status="error", message=err)

    code = payload.get("error_code")
    if code in (0, None):
        groups = payload.get("group_id_list") or []
        return VerifyResult(status="valid", message="凭证有效",
                            account_info={"face_groups": len(groups)})
    if code in _TOKEN_BAD_CODES:
        return VerifyResult(status="invalid",
                            message=f"凭证无效：{payload.get('error_msg') or code}")
    if code in _QUOTA_CODES:
        # 配额类错误说明凭据本身有效，只是当下受限——按项目惯例落 valid，把限制写进 message
        return VerifyResult(status="valid", message=f"凭证有效但受限：{payload.get('error_msg') or code}")
    return VerifyResult(status="error",
                        message=f"未预期响应：{code} {payload.get('error_msg') or ''}".strip())


@guarded
def verify(data: dict) -> VerifyResult:
    """验证 API Key / Secret Key 是否有效。保证不抛出异常。"""
    return _verify(data or {})


def actions(data: dict) -> list[dict]:
    """声明可用操作（凭据完整时才提供）。"""
    client_id, client_secret = _creds(data or {})
    if not client_id or not client_secret:
        return []
    return [{"id": "list_groups", "name": "人脸库分组列表", "kind": "list"}]


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行操作：列出人脸库分组（只读）。"""
    if action_id != "list_groups":
        return {"ok": False, "error": "未知操作"}
    data = data or {}
    params = params or {}
    client_id, client_secret = _creds(data)
    base_url = _base(data)
    token, err = _get_access_token(base_url, client_id, client_secret)
    if err:
        return {"ok": False, "error": err}

    url = base_url.rstrip("/") + _GROUP_LIST_PATH
    length = params.get("length") or 100
    try:
        with make_client() as client:
            resp = client.post(
                url,
                params={"access_token": token},
                headers={"Content-Type": "application/json"},
                json={"start": int(params.get("start") or 0), "length": int(length)},
            )
    except httpx.TimeoutException:
        return {"ok": False, "error": f"请求超时（{int(timeout())} 秒）"}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"网络请求失败（{type(exc).__name__}）"}
    payload = safe_json(resp)
    if not isinstance(payload, dict):
        return {"ok": False, "error": f"接口返回非 JSON（HTTP {resp.status_code}）"}
    if payload.get("error_code") not in (0, None):
        return {"ok": False,
                "error": f"{payload.get('error_code')}: {payload.get('error_msg') or ''}".strip()}

    groups = payload.get("group_id_list") or []
    rows = [{"名称": str(g), "类型": "人脸库分组"} for g in groups]
    return {"ok": True, "rows": rows,
            "columns": [{"key": "名称", "label": "分组名"}, {"key": "类型", "label": "类型"}],
            "total": len(rows)}


def get_display_summary(data: dict) -> str:
    """凭据列表中的简短展示文本。"""
    return f"{NAME} | {_base(data or {})}"
