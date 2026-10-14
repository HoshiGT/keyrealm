"""对象存储类别的**共享工具**（XML 解析 / 体积格式化 / 带进度与取消的流式下载）。

**为什么放在类别目录、且带 ``storage_`` 前缀**：插件加载器会把「插件目录的父目录」（即本类别
目录）临时插进 ``sys.path``，所以 ``import storage_common`` 这类**裸名导入**可用（先例
``plugins/cloud/google_sa.py``）。而框架的「同名辅助模块串味」清理**只针对来自别的插件目录**的
文件，**类别目录下的同名模块之间不会被去重**——所以这里的名字必须带类别前缀，否则将来别的类别
也放一个 ``common.py`` 时，先加载者会永久霸占该模块名且**不报错**（见 PLUGIN_GUIDE §1）。

本模块**只用标准库**，可离线单测（不触碰 httpx 与后端）。
"""
from __future__ import annotations

import os
import time
import xml.etree.ElementTree as ET
from typing import Any

# 下载进度上报的节流：每写入这么多字节、或隔这么久才回调一次
PROGRESS_STEP = 256 * 1024     # 256 KiB
PROGRESS_INTERVAL = 0.2        # 秒


# ── 小工具 ────────────────────────────────────────────────

def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on", "是")


def human_size(size: Any) -> str:
    try:
        value = float(size)
    except (TypeError, ValueError):
        return str(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def redact(text: Any, *secrets: str) -> str:
    out = str(text or "")
    for secret in secrets:
        if secret:
            out = out.replace(secret, "***")
    return out


# ── XML（去命名空间，按本地名匹配）─────────────────────────

def local_name(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def xml_root(text: str):
    try:
        return ET.fromstring(text)
    except Exception:  # noqa: BLE001 - 非 XML（错误页 / HTML）由调用方兜底
        return None


def xml_findall(node, local: str) -> list:
    if node is None:
        return []
    return [el for el in node.iter() if local_name(el.tag) == local]


def child_text(node, local: str) -> str:
    if node is None:
        return ""
    for child in list(node):
        if local_name(child.tag) == local:
            return (child.text or "").strip()
    return ""


def xml_text(node, local: str) -> str:
    found = xml_findall(node, local)
    return (found[0].text or "").strip() if found else ""


def xml_error(text: str) -> tuple[str, str]:
    """解析 S3 ``<Error><Code/><Message/></Error>``，返回 ``(code, message)``。"""
    root = xml_root(text)
    if root is None or local_name(root.tag) != "Error":
        return "", ""
    return child_text(root, "Code"), child_text(root, "Message")


# ── 流式下载（带进度 / 取消）────────────────────────────────

class Cancelled(Exception):
    """协作式取消：内部信号，不会逃出插件边界。"""


def remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def content_length(response) -> int:
    try:
        value = int(str(response.headers.get("Content-Length") or "").strip())
    except Exception:  # noqa: BLE001
        return 0
    return value if value > 0 else 0


def download_with_progress(response, path: str, label: str = "",
                           progress=None, should_cancel=None) -> tuple[bool, str]:
    """流式落盘（``.part`` + 原子替换）。``progress(current, total, text)`` 有节流；
    ``should_cancel()`` 为真即尽快停下并删掉 ``.part``（目标路径始终没被写过）。
    """
    target = str(path or "").strip()
    if not target:
        return False, "未指定保存路径"
    total = content_length(response)

    def _emit(current: int, text: str) -> None:
        if progress is not None:
            progress(current, total, text)

    def _amount(current: int) -> str:
        return f"{human_size(current)} / {human_size(total)}" if total > 0 else human_size(current)

    name = label.rsplit("/", 1)[-1] or label
    _emit(0, f"开始下载 {name}")
    tmp = target + ".part"
    written = reported = 0
    last = time.monotonic()
    try:
        with open(tmp, "wb") as handle:
            for chunk in response.iter_bytes(65536):
                if should_cancel is not None and should_cancel():
                    raise Cancelled
                handle.write(chunk)
                written += len(chunk)
                now = time.monotonic()
                if written - reported >= PROGRESS_STEP or now - last >= PROGRESS_INTERVAL:
                    reported, last = written, now
                    _emit(written, _amount(written))
        os.replace(tmp, target)
    except Cancelled:
        remove_quietly(tmp)
        return False, "下载已取消"
    except Exception as exc:  # noqa: BLE001 - 插件边界兜底
        remove_quietly(tmp)
        return False, f"写入失败：{type(exc).__name__}"
    if written != reported:      # 不足一个节流阈值的小文件也补报终值
        _emit(written, _amount(written))
    return True, target
