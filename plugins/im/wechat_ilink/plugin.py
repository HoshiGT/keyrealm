"""微信 iLink Bot 凭据插件（plugins/im/ 类别下的独立子插件）。

联网校验：用官方 Bot 长轮询接口 ``getupdates``，请求体里 ``timeout: 0`` = **立即探测**
（避开默认 35 秒长轮询；凭据无效时约 0.3 秒就返回），全程**只读**、不消费也不发送消息。

可选操作（凭据齐全时可见）：
- ``messages``（list）拉取最近消息：每条消息按 ``item_list`` 拆成一行，列出时间/来自/类型/内容/大小；
- ``download``（form）下载消息里的图片 / 语音 / 文件 / 视频：经 CDN ``/c2c/download`` 取密文，
  用随媒体下发的 AES-128-ECB 密钥本地解密后写盘（``save_to`` 弹「另存为」）。

协议要点（字段名照上游，未做改名）：
- 业务基座 ``https://ilinkai.weixin.qq.com``，媒体 CDN ``https://novac2c.cdn.weixin.qq.com/c2c``；
- 通用头：``AuthorizationType: ilink_bot_token`` + ``Authorization: Bearer <token>`` +
  ``X-WECHAT-UIN``（十进制随机 uint32 的 base64）+ ``iLink-App-Id: bot`` + 客户端版本；
- 业务错误看 ``ret``（或 ``errcode``）：``-14`` = session 过期，需重新扫码登录；
- 媒体密钥 ``CDNMedia.aes_key`` 有两种形态（base64 → 16 字节；或 base64 → 32 字节 hex → 16 字节），
  图片另有 ``image_item.aeskey``（32 位 hex）优先用它。

**绝不回显 token**：任何 message / output / rows 都不出现 ``credential`` 明文。
"""
import base64
import binascii
import os
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from backend.plugin_sdk import (
    VerifyResult,
    body_snippet,
    guarded,
    log,
    make_client,
    safe_json,
    timeout,
)

SERVICE = "wechat_ilink"
NAME = "微信 iLink Bot"
REQUIRED = (("account", "账号"), ("credential", "凭据"))
DISPLAY_FIELD = "account"

BASE = "https://ilinkai.weixin.qq.com"
CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"
GETUPDATES_URL = f"{BASE}/ilink/bot/getupdates"
DOWNLOAD_URL = f"{CDN_BASE}/download"

# 上游要求的客户端标识（照抄官方客户端）
CHANNEL_VERSION = "2.0.0"
CLIENT_VERSION = "131072"      # 2.0.0 → 0x00020000
APP_ID = "bot"

# 业务码：session 过期（需重新扫码登录）
CODE_SESSION_EXPIRED = -14

# item.type → 展示名
_ITEM_KINDS = {1: "文本", 2: "图片", 3: "语音", 4: "文件", 5: "视频"}
# 没有原始文件名时给的默认后缀（图片/视频/语音上游不带文件名，只能按惯例猜；用户可在另存为里改）
_KIND_EXT = {2: ".jpg", 3: ".silk", 5: ".mp4"}


# ── 凭据与请求头 ─────────────────────────────────────────

def _token(data: dict) -> str:
    """把 account + credential 拼成上游要的 ``<botid>@im.bot:<secret>``。"""
    account = str((data or {}).get("account") or "").strip().replace("/", ":")
    secret = str((data or {}).get("credential") or "").strip()
    if not account:
        return ""
    if ":" in account:      # 用户把整串都填在「账号」里了
        return account
    return f"{account}:{secret}" if secret else ""


def _random_wechat_uin() -> str:
    """``X-WECHAT-UIN``：十进制随机 uint32 的 base64。"""
    value = int.from_bytes(os.urandom(4), "big") % 4294967296
    return base64.b64encode(str(value).encode()).decode()


def _headers(token: str, json_body: bool = True) -> dict:
    headers = {
        "iLink-App-Id": APP_ID,
        "iLink-App-ClientVersion": CLIENT_VERSION,
        "X-WECHAT-UIN": _random_wechat_uin(),
    }
    if json_body:
        headers["Content-Type"] = "application/json"
        headers["AuthorizationType"] = "ilink_bot_token"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def validate(data) -> list[str]:
    if not data:
        return ["配置数据为空"]
    errors: list[str] = []
    for key, label in REQUIRED:
        if not str(data.get(key) or "").strip():
            errors.append(f"{label} 不能为空")
    if len(_token(data)) < 20:      # 形如 xxx@im.bot:<32 位 hex>
        errors.append("凭据格式不对：应为 <botid>@im.bot 与 <secret>（或整串填在账号里，用 : 分隔）")
    return errors


# ── 业务码 / 时间格式化 ──────────────────────────────────

def _biz_error(payload: dict) -> tuple[Optional[int], str]:
    """取业务错误码与说明：优先 ``ret``，其次 ``errcode``（两者上游都在用）。"""
    if not isinstance(payload, dict):
        return None, ""
    for key in ("ret", "errcode"):
        if payload.get(key) is not None:
            try:
                return int(payload[key]), str(payload.get("errmsg") or payload.get("err_msg") or "")
            except (TypeError, ValueError):
                continue
    return None, ""


def _fmt_ms(value) -> str:
    """毫秒时间戳 → 本地可读时间；非法值原样返回空串。"""
    try:
        seconds = int(float(value)) / 1000.0
    except (TypeError, ValueError):
        return ""
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return ""


# ── 验证 ─────────────────────────────────────────────────

@guarded
def verify(data) -> VerifyResult:
    """用 ``getupdates`` + ``timeout: 0`` 做一次只读探测，立即返回。"""
    data = data or {}
    missing = validate(data)
    if missing:
        return VerifyResult(status="invalid", message="；".join(missing))

    token = _token(data)
    body = {"get_updates_buf": "", "base_info": {"channel_version": CHANNEL_VERSION}, "timeout": 0}
    try:
        with make_client() as client:
            resp = client.post(GETUPDATES_URL, json=body, headers=_headers(token))
    except httpx.HTTPError as exc:
        return VerifyResult(status="error", message=f"网络请求失败（{type(exc).__name__}）")

    payload = safe_json(resp)
    if resp.status_code != 200:
        if resp.status_code in (401, 403):
            return VerifyResult(
                status="invalid",
                message=f"登录态已失效（HTTP {resp.status_code}，请在微信里重新登录 Bot）",
            )
        return VerifyResult(
            status="error",
            message=f"无法确认凭据状态 (HTTP {resp.status_code}): {body_snippet(resp)}",
        )

    code, detail = _biz_error(payload)
    if code == CODE_SESSION_EXPIRED:
        return VerifyResult(
            status="invalid",
            message="登录态已失效（session 过期，请在微信里重新登录 Bot 后更新凭据）",
        )
    if code not in (None, 0):
        return VerifyResult(status="error", message=f"上游返回错误码 {code}：{detail or '无说明'}")

    msgs = payload.get("msgs") if isinstance(payload, dict) else None
    pending = len(msgs) if isinstance(msgs, list) else 0
    return VerifyResult(
        status="valid",
        message=f"凭据有效（getupdates 探测通过，当前有 {pending} 条待取消息）",
        account_info={"bot": str(data.get("account") or "").strip()},
    )


# ── 拉取消息 ─────────────────────────────────────────────

def _media_of(item: dict) -> tuple[str, str]:
    """取该 item 的 ``(encrypt_query_param, aes_key)``。"""
    kind = item.get("type")
    sub = {2: "image_item", 3: "voice_item", 4: "file_item", 5: "video_item"}.get(kind)
    if not sub:
        return "", ""
    body = item.get(sub) or {}
    media = body.get("media") or {}
    param = str(media.get("encrypt_query_param") or "").strip()
    # 图片优先用 image_item.aeskey；其余用 media.aes_key
    key = ""
    if kind == 2:
        key = str(body.get("aeskey") or "").strip()
    if not key:
        key = str(media.get("aes_key") or "").strip()
    return param, key


def _row_from_item(item: dict, when: str, sender: str, index: int) -> Optional[dict]:
    kind = _ITEM_KINDS.get(item.get("type"))
    if kind is None:
        return None
    row = {"time": when, "from": sender, "kind": kind, "content": "", "size": ""}
    if kind == "文本":
        row["content"] = str((item.get("text_item") or {}).get("text") or "")
        return row

    param, key = _media_of(item)
    if kind == "文件":
        body = item.get("file_item") or {}
        row["content"] = str(body.get("file_name") or "（无文件名）")
        row["size"] = _human_size(body.get("len"))
    elif kind == "图片":
        sub = item.get("image_item") or {}
        row["content"] = "图片"
        row["size"] = _human_size(sub.get("mid_size") or sub.get("hd_size"))
    elif kind == "语音":
        sub = item.get("voice_item") or {}
        row["content"] = str(sub.get("text") or "语音")
        row["size"] = _human_size(sub.get("size"))
    else:  # 视频
        sub = item.get("video_item") or {}
        row["content"] = "视频"
        row["size"] = _human_size(sub.get("video_size"))

    # 可下载的行才带上这三个 key —— 前端对取不到的字段会自动置灰「下载」按钮
    if param:
        row["media_param"] = param
        row["aes_key"] = key
        row["file_name"] = _default_name(item, index)
    return row


def _default_name(item: dict, index: int) -> str:
    """默认保存名：文件用原始文件名，其余按惯例给个后缀（用户可在「另存为」里改）。"""
    if item.get("type") == 4:
        name = str((item.get("file_item") or {}).get("file_name") or "").strip()
        if name:
            return name
    return f"wechat_{_ITEM_KINDS.get(item.get('type'), 'media')}_{index}{_KIND_EXT.get(item.get('type'), '')}"


def _human_size(value) -> str:
    try:
        size = int(value)
    except (TypeError, ValueError):
        return ""
    if size <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return ""


def _rows_from_msgs(msgs) -> list[dict]:
    rows: list[dict] = []
    counter = 0
    for msg in msgs or []:
        if not isinstance(msg, dict):
            continue
        when = _fmt_ms(msg.get("create_time_ms"))
        sender = str(msg.get("from_user_id") or "")
        for item in msg.get("item_list") or []:
            if not isinstance(item, dict):
                continue
            counter += 1
            row = _row_from_item(item, when, sender, counter)
            if row:
                rows.append(row)
    return rows


# ── 下载（CDN 密文 → 本地解密）────────────────────────────

def _resolve_aes_key(raw: str) -> Optional[bytes]:
    """CDN ``aes_key`` 的三种编码形态 → 16 字节密钥；认不出返回 None。

    1) 32 位 hex → 16 字节；2) base64 解出 16 字节；3) base64 解出 32 字节 hex → 16 字节。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    if len(text) == 32:
        try:
            return binascii.unhexlify(text)
        except (binascii.Error, ValueError):
            pass
    try:
        decoded = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32:
        try:
            return binascii.unhexlify(decoded.decode("ascii"))
        except (binascii.Error, ValueError, UnicodeDecodeError):
            return None
    return None


def _unpad(data: bytes) -> bytes:
    """去掉 PKCS7 padding；padding 不合法时原样返回（沿用上游客户端的宽松做法）。"""
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= 16 and len(data) >= pad:
        return data[:-pad]
    return data


def _download_to_file(resp: httpx.Response, key: Optional[bytes], path: str) -> tuple[bool, str]:
    """流式读 CDN 密文、边解密边写盘；原子落盘（先 .part 再 os.replace）。"""
    target = str(path or "").strip()
    if not target:
        return False, "未指定保存路径"
    if resp.status_code != 200:
        return False, f"下载失败（HTTP {resp.status_code}）"

    decryptor = None
    if key:
        decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()

    tmp = target + ".part"
    try:
        tail = b""
        with open(tmp, "wb") as handle:
            for chunk in resp.iter_bytes(65536):
                if decryptor is None:
                    handle.write(chunk)
                    continue
                # ECB 是分组密码：只写「确定不是最后一块」的部分，
                # 把尾巴留到收尾——PKCS7 的 padding 就在最后一块里，必须先扣掉再落盘。
                data = tail + chunk
                usable = len(data) - (len(data) % 16)
                if usable > 16:
                    handle.write(decryptor.update(data[:usable - 16]))
                    data = data[usable - 16:]
                tail = data
            if decryptor is not None:
                handle.write(_unpad(decryptor.update(tail) + decryptor.finalize()))
        os.replace(tmp, target)
    except Exception as exc:  # noqa: BLE001 - 插件边界兜底
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False, f"写入失败：{type(exc).__name__}"
    return True, target


# ── 操作声明 ─────────────────────────────────────────────

_MESSAGES_ACTION = {
    "id": "messages",
    "name": "拉取消息",
    "kind": "list",
    "group": "查看",
    "order": 10,
    "description": "短轮询拉取最近消息（只读，不消费也不回复）",
    "fields": [
        {
            "key": "wait",
            "label": "等待秒数",
            "type": "number",
            "default": 5,
            "hint": "长轮询最多等这么久，期间有新消息就立刻返回。0 = 立即返回当前待取消息。",
        },
    ],
    "columns": [
        {"key": "time", "label": "时间", "width": 170},
        {"key": "from", "label": "来自"},
        {"key": "kind", "label": "类型", "type": "tag", "width": 80},
        {"key": "content", "label": "内容"},
        {"key": "size", "label": "大小", "width": 90},
    ],
    "row_actions": [
        {
            "id": "download",
            "label": "下载",
            "params": {
                "param": "$row.media_param",
                "aes": "$row.aes_key",
                "name": "$row.file_name",
            },
        },
    ],
    "empty_text": "这段时间没有新消息",
}

_DOWNLOAD_ACTION = {
    "id": "download",
    "name": "下载附件",
    "kind": "form",
    "group": "查看",
    "order": 20,
    "submit": "下载",
    "description": "下载消息里的图片 / 语音 / 文件 / 视频（CDN 密文，本地解密后写盘）",
    "fields": [
        {"key": "param", "label": "媒体参数", "type": "text", "required": True,
         "placeholder": "从「拉取消息」某一行点「下载」进入"},
        {"key": "aes", "label": "解密密钥", "type": "text",
         "placeholder": "媒体随附的 aes_key；留空则按未加密写入"},
        {"key": "name", "label": "文件名", "type": "text",
         "placeholder": "仅用于「另存为」的默认名"},
    ],
    "save_to": {"key": "path", "default_name": "$row.file_name"},
}


def actions(data) -> list[dict]:
    """凭据齐全时提供「拉取消息」与「下载附件」。"""
    if validate(data):
        return []
    return [dict(_MESSAGES_ACTION), dict(_DOWNLOAD_ACTION)]


def run_action(action_id: str, params: dict, data: dict) -> dict:
    data = data or {}
    params = params or {}
    token = _token(data)
    if not token:
        return {"ok": False, "error": "缺少账号或凭据"}

    if action_id == "messages":
        return _run_messages(token, params)
    if action_id == "download":
        return _run_download(params)
    return {"ok": False, "error": "未知操作"}


def _run_messages(token: str, params: dict) -> dict:
    try:
        wait = float(str(params.get("wait") or 5).strip())
    except (TypeError, ValueError):
        wait = 5.0
    wait = max(0.0, min(wait, 60.0))

    body = {
        "get_updates_buf": "",
        "base_info": {"channel_version": CHANNEL_VERSION},
        "timeout": int(wait),
    }
    client_kwargs = {"timeout": max(wait + 8.0, timeout())}
    try:
        with make_client() as client:
            resp = client.post(GETUPDATES_URL, json=body, headers=_headers(token), **client_kwargs)
    except httpx.TimeoutException:
        # 长轮询到点没消息是常态，不算失败
        return {"ok": True, "rows": [], "total": 0, "empty_text": f"{int(wait)} 秒内没有新消息"}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"网络请求失败（{type(exc).__name__}）"}

    payload = safe_json(resp)
    if resp.status_code != 200:
        return {"ok": False, "error": f"HTTP {resp.status_code}：{body_snippet(resp)}"}
    code, detail = _biz_error(payload)
    if code == CODE_SESSION_EXPIRED:
        return {"ok": False, "error": "登录态已失效（session 过期），请在微信里重新登录 Bot"}
    if code not in (None, 0):
        return {"ok": False, "error": f"上游返回错误码 {code}：{detail or '无说明'}"}

    rows = _rows_from_msgs(payload.get("msgs") if isinstance(payload, dict) else [])
    return {"ok": True, "rows": rows, "total": len(rows)}


def _run_download(params: dict) -> dict:
    param = str(params.get("param") or "").strip()
    path = str(params.get("path") or "").strip()
    if not param:
        return {"ok": False, "error": "缺少媒体参数（请从「拉取消息」行内点「下载」进入）"}
    if not path:
        return {"ok": False, "error": "未指定保存路径"}

    key = _resolve_aes_key(params.get("aes") or "")
    if params.get("aes") and key is None:
        return {"ok": False, "error": "解密密钥无法识别（已按未加密处理会写出乱码，故中止）"}

    url = f"{DOWNLOAD_URL}?encrypted_query_param={quote(param, safe='')}"
    try:
        # CDN 不需要业务鉴权头，只按 encrypted_query_param 定位资源
        with make_client() as client:
            with client.stream("GET", url, timeout=60.0) as resp:
                ok, message = _download_to_file(resp, key, path)
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"下载失败（{type(exc).__name__}）"}

    if not ok:
        return {"ok": False, "error": message}
    suffix = "（已解密）" if key else "（未加密）"
    return {"ok": True, "output": f"已保存到 {message}{suffix}"}


def get_display_summary(data) -> str:
    data = data or {}
    detail = str(data.get(DISPLAY_FIELD) or "").strip()
    return f"{NAME} | {detail}" if detail else NAME
