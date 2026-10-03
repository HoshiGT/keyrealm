# 插件开发指南

## 概述

插件是一个 `.zip` 文件，包含 `manifest.json`（元数据）和 `plugin.py`（逻辑）。
用户将 `.zip` 拖入应用窗口即完成安装，无需重启。

---

## 插件包结构

```
my_plugin.zip
├── manifest.json    # 必须：插件元数据与字段定义
└── plugin.py        # 必须：验证逻辑实现
```

---

## manifest.json 规范

```json
{
  "name": "LLM API Key",
  "version": "1.0.0",
  "author": "Your Name",
  "description": "管理和验证各大 LLM 厂商的 API Key",
  "credential_type": "llm_api_key",
  "icon": "🤖",
  "fields": [
    {
      "key": "base_url",
      "label": "Base URL",
      "type": "text",
      "required": true,
      "default": "https://api.openai.com/v1",
      "sensitive": false,
      "placeholder": "https://api.openai.com/v1"
    },
    {
      "key": "api_key",
      "label": "API Key",
      "type": "password",
      "required": true,
      "sensitive": true,
      "placeholder": "sk-..."
    }
  ]
}
```

### 字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | ✅ | 插件显示名称 |
| `version` | string | ✅ | 语义化版本号 |
| `author` | string | ❌ | 作者 |
| `description` | string | ❌ | 插件描述 |
| `credential_type` | string | ✅ | 全局唯一标识符（snake_case），作为数据库中的类型键 |
| `icon` | string | ❌ | Emoji 或图标名，前端展示用 |
| `fields` | array | ✅ | 字段定义列表 |

### 字段类型（fields[].type）

| type | 说明 | 额外属性 |
|------|------|----------|
| `text` | 单行文本 | `placeholder`, `default` |
| `password` | 密码输入框（掩码显示） | `placeholder` |
| `textarea` | 多行文本 | `placeholder`, `rows` |
| `select` | 下拉选择 | `options: string[]` |
| `number` | 数字输入 | `min`, `max`, `step` |
| `url` | URL 输入（带格式校验） | `placeholder`, `default` |
| `checkbox` | 布尔开关 | `default: bool` |

### 字段通用属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `key` | string | 字段键名（snake_case），传给 plugin.py 的字典键 |
| `label` | string | 前端显示标签 |
| `required` | bool | 是否必填（默认 false） |
| `sensitive` | bool | 是否加密存储（true → data_encrypted，false → data_plain） |
| `default` | any | 默认值 |
| `placeholder` | string | 输入提示文字 |

---

## plugin.py 规范

```python
"""
插件必须实现以下接口：
- verify(data: dict) -> VerifyResult
- 可选：validate(data: dict) -> list[str]   # 返回错误列表，空列表表示通过
- 可选：get_display_summary(data: dict) -> str  # 列表中展示的摘要文字
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class VerifyResult:
    """验证结果，由主程序定义，插件直接返回此类型"""
    status: str                  # "valid" | "invalid" | "error"
    message: str = ""            # 人类可读的结果描述
    account_info: Optional[dict] = None  # 可选：账号名、组织、余额等
    # 示例：{"account": "user@example.com", "org": "MyOrg", "balance": "$12.34"}


def validate(data: dict) -> list[str]:
    """
    保存前的字段校验（可选实现）。
    返回错误消息列表，空列表表示校验通过。
    """
    errors = []
    if not data.get("api_key", "").startswith(("sk-", "pk-")):
        errors.append("API Key 格式不正确，应以 sk- 或 pk- 开头")
    return errors


def verify(data: dict) -> VerifyResult:
    """
    核心验证逻辑（必须实现）。
    data 字典的键与 manifest.json 中 fields[].key 对应，值为明文。
    
    注意：此函数在独立线程中调用，可以执行网络请求，但应避免阻塞超过 30 秒。
    """
    import httpx
    
    api_key = data["api_key"]
    base_url = data.get("base_url", "https://api.openai.com/v1").rstrip("/")
    
    try:
        resp = httpx.get(
            f"{base_url}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15.0
        )
        if resp.status_code == 200:
            return VerifyResult(
                status="valid",
                message="API Key 有效",
                account_info={"models_count": len(resp.json().get("data", []))}
            )
        elif resp.status_code == 401:
            return VerifyResult(status="invalid", message="API Key 无效或已过期")
        else:
            return VerifyResult(status="error", message=f"HTTP {resp.status_code}: {resp.text[:200]}")
    except httpx.TimeoutException:
        return VerifyResult(status="error", message="请求超时（15s）")
    except Exception as e:
        return VerifyResult(status="error", message=str(e))


def get_display_summary(data: dict) -> str:
    """
    列表中展示的摘要文字（可选实现）。
    data 为非敏感字段的明文字典。
    """
    vendor = data.get("vendor", "unknown")
    return f"{vendor}"
```

---

## 插件生命周期

```
安装（.zip 拖入）
    │
    ▼ manifest.json 校验（格式、credential_type 唯一性）
    │
    ▼ 解压到 <data_dir>/installed_plugins/<credential_type>/
    │
    ▼ importlib 动态加载 plugin.py
    │
    ▼ 写入 plugins 表
    │
    ▼ 前端刷新，新凭据类型出现在"添加凭据"下拉框
    
使用中
    │
    ▼ 用户添加凭据 → 前端根据 fields_schema 渲染表单
    │
    ▼ 保存 → 调用 plugin.validate()（若实现）→ 加密存储
    │
    ▼ 验证 → 调用 plugin.verify()（独立线程）→ 更新状态
    
卸载
    │
    ▼ 从 plugins 表标记 enabled=0（凭据数据保留）
    │
    ▼ 或完全删除（凭据数据一并删除，需用户二次确认）
```

---

## 开发调试

### 本地开发模式

- **内置插件**：源码在应用根目录 `plugins/<credential_type>/`，这是内置插件的唯一事实来源。
  直接编辑其 `plugin.py` 后重启应用即可（启动时会用 `plugins/` 覆盖重建运行期缓存
  `data/installed_plugins/`），也可在"插件管理"页点击**重新加载**。
- **外部/调试插件**：将插件目录放入 `<data_dir>/installed_plugins/<credential_type>/`，
  修改 `plugin.py` 后在"插件管理"页点击**重新加载**。

### 打包为 .zip

```bash
cd my_plugin_dir
zip -r my_plugin.zip manifest.json plugin.py
```

### 测试脚本

```python
# test_plugin.py
import sys
sys.path.insert(0, "plugins/llm_api_key")
from plugin import verify

result = verify({
    "api_key": "sk-test...",
    "base_url": "https://api.openai.com/v1",
    "vendor": "openai"
})
print(result)
```

---

## 内置插件

项目内置两个插件，随应用分发，位于 `<app>/plugins/` 下：

### LLM API Key（`credential_type: llm_api_key`）

验证 LLM 对话模型 API Key，支持 18 家厂商，采用协议驱动架构：

| 协议 | 验证方式 | 覆盖厂商 |
|------|----------|----------|
| openai_models | `GET {base}/models`，Bearer | OpenAI、DeepSeek、MiniMax、Moonshot、硅基流动、阿里百炼、华为 MaaS、字节豆包、阿里通义、各类 OpenAI 兼容中转 |
| anthropic_models | `GET {base}/models`，`x-api-key` | Anthropic、各类 Anthropic 兼容中转 |
| openai_chat | `POST {base}/chat/completions`（识别欠费） | NVIDIA NIM、智谱 GLM |
| anthropic_chat | `POST {base}/v1/messages`（识别欠费） | 智谱 GLM (Anthropic) |
| openrouter | `GET {base}/auth/key` | OpenRouter |
| gemini | `GET {base}/models?key=` | Google Gemini |
| wenxin | `POST {base}/oauth/2.0/token` | 百度文心 |

余额查询（best-effort）：DeepSeek `/user/balance`、Moonshot `/users/me/balance`，负余额自动判定为「账户欠费」。

### API 服务（`credential_type: api_service`）

验证非 LLM 的第三方 API 服务 Key：

| 服务 | 验证端点 | 验证方式 |
|------|----------|----------|
| Firecrawl | `POST /v1/scrape` | Bearer Token（网页抓取） |
| Tavily | `POST /search` | Bearer Token（搜索） |
| 字节扣子 Coze | `GET /v1/workspaces` | Bearer Token（Agent 平台） |
