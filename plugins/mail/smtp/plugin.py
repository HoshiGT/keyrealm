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
from typing import Optional

from mail_common import VerifyResult, _as_bool, _as_port, _as_text, guarded

SERVICE_NAME = "通用 SMTP 邮箱"

# 连接与登录超时（秒）
TIMEOUT = 15.0

# SSL 与非 SSL 的默认端口
DEFAULT_SSL_PORT = 465
DEFAULT_STARTTLS_PORT = 587


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
