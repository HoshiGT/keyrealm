"""GitHub 令牌验证插件（plugins/dev/ 类别下的独立子插件）。

本插件提供：

- ``verify``：``GET /user`` 验证 Personal Access Token / OAuth Token 是否有效
- ``capabilities``：只按「接口能不能通」探测当前令牌真正可用的操作（fine-grained
  与 classic 令牌权限模型不同，不做权限名假设）
- ``actions`` / ``run_action``（均只读，绝不回显 token）：
  - ``account_info``  (list) 账号信息（登录名/名称/类型/公开仓库数/关注者/权限范围/创建时间）
  - ``list_repos``    (list) 我的仓库（名称/属主/可见性/语言/更新时间/大小 KB/默认分支）
  - ``repo_detail``   (list) 单个仓库详情（list_repos 的行内动作入口）
  - ``download_repo`` (form) 打包下载仓库 zipball，路径由前端「另存为」选好后经 params 传入
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from dev_common import (
    VerifyResult,
    body_snippet,
    download_to_file,
    guarded,
    make_client,
    safe_json,
)

NAME = "GitHub"
TOKEN_KEY = "token"
ENDPOINT = "https://api.github.com/user"


def _mask(secret: str) -> str:
    """脱敏展示：保留前 4 位，其余以 * 代替。"""
    secret = secret or ""
    if len(secret) <= 4:
        return "*" * len(secret)
    return secret[:4] + "*" * 6


def _unconfirmed(status: int, snippet: str) -> VerifyResult:
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def validate(data) -> list[str]:
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]

    errors: list[str] = []
    token = (data.get(TOKEN_KEY) or "").strip()
    if not token:
        errors.append("Token 不能为空")
    return errors


def _verify_github(token: str) -> VerifyResult:
    """GitHub 令牌验证：GET /user（Personal Access Token / OAuth Token 通用）。"""
    with make_client() as client:
        resp = client.get(ENDPOINT, headers=_headers(token))
    status = resp.status_code
    payload = safe_json(resp)

    if status == 200:
        info: dict[str, Any] = {}
        if isinstance(payload, dict):
            for key in ("login", "name", "type"):
                value = payload.get(key)
                if value:
                    info[key] = value
        # OAuth / classic 令牌会回传授权范围，便于确认权限面
        scopes = resp.headers.get("X-OAuth-Scopes")
        if scopes:
            info["scopes"] = scopes
        return VerifyResult(status="valid", message="凭证有效", account_info=info or None)

    if status == 401:
        return VerifyResult(status="invalid", message="凭证无效 (HTTP 401)")

    if status == 403:
        # 403 多为触发 API 速率限制；剩余配额为 0 时给出更明确的提示
        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining == "0":
            return VerifyResult(
                status="error",
                message="请求被 GitHub 限流（API 速率限制已用尽），暂时无法确认凭证状态",
            )
        return VerifyResult(
            status="error",
            message=f"请求被拒绝 (HTTP 403)，可能触发限流或令牌权限不足: {body_snippet(resp)}",
        )

    return _unconfirmed(status, body_snippet(resp))


@guarded
def verify(data) -> VerifyResult:
    data = data or {}
    token = (data.get(TOKEN_KEY) or "").strip()
    if not token:
        return VerifyResult(status="invalid", message="Token 为空")
    return _verify_github(token)


# ── 操作实现 ─────────────────────────────────────────────

def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "CredentialVault",
    }


def _repo_path(owner: str, repo: str) -> str:
    """仓库路径片段（owner/repo 各自的特殊字符做转义）。"""
    return f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"


def _http_error(resp: Any, what: str = "请求") -> str:
    """把非 200 响应转成一句人话（状态码 + 摘要，绝不带 token）。"""
    status = resp.status_code
    if status == 401:
        return "凭证无效 (HTTP 401)"
    if status == 404:
        return f"未找到资源 (HTTP 404)：{what}"
    if status == 403:
        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining == "0":
            return "请求被 GitHub 限流（API 速率限制已用尽），请稍后重试"
        return f"请求被拒绝 (HTTP 403)，可能限流或权限不足：{body_snippet(resp)}"
    return f"{what}失败 (HTTP {status})"


def _probe(client, token: str, url: str, params: dict | None = None) -> str:
    """GET 单个接口，返回 ``""``（可用）或一句「为什么不可用」。

    区分两种失败：

    - **有响应但状态非 200** → 这是确定的答案（如 401 令牌无效、404 无权限），返回原因；
    - **限流（403 + 配额用尽）** → 与权限无关、且是暂时的，**抛异常**交给 fail-open，
      否则会因为「此刻恰好没配额」把用户本可用的操作全藏掉；
    - 网络异常（httpx 抛出）→ 自然向上传播，同样走 fail-open。
    """
    resp = client.get(url, params=params, headers=_headers(token))
    if resp.status_code == 200:
        return ""
    if resp.status_code == 403 and resp.headers.get("X-RateLimit-Remaining") == "0":
        raise RuntimeError("GitHub API 限流，暂时无法探测权限")
    snippet = body_snippet(resp)
    return f"HTTP {resp.status_code}{'：' + snippet if snippet else ''}"


def capabilities(data: dict) -> dict:
    """探测当前令牌**真正能用**的操作（只按接口能否通判断）。

    - ``GET /user`` 通 → ``account_info``
    - ``GET /user/repos?per_page=1`` 通 → ``list_repos`` / ``repo_detail`` / ``download_repo``

    fine-grained 令牌没有 ``X-OAuth-Scopes``、与 classic 的权限模型也不同，所以**不解读
    权限名**，只看接口返回码。探测「拿不准」（网络异常、限流）时直接抛出，由框架当作
    「未知 → 全部可用」（fail-open），避免把能用的操作藏掉。
    """
    data = data or {}
    token = (data.get(TOKEN_KEY) or "").strip()
    if not token:
        return {"available": [], "note": "缺少 Token"}

    available: list[str] = []
    notes: list[str] = []
    with make_client() as client:
        reason = _probe(client, token, ENDPOINT)
        if not reason:
            available.append("account_info")
        else:
            notes.append(f"账号信息不可用（/user {reason}）")

        reason = _probe(client, token, f"{ENDPOINT}/repos", params={"per_page": 1})
        if not reason:
            available += ["list_repos", "repo_detail", "download_repo"]
        else:
            notes.append(f"仓库列表不可用（/user/repos {reason}）")

    return {"available": available, "note": "；".join(notes)}


def actions(data: dict) -> list[dict]:
    """声明可用操作（凭据含 token 时才提供）。"""
    data = data or {}
    if not (data.get(TOKEN_KEY) or "").strip():
        return []
    return [
        {
            "id": "account_info",
            "name": "账号信息",
            "kind": "list",
            "order": 10,
            "description": "当前令牌对应的账号信息",
            "columns": [
                {"key": "field", "label": "字段", "width": 140},
                {"key": "value", "label": "值"},
            ],
            "empty_text": "未获取到账号信息",
        },
        {
            "id": "list_repos",
            "name": "我的仓库",
            "kind": "list",
            "order": 20,
            "description": "当前令牌可见的仓库（按更新时间倒序）",
            "columns": [
                {"key": "name", "label": "名称", "mono": True},
                {"key": "owner", "label": "属主", "width": 140},
                {"key": "private", "label": "可见性", "width": 90, "type": "tag"},
                {"key": "language", "label": "语言", "width": 110},
                {"key": "updated_at", "label": "更新时间", "width": 170},
                {"key": "size", "label": "大小(KB)", "width": 100, "type": "number"},
                {"key": "default_branch", "label": "默认分支", "width": 130, "mono": True},
            ],
            "row_actions": [
                {
                    "id": "download_repo",
                    "label": "打包下载",
                    "params": {"owner": "$row.owner", "repo": "$row.name"},
                },
                {
                    "id": "repo_detail",
                    "label": "详情",
                    "params": {"owner": "$row.owner", "repo": "$row.name"},
                },
            ],
            "empty_text": "该令牌下没有可见的仓库",
        },
        {
            "id": "repo_detail",
            "name": "仓库详情",
            "kind": "list",
            "description": "单个仓库的详情",
            "columns": [
                {"key": "field", "label": "字段", "width": 160},
                {"key": "value", "label": "值"},
            ],
            "empty_text": "未获取到仓库信息",
        },
        {
            "id": "download_repo",
            "name": "打包下载",
            "kind": "form",
            "order": 30,
            "description": "下载仓库源码 zipball（zip 归档，默认分支或指定 ref）",
            "fields": [
                {"key": "owner", "label": "属主", "type": "text", "required": True,
                 "placeholder": "用户名或组织名"},
                {"key": "repo", "label": "仓库名", "type": "text", "required": True},
                {"key": "ref", "label": "分支/标签/Commit", "type": "text",
                 "placeholder": "留空 = 默认分支"},
            ],
            "submit": "下载",
            "save_to": {"key": "path", "default_name": "$row.name.zip"},
        },
    ]


def _account_rows(resp) -> list[dict]:
    payload = safe_json(resp) or {}
    scopes = (resp.headers.get("X-OAuth-Scopes") or "").strip()
    public_repos = payload.get("public_repos")
    followers = payload.get("followers")

    def text(value: Any) -> str:
        return "—" if value is None or value == "" else str(value)

    return [
        {"field": "登录名", "value": text(payload.get("login"))},
        {"field": "名称", "value": text(payload.get("name"))},
        {"field": "类型", "value": text(payload.get("type"))},
        {"field": "公开仓库数", "value": text(public_repos)},
        {"field": "关注者", "value": text(followers)},
        # X-OAuth-Scopes 仅 classic token 回传；fine-grained / GitHub App 令牌没有
        {"field": "权限范围", "value": scopes or "—"},
        {"field": "创建时间", "value": text(payload.get("created_at"))},
    ]


def _repo_rows(repos: list) -> list[dict]:
    rows: list[dict] = []
    for repo in repos:
        if not isinstance(repo, dict):
            continue
        owner = repo.get("owner")
        owner_login = owner.get("login") if isinstance(owner, dict) else None
        size = repo.get("size")
        rows.append(
            {
                "name": repo.get("name") or "—",
                "owner": owner_login or "—",
                "private": "私有" if repo.get("private") else "公开",
                "language": repo.get("language") or "—",
                "updated_at": repo.get("updated_at") or "—",
                "size": size if isinstance(size, int) else "—",
                "default_branch": repo.get("default_branch") or "—",
            }
        )
    return rows


def _repo_detail_rows(payload: dict) -> list[dict]:
    def text(value: Any) -> str:
        return "—" if value is None or value == "" else str(value)

    owner = payload.get("owner")
    owner_login = owner.get("login") if isinstance(owner, dict) else None
    license_info = payload.get("license")
    license_name = license_info.get("spdx_id") if isinstance(license_info, dict) else None
    return [
        {"field": "全名", "value": text(payload.get("full_name"))},
        {"field": "属主", "value": text(owner_login)},
        {"field": "可见性", "value": "私有" if payload.get("private") else "公开"},
        {"field": "描述", "value": text(payload.get("description"))},
        {"field": "语言", "value": text(payload.get("language"))},
        {"field": "默认分支", "value": text(payload.get("default_branch"))},
        {"field": "大小(KB)", "value": text(payload.get("size"))},
        {"field": "Star / Fork / Watch",
         "value": f"{text(payload.get('stargazers_count'))} / "
                  f"{text(payload.get('forks_count'))} / {text(payload.get('subscribers_count'))}"},
        {"field": "开放 Issue", "value": text(payload.get("open_issues_count"))},
        {"field": "许可证", "value": text(license_name)},
        {"field": "创建时间", "value": text(payload.get("created_at"))},
        {"field": "更新时间", "value": text(payload.get("updated_at"))},
        {"field": "推送时间", "value": text(payload.get("pushed_at"))},
        {"field": "是否归档", "value": "是" if payload.get("archived") else "否"},
        {"field": "仓库地址", "value": text(payload.get("html_url"))},
    ]


def _run_list(action_id: str, params: dict, token: str) -> dict:
    if action_id == "account_info":
        with make_client() as client:
            resp = client.get(ENDPOINT, headers=_headers(token))
        if resp.status_code != 200:
            return {"ok": False, "error": _http_error(resp, "查询账号信息")}
        rows = _account_rows(resp)
        return {"ok": True, "rows": rows, "total": len(rows)}

    if action_id == "list_repos":
        try:
            per_page = int(str(params.get("per_page") or "30").strip())
        except (TypeError, ValueError):
            per_page = 30
        per_page = max(1, min(per_page, 100))
        with make_client() as client:
            resp = client.get(
                f"{ENDPOINT}/repos",
                params={"per_page": per_page, "sort": "updated"},
                headers=_headers(token),
            )
        if resp.status_code != 200:
            return {"ok": False, "error": _http_error(resp, "查询仓库列表")}
        try:
            repos = resp.json()
        except Exception:  # noqa: BLE001 - 非 JSON 响应按空列表处理
            repos = None
        rows = _repo_rows(repos if isinstance(repos, list) else [])
        # total 只报本次返回条数（GitHub 分页需另算，不编造）；满页才提示可能还有更多
        return {
            "ok": True,
            "rows": rows,
            "total": len(rows),
            "has_more": len(rows) >= per_page,
            "empty_text": "该令牌下没有可见的仓库",
        }

    if action_id == "repo_detail":
        owner = (params.get("owner") or "").strip()
        repo = (params.get("repo") or "").strip()
        if not owner or not repo:
            return {"ok": False, "error": "缺少 owner 或 repo"}
        # _repo_path 返回的是路径（/repos/<owner>/<repo>），必须补上 api.github.com，
        # 否则 httpx 会因为「没有协议」抛 UnsupportedProtocol。
        with make_client() as client:
            resp = client.get(f"https://api.github.com{_repo_path(owner, repo)}",
                              headers=_headers(token))
        if resp.status_code != 200:
            return {"ok": False, "error": _http_error(resp, f"{owner}/{repo}")}
        payload = safe_json(resp)
        if not isinstance(payload, dict):
            return {"ok": False, "error": "响应格式异常"}
        rows = _repo_detail_rows(payload)
        return {"ok": True, "rows": rows, "total": len(rows)}

    return {"ok": False, "error": "未知操作"}


def _run_download(params: dict, token: str) -> dict:
    owner = (params.get("owner") or "").strip()
    repo = (params.get("repo") or "").strip()
    ref = (params.get("ref") or "").strip()
    path = (params.get("path") or "").strip()
    if not owner or not repo:
        return {"ok": False, "error": "缺少 owner 或 repo"}
    if not path:
        return {"ok": False, "error": "未指定保存路径"}

    # ref 里可能含 "/"（如 feature/x），保留斜杠交给 GitHub 解析；其余字符转义
    url = f"https://api.github.com{_repo_path(owner, repo)}/zipball"
    if ref:
        url = f"{url}/{quote(ref, safe='/')}"

    with make_client() as client:
        # zipball 会 302 到 codeload，make_client 已 follow_redirects=True
        with client.stream("GET", url, headers=_headers(token)) as resp:
            if resp.status_code != 200:
                return {"ok": False, "error": _http_error(resp, f"{owner}/{repo} 打包下载")}
            ok, message = download_to_file(resp, path)
    if not ok:
        return {"ok": False, "error": message}
    return {"ok": True, "output": f"已保存到 {message}"}


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行操作：账号信息 / 仓库列表 / 仓库详情 / 打包下载（只读，绝不回显 token）。"""
    data = data or {}
    params = params or {}
    token = (data.get(TOKEN_KEY) or "").strip()
    if not token:
        return {"ok": False, "error": "缺少 Token"}
    # 用户自填的 per_page（若非本插件声明）不参与，这里直接忽略未知键
    params = {k: v for k, v in params.items()
              if k in ("per_page", "owner", "repo", "ref", "path")}
    try:
        if action_id == "download_repo":
            return _run_download(params, token)
        return _run_list(action_id, params, token)
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底
        return {"ok": False, "error": f"执行异常：{type(exc).__name__}"}


def get_display_summary(data) -> str:
    """凭据列表中的简短展示文本。"""
    data = data or {}
    secret = (data.get(TOKEN_KEY) or "").strip()
    if not secret:
        return NAME
    return f"{NAME} | {_mask(secret)}"
