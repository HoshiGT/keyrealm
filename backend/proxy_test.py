"""验证代理可用性诊断。

目的：代理配置光看字符串没法判断好坏。这里做一次**对照实验**——同一套探测分别在
「直连」与「走代理」两种模式下跑一遍，再对比结果：

  1. 国内站点可达性（baidu.com）
  2. 国外站点可达性（gstatic.com/generate_204）
  3. 出口 IP 与归属地

据此给出结论：代理是否真的改变了出口、国内外站点分别通不通、延迟是否可接受。

几个刻意的设计：
- `trust_env=False`：屏蔽系统环境变量里的 HTTP(S)_PROXY/ALL_PROXY，否则「直连」
  这一组会被环境代理污染，对照实验失去意义。
- **出口 IP 只认「无 CDN 的 HTTPS 见证源」**（ipify / icanhazip / ifconfig.me）。
  最初用 ip-api.com 作首选，实测踩坑：它在国内有 CDN，且规则型代理客户端
  （Clash 之类）常把它判为直连，于是「挂代理」拿到的仍是本机真实出口 IP，
  结论直接反了。ip-api 只用于**查归属地**——IP 写在路径里，谁去查都一样准，
  不受路由影响。
- 六项探测并发执行：直连国外站点在墙内通常要等到超时，串行会拖到几十秒。
- 单项失败不影响其它项，全部异常都收敛为 `{"ok": False, "detail": ...}`。
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from . import proxy as proxy_config

# 出口 IP 见证源：必须是「没有国内 CDN、HTTPS」的服务，否则测到的不是真实出口。
_IP_WITNESSES: list[tuple[str, str]] = [
    ("api.ipify.org", "https://api.ipify.org?format=json"),
    ("ipv4.icanhazip.com", "https://ipv4.icanhazip.com"),
    ("ifconfig.me", "https://ifconfig.me/ip"),
    ("ipinfo.io", "https://ipinfo.io/json"),
]

# 归属地查询：IP 在路径里，因此对结果无影响，仅作数据源。
_GEO_SOURCES: list[str] = [
    "http://ip-api.com/json/{ip}?fields=status,message,country,countryCode,regionName,city,isp,as",
    "https://ipinfo.io/{ip}/json",
]

_DOMESTIC_SITE = ("baidu.com", "https://www.baidu.com/")
_FOREIGN_SITE = ("gstatic.com", "https://www.gstatic.com/generate_204")

_TIMEOUT = httpx.Timeout(connect=6.0, read=8.0, write=8.0, pool=6.0)

_IPV4_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
_IPV6_RE = re.compile(r"\b([0-9a-fA-F]{0,4}(?::[0-9a-fA-F]{0,4}){2,7})\b")


def _client(proxy: str | None) -> httpx.Client:
    """构造客户端：直连时显式忽略环境代理，避免污染对照实验。"""
    return httpx.Client(proxy=proxy or None, timeout=_TIMEOUT, trust_env=False)


def _explain(exc: Exception) -> str:
    """异常 → 给人看的文本。

    httpx 缺 SOCKS 依赖时抛的是英文 ImportError（"…'socksio' package is not installed…"），
    直接甩给用户没用，换成中文处理办法；其它异常保持原样（类型名有助于排查）。
    """
    if "socksio" in str(exc):
        return proxy_config.SOCKS_HINT
    return f"{type(exc).__name__}: {exc}"


def _check_site(client: httpx.Client, name: str, url: str) -> dict:
    """访问站点，返回可达性与耗时。4xx 以下视为可达。"""
    start = time.monotonic()
    try:
        resp = client.get(url)
        ms = int((time.monotonic() - start) * 1000)
        return {
            "ok": resp.status_code < 400,
            "ms": ms,
            "status": resp.status_code,
            "detail": f"{name} 返回 HTTP {resp.status_code}",
        }
    except Exception as e:
        ms = int((time.monotonic() - start) * 1000)
        return {
            "ok": False,
            "ms": ms,
            "status": None,
            "detail": f"{name} 不可达：{_explain(e)}",
        }


def _extract_ip(text: str) -> str:
    """从 JSON 或纯文本响应里抠出 IP。"""
    text = (text or "").strip()
    try:
        data = json.loads(text)
    except Exception:
        data = None
    if isinstance(data, dict):
        for key in ("ip", "query", "origin"):
            if data.get(key):
                return str(data[key]).strip()
    m = _IPV4_RE.search(text) or _IPV6_RE.search(text)
    return m.group(1) if m else ""


def _fetch_ip(client: httpx.Client) -> dict:
    """取当前模式下的出口 IP（见证源顺序尝试，第一个成功即采用）。"""
    start = time.monotonic()
    last_err = "无可用见证源"
    for host, url in _IP_WITNESSES:
        try:
            resp = client.get(url)
            if resp.status_code >= 400:
                last_err = f"{host} 返回 HTTP {resp.status_code}"
                continue
            ip = _extract_ip(resp.text)
            if not ip:
                last_err = f"{host} 响应中未找到 IP"
                continue
            return {
                "ok": True,
                "ip": ip,
                "country": "", "country_code": "", "region": "", "city": "", "isp": "",
                "witness": host,
                "ms": int((time.monotonic() - start) * 1000),
            }
        except Exception as e:
            last_err = f"{host}: {_explain(e)}"
            continue
    return {"ok": False, "ip": "", "witness": "", "detail": last_err,
            "ms": int((time.monotonic() - start) * 1000)}


def _fetch_geo(ip: str) -> dict:
    """查 IP 归属地。IP 写在请求路径里，因此不受「走哪条路由」影响。"""
    if not ip:
        return {}
    for tpl in _GEO_SOURCES:
        url = tpl.format(ip=ip)
        try:
            with httpx.Client(timeout=_TIMEOUT, trust_env=False) as c:
                resp = c.get(url)
            if resp.status_code >= 400:
                continue
            data = resp.json()
            if not isinstance(data, dict) or data.get("status") == "fail":
                continue
            geo = {
                "country": str(data.get("country") or "").strip(),
                "country_code": str(data.get("countryCode") or data.get("country_code") or "").strip(),
                "region": str(data.get("regionName") or data.get("region") or "").strip(),
                "city": str(data.get("city") or "").strip(),
                "isp": str(data.get("isp") or data.get("org") or "").strip(),
            }
            # ipinfo.io 的 country 只有国家码，没有中文名；保持字段语义一致即可
            return {k: v for k, v in geo.items() if v}
        except Exception:
            continue
    return {}


def _probe(proxy: str | None) -> dict:
    """在给定模式下并发跑完三项探测（国内站点 / 国外站点 / 出口 IP）。"""
    with _client(proxy) as client:
        with ThreadPoolExecutor(max_workers=3) as pool:
            f_dom = pool.submit(_check_site, client, *_DOMESTIC_SITE)
            f_for = pool.submit(_check_site, client, *_FOREIGN_SITE)
            f_ip = pool.submit(_fetch_ip, client)
            info = f_ip.result()
            dom, foreign = f_dom.result(), f_for.result()
    if info.get("ok"):
        info.update(_fetch_geo(info["ip"]))
    return {"domestic": dom, "foreign": foreign, "ip": info}


def _ip_label(info: dict) -> str:
    if not info.get("ok"):
        return f"未知（{info.get('detail', '查询失败')}）"
    geo = " ".join(x for x in (info.get("country"), info.get("region"),
                               info.get("city")) if x)
    return info["ip"] + (f"（{geo}）" if geo else "")


def _verdict(direct: dict, proxied: dict | None, proxy: str) -> dict:
    """综合直连/代理两组结果给出结论。"""
    details: list[str] = []
    level = "ok"

    d_ip, d_for, d_dom = direct["ip"], direct["foreign"], direct["domestic"]
    details.append(
        f"直连出口 IP：{_ip_label(d_ip)}"
        f"，国内站点{'可达' if d_dom.get('ok') else '不可达'}"
        f"，国外站点{'可达' if d_for.get('ok') else '不可达'}"
    )

    if proxied is None:
        details.append("未配置代理，本次仅测试直连。填入代理地址（如 socks5://127.0.0.1:10808）后可测试代理效果。")
        if not d_dom.get("ok"):
            return {"level": "fail",
                    "headline": "直连异常：国内站点不可达，请先检查本机网络",
                    "details": details}
        return {"level": "warn", "headline": "未配置代理，仅测试了直连",
                "details": details}

    p_ip, p_for, p_dom = proxied["ip"], proxied["foreign"], proxied["domestic"]
    details.append(
        f"代理出口 IP：{_ip_label(p_ip)}"
        f"，国内站点{'可达' if p_dom.get('ok') else '不可达'}"
        f"，国外站点{'可达' if p_for.get('ok') else '不可达'}"
    )

    if not p_dom.get("ok") and not p_for.get("ok"):
        return {"level": "fail",
                "headline": "代理不可用：经代理国内外站点均不可达",
                "details": details + [f"请确认 {proxy} 已启动、端口与协议正确"]}

    if p_ip.get("ok") and d_ip.get("ok"):
        if p_ip["ip"] != d_ip["ip"]:
            details.append(f"出口 IP 已由 {d_ip['ip']} 变为 {p_ip['ip']} ✅")
            if p_for.get("ok") and not d_for.get("ok"):
                headline = "代理工作正常：出口已切换，国外站点由不可达变为可达"
            else:
                headline = "代理工作正常：出口 IP 已切换"
        else:
            level = "warn"
            headline = "代理出口 IP 与直连相同：代理可能未生效"
            details.append("挂代理与不挂代理的 IP 一致，说明流量没有真正经过代理"
                           "（透明转发、未启动，或该代理客户端把此请求判为直连）")
    else:
        level = "warn"
        headline = "代理部分可用，但出口 IP 无法确认"
        details.append("至少一种模式下 IP 见证源不可达，无法比对出口")

    if not p_for.get("ok") and d_for.get("ok"):
        level = "warn"
        headline = "代理反而使国外站点不可达：代理线路本身受限"
        details.append("直连可访问国外站点，走代理后反而不通，建议更换代理节点")
    if not p_dom.get("ok"):
        if level == "ok":
            level = "warn"
        details.append("经代理访问国内站点失败：该代理可能只放行国外路由")

    if p_for.get("ok") and d_for.get("ok"):
        details.append(f"国外站点延迟：直连 {d_for['ms']}ms → 代理 {p_for['ms']}ms")
    elif p_for.get("ok"):
        details.append(f"国外站点延迟（经代理）：{p_for['ms']}ms")

    return {"level": level, "headline": headline, "details": details}


def _missing_socks(proxy: str, direct: dict) -> dict:
    """socksio 未安装时的短路结果：不发起网络请求，直接给结论。

    伪造一份「代理侧」探测结构，让界面两个卡片照常渲染——左列直连真实数据，
    右列三项都显示中文处理办法，而不是让卡片空着或抛一句英文异常。
    """
    hint = proxy_config.SOCKS_HINT
    # 单项 detail 要进卡片窄格，用短句；完整处理办法在 verdict.details 里给
    short = "未执行探测：当前环境缺少 socksio，socks 代理不可用"
    return {
        "ok": True,
        "proxy": proxy,
        "direct": direct,
        "proxied": {
            "domestic": {"ok": False, "ms": 0, "status": None, "detail": short},
            "foreign": {"ok": False, "ms": 0, "status": None, "detail": short},
            "ip": {"ok": False, "ip": "", "witness": "", "detail": short, "ms": 0},
        },
        "verdict": {
            "level": "fail",
            "headline": f"代理不可用：当前环境缺少 SOCKS 支持（socksio），{proxy} 无法连接",
            "details": [hint, "本次未发起代理探测，左侧直连结果仍然有效。"],
        },
    }


def test_proxy(proxy: str = "") -> dict:
    """对照测试代理：返回直连/代理两组探测结果与综合结论。

    proxy 为空时只测直连（proxied 为 None）。
    """
    proxy = (proxy or "").strip()
    if proxy and proxy_config.needs_socks(proxy) and not proxy_config.socks_available():
        # 缺依赖是确定性问题，不必等国外站点超时——但直连那组仍照跑，便于对照
        return _missing_socks(proxy, _probe(None))

    direct = _probe(None)
    proxied = _probe(proxy) if proxy else None
    return {
        "ok": True,
        "proxy": proxy,
        "direct": direct,
        "proxied": proxied,
        "verdict": _verdict(direct, proxied, proxy),
    }
