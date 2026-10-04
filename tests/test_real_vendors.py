"""用真实 API Key 验证插件对全部厂商的支持情况。

读取 data/real_keys.json（含真实密钥，已被 .gitignore 排除），按每条数据的
vendor 加载对应子插件（plugins/llm/<厂商>/ 或 plugins/api/<服务>/）调用 verify()，
对比表格中的预期（valid=可用 / overdue=欠费）。

运行：source .venv/bin/activate && python tests/test_real_vendors.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEYS_PATH = os.path.join(BASE_DIR, "data", "real_keys.json")

# 子插件通过 `from <category>_common import ...` 复用类别共享模块，
# 需把所有类别目录加入 sys.path（与 plugin_base 的加载行为一致）
PLUGINS_DIR = os.path.join(BASE_DIR, "plugins")
_CATEGORIES = (
    sorted(
        d
        for d in os.listdir(PLUGINS_DIR)
        if os.path.isdir(os.path.join(PLUGINS_DIR, d))
    )
    if os.path.isdir(PLUGINS_DIR)
    else []
)
for _category in _CATEGORIES:
    _cat_dir = os.path.join(PLUGINS_DIR, _category)
    if _cat_dir not in sys.path:
        sys.path.insert(0, _cat_dir)


def plugin_path_for(vendor: str) -> str:
    """vendor 名即子插件目录名，跨所有类别查找 plugins/<类别>/<vendor>/plugin.py。"""
    for category in _CATEGORIES:
        path = os.path.join(PLUGINS_DIR, category, vendor, "plugin.py")
        if os.path.exists(path):
            return path
    raise KeyError(f"未找到厂商 {vendor} 对应的子插件目录")


def load_plugin(name: str, path: str):
    mod_name = f"_real_test_plugin_{name}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod  # dataclass 需要模块已注册
    spec.loader.exec_module(mod)
    return mod


def load_plugins(items) -> dict:
    """按 real_keys 中出现的 vendor 加载对应子插件，vendor -> module。"""
    plugins = {}
    for item in items:
        vendor = item["vendor"]
        if vendor not in plugins:
            plugins[vendor] = load_plugin(vendor, plugin_path_for(vendor))
    return plugins


def mask(key: str) -> str:
    return (key[:12] + "…") if len(key) > 12 else key


def verify_one(plugins, item):
    """调用子插件 verify。data 支持任意多字段；缺省回退到 base_url/api_key。"""
    plugin = plugins[item["vendor"]]
    data = item.get("data")
    if not data:
        data = {"base_url": item.get("base_url", ""), "api_key": item.get("api_key", "")}
    return item, plugin.verify(data)


# 网络层不可达/环境问题的特征串，用于把 error 归为 NETERR 而非 FAIL
_NETWORK_ERROR_HINTS = (
    "Network is unreachable", "ConnectError", "请求超时", "ConnectTimeout",
    "timed out", "Timeout", "getaddrinfo", "Name or service not known",
    "Connection refused", "Connection reset", "网络请求失败", "地域限制",
    "ahead of the server",
)


def judge(item, result) -> str:
    """返回 PASS / FAIL / NETERR / SKIP 之一。"""
    expected = item["expected"]
    # 已被服务端拒绝但原因属环境/白名单（如 Binance IP 未白名单），无法据此判断密钥真伪
    if expected == "ip_restricted":
        return "SKIP"
    if result.status == "error" and any(h in (result.message or "") for h in _NETWORK_ERROR_HINTS):
        return "NETERR"
    if expected == "valid":
        return "PASS" if result.status == "valid" else "FAIL"
    if expected == "overdue":
        # 欠费不算失效：凭据本身有效，仅余额不足，应按 valid 判定
        return "PASS" if result.status == "valid" else "FAIL"
    if expected == "invalid":
        return "PASS" if result.status == "invalid" else "FAIL"
    # 无法联网验证 / 未知预期：跳过
    return "SKIP"


def main() -> None:
    if not os.path.exists(KEYS_PATH):
        print(f"缺少测试数据文件：{KEYS_PATH}")
        sys.exit(2)

    with open(KEYS_PATH, "r", encoding="utf-8") as f:
        items = json.load(f)

    # 仅验证新增类别（ONLY_NEW=1 时），便于增量复测
    if os.environ.get("ONLY_NEW") == "1":
        _prefixes = ("dns_", "dev_", "im_", "cloud_", "exchange_", "mail_")
        items = [it for it in items if str(it.get("credential_type", "")).startswith(_prefixes)]

    # expected=="skip" 的条目不调用 verify：这些服务要么无公开校验接口，
    # 要么校验本身是写操作（如 DuckDNS 的 update 会改写解析记录）。
    skipped = [it for it in items if it.get("expected") == "skip"]
    active = [it for it in items if it.get("expected") != "skip"]

    plugins = load_plugins(active)
    print(f"共 {len(items)} 个真实 Key（{len(plugins)} 个子插件），"
          f"{len(active)} 条验证、{len(skipped)} 条跳过…\n")

    stats = {"PASS": 0, "FAIL": 0, "NETERR": 0, "SKIP": len(skipped)}
    failures = []

    for it in skipped:
        print(f"[{'SKIP':6s}] {it['vendor']:<22s} -> 跳过：{it.get('note', '')}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(verify_one, plugins, item) for item in active]
        for fut in as_completed(futures):
            item, result = fut.result()
            verdict = judge(item, result)
            stats[verdict] += 1

            bal = ""
            if result.account_info and "balance" in result.account_info:
                bal = f" 余额={result.account_info['balance']}{result.account_info.get('currency','')}"
            line = (
                f"[{verdict:6s}] {item['vendor']:<22s} {mask(item.get('api_key', '')):<14s} "
                f"-> {result.status:<7s} {result.message}{bal}"
            )
            print(line)
            if verdict == "FAIL":
                failures.append((item, result))

    print("\n" + "=" * 70)
    print(f"结果：通过 {stats['PASS']} / 失败 {stats['FAIL']} / "
          f"网络不可达 {stats['NETERR']} / 跳过 {stats['SKIP']}")

    if failures:
        print("\n失败明细：")
        for item, result in failures:
            print(f"  - {item['vendor']} {mask(item.get('api_key', ''))} "
                  f"期望={item['expected']} 实际={result.status}: {result.message}")

    sys.exit(1 if stats["FAIL"] else 0)


if __name__ == "__main__":
    main()
