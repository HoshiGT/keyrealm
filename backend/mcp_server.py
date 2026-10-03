"""
MCP Server — SSE 传输，Bearer Token 鉴权
端口：8765（自动降级 8766/8767）
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import database as db
from . import plugin_manager
from .crypto import decrypt_dict, hash_token

# 由 api.py 注入
_get_key_cb = None
_event_cb = None

_server_thread: Optional[threading.Thread] = None
_uvicorn_server: Optional[uvicorn.Server] = None
_mcp_app: Optional[FastMCP] = None
_running = False
_port = 8765


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

        # 将 token 记录注入 request.state
        request.state.mcp_token = record
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


# ── MCP 工具定义 ──────────────────────────────────────────

def _build_mcp_app() -> FastMCP:
    mcp = FastMCP(
        "CredentialVault",
        instructions="Credential Vault MCP Server — 管理、验证 LLM API Key 及其他凭据",
    )

    # 用于在工具内获取当前请求的 token 记录
    _current_token: threading.local = threading.local()

    def _require_perm(tool_name: str):
        record = getattr(_current_token, "record", None)
        if record is None or not _check_permission(tool_name, record):
            raise PermissionError(f"权限不足：{tool_name} 未授权")

    # ── vault_list_credentials ────────────────────────────
    @mcp.tool()
    def vault_list_credentials(
        group_id: Optional[int] = None,
        credential_type: Optional[str] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
    ) -> list[dict]:
        """列出凭据（不含敏感字段明文）"""
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
        """创建凭据"""
        from .crypto import encrypt_dict

        key = _get_key()
        if key is None:
            return {"error": "vault_locked", "message": "保险库已锁定"}

        errors = plugin_manager.validate_credential(credential_type, data)
        if errors:
            return {"error": "validation_failed", "details": errors}

        # 分离敏感/非敏感字段
        plugin_record = db.get_plugin_by_type(credential_type)
        sensitive_keys = set()
        if plugin_record:
            for f in plugin_record.get("fields_schema", []):
                if f.get("sensitive"):
                    sensitive_keys.add(f["key"])

        data_plain = {k: v for k, v in data.items() if k not in sensitive_keys}
        encrypted = encrypt_dict(key, data)

        cred = db.create_credential(
            name=name,
            credential_type=credential_type,
            data_encrypted=encrypted,
            data_plain=data_plain,
            group_id=group_id,
        )

        if auto_verify:
            from .scheduler import verify_single_credential
            result = verify_single_credential(cred["id"], trigger_type="auto")
            cred = db.get_credential(cred["id"]) or cred

        _push_event("vault:credentials_changed", {})
        return {"ok": True, "credential": cred}

    # ── vault_update_credential ───────────────────────────
    @mcp.tool()
    def vault_update_credential(
        credential_id: int,
        name: Optional[str] = None,
        group_id: Optional[int] = None,
        data: Optional[dict] = None,
    ) -> dict:
        """修改凭据"""
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
            kwargs["data_encrypted"] = encrypt_dict(key, data)
            kwargs["data_plain"] = {k: v for k, v in data.items() if k not in sensitive_keys}

        updated = db.update_credential(credential_id, **kwargs)
        _push_event("vault:credentials_changed", {})
        return {"ok": True, "credential": updated}

    # ── vault_delete_credential ───────────────────────────
    @mcp.tool()
    def vault_delete_credential(credential_id: int) -> dict:
        """删除凭据"""
        db.delete_credential(credential_id)
        _push_event("vault:credentials_changed", {})
        return {"ok": True}

    # ── vault_verify_credential ───────────────────────────
    @mcp.tool()
    def vault_verify_credential(credential_id: int) -> dict:
        """触发验证单条凭据"""
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
        return db.list_groups()

    # ── vault_create_group ────────────────────────────────
    @mcp.tool()
    def vault_create_group(name: str, description: str = "", color: str = "#409EFF") -> dict:
        """创建分组"""
        group = db.create_group(name, description, color)
        _push_event("vault:credentials_changed", {})
        return {"ok": True, "group": group}

    # ── vault_list_plugins ────────────────────────────────
    @mcp.tool()
    def vault_list_plugins() -> list[dict]:
        """列出已安装插件"""
        return plugin_manager.list_installed_plugins()

    # ── vault_install_plugin ──────────────────────────────
    @mcp.tool()
    def vault_install_plugin(zip_path: str) -> dict:
        """安装插件（.zip 路径）"""
        ok, msg = plugin_manager.install_from_zip(zip_path)
        if ok:
            _push_event("vault:plugin_installed", {"message": msg})
        return {"ok": ok, "message": msg}

    # ── vault_get_secret ──────────────────────────────────
    @mcp.tool()
    def vault_get_secret(credential_id: int, field: Optional[str] = None) -> dict:
        """获取解密后的敏感字段（高危操作）"""
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

    return mcp


# ── SSE 服务启动 ──────────────────────────────────────────

def _make_asgi_app(mcp: FastMCP):
    """将 FastMCP 的 SSE app 包装上 Bearer 鉴权中间件"""
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


def start_server(port: int = 8765) -> tuple[bool, int, str]:
    """启动 MCP SSE 服务（后台线程）"""
    global _server_thread, _uvicorn_server, _mcp_app, _running, _port

    if _running:
        return True, _port, f"http://127.0.0.1:{_port}/sse"

    _mcp_app = _build_mcp_app()
    app = _make_asgi_app(_mcp_app)

    # 尝试端口
    actual_port = port
    for p in [port, port + 1, port + 2]:
        try:
            config = uvicorn.Config(app, host="127.0.0.1", port=p, log_level="warning")
            _uvicorn_server = uvicorn.Server(config)
            # 快速检测端口是否可用
            import socket
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.1)
                if s.connect_ex(("127.0.0.1", p)) == 0:
                    continue  # 端口被占用
            actual_port = p
            break
        except Exception:
            continue

    config = uvicorn.Config(app, host="127.0.0.1", port=actual_port, log_level="warning")
    _uvicorn_server = uvicorn.Server(config)
    _port = actual_port

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

    return _running, _port, f"http://127.0.0.1:{_port}/sse"


def stop_server():
    global _running, _uvicorn_server
    if _uvicorn_server:
        _uvicorn_server.should_exit = True
    _running = False


def is_running() -> bool:
    return _running


def get_port() -> int:
    return _port
