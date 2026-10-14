"""「匿名对象存储」插件（plugins/storage/ 类别）——对**公开桶**做未签名探测与浏览。

与同类的 ``s3`` 插件互补：``s3`` 用签名凭据访问**自己的**存储；本插件**不带任何凭据**，
直接以**未签名请求**判定目标桶是否允许匿名访问，并浏览/下载其中内容。

覆盖任意 HTTP（S3 兼容）桶：阿里云 OSS / 腾讯 COS / 华为 OBS / AWS S3 / 百度 BOS / MinIO …
——输入就是桶的根地址，与厂商无关（虚拟主机式与路径式都支持）。

本插件提供的操作（``actions``，**全部未签名**）：

- ``list_objects``     匿名列举（能列出来即「公开可列」；行内可下载）
- ``download_object``  匿名读取单个对象（能读到即「公开可读」）
- ``download_prefix``  把某前缀下的对象匿名打包下载
- ``write_probe``      **匿名写探测**：PUT 一个临时探针对象后立即 DELETE，判定「公开可写」

``verify`` 语义 = 「该桶是否允许匿名列举」：200 → valid；403 → invalid（私有）；404 → invalid（不存在）。

**TLS 兼容**：默认走标准 TLS 协商；若握手失败（实测有对象存储前置网关**只接受 TLS 1.2**——
对 TLS 1.3 的 ClientHello 直接回一段明文门户页再断连），会自动**回退到 TLS 1.2 重试一次**
（证书校验照旧，仅在确为握手失败时触发）。

参考文档：https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListObjectsV2.html
"""
from __future__ import annotations

import os
import shutil
import ssl
import tempfile
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import quote, urlsplit

from backend.plugin_sdk import VerifyResult, log, make_client, proxy, timeout

import storage_s3v4 as s3v4
from storage_common import (
    Cancelled,
    child_text,
    download_with_progress,
    human_size,
    redact,
    remove_quietly,
    truthy,
    xml_error,
    xml_findall,
    xml_root,
    xml_text,
)

_PAGE_SIZE = 50
_ENUM_PAGE = 1000
MAX_ZIP_OBJECTS = 1000
MAX_ZIP_BYTES = 512 * 1024 * 1024
_ZIP_WORKERS = 8

# 探针对象：唯一 + 可识别（万一删不掉，用户一眼能认出来）
_PROBE_PREFIX = ".keyrealm-write-probe-"
_PROBE_BODY = b"keyrealm-write-probe\n"

_DENIED_CODES = {"AccessDenied", "AllAccessDisabled"}
_NOT_FOUND_CODES = {"NoSuchBucket", "NoSuchKey"}


# ── URL 处理 ──────────────────────────────────────────────

def _short(text: Any, limit: int = 200) -> str:
    return " ".join(str(text or "").split())[:limit]


def _base_url(data: dict) -> tuple[str, str]:
    """归一化桶根地址：去查询串/片段/尾斜杠，缺 scheme 补 https。返回 ``(base, 原因)``。"""
    raw = str((data or {}).get("url") or "").strip()
    if not raw:
        return "", "桶地址为空"
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
    except Exception:  # noqa: BLE001
        return "", "桶地址格式不合法"
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        return "", "桶地址只支持 http / https"
    if not parts.netloc:
        return "", "桶地址缺少主机"
    path = (parts.path or "").rstrip("/")
    return f"{scheme}://{parts.netloc}{path}", ""


def _list_url(base: str, params: dict) -> str:
    return f"{base}/?{s3v4.canonical_query(params)}"


def _object_url(base: str, key: str) -> str:
    """对象的未签名 URL：key **只编码一次**，斜杠保留。"""
    return f"{base}/{quote(key.lstrip('/'), safe='/-_.~')}"


def _client(data: dict, tls_max_12: bool = False):
    """建一个 httpx 客户端。

    只在需要**自定义 TLS 上下文**时才自己建（关闭证书校验、或把 TLS 上限压到 1.2），
    其余情况走框架的 ``make_client()``。``tls_max_12`` 供 ``_with_client`` 的降级重试使用。
    """
    skip = truthy(data.get("skip_tls_verify"))
    if not skip and not tls_max_12:
        return make_client()
    import httpx  # noqa: PLC0415
    context = ssl.create_default_context()
    if skip:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        log("已对该凭据关闭 TLS 证书校验（skip_tls_verify=true）")
    if tls_max_12:
        context.maximum_version = ssl.TLSVersion.TLSv1_2
    return httpx.Client(timeout=timeout(), follow_redirects=True, proxy=proxy(), verify=context)


def _retryable_tls_error(exc: BaseException) -> bool:
    """连接阶段的失败**是否值得用 TLS 1.2 再试一次**。

    命中：TLS 协议层错误（``SSLError``）、或**连接被重置**（``ConnectionResetError``——
    中间设备对 ClientHello 直接回 RST 也是这个表现，实测该端点两种表现都出现过）。

    **不命中**：证书校验失败（``SSLCertVerificationError``）、DNS 失败、连接被拒——
    换个 TLS 版本也修不好，只会多打一次无谓请求、还误导用户。
    """
    seen: set[int] = set()
    chain: list[BaseException] = []
    node: BaseException | None = exc
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        chain.append(node)
        node = node.__cause__ or node.__context__
    if any(isinstance(item, ssl.SSLCertVerificationError) for item in chain):
        return False
    return any(isinstance(item, (ssl.SSLError, ConnectionResetError)) for item in chain)


def _with_client(data: dict, run):
    """建客户端执行 ``run(client)``；**连接阶段 TLS 失败时，回退到 TLS 1.2 再试一次**。

    实测：某些对象存储前置网关（``Server: DisplayServer``）**只完成 TLS 1.2 握手**——
    客户端一上来就报 TLS 1.3，网关把 ClientHello 当垃圾请求、回一段**明文 HTML 门户页**
    后直接断连；httpx 侧表现为 ``ConnectError``（根因 ``ssl.SSLEOFError`` 或 RST）。于是
    出现「浏览器能打开（会自动降级重试、只是慢一拍）、插件却报连接失败」。

    只在**确为握手/连接被重置**时降级一次（证书校验照旧），其余连接错误原样抛出。
    ``run`` 必须在**一次连接内**跑完并返回结果（流式下载也要在 ``run`` 里把流读完再返回），
    这样重试是整段重跑，不会留下半截状态。
    """
    try:
        with _client(data) as client:
            return run(client)
    except Exception as exc:  # noqa: BLE001 - 仅用于识别可重试的连接失败，其余照样抛
        if not _retryable_tls_error(exc):
            raise
        log("连接在 TLS 阶段失败（疑似目标仅支持 TLS 1.2）→ 回退 TLS 1.2 重试一次")
        with _client(data, tls_max_12=True) as client:
            return run(client)


def _connect_error(exc: BaseException) -> str:
    """把连接阶段异常翻成一句用户看得懂的话。"""
    if _retryable_tls_error(exc):
        return ("连接失败：连接在 TLS 阶段被重置/中断——端点可能只接受特定 TLS 版本，"
                "或链路被中间设备干扰（已按 TLS 1.2 重试仍失败）")
    return f"连接失败：{type(exc).__name__}"


def _failure(resp) -> str:
    code, message = xml_error(resp.text or "")
    detail = message or code or f"HTTP {resp.status_code}"
    return f"请求失败：{detail}"


# ── 校验 / 验证 ───────────────────────────────────────────

def validate(data) -> list[str]:
    """服务端兜底校验（``required`` 只在前端拦）。"""
    data = data or {}
    base, error = _base_url(data)
    if error:
        return [error]
    raw = str(data.get("url") or "")
    if "@" in raw.split("://", 1)[-1].split("/", 1)[0]:
        return ["桶地址不支持用户名密码（userinfo），请去掉 @ 前的部分"]
    if "?" in raw:
        return ["桶地址不要带查询串——若这是预签名 URL，它不属于「匿名桶」，请去掉 ? 之后的参数"]
    return []


def verify(data) -> VerifyResult:
    data = data or {}
    errors = validate(data)
    if errors:
        return VerifyResult(status="invalid", message="；".join(errors))
    base, error = _base_url(data)
    if error:
        return VerifyResult(status="error", message=error)
    log(f"匿名探测对象存储桶 {base}")
    try:
        resp = _with_client(data, lambda client: client.get(
            _list_url(base, {"list-type": "2", "max-keys": "1"})))
    except Exception as exc:  # noqa: BLE001 - 边界兜底
        return VerifyResult(status="error", message=_connect_error(exc))

    root = xml_root(resp.text or "")
    if resp.status_code == 200 and root is not None and _local(root) == "ListBucketResult":
        return VerifyResult(status="valid", message="匿名可列举（公开桶）",
                            account_info={"anonymous_list": "允许"})
    if resp.status_code == 200 and (root is None or _local(root) != "ListBucketResult"):
        # 200 但根元素不是 `ListBucketResult`：多半打到了**站点首页 / 网关页**而不是对象存储端点
        # （响应是良构 XML 也一样——HTML 页面同样能被解析，只是根元素叫 `html`）。
        # 最常见的原因是**端点或路径写错**——服务端会把它当普通路径，返回站点页面。
        return VerifyResult(status="error", message=(
            "该地址返回的不是 S3 列举结果（像**站点页面 / 网关页**而非对象存储端点）："
            "请核对**端点与路径**——路径式是 https://<域>/<桶>，虚拟主机式是 https://<桶>.<域>；"
            "路径写错时服务端会把它当普通路径、返回站点页面。"))
    code, _message = xml_error(resp.text or "")
    if resp.status_code == 403 or code in _DENIED_CODES:
        return VerifyResult(status="invalid", message=f"桶存在但未开放匿名列举（{code or 'HTTP 403'}）")
    if resp.status_code == 404 or code in _NOT_FOUND_CODES:
        return VerifyResult(status="invalid", message=f"桶不存在或地址有误（{code or 'HTTP 404'}）")
    return VerifyResult(status="error", message=f"未预期响应：HTTP {resp.status_code}：{_short(resp.text)}")


def _local(node) -> str:
    from storage_common import local_name
    return local_name(node.tag) if node is not None else ""


# ── 操作声明 ──────────────────────────────────────────────

_OBJECT_COLUMNS = [
    {"key": "key", "label": "对象 Key", "mono": True},
    {"key": "type", "label": "类型", "width": 80, "type": "tag"},
    {"key": "size", "label": "大小", "width": 100, "type": "number"},
    {"key": "lastModified", "label": "最后修改", "width": 200},
]

_OBJECT_ROW_ACTIONS = [
    {"id": "list_objects", "label": "进入", "params": {"prefix": "$row.folderPath"}},
    {"id": "download_object", "label": "下载", "params": {"key": "$row.fileKey"}},
    {"id": "download_prefix", "label": "打包下载", "params": {"prefix": "$row.folderPath"}},
]

_PREFIX_FIELD = {"key": "prefix", "label": "前缀", "placeholder": "留空为桶根目录，如 dir/"}


def actions(data: dict) -> list[dict]:
    """声明可用操作（桶地址有效时提供；全部未签名）。"""
    base, error = _base_url(data or {})
    if error:
        return []
    return [
        {
            "id": "list_objects", "name": "匿名浏览对象", "kind": "list",
            "group": "匿名读取", "order": 10,
            "description": "未签名 ListObjectsV2（能列出来即该桶「公开可列」；行内可下载）",
            "columns": _OBJECT_COLUMNS, "fields": [_PREFIX_FIELD],
            "row_actions": _OBJECT_ROW_ACTIONS, "submit": "查询",
            "empty_text": f"该前缀下暂无对象（或该桶不允许匿名列举，仅显示前 {_PAGE_SIZE} 个）",
        },
        {
            "id": "download_object", "name": "匿名下载对象", "kind": "form",
            "group": "匿名读取", "order": 20,
            "description": "未签名读取单个对象（能读到即「公开可读」；弹「另存为」）",
            "fields": [{"key": "key", "label": "对象 Key", "required": True,
                        "placeholder": "如 dir/file.txt"}],
            "save_to": {"key": "path", "default_name": "$row.fileName"},
            "progress": True, "submit": "下载",
        },
        {
            "id": "download_prefix", "name": "匿名打包下载（zip）", "kind": "form",
            "group": "匿名读取", "order": 30,
            "description": (
                f"把该前缀下的对象匿名拉取后打成一个 zip（最多 {MAX_ZIP_OBJECTS} 个 / "
                f"{human_size(MAX_ZIP_BYTES)}，超出会明确报错而不静默截断）"
            ),
            "fields": [_PREFIX_FIELD],
            "save_to": {"key": "path", "default_name": "$row.folderName"},
            "progress": True, "submit": "打包下载",
        },
        {
            "id": "write_probe", "name": "匿名写探测", "kind": "form",
            "group": "写入探测", "order": 90, "write": True,
            "description": "**会在目标桶写入**一个临时探针对象并立即删除，用于判定是否允许匿名写入",
            "confirm": (
                "将在目标桶（桶根或指定前缀下）匿名写入一个临时探针对象"
                "（名如 .keyrealm-write-probe-xxxxxxxx）并**立即删除**，用于判定该桶是否允许未签名写入。\n"
                "这是对**远端数据的写入操作**——请确认目标桶在你的授权范围内。\n"
                "若删除失败，该对象会保留，结果里会给出它的完整地址供你手动清理。是否继续？"
            ),
            "fields": [{"key": "prefix", "label": "写入位置（前缀）",
                        "placeholder": "留空 = 桶根", "hint": "探针对象写在这下面，随后立即删除。"}],
            "submit": "开始探测",
        },
    ]


def run_action(action_id: str, params: dict, data: dict, progress=None, should_cancel=None) -> dict:
    """执行操作。除 ``write_probe`` 外全部只读；``progress`` / ``should_cancel`` 为下载钩子。"""
    data = data or {}
    params = params or {}
    try:
        base, error = _base_url(data)
        if error:
            return {"ok": False, "error": error}
        if action_id == "list_objects":
            return _run_list_objects(base, data, params)
        if action_id == "download_object":
            return _run_download_object(base, data, params, progress, should_cancel)
        if action_id == "download_prefix":
            return _run_download_prefix(base, data, params, progress, should_cancel)
        if action_id == "write_probe":
            return _run_write_probe(base, data, params)
        return {"ok": False, "error": "未知操作"}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底，绝不抛出
        return {"ok": False, "error": f"执行异常：{type(exc).__name__}"}


def stream_action(action_id: str, params: dict, data: dict, on_delta,
                  on_progress=None, should_cancel=None) -> dict:
    if action_id in ("download_object", "download_prefix"):
        return run_action(action_id, params, data, on_progress, should_cancel)
    return run_action(action_id, params, data)


# ── 行构造 ────────────────────────────────────────────────

def _object_row_file(key: str, size_raw: str, modified: str) -> dict:
    return {
        "key": key, "type": "文件",
        "size": int(size_raw) if str(size_raw).isdigit() else size_raw,
        "lastModified": modified,
        "fileKey": key, "fileName": key.rstrip("/").rsplit("/", 1)[-1],
    }


def _object_row_folder(prefix: str) -> dict:
    name = prefix.rstrip("/").rsplit("/", 1)[-1] or "bucket"
    return {"key": prefix, "type": "文件夹", "size": "", "lastModified": "",
            "folderPath": prefix, "folderName": f"{name}.zip"}


# ── 匿名列举 / 下载 ───────────────────────────────────────

def _run_list_objects(base: str, data: dict, params: dict) -> dict:
    prefix = str(params.get("prefix") or "").strip()
    query: dict[str, str] = {"list-type": "2", "delimiter": "/", "max-keys": str(_PAGE_SIZE)}
    if prefix:
        query["prefix"] = prefix
    resp = _with_client(data, lambda client: client.get(_list_url(base, query)))
    if resp.status_code != 200:
        return {"ok": False, "error": _failure(resp)}

    root = xml_root(resp.text)
    rows: list[dict] = []
    for node in xml_findall(root, "CommonPrefixes"):
        child = child_text(node, "Prefix")
        if child:
            rows.append(_object_row_folder(child))
    for content in xml_findall(root, "Contents"):
        key = child_text(content, "Key")
        if not key or key.endswith("/"):
            continue
        rows.append(_object_row_file(key, child_text(content, "Size"),
                                     child_text(content, "LastModified")))
    where = prefix or "桶根目录"
    return {"ok": True, "rows": rows, "columns": _OBJECT_COLUMNS,
            "row_actions": _OBJECT_ROW_ACTIONS, "total": len(rows),
            "empty_text": f"该前缀下暂无对象（{where}，仅显示前 {_PAGE_SIZE} 个）",
            "has_more": xml_text(root, "IsTruncated").lower() == "true"}


def _run_download_object(base: str, data: dict, params: dict, progress=None, should_cancel=None) -> dict:
    key = str(params.get("key") or "").strip()
    if not key:
        return {"ok": False, "error": "缺少对象 Key"}
    path = str(params.get("path") or "").strip()
    if not path:
        return {"ok": False, "error": "未指定保存路径"}

    def _attempt(client) -> dict:
        with client.stream("GET", _object_url(base, key)) as resp:
            if resp.status_code != 200:
                return {"ok": False, "error": _failure(resp)}
            ok, message = download_with_progress(resp, path, key, progress, should_cancel)
        if not ok:
            return {"ok": False, "error": message}
        return {"ok": True, "output": f"已保存到 {message}"}

    return _with_client(data, _attempt)


def _list_all_objects(client, base: str, prefix: str, limit: int):
    objects: list[tuple[str, int]] = []
    token = ""
    while True:
        query: dict[str, str] = {"list-type": "2", "max-keys": str(_ENUM_PAGE)}
        if prefix:
            query["prefix"] = prefix
        if token:
            query["continuation-token"] = token
        resp = client.get(_list_url(base, query))
        if resp.status_code != 200:
            return objects, False, _failure(resp)
        root = xml_root(resp.text)
        for content in xml_findall(root, "Contents"):
            key = child_text(content, "Key")
            if not key or key.endswith("/"):
                continue
            size_raw = child_text(content, "Size")
            objects.append((key, int(size_raw) if size_raw.isdigit() else 0))
            if len(objects) > limit:
                return objects, True, ""
        if xml_text(root, "IsTruncated").lower() != "true":
            break
        token = xml_text(root, "NextContinuationToken")
        if not token:
            break
    return objects, False, ""


def _zip_entry_name(key: str, prefix: str) -> str | None:
    rel = key[len(prefix):] if prefix and key.startswith(prefix) else key
    rel = rel.lstrip("/")
    parts = rel.split("/")
    if not rel or any(part in ("", ".", "..") for part in parts):
        return None
    return rel


def _run_download_prefix(base: str, data: dict, params: dict, progress=None, should_cancel=None) -> dict:
    path = str(params.get("path") or "").strip()
    if not path:
        return {"ok": False, "error": "未指定保存路径"}
    prefix = str(params.get("prefix") or "").strip()
    if not prefix:
        return {"ok": False, "error": "缺少前缀（文件夹），请在表单里填写，或从对象列表行内点击「打包下载」进入"}

    def _attempt(client) -> dict:
        objects, over_limit, list_error = _list_all_objects(client, base, prefix, MAX_ZIP_OBJECTS)
        if list_error:
            return {"ok": False, "error": list_error}
        if over_limit:
            return {"ok": False, "error": (
                f"「{prefix}」下的对象数超过打包上限（{MAX_ZIP_OBJECTS} 个），已枚举前 "
                f"{MAX_ZIP_OBJECTS + 1} 个即停止。请换一个更小的前缀，或逐个下载。")}
        if not objects:
            return {"ok": False, "error": f"「{prefix}」下没有可下载的对象"}

        total = sum(size for _, size in objects)
        if total > MAX_ZIP_BYTES:
            return {"ok": False, "error": (
                f"「{prefix}」下共 {len(objects)} 个对象、{human_size(total)}，"
                f"超过单次打包上限 {human_size(MAX_ZIP_BYTES)}。请换一个更小的前缀，或逐个下载。")}

        tmpdir = tempfile.mkdtemp(prefix="vault_anon_zip_")
        part_path = path + ".part"
        failed: list[str] = []
        skipped: list[str] = []
        written = 0
        count = len(objects)
        done = 0
        if progress is not None:
            progress(0, count, f"开始打包 {count} 个对象")

        def _fetch(index: int, key: str):
            if should_cancel is not None and should_cancel():
                raise Cancelled
            local = os.path.join(tmpdir, f"{index}.bin")
            try:
                with client.stream("GET", _object_url(base, key)) as resp:
                    if resp.status_code != 200:
                        return key, "", f"{key} (HTTP {resp.status_code})"
                    with open(local, "wb") as handle:
                        for chunk in resp.iter_bytes(65536):
                            if should_cancel is not None and should_cancel():
                                raise Cancelled
                            handle.write(chunk)
            except Cancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                return key, "", f"{key} ({type(exc).__name__})"
            return key, local, ""

        try:
            with ThreadPoolExecutor(max_workers=_ZIP_WORKERS) as pool:
                futures = [pool.submit(_fetch, i, key) for i, (key, _size) in enumerate(objects)]
                with zipfile.ZipFile(part_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                    for future in as_completed(futures):
                        key, local, fetch_error = future.result()
                        if should_cancel is not None and should_cancel():
                            raise Cancelled
                        done += 1
                        if progress is not None:
                            progress(done, count, f"{done}/{count} {key}")
                        if fetch_error:
                            failed.append(fetch_error)
                            continue
                        try:
                            entry = _zip_entry_name(key, prefix)
                            if entry is None:
                                skipped.append(f"{key}（路径不安全）")
                                continue
                            archive.write(local, arcname=entry)
                            written += 1
                        except Exception as exc:  # noqa: BLE001
                            failed.append(f"{key} ({type(exc).__name__})")
                        finally:
                            try:
                                os.remove(local)
                            except OSError:
                                pass
            if written == 0:
                raise RuntimeError("没有对象写入 zip")
            os.replace(part_path, path)
        except Cancelled:
            remove_quietly(part_path)
            return {"ok": False, "error": "打包已取消"}
        except Exception as exc:  # noqa: BLE001
            remove_quietly(part_path)
            detail = f"，失败 {len(failed)} 个" if failed else ""
            return {"ok": False, "error": f"打包失败（{type(exc).__name__}{detail}）"}
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

        lines = [f"已打包 {written} 个对象（{human_size(total)}）到 {path}", f"字节数（未压缩）：{total}"]
        if failed:
            lines.append(f"失败 {len(failed)} 个：" + "；".join(failed[:5]) + ("…" if len(failed) > 5 else ""))
        if skipped:
            lines.append(f"跳过 {len(skipped)} 个：" + "；".join(skipped[:5]) + ("…" if len(skipped) > 5 else ""))
        lines.append(f"zip 内为相对「{prefix}」的路径")
        return {"ok": True, "output": "\n".join(lines)}

    return _with_client(data, _attempt)


# ── 匿名写探测 ────────────────────────────────────────────

def _run_write_probe(base: str, data: dict, params: dict) -> dict:
    """匿名 PUT 一个临时探针对象再 DELETE，判定该桶是否允许未签名写入。

    顺序严格 **PUT →（2xx 才）DELETE**；DELETE 失败时**不算成功**，并把对象完整地址回给用户手动清理
    ——绝不把「写成功删失败」当成成功，也不重试掩盖。
    """
    prefix = str(params.get("prefix") or "").strip().strip("/")
    key = f"{prefix + '/' if prefix else ''}{_PROBE_PREFIX}{uuid.uuid4().hex}"
    url = _object_url(base, key)

    def _attempt(client) -> dict:
        # PUT 的**连接 / TLS 失败要抛出去**（交给 _with_client 决定是否降级重试）——
        # 此时尚未写入任何东西，重试是安全的；若在这里吞掉，就永远走不到 TLS 1.2 重试。
        put = client.request("PUT", url, content=_PROBE_BODY)
        if put.status_code not in (200, 201, 204):
            return {"ok": False, "error": (
                f"不支持匿名写入：PUT 被拒（HTTP {put.status_code}）"
                f"{_error_suffix(put)}")}
        # PUT 成功 → 立刻删除，避免留下残留
        try:
            delete = client.request("DELETE", url)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": (
                f"**该桶允许匿名写入**（PUT 成功），但删除探针时异常（{type(exc).__name__}）。"
                f"请手动删除：{url}")}
        if delete.status_code in (200, 202, 204):
            return {"ok": True, "output": (
                f"**该桶允许匿名写入**（未签名 PUT 成功），探针对象已删除。\n探针键：{key}")}
        return {"ok": False, "error": (
            f"**该桶允许匿名写入**（PUT 成功），但删除失败（HTTP {delete.status_code}）——"
            f"探针对象可能仍在，请手动删除：\n{url}")}

    try:
        return _with_client(data, _attempt)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"写入请求失败：{type(exc).__name__}"}


def _error_suffix(resp) -> str:
    code, message = xml_error(resp.text or "")
    detail = message or code
    return f"：{redact(_short(detail))}" if detail else ""
