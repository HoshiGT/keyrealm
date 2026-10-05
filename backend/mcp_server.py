"""
MCP Server — SSE 传输，Bearer Token 鉴权
端口：8765（自动降级 8766/8767）
"""
from __future__ import annotations

import asyncio
import threading
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Optional

import uvicorn
from mcp.server.mcpserver import MCPServer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import backup
from . import database as db
from . import plugin_manager
from .crypto import decrypt_dict, encrypt_dict, hash_token

# 由 api.py 注入
_get_key_cb = None
_event_cb = None

# 每个请求的 token 记录，通过 ContextVar 在 ASGI → asyncio.to_thread 链中传播
_current_token_var: ContextVar[Optional[dict]] = ContextVar("_current_token", default=None)

_server_thread: Optional[threading.Thread] = None
_uvicorn_server: Optional[uvicorn.Server] = None
_mcp_app: Optional[MCPServer] = None
_running = False
_port = 8765
_host = "127.0.0.1"


def set_callbacks(get_key_cb, event_cb):
    global _get_key_cb, _event_cb
    _get_key_cb = get_key_cb
    _event_cb = event_cb


def _get_key() -> Optional[bytes]:
    return _get_key_cb() if _get_key_cb else None


def _push_event(name: str, detail: dict):
    if _event_cb:
        try:
            _event_cb(name, detail)
        except Exception:
            pass


# ── Bearer Token 中间件 ────────────────────────────────────

class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # SSE 连接和消息端点需要鉴权
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse({"error": "missing_token", "message": "需要 Bearer Token"}, status_code=401)

        token = auth[7:].strip()
        token_hash = hash_token(token)
        record = db.get_token_by_hash(token_hash)

        if not record:
            return JSONResponse({"error": "invalid_token", "message": "Token 无效或已撤销"}, status_code=401)

        # 检查过期
        expires_at = record.get("expires_at")
        if expires_at:
            try:
                exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if exp < datetime.now(timezone.utc):
                    return JSONResponse({"error": "token_expired", "message": "Token 已过期"}, status_code=401)
            except Exception:
                pass

        # 更新最后使用时间（异步，不阻塞）
        threading.Thread(target=db.touch_token, args=(token_hash,), daemon=True).start()

        # 将 token 记录注入 request.state 与 ContextVar（工具内通过 ContextVar 读取）
        request.state.mcp_token = record
        _current_token_var.set(record)
        return await call_next(request)


# ── 权限检查 ──────────────────────────────────────────────

def _check_permission(tool_name: str, token_record: dict) -> bool:
    """双层权限：全局开关 ∩ Token 范围"""
    if not db.is_tool_enabled(tool_name):
        return False
    tools_allowed = token_record.get("tools_allowed", [])
    if tools_allowed:  # 空列表 = 继承全局
        return tool_name in tools_allowed
    return True


def _require_perm(tool_name: str) -> None:
    """工具内调用：校验当前请求的 Token 是否有权调用该工具，无权则抛出 PermissionError"""
    record = _current_token_var.get()
    if record is None or not _check_permission(tool_name, record):
        raise PermissionError(f"权限不足：{tool_name} 未授权")


# ── MCP 工具定义 ──────────────────────────────────────────

def _build_mcp_app() -> MCPServer:
    mcp = MCPServer(
        "CredentialVault",
        instructions="Credential Vault MCP Server — 管理、验证 LLM API Key 及其他凭据",
    )

    # ── vault_list_credentials ────────────────────────────
    @mcp.tool()
    def vault_list_credentials(
        group_id: Optional[int] = None,
        credential_type: Optional[str] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
    ) -> list[dict]:
        """列出凭据（不含敏感字段明文）"""
        _require_perm("vault_list_credentials")
        creds = db.list_credentials(group_id=group_id, credential_type=credential_type,
                                     status=status, search=search)
        return [
            {
                "id": c["id"],
                "name": c["name"],
                "credential_type": c["credential_type"],
                "group_name": c.get("group_name"),
                "status": c["status"],
                "data_plain": c.get("data_plain", {}),
                "last_verified_at": c.get("last_verified_at"),
            }
            for c in creds
        ]

    # ── vault_get_credential ──────────────────────────────
    @mcp.tool()
    def vault_get_credential(credential_id: int) -> dict:
        """获取单条凭据详情（敏感字段掩码）"""
        _require_perm("vault_get_credential")
        c = db.get_credential(credential_id)
        if not c:
            return {"error": "not_found"}
        return {
            "id": c["id"],
            "name": c["name"],
            "credential_type": c["credential_type"],
            "group_id": c.get("group_id"),
            "group_name": c.get("group_name"),
            "status": c["status"],
            "status_detail": c.get("status_detail", {}),
            "data_plain": c.get("data_plain", {}),
            "last_verified_at": c.get("last_verified_at"),
            "created_at": c.get("created_at"),
            "updated_at": c.get("updated_at"),
        }

    # ── vault_create_credential ───────────────────────────
    @mcp.tool()
    def vault_create_credential(
        name: str,
        credential_type: str,
        data: dict,
        group_id: Optional[int] = None,
        auto_verify: bool = True,
    ) -> dict:
        """创建凭据（与界面共用同一份实现，因此同样会继承类别/插件默认设置）"""
        _require_perm("vault_create_credential")
        # 不要在这里复制一份创建逻辑——否则「表单创建继承默认、MCP 创建不继承」这类
        # 不一致会悄悄出现（历史上就是复制出来的两份，改一处漏一处）。
        from . import api as _api
        result = _api.create_credential(name, credential_type, data, group_id, auto_verify)
        if isinstance(result, dict) and result.get("error"):
            return result
        return {"ok": True, "credential": result}

    # ── vault_update_credential ───────────────────────────
    @mcp.tool()
    def vault_update_credential(
        credential_id: int,
        name: Optional[str] = None,
        group_id: Optional[int] = None,
        data: Optional[dict] = None,
    ) -> dict:
        """修改凭据"""
        _require_perm("vault_update_credential")
        from .crypto import encrypt_dict

        key = _get_key()
        if key is None:
            return {"error": "vault_locked"}

        kwargs: dict[str, Any] = {}
        if name is not None:
            kwargs["name"] = name
        if group_id is not None:
            kwargs["group_id"] = group_id
        if data is not None:
            cred = db.get_credential(credential_id)
            if not cred:
                return {"error": "not_found"}
            plugin_record = db.get_plugin_by_type(cred["credential_type"])
            sensitive_keys = set()
            if plugin_record:
                for f in plugin_record.get("fields_schema", []):
                    if f.get("sensitive"):
                        sensitive_keys.add(f["key"])

            # 防御：敏感字段缺失或为空时保留已存明文，避免部分更新覆盖密钥
            merged = dict(data)
            if sensitive_keys:
                try:
                    existing_data = decrypt_dict(key, cred["data_encrypted"])
                except Exception:
                    existing_data = {}
                for sk in sensitive_keys:
                    if not merged.get(sk) and existing_data.get(sk):
                        merged[sk] = existing_data[sk]

            kwargs["data_encrypted"] = encrypt_dict(key, merged)
            kwargs["data_plain"] = {k: v for k, v in merged.items() if k not in sensitive_keys}

        updated = db.update_credential(credential_id, **kwargs)
        _push_event("vault:credentials_changed", {})
        return {"ok": True, "credential": updated}

    # ── vault_delete_credential ───────────────────────────
    @mcp.tool()
    def vault_delete_credential(credential_id: int) -> dict:
        """删除凭据"""
        _require_perm("vault_delete_credential")
        db.delete_credential(credential_id)
        _push_event("vault:credentials_changed", {})
        return {"ok": True}

    # ── vault_verify_credential ───────────────────────────
    @mcp.tool()
    def vault_verify_credential(credential_id: int) -> dict:
        """触发验证单条凭据"""
        _require_perm("vault_verify_credential")
        key = _get_key()
        if key is None:
            return {"error": "vault_locked"}
        from .scheduler import verify_single_credential
        return verify_single_credential(credential_id, trigger_type="mcp")

    # ── vault_batch_verify ────────────────────────────────
    @mcp.tool()
    def vault_batch_verify(
        scope: str = "all",
        group_id: Optional[int] = None,
        credential_type: Optional[str] = None,
    ) -> dict:
        """批量验证（异步执行，进度通过事件推送）"""
        _require_perm("vault_batch_verify")
        key = _get_key()
        if key is None:
            return {"error": "vault_locked"}
        from .scheduler import batch_verify
        batch_verify(scope=scope, group_id=group_id, credential_type=credential_type,
                     trigger_type="mcp")
        return {"ok": True, "message": "批量验证已启动"}

    # ── vault_list_groups ─────────────────────────────────
    @mcp.tool()
    def vault_list_groups() -> list[dict]:
        """列出分组"""
        _require_perm("vault_list_groups")
        return db.list_groups()

    # ── vault_create_group ────────────────────────────────
    @mcp.tool()
    def vault_create_group(name: str, description: str = "", color: str = "#409EFF") -> dict:
        """创建分组"""
        _require_perm("vault_create_group")
        group = db.create_group(name, description, color)
        _push_event("vault:credentials_changed", {})
        return {"ok": True, "group": group}

    # ── vault_list_plugins ────────────────────────────────
    @mcp.tool()
    def vault_list_plugins() -> list[dict]:
        """列出已安装插件"""
        _require_perm("vault_list_plugins")
        return plugin_manager.list_installed_plugins()

    # ── vault_get_secret ──────────────────────────────────
    @mcp.tool()
    def vault_get_secret(credential_id: int, field: Optional[str] = None) -> dict:
        """获取解密后的敏感字段（高危操作）"""
        _require_perm("vault_get_secret")
        key = _get_key()
        if key is None:
            return {"error": "vault_locked"}
        cred = db.get_credential(credential_id)
        if not cred:
            return {"error": "not_found"}
        try:
            data = decrypt_dict(key, cred["data_encrypted"])
        except Exception as e:
            return {"error": "decrypt_failed", "message": str(e)}
        if field:
            return {"field": field, "value": data.get(field)}
        return {"data": data}

    # ── vault_export_backup ───────────────────────────────
    @mcp.tool()
    def vault_export_backup(
        scope: str = "all",
        encrypted: bool = True,
        credential_ids: Optional[list] = None,
        path: Optional[str] = None,
    ) -> dict:
        """导出备份（触及整库数据，高危）。

        - `scope`：`all` = 凭据 + 分组 + 偏好设置；`credentials` = 仅凭据
        - `encrypted`：True 时敏感字段保持密文（只能用同一主密码导入）；
          **False 会把密钥明文写出**，仅在明确需要跨主密码迁移时使用
        - `credential_ids`：只导出这些凭据 id（省略 = 全部）
        - `path`：写入该文件；**省略则把备份文档放在返回值的 `document` 字段里**
          （便于直接交给 `vault_import_backup` 或由 Agent 自行保存）
        """
        _require_perm("vault_export_backup")
        from . import api as _api

        built = _api.build_backup_doc(scope, bool(encrypted), credential_ids)
        if built.get("error") or built.get("ok") is False:
            return built
        doc = built["document"]

        result: dict = {
            "ok": True,
            "count": doc["counts"]["credentials"],
            "scope": scope,
            "encrypted": bool(encrypted),
        }
        if path:
            try:
                backup.write_backup_file(path, doc)
            except Exception as e:
                return {"ok": False, "message": f"写入失败: {e}"}
            result["path"] = path
        else:
            result["document"] = doc
        return result

    # ── vault_import_backup ───────────────────────────────
    @mcp.tool()
    def vault_import_backup(
        document: Optional[dict] = None,
        path: Optional[str] = None,
        dedupe: bool = True,
    ) -> dict:
        """导入备份（合并入库，高危：会新增凭据、可能覆盖偏好设置）。

        `document`（备份 JSON 对象）与 `path`（备份文件路径）**二选一**。
        `dedupe=True` 时「同类型 + 敏感值相同」的凭据会跳过；插件未安装的条目也会跳过。
        加密备份需与当前主密码一致，否则报错。
        """
        _require_perm("vault_import_backup")
        from . import api as _api

        if document is None and not path:
            return {"ok": False, "message": "需要提供 document 或 path"}
        doc = document
        if doc is None:
            try:
                doc = backup.read_backup_file(path)
            except Exception as e:
                return {"ok": False, "message": f"读取失败: {e}"}
        if not isinstance(doc, dict):
            return {"ok": False, "message": "备份内容必须是 JSON 对象"}

        result = _api.apply_backup_doc(doc, dedupe=bool(dedupe))
        if result.get("ok"):
            result["source"] = path or "document"
        return result

    return mcp


# ── SSE 服务启动 ──────────────────────────────────────────

def _make_asgi_app(mcp: MCPServer):
    """将 MCPServer 的 SSE app 包装上 Bearer 鉴权中间件"""
    from starlette.applications import Starlette
    from starlette.routing import Mount

    sse_app = mcp.sse_app()
    # 在 sse_app 外包一层中间件
    from starlette.middleware import Middleware as MW
    wrapped = Starlette(
        routes=[Mount("/", app=sse_app)],
        middleware=[MW(BearerAuthMiddleware)],
    )
    return wrapped


def _port_in_use(host: str, port: int) -> bool:
    """目标端口是否已有服务在监听（能连上就算被占）。"""
    import socket

    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def _url() -> str:
    return f"http://{_host}:{_port}/sse"


def start_server(port: int = 8765, host: str = "127.0.0.1") -> tuple[bool, int, str]:
    """在 ``host:port`` 启动 MCP SSE 服务（后台线程）。

    **端口被占用时直接失败**，不再自动退到 +1/+2 —— 端口是用户显式配的，
    悄悄换一个会让人对着一个「配了 9000 却在 9001」的界面发懵。
    """
    global _server_thread, _uvicorn_server, _mcp_app, _port, _host

    if _running:
        return True, _port, _url()

    if _port_in_use(host, port):
        return False, port, ""

    _mcp_app = _build_mcp_app()
    app = _make_asgi_app(_mcp_app)
    _uvicorn_server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    _port = port
    _host = host

    def _run():
        global _running
        _running = True
        asyncio.run(_uvicorn_server.serve())
        _running = False

    _server_thread = threading.Thread(target=_run, daemon=True)
    _server_thread.start()

    # 等待启动
    for _ in range(20):
        time.sleep(0.1)
        if _running:
            break

    return _running, _port, _url()


def stop_server():
    global _running
    if _uvicorn_server:
        _uvicorn_server.should_exit = True
    _running = False


def is_running() -> bool:
    return _running


def get_port() -> int:
    return _port


def get_host() -> str:
    return _host


def get_url() -> str:
    """MCP 服务地址（与界面、客户端配置里展示的保持一致）。"""
    return _url()
