# 插件开发指南

Credential Vault 的插件用于**验证一类凭据的有效性**（如某家 LLM 厂商的 API Key）。
每个服务是一个独立插件，放入 `plugins/` 目录即自动加载，删除即卸载——没有安装器、
没有启停开关、没有"插件管理"界面。

---

## 1. 目录结构

```
plugins/
├── llm/                        # 类别目录（LLM 模型）
│   ├── category.json           # 类别元数据：{"name": "LLM 模型", "icon": "🤖"}
│   ├── llm_common.py           # 类别共享模块：底层通用工具 + 子插件原型接口
│   ├── llm_protocols.py        # 协议级工具（探针 / 简单对话等），供子插件按需复用
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
├── dns/                        # 类别目录（域名解析）
│   ├── category.json
│   ├── dns_common.py
│   ├── dnspod/
│   ├── duckdns/
│   └── godaddy/
├── dev/                        # 类别目录（开发者平台）
│   ├── category.json
│   ├── dev_common.py
│   ├── github/
│   ├── huggingface/
│   └── wandb/
├── im/                         # 类别目录（即时通讯）
│   ├── category.json
│   ├── im_common.py
│   ├── feishu/
│   ├── dingtalk/
│   ├── telegram/
│   ├── discord/
│   ├── qq_bot/
│   └── wechat_ilink/
├── cloud/                      # 类别目录（云服务商）
│   ├── category.json
│   ├── cloud_common.py
│   ├── aliyun/
│   ├── tencentcloud/
│   └── cloudflare/
├── exchange/                   # 类别目录（交易所）
│   ├── category.json
│   ├── exchange_common.py
│   ├── ht/
│   ├── okx/
│   └── binance/
├── mail/                       # 类别目录（邮箱）
│   ├── category.json
│   ├── mail_common.py
│   └── smtp/
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
  共享模块（`llm_common.py` 等）只是被各子插件 `import` 的普通 Python 文件，
  **只放通用工具**（结果类型、HTTP 客户端、解析/兜底辅助），具体验证逻辑在各子插件内。
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
  "batch_verify": true,
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
| `batch_verify` | 是否参与批量验证，默认 `true`。**无法自动化验证或有写副作用**的插件应设为 `false`（如 `duckdns`、`wechat_ilink`、`ht`）：前端在选中该类型时禁用「批量验证」按钮，只能逐条手动验证；后端批量时也会跳过这些类型并在结果里统计「跳过 N」 |

---

## 2.1 验证代理

「系统设置 → 网络与代理」可配置验证代理（`http` / `https` / `socks5`），持久化到 settings 表。
应用启动时把它写入环境变量 `VAULT_PROXY`，各插件在发起请求时读取该变量（见 `plugins/<类别>/<类别>_common.py` 的 `make_client()`）。
解析优先级：`VAULT_PROXY` > `ALL_PROXY`/`all_proxy` > `HTTPS_PROXY`/`https_proxy`；
若 `VAULT_PROXY` 由外部环境显式设置，则它优先于界面设置。socks5 需 `httpx[socks]`（`socksio`），已在 `requirements.txt` 声明。
注意：SMTP 插件（`mail_smtp`）走标准库 `smtplib`，不受该代理影响。

---

## 2.2 附加信息（account_info）

`verify` 返回的 `account_info` 会显示在凭据列表的「附加信息」列，前端按「标签 值」渲染。约定：

| 场景 | account_info 写法 | 展示效果 |
|------|-------------------|----------|
| 余额 | `{"balance": 12.34, "currency": "CNY"}` | 余额 12.34 CNY（保留 2 位小数） |
| 使用率（plan/额度制） | `{"usage_rate": 0.52}` | 使用率 52%（已用 ÷ 总额度，按服务适配） |
| 其它可读字段 | 如 `organization` / `domain_count` / `account_id` | `组织 Xxx` / `域名数 1` / `账号 ID …` |

- **LLM 类**：附加信息只展示**余额**；无余额概念的 plan 账号展示**使用率**。余额由 `llm_common` 按厂商 `balance_url`/`balance_kind` best-effort 查询——目前 DeepSeek / Moonshot 有余额接口，OpenRouter 用 `/credits` 计算余额与使用率；OpenAI / Anthropic / Gemini / 智谱 / 中转等多数无公开余额接口，附加信息为空。
- 无信息量的字段（`object` / `type` / `auth_mode` / 原始时间戳等）不展示，前端 `extraInfo()` 统一处理。

---

## 3. plugin.py 接口

必须暴露 `verify(data)`；可选 `validate(data)` 与 `get_display_summary(data)`。
`data` 是用户按 `fields` 填写的字典。

**每个子插件自带完整实现**，只从 `<类别>_common.py` 引用通用工具；服务之间允许重复代码（刻意为之）。

```python
"""MyVendor 验证插件（plugins/llm/my_vendor/，自包含实现）。"""
from llm_common import VerifyResult, guarded, make_client, safe_json

DEFAULT_BASE_URL = "https://api.myvendor.com/v1"


def validate(data):
    return [] if (data or {}).get("api_key") else ["API Key 不能为空"]


def _verify(data) -> VerifyResult:
    if not data.get("api_key"):
        return VerifyResult(status="invalid", message="API Key 为空")
    base = (data.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    with make_client() as client:
        resp = client.get(f"{base}/models", headers={"Authorization": f"Bearer {data['api_key']}"})
    if resp.status_code == 200:
        return VerifyResult(status="valid", message="凭证有效", account_info=None)
    if resp.status_code in (401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {resp.status_code})")
    return VerifyResult(status="error", message=f"无法确认凭证状态 (HTTP {resp.status_code})")


def verify(data) -> VerifyResult:
    return guarded(lambda: _verify(data or {}))


def get_display_summary(data):
    return "MyVendor"
```

要点：

- **`<类别>_common.py` 只放底层通用工具 + 原型接口**：`VerifyResult`、`TIMEOUT`、`log`、`proxy`、
  `make_client`、`safe_json`、`body_snippet`、`guarded`，以及子插件应实现的
  `validate`/`verify`/`get_display_summary`/`actions`/`run_action` 原型声明。
  **协议级、跨服务完全相同的工具**（如 `llm_protocols.py` 的 `probe_*` 探针与
  `chat_completion`）另建**独立工具模块**，由需要的子插件按需引用；
  **厂商/服务专属逻辑**（端点、字段、余额解析、签名等）写在各子插件自己的 `plugin.py` 里，
  服务之间允许重复（刻意为之）。加载子插件时框架会把插件目录与类别目录注入 `sys.path`。
- **端点、字段、判定、余额、签名等具体逻辑写在各子插件自己的 `plugin.py` 里**，
  **新增服务插件无需修改任何上层/`common` 文件**——照抄同级子插件即可。
- `verify` 用 `guarded(...)` 包住，保证不抛异常；异常只暴露类型名（有些服务密钥在
  URL/query 中，透出异常文本会把密钥写进凭据记录）。
- `verify(data)` 的返回值做鸭子类型处理，接受：
  - `VerifyResult` 风格的 dataclass（`status` / `message` / `account_info`）；
  - 普通 `dict`，如 `{"status": "valid", "message": "…", "account_info": {...}}`。
- `status` 取值：`valid`（有效）/ `invalid`（凭据无效）/ `error`（网络等异常）。
  **欠费（余额不足/负余额/402/限流欠费）不算失效**：仍判 `valid`，把欠费原因写进 `message`、
  负余额写进 `account_info`（前端「附加信息」会展示余额）。
- `account_info` 为可选 dict，验证通过后回传账户信息（见「附加信息」约定），前端凭据列表会展示。
- 插件模块**不应导入 backend 包**，保持可脱离主程序独立运行（便于直接脚本调试）。

### 3.1 可选「简易操作」（actions / run_action）

插件可选实现两个函数，为凭据提供额外操作（前端凭据行的「操作」按钮）：

- `actions(data) -> list[dict]`：声明可用操作，每项形如
  `{"id": "chat", "name": "简单对话", "kind": "chat", "model": "<默认模型>"}`；
  返回空列表表示无操作（前端不显示按钮）。
- `run_action(action_id, params, data) -> dict`：执行操作，返回
  `{"ok": True, "output": "..."}` 或 `{"ok": False, "error": "..."}`。

例：LLM 厂商插件复用 `llm_protocols.chat_completion(...)` 提供「简单对话」（`kind: "chat"`）；
DNS 插件提供「查看/修改解析记录」、SMTP 插件提供「查看收件箱」（`kind: "form"`）。

**操作项（action）字段**：

| 字段 | 说明 |
|------|------|
| `id` / `name` | 操作标识与显示名（`run_action` 按 id 分发） |
| `kind` | 前端渲染方式：`chat`（对话气泡）或 `form`（动态表单 + 文本输出） |
| `fields` | `kind=form` 时的输入项：`[{key,label,type(text/password/select/textarea),default,placeholder,options,required}]` |
| `submit` | 提交按钮文案（默认「运行」） |
| `model` | `kind=chat` 时的默认模型（可编辑） |

`run_action(action_id, params, data)` 中 `params` 即用户在表单里填的值（或对话的 `{model, messages}`），
返回 `{"ok": True, "output": "多行文本"}` 或 `{"ok": False, "error": "..."}`。
**必须在整体 try/except 内，绝不抛异常；输出里不得回显密钥/密码/token。**

可选再实现 `list_models(data) -> list`：返回该凭据可用的模型名列表。
LLM「简单对话」会据此把模型输入框变成**可搜索下拉**（失败/无权限时回退为手动输入）。

还可实现 `stream_action(action_id, params, data, on_delta) -> dict`：**流式**执行操作，
每收到一段文本调用 `on_delta(text)`，返回结构同 `run_action`（含 `output` 全文）。
LLM「简单对话」据此逐段显示；未实现时后端自动回退到 `run_action`（一次性返回）。

---

## 4. 编写一个新插件（以新增 OpenAI 兼容服务为例）

1. 新建目录 `plugins/llm/my_vendor/`。
2. 写 `manifest.json`（`credential_type` 取 `llm_my_vendor`，字段照抄上文结构）。
3. 写 `plugin.py` —— **照抄同类别下任一已有子插件**（如 `plugins/llm/openai_compatible/plugin.py`），
   端点、判定、余额、`validate`、`get_display_summary` 全部自带，只从 `llm_common`
   引用通用工具：

```python
"""MyVendor 验证插件（自包含）。"""
from llm_common import VerifyResult, guarded, make_client

DEFAULT_BASE_URL = "https://api.myvendor.com/v1"


def validate(data):
    return []


def _verify(data) -> VerifyResult:
    ...


def verify(data) -> VerifyResult:
    return guarded(lambda: _verify(data or {}))


def get_display_summary(data):
    return "MyVendor"
```

4. **无需修改 `llm_common.py` 或任何上层代码**——共享模块只提供通用工具，
   厂商专属逻辑都在你自己的 `plugin.py` 里（服务之间允许重复）。
5. 重启应用即自动加载；在"添加凭据"的服务下拉中即可搜索到。

安全约定：

- 声明 `sensitive: true` 的字段全程加密存储，日志与列表页均不出现明文。
- 重复凭据检测会比较敏感字段明文（内存中解密比对），相同 Key 重复添加时前端弹确认。

---

## 5. 插件清单

**llm/ 类别（18 个）**：openai、anthropic、deepseek、openrouter、gemini、minimax、
nvidia、moonshot、siliconflow（硅基流动）、dashscope（阿里百炼）、huawei_maas、
glm（智谱）、glm_anthropic、doubao（豆包）、tongyi（通义）、wenxin（文心）、
openai_compatible、anthropic_compatible（两类兼容中转）。

**api/ 类别（3 个）**：firecrawl（网页抓取）、tavily（搜索）、coze（字节扣子）。

**dns/ 类别（3 个）**：dnspod、duckdns、godaddy。

**dev/ 类别（3 个）**：github（令牌）、huggingface、wandb（Weights & Biases）。

**im/ 类别（6 个）**：feishu（飞书）、dingtalk（钉钉）、telegram、discord（Webhook）、
qq_bot（QQ 频道机器人）、wechat_ilink（微信 iLink Bot）。

**cloud/ 类别（3 个）**：aliyun（阿里云 AK/SK）、tencentcloud（腾讯云 SecretId/Key）、
cloudflare（API Token 或 Global API Key）。

**exchange/ 类别（3 个）**：ht（华泰证券）、okx、binance。

**mail/ 类别（1 个）**：smtp（通用 SMTP，覆盖 Gmail / QQ / 163 / 126 等）。

> 说明：`duckdns` 的校验本身是写操作（先写入 `1.1.1.1` 确认生效再还原原值），
> `wechat_ilink` 与 `ht` 无公开校验接口——三者均标记 `batch_verify: false`，只能手动单个验证。

### 内置「简易操作」一览

凭据齐全时，凭据行会出现「操作」按钮（见 §3.1）。当前内置操作：

- **llm**：全部 18 个厂商 —— 简单对话
- **dns**：dnspod（查看解析记录、新增/修改解析记录）、godaddy（查看域名解析记录、查看域名列表）、
  duckdns（查看当前解析）
- **cloud**：cloudflare（查看站点、查看/修改 DNS 记录）、aliyun / tencentcloud（查看调用者身份）
- **dev**：github（查看账号信息、查看我的仓库）、huggingface / wandb（查看账号信息）
- **im**：telegram（查看机器人信息、发送消息）、discord（查看 Webhook 信息）
- **exchange**：okx（查看账户余额）、binance（查看账户信息、查看交易所时间）
- **api**：coze（查看工作区）、tavily（执行搜索）、firecrawl（抓取网页）
- **mail**：smtp（查看收件箱，IMAP 只读）

> 写操作（名称中已标注，如「新增/修改解析记录」「发送消息」）会真实改动远端数据，请谨慎使用。

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

- 批量真实凭据测试：`data/real_keys.json`（含真实密钥，已 `.gitignore`）中每条可携带任意字段的
  `data` 字典；运行 `python tests/test_real_vendors.py` 会跨全部类别自动发现插件并逐一验证。
  常用开关：
  - `ONLY_NEW=1`：仅验证新增类别（dns / dev / im / cloud / exchange / mail）；
  - `VAULT_PROXY=socks5://127.0.0.1:10808`：让校验走代理（telegram / discord / huggingface 等被墙厂商必需）；
  - 条目 `expected` 取值：`valid` / `invalid` / `overdue`（按 invalid 判定）/ `ip_restricted` / `skip`。
    其中 `expected: "skip"` 的条目**不会调用 verify**（用于有写副作用或无校验接口的类型）。
