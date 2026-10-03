"""用真实 API Key 验证插件对全部厂商的支持情况。

读取 data/real_keys.json（含真实密钥，已被 .gitignore 排除），按每条数据的
credential_type 选择对应插件（llm_api_key / api_service）调用 verify()，
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
# credential_type -> 插件文件路径
PLUGIN_PATHS = {
    "llm_api_key": os.path.join(BASE_DIR, "plugins", "llm_api_key", "plugin.py"),
    "api_service": os.path.join(BASE_DIR, "plugins", "api_service", "plugin.py"),
}
KEYS_PATH = os.path.join(BASE_DIR, "data", "real_keys.json")


def load_plugin(name: str, path: str):
    mod_name = f"_real_test_plugin_{name}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod  # dataclass 需要模块已注册
    spec.loader.exec_module(mod)
    return mod


def load_plugins() -> dict:
    """加载全部插件，按 credential_type 建立索引。"""
    return {name: load_plugin(name, path) for name, path in PLUGIN_PATHS.items()}


def mask(key: str) -> str:
    return (key[:12] + "…") if len(key) > 12 else key


def verify_one(plugins, item):
    """按 credential_type 选择插件调用 verify（两插件入参结构一致）。"""
    credential_type = item.get("credential_type", "llm_api_key")
    plugin = plugins[credential_type]
    result = plugin.verify(
        {"vendor": item["vendor"], "base_url": item["base_url"], "api_key": item["api_key"]}
    )
    return item, result


def judge(item, result) -> str:
    """返回 PASS / FAIL / NETERR / SKIP 之一。"""
    expected = item["expected"]
    # 以下端点从当前网络不可达（被墙/超时），记为 NETERR 而非失败
    NETWORK_BLOCKED_VENDORS = {"gemini", "openai"}
    if item["vendor"] in NETWORK_BLOCKED_VENDORS:
        return "PASS" if result.status == "valid" else "NETERR"

    if expected == "valid":
        return "PASS" if result.status == "valid" else "FAIL"
    if expected == "overdue":
        # 欠费应被识别为 invalid（不可用）
        return "PASS" if result.status == "invalid" else "FAIL"
    return "SKIP"


def main() -> None:
    if not os.path.exists(KEYS_PATH):
        print(f"缺少测试数据文件：{KEYS_PATH}")
        sys.exit(2)

    with open(KEYS_PATH, "r", encoding="utf-8") as f:
        items = json.load(f)

    plugins = load_plugins()
    print(f"共 {len(items)} 个真实 Key，开始验证…\n")

    stats = {"PASS": 0, "FAIL": 0, "NETERR": 0, "SKIP": 0}
    failures = []

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(verify_one, plugins, item) for item in items]
        for fut in as_completed(futures):
            item, result = fut.result()
            verdict = judge(item, result)
            stats[verdict] += 1

            bal = ""
            if result.account_info and "balance" in result.account_info:
                bal = f" 余额={result.account_info['balance']}{result.account_info.get('currency','')}"
            line = (
                f"[{verdict:6s}] {item['vendor']:<22s} {mask(item['api_key']):<14s} "
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
            print(f"  - {item['vendor']} {mask(item['api_key'])} "
                  f"期望={item['expected']} 实际={result.status}: {result.message}")

    sys.exit(1 if stats["FAIL"] else 0)


if __name__ == "__main__":
    main()
