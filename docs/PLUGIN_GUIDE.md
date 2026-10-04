# 插件开发指南

Credential Vault 的插件用于**验证一类凭据的有效性**（如某家 LLM 厂商的 API Key）。
每个服务是一个独立插件，放入 `plugins/` 目录即自动加载，删除即卸载——没有安装器、
没有启停开关、没有"插件管理"界面。

---

## 1. 目录结构

```
plugins/
├── llm/                        # 类别目录（LLM 对话模型）
│   ├── category.json           # 类别元数据：{"name": "LLM 对话模型", "icon": "🤖"}
│   ├── llm_common.py           # 类别共享模块（可选）：协议处理、厂商配置
│   ├── openai/                 # 一个服务 = 一个独立子插件
│   │   ├── manifest.json
│   │   └── plugin.py
│   ├── deepseek/
│   │   ├── manifest.json
│   │   └── plugin.py
│   └── …（共 18 个子插件）
├── api/                        # 类别目录（API 服务）
│   ├── category.json
│   ├── api_common.py
│   ├── firecrawl/
│   ├── tavily/
│   └── coze/
└── my_flat_plugin/             # 也支持根级平铺插件（无类别）
    ├── manifest.json
    └── plugin.py
```

规则：

- 启动时 `sync_plugins()` 扫描 `plugins/` 两层结构：
  - 根级含 `manifest.json` 的目录 → 平铺插件（`category` 为空，前端归入"其他"）；
  - 否则视为类别目录，其下含 `manifest.json` 的子目录为子插件，
    `category.json` 提供类别显示名与图标。
- **一个服务就是一个插件**。删除某个子插件目录不影响同类别其他子插件运行；
  共享模块（`llm_common.py` 等）只是被各子插件 `import` 的普通 Python 文件。
- 名称以 `_` 或 `.` 开头的目录会被跳过（可用于临时禁用）。

---

## 2. manifest.json

```json
{
  "name": "DeepSeek",
  "version": "1.0.0",
  "credential_type": "llm_deepseek",
  "author": "Credential Vault",
  "description": "验证 DeepSeek API Key 有效性",
  "icon": "🐋",
  "fields": [
    {
      "key": "base_url",
      "label": "Base URL",
      "type": "url",
      "required": false,
      "placeholder": "留空使用默认地址",
      "default": "https://api.deepseek.com/v1"
    },
    {
      "key": "api_key",
      "label": "API Key",
      "type": "password",
      "required": true,
      "sensitive": true,
      "placeholder": "sk-..."
    },
    {
      "key": "notes",
      "label": "备注",
      "type": "textarea",
      "required": false
    }
  ]
}
```

| 字段 | 说明 |
|------|------|
| `credential_type` | **插件身份，全局唯一**。它是数据库插件表的 UNIQUE 键、模块缓存键、凭据与插件的关联键。同类插件约定前缀：`llm_<厂商>`、`api_<服务>` |
| `fields[].type` | `text` / `url` / `password` / `textarea` / `select`（配 `options`）/ `number` / `checkbox` |
| `fields[].sensitive` | `true` 的字段加密存储（AES-256-GCM），列表页只显示掩码（`crypto.mask_secret`，形如 `sk-123…cdef`），也参与重复凭据检测 |
| `icon` | 服务显示图标。填 emoji（如 `🐋`）直接使用；也可填插件目录内的图片文件名（如 `"logo.png"`，支持 png/jpg/svg/webp/ico/gif），后端读取为 data URL 返回（`icon_image` 字段），前端优先展示图片，缺省回退 emoji `🔑` |

---

## 3. plugin.py 接口

必须暴露 `verify(data)`；可选 `validate(data)` 与 `get_display_summary(data)`。
`data` 是用户按 `fields` 填写的字典。

```python
"""DeepSeek API Key 验证插件（plugins/llm/ 类别下的独立子插件）。"""
from llm_common import display_summary, validate_common, verify_vendor

VENDOR = "deepseek"


def validate(data):
    return validate_common(VENDOR, data)


def verify(data):
    return verify_vendor(VENDOR, data)


def get_display_summary(data):
    return display_summary(VENDOR, data)
```

要点：

- **共享模块机制**：加载子插件时，框架会临时把插件目录与其类别目录注入
  `sys.path`，因此子插件可以直接 `from llm_common import …`。子插件自身应当
  很薄（上例即完整实现，仅 10 余行），协议逻辑沉淀在类别共享模块中。
- `verify(data)` 的返回值做鸭子类型处理，接受：
  - `VerifyResult` 风格的 dataclass（`status` / `message` / `account_info`）；
  - 普通 `dict`，如 `{"status": "valid", "message": "…", "account_info": {...}}`。
- `status` 取值：`valid`（有效）/ `invalid`（无效、欠费）/ `error`（网络等异常）。
- `account_info` 为可选 dict，验证通过后回传账户信息（如
  `{"balance": "97.82", "currency": "CNY"}`），前端凭据列表的"附加信息"列会展示。
- 插件模块**不应导入 backend 包**，保持可脱离主程序独立运行（便于直接脚本调试）。

---

## 4. 编写一个新插件（以新增 OpenAI 兼容服务为例）

1. 新建目录 `plugins/llm/my_vendor/`。
2. 写 `manifest.json`（`credential_type` 取 `llm_my_vendor`，字段照抄上文结构）。
3. 写薄 `plugin.py`：

```python
"""MyVendor API Key 验证插件。"""
from llm_common import display_summary, validate_common, verify_vendor

VENDOR = "my_vendor"


def validate(data):
    return validate_common(VENDOR, data)


def verify(data):
    return verify_vendor(VENDOR, data)


def get_display_summary(data):
    return display_summary(VENDOR, data)
```

4. 若是 `llm_common` 尚未支持的厂商，在 `llm_common.py` 的厂商配置表中登记
   `my_vendor`（协议、默认 Base URL、可选余额端点），或干脆不走共享模块、在
   `plugin.py` 里自实现 `verify`。
5. 重启应用即自动加载；在"添加凭据"的服务下拉中即可搜索到。

安全约定：

- 声明 `sensitive: true` 的字段全程加密存储，日志与列表页均不出现明文。
- 重复凭据检测会比较敏感字段明文（内存中解密比对），相同 Key 重复添加时前端弹确认。

---

## 5. 内置插件清单

**llm/ 类别（18 个）**：openai、anthropic、deepseek、openrouter、gemini、minimax、
nvidia、moonshot、siliconflow（硅基流动）、dashscope（阿里百炼）、huawei_maas、
glm（智谱）、glm_anthropic、doubao（豆包）、tongyi（通义）、wenxin（文心）、
openai_compatible、anthropic_compatible（两类兼容中转）。

**api/ 类别（3 个）**：firecrawl（网页抓取）、tavily（搜索）、coze（字节扣子）。

---

## 6. 调试

- 直接修改 `plugins/` 下的文件后**重启应用**即可生效（无热重载）。
- 可脱离主程序独立加载插件验证，参考 `tests/test_real_vendors.py`：

```python
import importlib.util, sys
sys.path.insert(0, "plugins/llm")  # 使 from llm_common import … 可用
spec = importlib.util.spec_from_file_location("p", "plugins/llm/deepseek/plugin.py")
mod = importlib.util.module_from_spec(spec)
sys.modules["p"] = mod
spec.loader.exec_module(mod)
print(mod.verify({"base_url": "https://api.deepseek.com/v1", "api_key": "sk-..."}))
```

- 旧版本（zip 安装时代）的 `data/installed_plugins/` 运行期缓存会在启动时由
  `migrate_legacy_installed_plugins()` 一次性迁移/清理，无需手工处理。
