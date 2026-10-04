"""通用 SMTP 邮箱验证插件（plugins/mail/ 类别下的独立子插件）。

本插件自带 SMTP 连接与登录的**全部实现**；mail_common 只提供通用工具。
新增邮箱类插件时照抄本目录（含 _safe_quit / 建连分类 / 登录判定）即可，
无需改动 mail_common.py。

常见服务商参数（均需使用「授权码 / 应用专用密码」，而非网页登录密码）：
  - Gmail    : smtp.gmail.com   465 SSL
  - QQ 邮箱   : smtp.qq.com      465 SSL
  - 网易 163 : smtp.163.com     465 SSL
  - 网易 126 : smtp.126.com     465 SSL
非 SSL 端口通常为 587（STARTTLS），25 端口为明文（不推荐）。

验证方式：按 ssl 开关选择加密通道后调用 login()。
  登录成功            → valid
  认证失败(授权码错)  → invalid
  连接/超时/其它异常  → error
"""
from __future__ import annotations

import smtplib
import socket
import ssl
from typing import Any, Optional

from mail_common import VerifyResult, guarded

SERVICE_NAME = "通用 SMTP 邮箱"

# 连接与登录超时（秒）
TIMEOUT = 15.0

# SSL 与非 SSL 的默认端口
DEFAULT_SSL_PORT = 465
DEFAULT_STARTTLS_PORT = 587


# ── 取值辅助（兼容前端 checkbox→bool、number→int、text→str）──

def _as_text(value: Any) -> str:
    """统一转字符串并去除首尾空白（避免密码/账号带入多余空格）。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else ""
    return str(value).strip()


def _as_bool(value: Any, default: bool = False) -> bool:
    """checkbox 可能为 bool / 数字 / 字符串，逐一兼容。"""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def _as_port(value: Any, default: int) -> int:
    """端口兼容 int / 数字字符串；非法或越界时回落默认值。"""
    if value is None or value == "":
        return default
    try:
        port = int(value)
    except (TypeError, ValueError):
        return default
    return port if 1 <= port <= 65535 else default


def _safe_quit(server: Optional[smtplib.SMTP]) -> None:
    """尽力关闭连接：先 quit() 正常登出，失败再 close() 兜底，绝不抛异常。"""
    if server is None:
        return
    try:
        server.quit()
        return
    except Exception:  # noqa: BLE001 - 关闭阶段无需隔离具体异常
        pass
    try:
        server.close()
    except Exception:  # noqa: BLE001
        pass


def _verify_smtp(data: dict) -> VerifyResult:
    """通用 SMTP 邮箱验证：连接 → （可选 STARTTLS）→ login()。"""
    host = _as_text(data.get("host"))
    username = _as_text(data.get("username"))
    password = _as_text(data.get("password"))
    use_ssl = _as_bool(data.get("ssl"), True)
    port = _as_port(data.get("port"), DEFAULT_SSL_PORT if use_ssl else DEFAULT_STARTTLS_PORT)

    if not host:
        return VerifyResult(status="invalid", message="SMTP 服务器地址为空")
    if not username:
        return VerifyResult(status="invalid", message="邮箱账号为空")
    if not password:
        return VerifyResult(status="invalid", message="授权码 / 应用专用密码为空")

    server: Optional[smtplib.SMTP] = None
    try:
        # 建连阶段：区分 SMTP 协议错误、SSL 错误、网络/超时错误
        try:
            if use_ssl:
                server = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT)
            else:
                server = smtplib.SMTP(host, port, timeout=TIMEOUT)
                server.ehlo()
                server.starttls()
                server.ehlo()
        except TimeoutError:
            return VerifyResult(status="error", message=f"连接超时（{int(TIMEOUT)} 秒）")
        except smtplib.SMTPConnectError as exc:
            return VerifyResult(status="error", message=f"无法连接 SMTP 服务器: {exc}")
        except smtplib.SMTPException as exc:
            return VerifyResult(status="error", message=f"SMTP 连接失败: {exc}")
        except ssl.SSLError as exc:
            return VerifyResult(status="error", message=f"SSL/TLS 握手失败: {exc}")
        except socket.gaierror as exc:
            return VerifyResult(status="error", message=f"无法解析服务器地址: {exc}")
        except OSError as exc:
            return VerifyResult(status="error", message=f"网络连接失败: {exc}")

        # 认证阶段：认证失败明确判 invalid，其余 SMTP 错误判 error
        try:
            server.login(username, password)
        except smtplib.SMTPAuthenticationError:
            return VerifyResult(status="invalid", message="认证失败：账号或授权码错误")
        except smtplib.SMTPException as exc:
            return VerifyResult(status="error", message=f"SMTP 认证异常: {exc}")

        return VerifyResult(
            status="valid",
            message="凭证有效：SMTP 登录成功",
            account_info={"server": host, "port": port},
        )
    finally:
        _safe_quit(server)


def validate(data: dict) -> list[str]:
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]

    errors: list[str] = []

    if not _as_text(data.get("host")):
        errors.append("SMTP 服务器地址不能为空")

    port_raw = data.get("port")
    if port_raw is None or port_raw == "":
        errors.append("端口不能为空")
    else:
        try:
            port = int(port_raw)
            if not 1 <= port <= 65535:
                errors.append("端口需在 1-65535 之间")
        except (TypeError, ValueError):
            errors.append("端口必须是数字")

    if not _as_text(data.get("username")):
        errors.append("邮箱账号不能为空")
    if not _as_text(data.get("password")):
        errors.append("授权码 / 应用专用密码不能为空")

    return errors


@guarded
def verify(data: dict) -> VerifyResult:
    """验证 SMTP 凭据是否有效。保证不抛出异常（guarded 兜底）。"""
    return _verify_smtp(data or {})


# ── 可选操作：查看收件箱（IMAP，只读）────────────────────

def _imap_host(smtp_host: str) -> str:
    """由 SMTP 主机推导 IMAP 主机：smtp.X → imap.X，否则原样返回。"""
    host = (smtp_host or "").strip()
    return "imap." + host[len("smtp."):] if host.startswith("smtp.") else host


def _decode_header(value: Optional[str]) -> str:
    from email.header import decode_header, make_header

    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001 - 头部编码异常时按原文返回
        return str(value)


def actions(data: dict) -> list[dict]:
    """声明可用操作（凭据含 host/username/password 时才提供收件箱查看）。"""
    data = data or {}
    if not (
        _as_text(data.get("host"))
        and _as_text(data.get("username"))
        and _as_text(data.get("password"))
    ):
        return []
    return [{
        "id": "inbox",
        "name": "查看收件箱",
        "kind": "form",
        "submit": "拉取",
        "fields": [
            {"key": "count", "label": "条数", "type": "text", "default": "10"},
            {"key": "folder", "label": "文件夹", "type": "text", "default": "INBOX"},
            {
                "key": "imap_host",
                "label": "IMAP 主机",
                "type": "text",
                "default": "",
                "placeholder": "留空则按 smtp.* → imap.* 推导",
            },
        ],
    }]


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行操作：经 IMAP 列出最近若干封邮件（只读，不改动邮箱）。"""
    if action_id != "inbox":
        return {"ok": False, "error": "未知操作"}

    import imaplib
    from email.parser import BytesParser

    data = data or {}
    params = params or {}
    host = _as_text(params.get("imap_host")) or _imap_host(_as_text(data.get("host")))
    if not host:
        return {"ok": False, "error": "无法确定 IMAP 主机，请填写"}
    username = _as_text(data.get("username"))
    password = _as_text(data.get("password"))
    try:
        count = max(1, min(int(_as_text(params.get("count")) or "10"), 50))
    except ValueError:
        count = 10
    folder = _as_text(params.get("folder")) or "INBOX"

    try:
        conn = imaplib.IMAP4_SSL(host, 993, timeout=TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"连接 IMAP 失败：{type(exc).__name__}"}
    try:
        try:
            conn.login(username, password)
        except imaplib.IMAP4.error as exc:
            return {"ok": False, "error": f"登录失败：{exc}"}
        conn.select(folder, readonly=True)
        typ, found = conn.search(None, "ALL")
        if typ != "OK" or not found:
            return {"ok": False, "error": "搜索邮件失败或无权限"}
        ids = found[0].split()
        recent = ids[-count:][::-1]
        lines = [f"共 {len(ids)} 封，显示最近 {len(recent)} 封（{folder}）：", ""]
        for index, uid in enumerate(recent, 1):
            typ, fetched = conn.fetch(uid, "(BODY.PEEK[HEADER])")
            if typ != "OK" or not fetched or not fetched[0]:
                continue
            msg = BytesParser().parsebytes(fetched[0][1])
            lines.append(f"{index}. [{_decode_header(msg.get('Date'))}] {_decode_header(msg.get('From'))}")
            lines.append(f"   主题：{_decode_header(msg.get('Subject'))}")
        return {"ok": True, "output": "\n".join(lines)}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底
        return {"ok": False, "error": f"读取邮件失败：{type(exc).__name__}"}
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def get_display_summary(data: dict) -> str:
    """凭据列表中的简短展示文本（绝不回显密码）。"""
    data = data or {}
    host = _as_text(data.get("host"))
    username = _as_text(data.get("username"))
    use_ssl = _as_bool(data.get("ssl"), True)
    port = _as_port(data.get("port"), DEFAULT_SSL_PORT if use_ssl else DEFAULT_STARTTLS_PORT)
    target = f"{host}:{port}" if host else "-"
    # 账号通常已是完整邮箱地址，避免再拼 "@" 造成 "a@qq.com@smtp.qq.com" 的歧义
    return f"{SERVICE_NAME} | {username} → {target}" if username else f"{SERVICE_NAME} | {target}"
