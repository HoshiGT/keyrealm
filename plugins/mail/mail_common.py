"""邮箱类别共享模块：SMTP 邮箱凭据验证处理器。

plugins/mail/ 下的每个子插件（smtp……）都是独立插件，通过
`from mail_common import ...` 复用本模块；删除任一子插件目录
不影响其他子插件运行。

本模块仅使用标准库 smtplib / ssl 完成 SMTP 登录验证，不依赖 httpx，
也完全不导入 backend 包，可脱离主程序单独运行。

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
import sys
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class VerifyResult:
    """插件验证结果（独立定义，不依赖 backend）"""

    status: str  # valid / invalid / error
    message: str = ""
    account_info: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "message": self.message,
            "account_info": self.account_info,
        }


# 连接与登录超时（秒）
TIMEOUT = 15.0

# SSL 与非 SSL 的默认端口
DEFAULT_SSL_PORT = 465
DEFAULT_STARTTLS_PORT = 587


SERVICE_CONFIGS = {
    "smtp": {
        "name": "通用 SMTP 邮箱",
        "default_port": DEFAULT_SSL_PORT,
    },
}


def _log(message: str) -> None:
    print(f"[mail] {message}", file=sys.stderr)


# ── 取值辅助（兼容前端 checkbox→bool、number→int、text→str） ──

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


# ── 各服务验证 ───────────────────────────────────────────

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


_SERVICE_VERIFIERS = {
    "smtp": _verify_smtp,
}


# ── 子插件入口（service 由子插件显式传入） ───────────────

def validate_common(service: str, data: dict) -> list[str]:
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]

    config = SERVICE_CONFIGS.get(service)
    if config is None:
        return [f"不支持的服务: {service}"]

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


def verify_service(service: str, data: dict) -> VerifyResult:
    """验证指定服务的 SMTP 凭据是否有效。保证不抛出异常。"""
    try:
        data = data or {}
        if SERVICE_CONFIGS.get(service) is None:
            return VerifyResult(status="error", message=f"不支持的服务: {service}")

        verifier = _SERVICE_VERIFIERS.get(service)
        if verifier is None:
            return VerifyResult(status="error", message=f"未知服务: {service}")
        return verifier(data)

    except TimeoutError:
        return VerifyResult(status="error", message=f"连接超时（{int(TIMEOUT)} 秒）")
    except socket.gaierror as exc:
        return VerifyResult(status="error", message=f"无法解析服务器地址: {exc}")
    except ssl.SSLError as exc:
        return VerifyResult(status="error", message=f"SSL/TLS 握手失败: {exc}")
    except smtplib.SMTPException as exc:
        return VerifyResult(status="error", message=f"SMTP 异常: {exc}")
    except OSError as exc:
        return VerifyResult(status="error", message=f"网络连接失败: {exc}")
    except Exception as exc:  # noqa: BLE001 - 插件边界，必须兜底
        _log(f"verify 未预期异常: {type(exc).__name__}")
        return VerifyResult(status="error", message=f"插件执行异常: {type(exc).__name__}")


def display_summary(service: str, data: dict) -> str:
    """凭据列表中的简短展示文本（绝不回显密码）。"""
    data = data or {}
    name = SERVICE_CONFIGS.get(service, {}).get("name", service)
    host = _as_text(data.get("host"))
    username = _as_text(data.get("username"))
    use_ssl = _as_bool(data.get("ssl"), True)
    port = _as_port(data.get("port"), DEFAULT_SSL_PORT if use_ssl else DEFAULT_STARTTLS_PORT)
    target = f"{host}:{port}" if host else "-"
    # 账号通常已是完整邮箱地址，避免再拼 "@" 造成 "a@qq.com@smtp.qq.com" 的歧义
    return f"{name} | {username} → {target}" if username else f"{name} | {target}"
