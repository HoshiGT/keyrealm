"""Telegram api_id / api_hash / 会话串 验证插件（plugins/im/ 类别下的独立子插件）。

**为什么不用 help.GetConfig**：实测在 MTProto 上，伪造的 api_id/api_hash 与真实的一模一样能
拿到 ``help.GetConfig`` 回包（免登录方法只检查 initConnection 里「有没有」api_id，不校验它是否
注册）。把一整批免登录方法（GetNearestDc / GetAppConfig / GetCountriesList / langpack.* …）逐
个试过，真伪两者的行为完全一致，其中几个抛的 ``AuthKeyUnregisteredError`` 说的是**会话 auth_key**
未登录，与 api_id 无关。所以免登录路径根本区分不了 api_id。

**唯一做服务端校验的是 auth.* 系列**，而它的副作用是「向该号码投递登录验证码」。本插件的取巧：

  用 ``auth.sendCode`` 配一个**杜撰的号码**（``+999…``，不可能投递）。实测服务端的校验顺序是
  **先 api_id/api_hash、后号码**，于是：
    真实 api_id/api_hash → ``PhoneNumberInvalidError``（号码格式非法）⇒ 有效
    伪造 api_id 或 api_hash → ``ApiIdInvalidError``                ⇒ 无效
  号码是杜撰的，因此**不可能真的发出任何验证码**，也不需要用到凭据里的手机号。
  （这也正是交付文档 11 §九 记录的那套探针手法。）

**会话串**只做本地解码：解析出 dc_id / 服务器 / auth_key 指纹，并检查结构是否成立。刻意**不**
联网复用会话——同一个会话从另一个出口登录，有可能触发风控把会话踢掉，把好凭据验废。因此格式
正确时给的是「未验证」而不是「有效」，绝不下假结论。

telethon 是可选依赖（已列入 requirements.txt，与 ``python-socks`` 分列两行）：缺席时联网校验跳过、
只做本地解码与格式检查。``python-socks`` **不能省**：若框架配了代理而它缺席，Telethon 会**静默忽略
代理直连**、脱离代理出口，所以此时一律中止联网校验。（别写 ``telethon[socks]``——telethon 的轮子里
没有这个 extra，那个 extras 装不来 python-socks。）

仅从 im_common 复用通用工具。

参考文档：https://core.telegram.org/api/obtaining_api_id
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import importlib
import ipaddress
import string
import struct
from urllib.parse import unquote, urlsplit

from im_common import VerifyResult, guarded, log, proxy, timeout

NAME = "Telegram API ID / Hash"
DISPLAY_FIELD = "api_id"
HASH_FIELD = "api_hash"
SESSION_FIELD = "session_string"

# 杜撰的号码：格式上像个号码，但绝无可能投递验证码
PROBE_PHONE = "+9990000000001"

# Telethon StringSession 布局（照 telethon/sessions/string.py）：
#   '1' + base64url(struct.pack('>B{4|16}sH256s', dc_id, ip, port, auth_key))
SESSION_VERSION = "1"
_SESSION_FORMATS = {4: ">B4sH256s", 16: ">B16sH256s"}

# Telethon 错误类名 → 判定。只按类名比较（不 import telethon 的错误类型），
# 这样 telethon 缺席时解释结果也用同一套词表。
API_ID_INVALID_ERRORS = {"ApiIdInvalidError"}
# api_id 存在但被官方标记为公开滥用、被限流
API_ID_FLOOD_ERRORS = {"ApiIdPublishedFloodError"}
# 号码被拒 = api_id 那一关已经过了
PHONE_REJECT_PREFIX = "PhoneNumber"

PROXY_SCHEMES = {
    "socks5": "socks5",
    "socks5h": "socks5",
    "socks4": "socks4",
    "socks4a": "socks4",
    "http": "http",
    "https": "http",
}

_STATUS_RANK = {"invalid": 3, "error": 2, "valid": 1, "unknown": 0}


# ── 会话串：本地解码 ─────────────────────────────────────

def decode_session(text) -> tuple[dict | None, str]:
    """解析 Telethon StringSession，返回 ``(信息 dict 或 None, 错误说明)``。

    纯 base64 + struct，不依赖 telethon。两个判定要分开看：**结构不成立**（版本号不对 /
    解码失败 / 长度不对 / auth_key 全 0）是确定的无效；结构成立只是「格式正确」，不代表
    远端还能用，故调用方给的是「未验证」。
    """
    raw = "".join(str(text or "").split())  # 会话串本身不含空白，顺手吃掉粘贴带来的换行
    if not raw:
        return None, "内容为空"
    if raw[0] != SESSION_VERSION:
        return None, f"版本号不是 '{SESSION_VERSION}'（可能是别家客户端或新版的会话串）"
    body = raw[1:]
    try:
        blob = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except (binascii.Error, ValueError):
        return None, "base64 解码失败"
    for struct_format in _SESSION_FORMATS.values():
        if len(blob) != struct.calcsize(struct_format):
            continue
        dc_id, ip_bytes, port, key = struct.unpack(struct_format, blob)
        if not any(key):
            return None, "auth_key 全为 0（空会话），不是可用的会话串"
        return {
            "dc_id": dc_id,
            "server": ipaddress.ip_address(ip_bytes).compressed,
            "port": port,
            "auth_key_bytes": len(key),
            "auth_key_fingerprint": hashlib.sha256(key).hexdigest()[:16],
        }, ""
    sizes = " / ".join(str(struct.calcsize(fmt)) for fmt in _SESSION_FORMATS.values())
    return None, f"长度不匹配（解出 {len(blob)} 字节，IPv4 / IPv6 布局应分别为 {sizes} 字节）"


def _inspect_session(text: str) -> VerifyResult:
    info, error = decode_session(text)
    if error:
        return VerifyResult(status="invalid", message=f"会话串无法解析：{error}")
    return VerifyResult(
        status="unknown",
        message="会话串格式正确（dc_id / 服务器 / auth_key 指纹已解析），但未联网复用——异地登录可能触发风控把会话踢掉",
        account_info={"session": info},
    )


# ── 联网校验（api_id / api_hash）─────────────────────────

def _load_telethon():
    """可选依赖：装上就用，没装就返回 None（不抛）。"""
    try:
        return importlib.import_module("telethon")
    except ImportError:
        return None


def _python_socks_available() -> bool:
    try:
        importlib.import_module("python_socks")
        return True
    except ImportError:
        return False


def _telethon_proxy() -> tuple[tuple | None, str]:
    """把框架给的下游代理 URL 转成 Telethon / python-socks 的 ``(类型, 主机, 端口, rdns, 用户, 口令)``。

    返回 ``(proxy 参数或 None, 错误说明)``，二者必有一个为空。``None`` 参数表示直连
    （框架没配代理）。
    """
    raw = proxy()
    if not raw:
        return None, ""
    parts = urlsplit(raw)
    scheme = (parts.scheme or "").lower()
    kind = PROXY_SCHEMES.get(scheme)
    if not kind:
        return None, f"不支持的代理类型 {scheme or '(空)'}"
    if not parts.hostname or not parts.port:
        return None, "代理地址不完整"
    return (
        kind,
        parts.hostname,
        parts.port,
        scheme.endswith("h"),  # socks5h/ socks4a 交给代理侧解析域名
        unquote(parts.username or "") or None,
        unquote(parts.password or "") or None,
    ), ""


def _send_code_request(telethon, api_id: int, api_hash: str):
    """构造 SendCode 请求；``settings`` 参数依 telethon 版本有无，缺了就去掉。"""
    try:
        settings = telethon.types.CodeSettings()
        return telethon.functions.auth.SendCodeRequest(
            phone_number=PROBE_PHONE, api_id=api_id, api_hash=api_hash, settings=settings
        )
    except (AttributeError, TypeError):
        return telethon.functions.auth.SendCodeRequest(
            phone_number=PROBE_PHONE, api_id=api_id, api_hash=api_hash
        )


def _pair_verdict(exc: Exception, api_id: int) -> VerifyResult:
    """把探针异常翻成判定。只露异常**类型名**——Telethon 的异常文本可能带上请求内容。"""
    name = type(exc).__name__
    info = {"api_id": api_id, "probe": "auth.sendCode（杜撰号码，未发送验证码）"}
    if name in API_ID_INVALID_ERRORS:
        return VerifyResult(
            status="invalid", message=f"api_id / api_hash 组合无效（{name}）", account_info=info
        )
    if name.startswith(PHONE_REJECT_PREFIX):
        return VerifyResult(
            status="valid",
            message=f"api_id / api_hash 有效（探针号码被拒：{name}；未发送任何验证码）",
            account_info=info,
        )
    if name in API_ID_FLOOD_ERRORS:
        return VerifyResult(
            status="valid",
            message=f"api_id 确实存在，但已被官方标记为公开滥用并限流（{name}），可能已无法正常使用",
            account_info=info,
        )
    if name == "FloodWaitError":
        return VerifyResult(
            status="error",
            message=f"Telegram 限流，需等待 {getattr(exc, 'seconds', '?')} 秒后重试（与凭据本身无关）",
        )
    if isinstance(exc, TimeoutError) or name == "TimeoutError":
        return VerifyResult(status="error", message=f"连接 Telegram 超时（{int(timeout())} 秒）")
    return VerifyResult(status="error", message=f"无法确认 api_id / api_hash 状态（{name}）")


def _probe_pair(api_id: int, api_hash: str) -> VerifyResult:
    telethon = _load_telethon()
    if telethon is None:
        return VerifyResult(
            status="unknown",
            message="未安装 telethon，跳过联网校验（api_id / api_hash 格式正确）",
        )
    proxy_kwarg, proxy_error = _telethon_proxy()
    if proxy_error:
        return VerifyResult(status="unknown", message=f"已跳过联网校验：{proxy_error}")
    if proxy_kwarg is not None and not _python_socks_available():
        return VerifyResult(
            status="unknown",
            message="已配置代理但缺少 python-socks，Telethon 会静默忽略代理直连；"
                    "为避免脱离代理出口已跳过联网校验（安装 python-socks 后重试）",
        )

    client = telethon.TelegramClient(
        telethon.sessions.StringSession(),
        api_id,
        api_hash,
        proxy=proxy_kwarg,
        connection_retries=1,
        request_retries=1,
        timeout=timeout(),
        flood_sleep_threshold=0,  # 触到限流要立刻报出来，别默默睡过去
    )
    loop = client.loop
    try:
        log(f"校验 Telegram api_id {api_id}（未登录探针，号码为杜撰值）")
        loop.run_until_complete(client.connect())
        loop.run_until_complete(client(_send_code_request(telethon, api_id, api_hash)))
    except Exception as exc:  # noqa: BLE001 - 判定要看异常类型，必须全接
        return _pair_verdict(exc, api_id)
    finally:
        try:
            client.disconnect()
        except Exception as exc:  # noqa: BLE001 - 断开失败不影响判定
            log(f"Telegram 断开连接失败: {type(exc).__name__}")
    # 杜撰号码不可能真的收到码，走到这里说明 api_id/api_hash 确实注册且可用
    return VerifyResult(
        status="valid",
        message="api_id / api_hash 有效",
        account_info={"api_id": api_id, "probe": "auth.sendCode"},
    )


# ── 组合 ────────────────────────────────────────────────

def _combine(parts: list[VerifyResult]) -> VerifyResult:
    """多部分（api_id/api_hash 与 会话串）各自判定后合成一条：状态取最「重」的那个。"""
    if len(parts) == 1:
        return parts[0]
    worst = max(parts, key=lambda item: _STATUS_RANK.get(item.status, 0))
    info: dict = {}
    for item in parts:
        if item.account_info:
            info.update(item.account_info)
    message = "；".join(item.message for item in parts if item.message)
    return VerifyResult(status=worst.status, message=message, account_info=info or None)


def _verify_pair(api_id_raw: str, api_hash: str) -> VerifyResult:
    if not api_id_raw or not api_hash:
        return VerifyResult(status="error", message="api_id 与 api_hash 必须成对提供")
    if not api_id_raw.isdigit() or int(api_id_raw) <= 0:
        return VerifyResult(status="invalid", message="api_id 应为正整数")
    if len(api_hash) != 32 or any(char not in string.hexdigits for char in api_hash):
        return VerifyResult(status="invalid", message="api_hash 应为 32 位十六进制字符串")
    return _probe_pair(int(api_id_raw), api_hash)


# ── 插件接口 ────────────────────────────────────────────

def validate(data) -> list[str]:
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]
    api_id = str(data.get(DISPLAY_FIELD) or "").strip()
    api_hash = str(data.get(HASH_FIELD) or "").strip()
    session = str(data.get(SESSION_FIELD) or "").strip()
    errors: list[str] = []
    if api_id and not api_hash:
        errors.append("已填 api_id 但缺少 api_hash")
    if api_hash and not api_id:
        errors.append("已填 api_hash 但缺少 api_id")
    if api_id and not api_id.isdigit():
        errors.append("api_id 应为纯数字")
    if api_hash and (len(api_hash) != 32 or any(char not in string.hexdigits for char in api_hash)):
        errors.append("api_hash 应为 32 位十六进制字符串")
    if not (api_id and api_hash) and not session:
        errors.append("至少需要 api_id/api_hash，或一段会话串")
    return errors


@guarded
def verify(data) -> VerifyResult:
    data = data or {}
    api_id_raw = str(data.get(DISPLAY_FIELD) or "").strip()
    api_hash = str(data.get(HASH_FIELD) or "").strip()
    session_text = str(data.get(SESSION_FIELD) or "").strip()

    parts: list[VerifyResult] = []
    if api_id_raw or api_hash:
        parts.append(_verify_pair(api_id_raw, api_hash))
    if session_text:
        parts.append(_inspect_session(session_text))
    if not parts:
        return VerifyResult(status="invalid", message="至少需要 api_id/api_hash，或一段会话串")
    return _combine(parts)


def get_display_summary(data) -> str:
    """列表展示用 api_id（非敏感）；api_hash 与手机号绝不回显。"""
    api_id = str((data or {}).get(DISPLAY_FIELD) or "").strip()
    return f"{NAME} | api_id {api_id}" if api_id else NAME
