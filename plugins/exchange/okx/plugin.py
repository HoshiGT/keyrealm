"""OKX API 凭证验证插件（plugins/exchange/ 类别下的独立子插件）。

OKX：GET /api/v5/account/balance，HMAC-SHA256（base64）签名鉴权，
需 Passphrase。签名算法与判定逻辑随插件自带，不依赖 exchange_common。

参考文档：https://www.okx.com/docs-v5/en/
参考文档：https://www.okx.com/docs-v5/en/#overview-rest-authentication-signature
"""
import base64
import hashlib
import hmac
from datetime import datetime, timezone
from typing import Any

from exchange_common import VerifyResult, body_snippet, guarded, make_client, safe_json

SERVICE_NAME = "OKX"


def _mask(value: str) -> str:
    """仅展示密钥前缀，避免在列表/日志中回显完整凭据。"""
    value = (value or "").strip()
    if len(value) <= 8:
        return "***" if value else ""
    return f"{value[:4]}***{value[-4:]}"


def _unconfirmed(status: int, snippet: str) -> VerifyResult:
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def validate(data) -> list[str]:
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]
    errors: list[str] = []
    if not (data.get("api_key") or "").strip():
        errors.append("API Key 不能为空")
    if not (data.get("secret_key") or "").strip():
        errors.append("Secret Key 不能为空")
    if not (data.get("passphrase") or "").strip():
        errors.append("Passphrase 不能为空")
    return errors


@guarded
def verify(data) -> VerifyResult:
    """OKX：GET /api/v5/account/balance，HMAC-SHA256 签名鉴权。"""
    api_key = (data.get("api_key") or "").strip()
    secret_key = (data.get("secret_key") or "").strip()
    passphrase = (data.get("passphrase") or "").strip()
    if not api_key:
        return VerifyResult(status="invalid", message="API Key 为空")
    if not secret_key or not passphrase:
        return VerifyResult(status="invalid", message="缺少 Secret Key 或 Passphrase")

    path = "/api/v5/account/balance"
    request_path = path  # 无 query 参数，prehash 直接用路径
    # OKX 要求 ISO8601 毫秒精度、UTC、以 Z 结尾
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    # prehash = timestamp + method + requestPath + body（GET 无 body）
    prehash = timestamp + "GET" + request_path
    signature = base64.b64encode(
        hmac.new(secret_key.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")

    headers = {
        "OK-ACCESS-KEY": api_key,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": passphrase,
    }
    url = "https://www.okx.com" + path
    with make_client() as client:
        resp = client.get(url, headers=headers)

    payload = safe_json(resp)
    code = str(payload.get("code", "")) if isinstance(payload, dict) else ""

    if code == "0":
        info: dict[str, Any] = {}
        rows = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            # 只取账户级汇总，不回显持仓/明细
            for key in ("totalEq", "adjEq", "isoEq", "uTime"):
                if rows[0].get(key) not in (None, ""):
                    info[key] = rows[0][key]
        return VerifyResult(status="valid", message="凭证有效", account_info=info or None)

    # OKX 鉴权类错误码：密钥/口令/签名/权限不正确
    if code in ("50111", "50112", "50113", "50114"):
        detail = payload.get("msg") or payload.get("data") or ""
        message = f"凭证无效 (code {code})"
        if detail:
            message = f"{message}: {detail}"
        return VerifyResult(status="invalid", message=message)

    if resp.status_code in (401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {resp.status_code})")

    if code:
        detail = payload.get("msg") if isinstance(payload, dict) else ""
        return VerifyResult(
            status="error",
            message=f"无法确认凭证状态 (code {code})" + (f": {detail}" if detail else ""),
        )
    return _unconfirmed(resp.status_code, body_snippet(resp))


# ── 可选操作：查看账户余额（只读）────────────────────

_BALANCE_PATH = "/api/v5/account/balance"

# 明细最多展示的币种数，避免输出过长
_MAX_CCY = 10


def _utc_text(ms_value: Any) -> str:
    """毫秒时间戳 → UTC 可读时间；无法解析时原样返回。"""
    try:
        milliseconds = int(ms_value)
    except (TypeError, ValueError):
        return "-" if ms_value in (None, "") else str(ms_value)
    try:
        moment = datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return str(ms_value)
    return moment.strftime("%Y-%m-%d %H:%M:%S UTC")


def _balance_headers(api_key: str, secret_key: str, passphrase: str) -> dict:
    """构造余额查询的鉴权头（签名算法与 verify 一致：GET 无 body）。"""
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    prehash = timestamp + "GET" + _BALANCE_PATH
    signature = base64.b64encode(
        hmac.new(secret_key.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")
    return {
        "OK-ACCESS-KEY": api_key,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": passphrase,
    }


def _balance_text(account: dict) -> str:
    """把账户汇总与币种明细格式化为多行文本（仅只读字段）。"""
    lines = [
        f"总权益 totalEq：{account.get('totalEq') or '-'}",
        f"隔离权益 isoEq：{account.get('isoEq') or '-'}",
        f"更新时间 uTime：{_utc_text(account.get('uTime'))}",
    ]
    details = account.get("details")
    rows = [row for row in details if isinstance(row, dict)] if isinstance(details, list) else []
    if rows:
        lines.append("")
        lines.append(f"币种余额（显示 {min(len(rows), _MAX_CCY)}/{len(rows)}）：")
        for row in rows[:_MAX_CCY]:
            lines.append(
                f"{str(row.get('ccy') or '-'):<8} 可用 {row.get('availBal') or '-'}"
                f"  权益 {row.get('eq') or '-'}"
            )
    return "\n".join(lines)


def actions(data) -> list[dict]:
    """声明可用操作（凭据含 api_key / secret_key / passphrase 时提供余额查询）。"""
    data = data or {}
    if not (
        (data.get("api_key") or "").strip()
        and (data.get("secret_key") or "").strip()
        and (data.get("passphrase") or "").strip()
    ):
        return []
    return [{
        "id": "balance",
        "name": "查看账户余额",
        "kind": "form",
        "submit": "查询",
        "fields": [],
    }]


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行操作：查询账户余额（只读）。不抛出异常、不回显密钥、错误不含 URL。"""
    try:
        if action_id != "balance":
            return {"ok": False, "error": "未知操作"}

        data = data or {}
        api_key = (data.get("api_key") or "").strip()
        secret_key = (data.get("secret_key") or "").strip()
        passphrase = (data.get("passphrase") or "").strip()
        if not api_key or not secret_key or not passphrase:
            return {"ok": False, "error": "缺少 API Key / Secret Key / Passphrase"}

        headers = _balance_headers(api_key, secret_key, passphrase)
        with make_client() as client:
            resp = client.get("https://www.okx.com" + _BALANCE_PATH, headers=headers)

        payload = safe_json(resp)
        if payload is None:
            return {"ok": False, "error": f"响应非 JSON（HTTP {resp.status_code}）"}
        code = str(payload.get("code", "")).strip()
        if code != "0":
            detail = str(payload.get("msg") or "").strip()
            tag = f"code {code}" if code else f"HTTP {resp.status_code}"
            return {"ok": False, "error": f"查询失败（{tag}）" + (f"：{detail}" if detail else "")}

        rows = payload.get("data")
        account = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
        return {"ok": True, "output": _balance_text(account)}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底
        return {"ok": False, "error": f"执行异常：{type(exc).__name__}"}


def get_display_summary(data) -> str:
    """凭据列表中的简短展示文本（不回显完整密钥）。"""
    data = data or {}
    api_key = _mask(data.get("api_key") or "")
    if not api_key:
        return SERVICE_NAME
    return f"{SERVICE_NAME} | {api_key}"
