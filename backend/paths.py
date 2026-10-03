"""路径解析：区分「随包只读资源」与「exe 同级可写/可改目录」。

PyInstaller onefile 冻结后，`Path(__file__)` 指向临时解压目录（sys._MEIPASS），
进程退出即销毁。若不区分，数据库、已装插件会写进临时目录而丢失，用户也无法
编辑插件。因此：

- resource_dir()：随包只读资源（frontend/dist 等）。冻结时 = sys._MEIPASS。
- app_dir()     ：exe 同级目录（可写数据 data/、可改插件 plugins/）。冻结时 = exe 所在目录。

开发模式（未冻结）下两者都等于项目根目录，行为与打包前完全一致。
"""
from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    """是否运行在 PyInstaller 冻结后的可执行文件中。"""
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> Path:
    """随包只读资源根目录（frontend/dist 等）。冻结时指向 _MEIPASS 解压目录。"""
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def app_dir() -> Path:
    """exe 同级目录（冻结）或项目根目录（开发）。

    用于放置用户可写数据（data/：数据库、已装插件）与用户可改插件（plugins/）。
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent
