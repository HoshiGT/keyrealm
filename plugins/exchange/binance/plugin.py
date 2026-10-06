"""Binance API 凭证验证插件（plugins/exchange/ 类别下的独立子插件）。

Binance：GET /api/v3/account，HMAC-SHA256 查询串签名鉴权。
签名算法与判定逻辑随插件自带，不依赖 exchange_common。

参考文档：https://developers.binance.com/legacy-docs/binance-spot-api-docs/rest-api/account-endpoints
参考文档：https://developers.binance.com/legacy-docs/binance-spot-api-docs/rest-api/request-security
"""
import hashlib
import hmac
from datetime import datetime, timezone
from typing import Any, Optional

from exchange_common import VerifyResult, body_snippet, guarded, make_client, safe_json

SERVICE_NAME = "Binance"


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
    return errors


def _binance_server_time() -> Optional[int]:
    """取 Binance 服务器时间（毫秒），避免本机时钟偏差触发 -1021；失败返回 None。"""
    try:
        with make_client() as client:
            resp = client.get("https://api.binance.com/api/v3/time")
        payload = safe_json(resp)
        if isinstance(payload, dict) and isinstance(payload.get("serverTime"), int):
            return payload["serverTime"]
    except Exception:  # noqa: BLE001 - 拿不到就回退本机时间
        return None
    return None


@guarded
def verify(data) -> VerifyResult:
    """Binance：GET /api/v3/account，HMAC-SHA256 查询串签名鉴权。"""
    api_key = (data.get("api_key") or "").strip()
    secret_key = (data.get("secret_key") or "").strip()
    if not api_key:
        return VerifyResult(status="invalid", message="API Key 为空")
    if not secret_key:
        return VerifyResult(status="invalid", message="缺少 Secret Key")

    path = "/api/v3/account"
    # 参与签名的查询串必须与实际请求顺序一致；优先用服务器时间规避时钟偏差(-1021)
    timestamp = _binance_server_time() or int(datetime.now(timezone.utc).timestamp() * 1000)
    query_string = f"timestamp={timestamp}&recvWindow=5000"
    signature = hmac.new(
        secret_key.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    url = f"https://api.binance.com{path}?{query_string}&signature={signature}"

    with make_client() as client:
        resp = client.get(url, headers={"X-MBX-APIKEY": api_key})

    status = resp.status_code
    payload = safe_json(resp)
    code = payload.get("code") if isinstance(payload, dict) else None

    if status == 200:
        info: dict[str, Any] = {}
        if isinstance(payload, dict):
            # 仅取非敏感账户能力字段
            for key in ("accountType", "canTrade", "canWithdraw", "canDeposit"):
                if payload.get(key) not in (None, ""):
                    info[key] = payload[key]
        return VerifyResult(status="valid", message="凭证有效", account_info=info or None)

    # 密钥无效 / 未启用现货 / 权限不足 / IP 未白名单（-2015 三者皆可能，附服务端原因）
    if code in (-2015, -1022, -1102):
        detail = payload.get("msg") if isinstance(payload, dict) else ""
        return VerifyResult(status="invalid", message=f"凭证无效 (code {code})" + (f": {detail}" if detail else ""))
    if status in (401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")
    # 451：因地域限制拒绝访问，与凭证本身无关
    if status == 451:
        return VerifyResult(status="error", message="地域限制，无法从当前网络校验 (HTTP 451)")
    if code is not None:
        detail = payload.get("msg") if isinstance(payload, dict) else ""
        return VerifyResult(
            status="error",
            message=f"无法确认凭证状态 (code {code})" + (f": {detail}" if detail else ""),
        )
    return _unconfirmed(status, body_snippet(resp))


# ── 可选操作：查看账户信息 / 交易所时间（只读）──────────

_ACCOUNT_PATH = "/api/v3/account"
_TIME_PATH = "/api/v3/time"

# 余额明细最多展示的币种数，避免输出过长
_MAX_ASSET = 10


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


def _signed_query(secret_key: str) -> str:
    """构造带签名的查询串（时间优先取服务器时间，规避 -1021；签名方式与 verify 一致）。"""
    timestamp = _binance_server_time() or int(datetime.now(timezone.utc).timestamp() * 1000)
    query_string = f"timestamp={timestamp}&recvWindow=5000"
    signature = hmac.new(
        secret_key.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{query_string}&signature={signature}"


def _is_nonzero(value: Any) -> bool:
    """余额字符串是否为非零（'0' / '0.00000000' 视为零）。"""
    try:
        return float(value or 0) != 0
    except (TypeError, ValueError):
        return False


def _account_text(account: dict) -> str:
    """把账户能力字段与非零余额格式化为多行文本（仅只读字段）。"""
    lines = [
        f"账户类型 accountType：{account.get('accountType') or '-'}",
        f"可交易 canTrade：{account.get('canTrade')}",
        f"可提现 canWithdraw：{account.get('canWithdraw')}",
        f"可充值 canDeposit：{account.get('canDeposit')}",
    ]
    balances = account.get("balances")
    rows = [row for row in balances if isinstance(row, dict)] if isinstance(balances, list) else []
    nonzero = [row for row in rows if _is_nonzero(row.get("free")) or _is_nonzero(row.get("locked"))]
    if nonzero:
        lines.append("")
        lines.append(f"非零余额（显示 {min(len(nonzero), _MAX_ASSET)}/{len(nonzero)}）：")
        for row in nonzero[:_MAX_ASSET]:
            lines.append(
                f"{str(row.get('asset') or '-'):<8} 可用 {row.get('free') or '0'}"
                f"  冻结 {row.get('locked') or '0'}"
            )
    return "\n".join(lines)


def actions(data) -> list[dict]:
    """声明可用操作（凭据含 api_key / secret_key 时提供账户信息与交易所时间）。"""
    data = data or {}
    if not ((data.get("api_key") or "").strip() and (data.get("secret_key") or "").strip()):
        return []
    return [
        {
            "id": "account",
            "name": "查看账户信息",
            "kind": "form",
            "submit": "查询",
            "fields": [],
        },
        {
            "id": "server_time",
            "name": "查看交易所时间",
            "kind": "form",
            "submit": "查询",
            "fields": [],
        },
    ]


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行操作：查看账户信息 / 交易所时间（只读）。不抛出异常、不回显密钥、错误不含 URL。"""
    try:
        data = data or {}
        api_key = (data.get("api_key") or "").strip()
        secret_key = (data.get("secret_key") or "").strip()

        if action_id == "server_time":
            # 公开接口，请求本身无需签名；仅在缺凭据时拒绝，与 actions 的暴露条件保持一致
            if not api_key or not secret_key:
                return {"ok": False, "error": "缺少 API Key / Secret Key"}
            with make_client() as client:
                resp = client.get("https://api.binance.com" + _TIME_PATH)
            payload = safe_json(resp)
            if payload is None or payload.get("serverTime") in (None, ""):
                return {"ok": False, "error": f"获取服务器时间失败（HTTP {resp.status_code}）"}
            return {"ok": True, "output": f"服务器时间 serverTime：{_utc_text(payload.get('serverTime'))}"}

        if action_id != "account":
            return {"ok": False, "error": "未知操作"}

        if not api_key or not secret_key:
            return {"ok": False, "error": "缺少 API Key / Secret Key"}

        url = f"https://api.binance.com{_ACCOUNT_PATH}?{_signed_query(secret_key)}"
        with make_client() as client:
            resp = client.get(url, headers={"X-MBX-APIKEY": api_key})

        payload = safe_json(resp)
        if resp.status_code != 200 or payload is None:
            code = payload.get("code") if isinstance(payload, dict) else None
            detail = str(payload.get("msg") or "").strip() if isinstance(payload, dict) else ""
            tag = f"code {code}" if code is not None else f"HTTP {resp.status_code}"
            return {"ok": False, "error": f"查询失败（{tag}）" + (f"：{detail}" if detail else "")}

        return {"ok": True, "output": _account_text(payload)}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底
        return {"ok": False, "error": f"执行异常：{type(exc).__name__}"}


def get_display_summary(data) -> str:
    """凭据列表中的简短展示文本（不回显完整密钥）。"""
    data = data or {}
    api_key = _mask(data.get("api_key") or "")
    if not api_key:
        return SERVICE_NAME
    return f"{SERVICE_NAME} | {api_key}"
