"""Pydantic 数据模型"""
from __future__ import annotations
from datetime import datetime
from typing import Optional, Union
from pydantic import BaseModel, Field


# ── 验证结果 ──────────────────────────────────────────────

class VerifyResult(BaseModel):
    status: str                          # valid / invalid / error
    message: str = ""
    account_info: Optional[dict] = None  # 账号名、组织、余额等


# ── 分组 ──────────────────────────────────────────────────

class Group(BaseModel):
    id: int
    name: str
    description: str = ""
    color: str = "#409EFF"
    sort_order: int = 0
    created_at: datetime
    credential_count: int = 0


class GroupCreate(BaseModel):
    name: str
    description: str = ""
    color: str = "#409EFF"


# ── 凭据 ──────────────────────────────────────────────────

class Credential(BaseModel):
    id: int
    name: str
    credential_type: str
    group_id: Optional[int] = None
    group_name: Optional[str] = None
    data_plain: dict = Field(default_factory=dict)
    status: str = "unknown"
    status_detail: dict = Field(default_factory=dict)
    last_verified_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class CredentialCreate(BaseModel):
    name: str
    credential_type: str
    group_id: Optional[int] = None
    data: dict = Field(default_factory=dict)   # 所有字段（敏感+非敏感）
    auto_verify: bool = True


class CredentialUpdate(BaseModel):
    name: Optional[str] = None
    group_id: Optional[int] = None
    data: Optional[dict] = None


# ── 验证历史 ──────────────────────────────────────────────

class VerificationRecord(BaseModel):
    id: int
    credential_id: int
    status: str
    result_data: dict = Field(default_factory=dict)
    error_message: str = ""
    trigger_type: str = "manual"
    duration_ms: int = 0
    verified_at: datetime


# ── 插件 ──────────────────────────────────────────────────

class FieldSchema(BaseModel):
    key: str
    label: str
    type: str = "text"
    required: bool = False
    sensitive: bool = False
    default: Optional[str] = None
    placeholder: Optional[str] = None
    options: Optional[list[str]] = None


class PluginInfo(BaseModel):
    id: int
    name: str
    version: str
    author: str = ""
    description: str = ""
    credential_type: str
    icon: str = "🔑"
    fields_schema: list[FieldSchema] = Field(default_factory=list)
    is_builtin: bool = False
    enabled: bool = True
    installed_at: datetime
    credential_count: int = 0


# ── MCP Token ─────────────────────────────────────────────

class McpToken(BaseModel):
    id: int
    name: str
    token_prefix: str
    tools_allowed: list[str] = Field(default_factory=list)
    created_at: datetime
    last_used_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    revoked: bool = False


class McpTokenCreate(BaseModel):
    name: str
    tools_allowed: list[str] = Field(default_factory=list)  # 空 = 继承全局
    expires_days: Optional[int] = None


class McpTokenCreated(BaseModel):
    """创建成功后返回，token 明文仅此一次"""
    id: int
    name: str
    token: str          # 明文，仅展示一次
    token_prefix: str


# ── MCP 权限 ──────────────────────────────────────────────

class McpPermission(BaseModel):
    tool_name: str
    enabled: bool
    description: str = ""


# ── 定时计划 ──────────────────────────────────────────────

class Schedule(BaseModel):
    id: int
    name: str
    trigger_type: str          # interval / cron
    trigger_args: dict
    target_scope: str = "all"  # all / group / type
    # 多态：scope=="group" 时为分组 ID(int)，scope=="type" 时为凭据类型名(str)
    target_id: Optional[Union[int, str]] = None
    enabled: bool = True
    last_run_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = None
    created_at: datetime


class ScheduleCreate(BaseModel):
    name: str
    trigger_type: str
    trigger_args: dict
    target_scope: str = "all"
    target_id: Optional[Union[int, str]] = None


# ── 应用状态 ──────────────────────────────────────────────

class TrayStatus(BaseModel):
    mcp_running: bool = False
    mcp_port: int = 8765
    active_tokens: int = 0
    is_locked: bool = True


class DashboardStats(BaseModel):
    total: int = 0
    valid: int = 0
    invalid: int = 0
    unknown: int = 0
    error: int = 0
    recent_activity: list[VerificationRecord] = Field(default_factory=list)
