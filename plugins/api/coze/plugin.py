"""字节扣子 Coze API Key 验证插件（plugins/api/ 类别下的独立子插件）。

自带完整的校验与展示逻辑，只复用 api_common 的通用工具；
复制本目录即可派生出新的服务插件。
"""
from typing import Any

from api_common import VerifyResult, body_snippet, first_list, guarded, make_client, safe_json

NAME = "字节扣子 Coze"
DEFAULT_BASE_URL = "https://api.coze.cn"


def _parse_workspaces(payload: dict) -> tuple:
    """从 ``/v1/workspaces`` 的响应里取出「工作区列表」与「总数」。

    **verify 与「操作」共用这一个解析器**：原先两处各写一份，verify 只认 ``total_count``、
    操作只认 ``workspace_list`` / ``items``，字段名一旦对不上就会出现
    「凭据列表说 1 个工作区、点了操作却说无工作区」这种自相矛盾。共用之后不可能再分叉。
    """
    if not isinstance(payload, dict):
        return [], None
    data = payload.get("data")
    if isinstance(data, list):
        return data, len(data)
    if not isinstance(data, dict):
        return [], None
    items = first_list(data, "workspace_list", "items", "list", "workspaces")
    total = data.get("total_count")
    try:
        total = int(total) if total is not None else None
    except (TypeError, ValueError):
        total = None
    return items, total


def _unconfirmed(status: int, snippet: str) -> VerifyResult:
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def _verify_coze(base_url: str, api_key: str) -> VerifyResult:
    url = base_url.rstrip("/") + "/v1/workspaces"
    with make_client() as client:
        resp = client.get(url, headers={"Authorization": f"Bearer {api_key}"})
    status = resp.status_code
    payload = safe_json(resp)
    if status == 200:
        info: dict[str, Any] = {}
        items, total = _parse_workspaces(payload)
        count = total if total is not None else len(items)
        if count:
            info["workspace_count"] = count
        return VerifyResult(status="valid", message="凭证有效", account_info=info or None)
    if status in (401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")
    return _unconfirmed(status, body_snippet(resp))


def validate(data):
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]

    errors: list[str] = []

    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        errors.append("API Key 不能为空")

    base_url = (data.get("base_url") or "").strip()
    if not DEFAULT_BASE_URL and not base_url:
        errors.append(f"服务「{NAME}」需要填写 Base URL")

    return errors


@guarded
def verify(data):
    """验证字节扣子 Coze API Key 是否有效。"""
    data = data or {}

    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        return VerifyResult(status="invalid", message="API Key 为空")

    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    if not base_url:
        return VerifyResult(status="error", message=f"服务「{NAME}」需要填写 Base URL")

    return _verify_coze(base_url, api_key)


# ── 可选操作：查看工作区 ────────────────────────────────

def actions(data):
    """声明可用操作（凭据含 api_key 时提供工作区查看）。"""
    data = data or {}
    if not (data.get("api_key") or "").strip():
        return []
    return [{
        "id": "workspaces",
        "name": "查看工作区",
        "kind": "form",
        "submit": "查询",
        "fields": [],
    }]


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行操作：列出 Coze 名下工作区（只读，不回显 API Key）。"""
    data = data or {}
    try:
        if action_id != "workspaces":
            return {"ok": False, "error": "未知操作"}

        api_key = (data.get("api_key") or "").strip()
        if not api_key:
            return {"ok": False, "error": "缺少 API Key"}
        base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL

        with make_client() as client:
            resp = client.get(
                base_url.rstrip("/") + "/v1/workspaces",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if resp.status_code != 200:
            return {"ok": False, "error": f"查询失败（HTTP {resp.status_code}）"}

        payload = safe_json(resp) or {}
        items, total = _parse_workspaces(payload)
        count = total if total is not None else len(items)
        if not items:
            if count:
                # 有总数却没有明细：如实说明，别让「字段名变了」看起来像「你没有工作区」
                return {"ok": True, "output": f"共 {count} 个工作区，但接口未返回明细（响应字段可能已变更）"}
            return {"ok": True, "output": "共 0 个工作区"}
        lines = [f"共 {count} 个工作区：", ""]
        for index, item in enumerate(items, 1):
            if not isinstance(item, dict):
                continue
            lines.append(f"{index}. {item.get('name', '')} (id={item.get('id', '')})")
        return {"ok": True, "output": "\n".join(lines)}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底
        return {"ok": False, "error": f"执行异常：{type(exc).__name__}"}


def get_display_summary(data):
    """凭据列表中的简短展示文本。"""
    data = data or {}
    base_url = data.get("base_url") or DEFAULT_BASE_URL
    return f"{NAME} | {base_url}"
