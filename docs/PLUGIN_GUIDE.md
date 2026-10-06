# 插件开发指南

Credential Vault 的插件用于**验证一类凭据的有效性**（如某家 LLM 厂商的 API Key）。
每个服务是一个独立插件，放入 `plugins/` 目录即自动加载，删除即卸载——没有安装器、
没有启停开关（「插件管理」只负责**删除插件**与**从内置源恢复/添加**，本身不带启停开关）。

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
│   └── …（共 19 个子插件）
├── api/                        # 类别目录（API 服务）
│   ├── category.json
│   ├── api_common.py
│   ├── firecrawl/
│   ├── tavily/
│   ├── metaso/
│   ├── coze/
│   ├── dify/
│   ├── app_secret/
│   └── gateway/
├── dns/                        # 类别目录（域名解析）
│   ├── category.json
│   ├── dns_common.py
│   ├── dnspod/
│   ├── duckdns/
│   ├── godaddy/
│   └── cloudflare/
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
│   ├── wechat_ilink/
│   ├── telegram_apihash/
│   └── webhook/
├── cloud/                      # 类别目录（云服务商）
│   ├── category.json
│   ├── cloud_common.py
│   ├── google_sa.py            # 协议级工具：服务账号 JWT 换 token（gcp / firebase 共用）
│   ├── aliyun/
│   ├── tencentcloud/
│   ├── aws/
│   ├── gcp/
│   ├── huaweicloud/
│   └── firebase/
├── exchange/                   # 类别目录（交易所）
│   ├── category.json
│   ├── exchange_common.py
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

### 1.1 `plugins_optin/` —— 默认不启用的插件源

与 `plugins/` **完全同构**（`<类别>/<服务>/{manifest.json,plugin.py}`，`category.json` 也有副本），
但 `sync_plugins()` **不扫它**，所以放着也不生效。它只让插件在「插件管理 → 可添加」里露一面，
用户点「添加」才被复制进 `plugins/` —— 与从 `plugins_builtin/` 恢复被删插件**同一条路径**
（见 `backend/plugin_manager.py` 的 `ADDABLE_SOURCE_DIRS`）。

**用途**：放**缺乏实例验证**的新插件。它们照现有类型与服务仿写，但端点是照官方文档写的、
判定阈值是照同类插件推的，只跑过阴性对照、**没拿真凭据跑过阳性验证**，因此不随包上架。
等用真凭据验过（**乱写的凭据必须判 `invalid`**）再由人工把目录**挪进** `plugins/`，
那时才成为「默认启用项」——`plugins/` 始终是插件的唯一事实来源。

**当前共 34 个**（8 个类别，2026-10-06 一次性启发式新增）：

| 类别 | 子插件 |
|------|--------|
| `api`（7） | airtable、censys、fofa、notion、shodan、stripe、virustotal |
| `cloud`（3） | azure_sp（Entra 服务主体）、backblaze_b2、digitalocean |
| `dev`（3） | gitee、npm、vercel |
| `dns`（2） | namecheap、porkbun |
| `exchange`（4） | bitget、bybit、gate、kucoin |
| `im`（3） | line、slack、wecom（企业微信） |
| `llm`（7） | azure_openai、cerebras、groq、hunyuan、mistral、stepfun、xai |
| `mail`（5） | brevo、mailgun、postmark、resend、sendgrid |

> 这批**只做过离线契约冒烟**（`tmp/smoke_optin_contract.py`：就地加载 + 有 verify/validate +
> 参考链接不外泄）与**阴性对照**（`tmp/smoke_optin_negative.py`：乱写凭据必须判 `invalid`／
> 不报错）——**没有拿真凭据跑过阳性验证**，所以仍留在 `plugins_optin/`。
> 缺的正是「真凭据能判 valid」这一半。
>
> 阴性对照 2026-10-06 用代理跑过（34 个逐一出网）：**31 个**判 `invalid`，**无一被误判为
> `valid`**；余下 **3 个**落在 `error` —— `dns/namecheap`（假 ClientIp 被拒：Namecheap 要求
> 调用 IP 加白名单，没走到密钥校验）、`exchange/bybit`（HTTP 403 地域限制）、
> `llm/azure_openai`（本就无默认端点，必须自填）。这 3 个的「乱写必须判 invalid」
> **未被真正行使**，等有实例时补跑。

约定：

- **只放已存在类别下的服务目录**，不要在这里新开类别（新类别要连带克隆一份 `<类别>_common.py`）。
- **这里不放 `<类别>_common.py`**：类别共享模块仍由 `plugins/<类别>/` 提供。插件被「添加」进
  `plugins/` 之后，同层的 `from api_common import …` 才解析得到；**直接就地加载会 ImportError，
  这是预期的**——它本就不该就地跑。
- 发行包会把本目录整体复制到 `release/plugins_optin/`（见 `scripts/build.py`）；`CredentialVault.spec`
  的标准库 hiddenimports 扫描**同时覆盖这里**——插件是运行时才复制进 `plugins/` 的，漏扫会导致
  用户装完一点就 `ModuleNotFoundError`。

详见 `plugins_optin/README.md`。

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
| `fields[].type` | `text` / `url` / `password` / `textarea` / `select`（配 `options`）/ `number` / `checkbox` / `model`（**可下拉可自由输入**的模型名，候选来自 `list_models`，取不到也能手打） |
| `fields[].options` | 仅 `select`。两种写法都行：纯字符串数组 `["openai","anthropic"]`（值与显示都用它）；或对象数组 `[{"value":"chatgpt","label":"ChatGPT 登录态"}]`（**显示 label、存 value**）。前端统一归一（`utils/format.normalizeOptions`），需要给用户看中文时用对象写法，别把中文当值存库 |
| `fields[].show_if` | 可选。**条件显隐**：`{"另一个字段key": 值 或 [值,…]}`，只有那个字段的当前值命中时才渲染本字段。用于「同厂商不同模式」——例如 `openai` 的 `auth_mode` 选 `api_key` 时只显示 API Key 那组、选 `chatgpt` 时只显示登录态那组。前端按当前表单值实时求值（`utils/format.visibleFields`），**保存时未命中字段的值不提交**（免得切模式后另一组的密钥还留在库里）。 |
| `fields[].preset` | 可选，是否允许作为「类别/插件默认设置」被预设。**默认允许**（只要非敏感）；填 `false` 可排除该字段。**敏感字段永远不可预设**（默认值表是明文列） |
| `fields[].hint` | 可选，字段下方的一行灰字说明。用于解释开关或高级项（如 LLM 的「余额未知时发一次对话探测」），前端 `CredentialForm.vue` 渲染为 `.field-hint` |
| `fields[].sensitive` | `true` 的字段加密存储（AES-256-GCM），列表页只显示掩码（`crypto.mask_secret`，形如 `sk-123…cdef`），也参与重复凭据检测 |
| `icon` | 服务显示图标。填 emoji（如 `🐋`）直接使用；也可填插件目录内的图片文件名（如 `"logo.png"`，支持 png/jpg/svg/webp/ico/gif），后端读取为 data URL 返回（`icon_image` 字段），前端优先展示图片，缺省回退 emoji `🔑` |
| `batch_verify` | 是否参与批量验证，默认 `true`。**有写副作用**的插件应设为 `false`（如 `duckdns`——它的校验本身就是一次写解析）：前端在选中该类型时禁用「批量验证」按钮，只能逐条手动验证；后端批量时也会跳过这些类型并在结果里统计「跳过 N」 |

### 字段输入约定（新增插件必守）

**凭据输入一律尽量单行**：一个字段一个值，不要求用户「粘贴整份配置」。

- **能拆就拆**：邮箱 / 私钥 / 项目 ID 各占一格，别用一个大 `textarea` 收一坨。
  硬约束来自前端渲染优先级——`CredentialForm.vue` 按 `text||url` → `password||sensitive` → `textarea`
  顺序判定，**`sensitive` 压过 `textarea`**：声明成 `textarea` 的敏感字段实际渲染的是**单行密码框**，
  「多行」与「加密存储」不可兼得。所以声明要与渲染一致：

  ```json
  { "key": "session_string", "type": "text", "sensitive": true }    // 对：单行 + 加密
  { "key": "session_string", "type": "textarea", "sensitive": true } // 错：声明与渲染不一致
  ```

- **不接收整段 JSON**：服务账号、各类 SDK 的 `*.json` 一律**取关键字段扁平化**，每个值单独一栏，
  由插件拼回协议要的形状。范例：`cloud/google_sa.py` 只收 `client_email` + `private_key`，
  自己还原成 PEM 再签 JWT。整段 JSON 塞一栏的代价：常量与展示字段跟着落库、与相邻字段重复
  且可能对不上、报错时说不清是哪个子字段错了。
- **单行框吃换行**：多行内容粘进单行框会被压成一行（PEM 私钥尤其明显），插件侧要**自己还原**，
  并在 `hint` 里写明。范例 `google_sa.normalize_pem`：先抹掉全部空白再按 64 列折行，于是
  「原样多行 / 压成一行 / 带字面 `\n` / CRLF」四种粘贴形态收敛到同一个结果；同时校验
  「私钥之外不该再有内容」，避免把整段 JSON 顺手存进来。
- **例外**：`notes`（备注）这类自由文本仍用 `textarea`——它不是凭据，没有加密与换行的约束。

---

## 2.1 验证代理

「系统设置 → 网络与代理」可配置验证代理（`http` / `https` / `socks5`），持久化到 settings 表。
应用启动时把它写入环境变量 `VAULT_PROXY`，各插件在发起请求时读取该变量（见 `plugins/<类别>/<类别>_common.py` 的 `make_client()`）。
解析优先级：`VAULT_PROXY` > `ALL_PROXY`/`all_proxy` > `HTTPS_PROXY`/`https_proxy`；
若 `VAULT_PROXY` 由外部环境显式设置，则它优先于界面设置。socks5 需 `httpx[socks]`（`socksio`），已在 `requirements.txt` 声明。

**多代理与「按调用生效」的覆盖值**：代理现在可以配多个（各自取名，存 `proxies` 表）。
解析按优先级逐层找：**本条凭据 > 该插件预设 > 所属类别预设 > 全局默认**；
「跟随上层设置」= 本层不指定、继续往上找（改上层预设，已有凭据会跟着变）。
批量验证是并发的，所以不能靠改环境变量——
框架用 `backend/vault_proxy_ctx.py` 提供一条**线程隔离**的覆盖通道，启动时以固定别名注册进
`sys.modules["vault_proxy_ctx"]`。各 `<类别>_common.py` 的 `proxy()` 会**先查它**：

```python
def proxy():
    ctx = sys.modules.get("vault_proxy_ctx")     # 脱离主程序单独跑时取不到 → 退回环境变量
    if ctx is not None:
        value = ctx.current()
        if value is not ctx.UNSET:
            return None if value is ctx.DIRECT else value   # DIRECT = 本次强制直连，压过所有环境变量
    for key in ("VAULT_PROXY", "ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy"):
        ...
```

**子插件什么都不用做**：`make_client()` 签名没变，它会自动带上本次调用该用的代理。
框架在 `plugin_manager` 的五个入口（验证 / 权限探测 / 简易操作 / 模型列表 / 流式操作）设好作用域，
所以单条、批量、MCP、流式四条路径都正确且互不干扰。

> **每次调用都记一行「实际生效的出口」**（INFO，日志页可见）：
>
> ```
> 验证 llm_nvidia：代理出口 = socks5://127.0.0.1:10808
> 验证 llm_moonshot：代理出口 = 直连（未配置代理）
> 验证 llm_nvidia：引用的代理 #1 **已不存在**，已回落直连（请到「系统设置 → 网络与代理」重新选择，或改预设）
> ```
>
> 为什么专门记它：凭据里写着「用代理 #1」、实际却直连，这种错配在界面上**完全看不出来**
> （下拉里照样显示着代理名），表现就是「配了代理还是不生效」且查不到线索。
> **引用的代理已不存在时升级成 WARNING**——那是静默降级成直连最常见的原因
> （删除又重建过代理时，`proxies` 表的自增 id 不会复用，而预设/凭据里存的还是旧 id）。

> **打包时注意**：httpx 是在**函数体内** `import socksio` 的，构建机没装这个包时
> PyInstaller 既不报错也不会打包它，症状是 exe 里一用 socks5 代理就抛
> `the 'socksio' package is not installed`。因此 `scripts/build.py` **构建前会按
> `requirements.txt`（含 extras 展开，所以 `httpx[socks]` → `socksio` 会被查出来）核对环境**，
> 缺包就打印清单并中止，提示 `pip install -r requirements.txt`（`--allow-missing-deps` 可强行继续）；
> `CredentialVault.spec` 里也显式 `collect_all("socksio")`。

注意：SMTP / IMAP 走的是**裸 TCP**，不吃 HTTP 代理那一套，但可以走 SOCKS / HTTP CONNECT 隧道
（依赖 PySocks，见 §2.6），所以邮箱同样支持「使用代理」。

**测试代理**：同一页有「测试代理」按钮，传当前输入框的值（可「先测再存」），调用
`backend/proxy_test.py` 做一次对照实验——同一批探测分别在直连与走代理下各跑一遍，比对
国内站点（baidu.com）可达性、国外站点（gstatic.com/generate_204）可达性与出口 IP 归属地，
综合给出「代理是否真的生效」的结论并在界面展示。排查「代理配了但插件仍走本地出口」时先用它。
详见 `docs/ARCHITECTURE.md` 第 2.4 节。

---

## 2.2 附加信息（account_info）

`verify` 返回的 `account_info` 会显示在凭据列表的「附加信息」列，前端按「标签 值」渲染。约定：

| 场景 | account_info 写法 | 展示效果 |
|------|-------------------|----------|
| 余额 | `{"balance": 12.34, "currency": "CNY"}` | 余额 12.34 CNY（保留 2 位小数） |
| 使用率（plan/额度制） | `{"usage_rate": 0.52}` | 使用率 52%（已用 ÷ 总额度，按服务适配） |
| 其它可读字段 | 如 `organization` / `domain_count` / `account_id` | `组织 Xxx` / `域名数 1` / `账号 ID …` |

- **LLM 类**：附加信息只展示**余额**；无余额概念的 plan 账号展示**使用率**。余额由 `llm_common` 按厂商 `balance_url`/`balance_kind` best-effort 查询——目前 DeepSeek / Moonshot 有余额接口，OpenRouter 用 `/credits` 计算余额与使用率；OpenAI / Anthropic / Gemini / 智谱等多数无公开余额接口，附加信息为空。
- **金额合理性闸**：one-api / new-api 类中转站在「不限额」时会把硬限额（`hard_limit_usd`）填成 `99999999.99`、`1e8` 这类哨兵值，拿它去减消耗就会得出 `99999996.47` 这种假余额。所有要展示的金额都必须先过 `llm_common.plausible_amount()`（有限数值且绝对值 < `AMOUNT_LIMIT = 1e7`），不通过就**宁可不显示**。新增带余额的厂商/中转插件时务必一并加这道闸。
- **中转站**：`openai_compatible`（通用 OpenAI 兼容）与 `newapi`（New API 中转站专用）都走 one-api 兼容的 `/dashboard/billing/subscription` + `/dashboard/billing/usage`。New API 的 `hard_limit_usd` 是**令牌总额度（剩余 + 已用）**，`total_usage` 单位是**美分**，故 `余额 = hard_limit_usd − total_usage / 100`；命中哨兵值则按「不限额度」处理，改为展示 `套餐 不限额度` 而非数字。
- 无信息量的字段（`object` / `type` / `auth_mode` / 原始时间戳等）不展示，前端 `extraInfo()` 统一处理。

---

## 2.3 验证超时

单个请求的连接/读取超时可调，入口在**「凭据管理 → 批量验证」对话框**（默认 20 秒，
范围 5–300 秒），持久化到 settings 表的 `verify_timeout`。与代理同一套机制：应用启动与
保存设置时把它写入环境变量 `VAULT_TIMEOUT`，插件侧读取——**插件不允许 `import backend`**，
环境变量是唯一的参数通道。

各 `<类别>_common.py` 用 `timeout()` 取代裸常量 `TIMEOUT`：

```python
def timeout() -> float:
    """实际使用的超时秒数：VAULT_TIMEOUT（应用设置写入）> 模块默认 TIMEOUT。"""
    raw = os.environ.get("VAULT_TIMEOUT")
    ...
```

用法：`make_client()` 里 `httpx.Client(timeout=timeout(), ...)`、`guarded()` 的超时提示
`f"请求超时（{int(timeout())} 秒）"`、以及非 httpx 的插件（`mail_smtp` 用标准库
`smtplib`/`imaplib`，默认 15 秒）各自在连接处传 `timeout()`。

- `timeout()` **每次发请求时读取**，所以改完立即生效、无需重启；
- `VAULT_TIMEOUT` 由外部环境显式设置时优先于设置页的值（与 `VAULT_PROXY` 同约定）；
- 模块里的 `TIMEOUT` 常量退化为**默认值**，不再是唯一来源。源码见 `backend/verify_options.py`。

**单次请求可以覆盖全局超时**（`make_client(request_timeout=…)`、
`probe_openai_chat(..., timeout=…)`）：用于「一次 `verify` 里要连试多个候选、每个只给短超时」
的厂商——例如 NVIDIA 的模型清单是过期超集，押注单个模型名必烂，于是按候选短名单逐个试
（`plugins/llm/nvidia/plugin.py` 的 `PROBE_MODELS` / `PROBE_TIMEOUT`）。

> ⚠️ 自己循环试候选时，**每个候选的超时（`httpx.TimeoutException`）与网络异常必须在插件内
> `try` 掉**。让它冒泡到 `@guarded` 会直接结束整次 `verify`——第一个候选挂死就白费了后面的候选，
> 表现和改之前一模一样。

---

## 2.4 欠费探测（quota_probe）

**问题**：`GET /v1/models` 这类探针只证明「Key 认证通过」，证明不了「账户有钱」。厂商没有
余额接口时（OpenAI / Anthropic / Gemini / 智谱 …），或中转站令牌是「不限额度」哨兵
（见 §2.2）时，凭据明明有效、实际一发对话就报欠费——界面却显示「凭证有效」。

**办法**：给这类凭据**可选地**补发一次 `max_tokens=1` 的最小对话请求，用真实计费路径换一个
确定结论。这是**凭据层面**的配置：LLM 插件的 manifest 里声明两个字段，用户在「新增/编辑凭据」
表单里逐条勾选，默认**关闭**（会真实消耗极少量额度）。

| manifest 字段 | 类型 | 含义 |
|---------------|------|------|
| `probe_chat` | `checkbox` | 余额未知时是否发一次最小对话探测，默认 false |
| `probe_model` | `model` | 探测模型（可下拉可手输）；留空 = 自动从模型列表挑一个便宜的 |

后端在验证通过、且 `account_info` 里**没有** `balance` 也没有 `usage_rate` 时，才会把机会交给
插件的 `quota_probe`（见 `backend/plugin_manager._apply_quota_probe`）。有余额的凭据不会再花
这一发请求。

**接入方式**

*通用协议*（OpenAI / Anthropic / Gemini）——子插件一行即可（`<对话协议>` 是
`openai` / `anthropic` / `gemini`，注意不是模块里的 `PROTOCOL` 常量）：

```python
from llm_common import VerifyResult, guarded, make_quota_probe
...
quota_probe = make_quota_probe(DEFAULT_BASE_URL, "openai")
```

*鉴权或端点非通用格式*——插件**自己实现** `quota_probe(data)`，复用 `llm_common` 提供的
读取与结论辅助函数即可，不必重复造轮子：

```python
from llm_common import probe_requested, probe_model, quota_ok, quota_insufficient
from llm_protocols import is_insufficient_balance

def quota_probe(data: dict):
    if not probe_requested(data):           # 该凭据是否勾选了 probe_chat
        return None
    ...                                     # 自己的鉴权 + 最小对话请求
    if 请求成功:
        return quota_ok()
    if 厂商错误码/文案表示欠费:
        return quota_insufficient()
    return None                             # 其它情况一律不下结论
```

实例：`plugins/llm/wenxin/plugin.py`——文心要用 `API_KEY|SECRET_KEY` 换 `access_token`、
聊天端点还带模型后缀，通用实现拼不出来；且它有个坑：**免费档模型（ernie-speed / lite / tiny）
不计费，欠费时照样能对话**，所以自动选型只挑付费档（`ernie-3.5-8k`）。

另一个实例：`plugins/llm/nvidia/plugin.py`——NVIDIA 没有余额接口，且**模型清单是过期超集**
（列着但已下线的模型会 `410`/`404`）。它的 `quota_probe` 直接复用 `verify` 的候选短名单逐个试，
谁通就用谁，全试完没结论就 `None`。

`quota_probe(data)` 的返回约定（后端消费，插件不必自己判断展示）：

| 返回 | 含义 |
|------|------|
| `None` | 未开启探测 / 无结论 / 实测未发现异常（后端什么都不改） |
| `quota_ok()` → `{"account_info": {"chat_probe": "可用"}}` | 实测通过 →「附加信息」补一项「实测 可用」 |
| `quota_insufficient()` → `{"message": "..."}` | 实测发现欠费 → 替换状态说明（`status` 仍是 `valid`，欠费 ≠ 凭据失效） |

判定细节（`llm_protocols.quota_chat_probe`，通用实现）：

- **模型选择**：凭据里指定的模型 > 自动挑便宜的（优先名字含 `flash`/`lite`/`mini`/`nano`/
  `haiku`/`turbo` 等，并排除 `embed`/`rerank`/`tts`/图像类）；候选最多试 3 个，模型不存在就换下一个。
- **结论只认两种**：请求成功 → 可用；错误含 `402` 或 `余额不足`/`insufficient` 等关键字 → 欠费。
  其它错误（401/403/429/模型不存在）**不下结论**，避免误报。自建实现请沿用同一原则。
- **开关与模型从凭据数据里读**（`data["probe_chat"]` / `data["probe_model"]`），不经环境变量——
  数据本来就在插件手里。插件侧没有 `quota_probe` 属性时后端直接跳过，故**接入是可选的**。

> **通用原则：同一个 HTTP 状态码可能含义完全不同，必须读响应体再下结论。**
> 写 `if status in (400, 401, 403): 判无效` 之前，先确认该厂商会不会用这些码表达**非凭据问题**。
> 两个已踩的坑：Gemini 的 `400` 既可能是 `API_KEY_INVALID`（真失效），也可能是
> `FAILED_PRECONDITION`「出口地区不支持」（key 没问题）；NVIDIA 的 `/models` **根本不鉴权**，
> 拿它验证会「任何 key 都有效」。判定时读 `error.status` / `details[].reason` / 错误文案，
> 拿不准就返回 `error`（不是 `invalid`）——**假红比假绿更坑人**。

---

## 2.5 不使用代理的插件要显式声明（USES_PROXY）

「用哪个代理」是**每个凭据的通用设置**，默认所有插件都会出现该选项——判据是插件**默认走代理**
（对应 `backend/plugin_manager.plugin_uses_proxy()`，默认 `True`）。

确实**完全不走网络**的插件在模块级显式声明即可把它从界面上隐去（目前仓库里已没有这类插件——
`im_wechat_ilink` 早先不发请求时标过 `False`，接入只读探测后已恢复默认）：

```python
USES_PROXY = False   # 本插件无出网请求，代理选项没有意义
```

> **`smtplib` / `imaplib` 类插件不适用这条**：它们不能走 HTTP 代理，但可以走 **SOCKS / HTTP CONNECT
> 隧道**，所以照样支持「使用代理」。做法见 §2.6。

> 早期是用「模块里有没有 `make_client`」判定的——那样会**漏掉间接发请求的插件**：
> 例如 NVIDIA NIM 自己没 `import make_client`，而是经 `llm_protocols` 的 `probe_openai_chat`
> 发请求，于是被误判为「不走代理」而隐藏了选项。现改为「默认 True + 显式排除」。

---

## 2.6 非 HTTP 协议怎么走代理（TCP 隧道）

`http.client` / `httpx` 那套（`make_client(proxy=…)`）只对 HTTP(S) 生效。**SMTP / IMAP 等裸 TCP 协议**
用不了它，但可以把连接**隧道**穿出去：SOCKS5（含 `socks5h` 远端解析）或 HTTP CONNECT。

框架侧能力在各类别的 `<类别>_common.py` 里（邮箱是 `mail_common`）：`open_socket()` 建一个
「已经穿过代理」的 socket，再交给标准库——只覆写建连那一处，其余行为与标准库一致：

```python
class TunneledSMTP(smtplib.SMTP):          # 普通连接
    def _get_socket(self, host, port, timeout):
        return open_socket(host, port, timeout)

class TunneledSMTP_SSL(smtplib.SMTP_SSL):  # SMTPS
    def _get_socket(self, host, port, timeout):
        return self.context.wrap_socket(open_socket(host, port, timeout), server_hostname=self._host)
# IMAP 对应的钩子是 IMAP4._create_socket
```

要点：

- `open_socket()` 自己读 `proxy()`（与其它类别同一套优先级），**没配代理就退回普通 `create_connection`**，
  所以插件可以无脑用它，不必分两条路径。
- 隧道依赖 **PySocks**（`import socks`，见 `requirements.txt`）。它是运行时动态加载的插件用到的，
  PyInstaller 静态分析看不到，**spec 里显式 `collect_all("socks")`**；缺失时 `open_socket()` 抛一句
  中文处理办法，而不是甩 `ImportError`。
- 支持的协议：`socks5` / `socks5h` / `socks4` / `socks4a` / `http`(CONNECT)。账号密码写在 URL 里
  （`socks5://user:pass@host:port`），百分号编码会还原。

---

## 3. plugin.py 接口

必须暴露 `verify(data)`；可选 `validate(data)` 与 `get_display_summary(data)`。
`data` 是用户按 `fields` 填写的字典。

**每个子插件自带完整实现**，只从 `<类别>_common.py` 引用通用工具；服务之间允许重复代码（刻意为之）。

**模块 docstring 里要带官方参考链接** —— 写清你实现端点与判定阈值时依据的**官方文档页**，
一行一个：

    参考文档：https://docs.example.com/api-reference

- **可以没有，不能乱写**：确认不了官方文档地址的，这一行**就不写**，别凭印象编 URL；
  也**只写官方**页面，不写 CSDN / 掘金 / 博客 / 第三方 SDK 仓库这类转述页。
- 官方**文档页**优先于官网首页。
- **位置不限，在模块 docstring 里即可**：惯用「紧跟首段摘要之后」或「放在 docstring 末尾」，
  两种现网都有，不必强求统一；但别把它混进正文注释里（那样就不成其为「模块说明」了）。
- 依据了多个官方页（如「鉴权」与「错误码」各一页）就写多行 `参考文档：`，但别为凑数堆链接。
- **写进去之前要实际打开确认过**（能访问、内容确实是该服务的文档）；被挡或 404 的一律不写。
- **只给开发者看，不上界面**：这行只留在 `plugin.py` 的 docstring（代码注释）里。
  不许写进 `manifest.json` 的 `description` 或任何字段的 label/hint/placeholder，不许出现在
  `verify()` / `get_display_summary()` 返回的 message 里，也不许写进 `actions` 文案 ——
  凡是前端会渲染出来的地方都不能有它。
  > 校验脚本 `tmp/smoke_optin_contract.py` 按「**只在 docstring 出现、正文里找不到**的 URL」
  > 判定参考链接；正文里也出现的那些是端点/默认值常量（如 ``DEFAULT_HOMESERVER``），
  > 它们在 manifest 里当 `default` / `placeholder` 渲染是功能性的，不算外泄。

```python
"""MyVendor 验证插件（plugins/llm/my_vendor/，自包含实现）。

参考文档：https://docs.myvendor.com/api-reference
"""
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

### 3.1 可选「简易操作」（actions / run_action / capabilities）

插件可选实现这几个函数，为凭据提供额外操作（前端凭据行的「操作」按钮）：

| 函数 | 必需 | 作用 |
|------|------|------|
| `actions(data) -> list[dict]` | ✅ | 声明有哪些操作；返回 `[]` 表示没有（前端不显示按钮）。**必须是本地、便宜、无副作用的**——它只负责渲染菜单 |
| `run_action(action_id, params, data) -> dict` | ✅ | 执行操作 |
| `capabilities(data) -> dict` | 可选 | **探测该凭据真正能用哪些操作**（可联网）；没实现 = 全部可用 |
| `list_models(data) -> list` | 可选 | 可用模型名列表（LLM「简单对话」把它变成可搜索下拉） |
| `stream_action(action_id, params, data, on_delta)` | 可选 | 流式执行；未实现时自动回退 `run_action` |

#### 操作项（action）的字段

| 字段 | 适用 | 说明 |
|------|------|------|
| `id` / `name` | 全部 | 标识与显示名（`run_action` 按 id 分发） |
| `kind` | 全部 | **前端渲染方式：`chat`（对话气泡）/ `form`（动态表单）/ `list`（表格 + 行内按钮）** |
| `group` / `order` / `description` | 全部 | 可选。多个操作时弹选择菜单，按 `group` 分组、`order` 排序（小的在前） |
| `fields` | `form` / `list` | 输入项：`[{key,label,type(text/password/select/textarea/number),default,placeholder,options,required}]`。**`list` 也能带 fields**：点「拉取/刷新」时把表单值作为 `params` 传进 `run_action` |
| `submit` | `form` / `list` | 提交/刷新按钮文案（默认「运行」） |
| `write` | 全部 | 可选布尔，声明为**会改动远端数据**。前端在**提交前**弹二次确认（只列非敏感字段值） |
| `model` | `chat` | 默认模型名（用户可改） |
| `columns` | `list` | `[{key,label,width?,mono?(等宽),type?("text"/"number"/"tag")}]` |
| `row_actions` | `list` | 每行的按钮，见 §3.1.2 |
| `empty_text` | `list` | 空列表文案 |
| `save_to` | `form` / `list` | 下载类操作，见 §3.1.3 |
| `confirm` | 全部 | 可选。**覆盖写操作二次确认的默认文案**。涉及不可逆副作用时应当写清楚——例如「刷新 access_token」会轮换 `refresh_token`，就会提示用户本机 Codex CLI 可能需要重新登录 |
| `progress` | 全部 | 可选布尔。声明该操作**会上报进度**（下载 / 打包这类长操作）：前端改走流式入口并在弹窗里渲染进度条，同时它会被登记成一条**后台任务**（关掉弹窗照样跑，可在「任务」页取消）。插件侧需实现 `stream_action(..., on_progress=…, should_cancel=…)`，见 §3.1.9 |
| `updated_data`（**返回值**） | 全部 | 可选。`run_action` 的返回里带上 `{"字段key": 新值}`，框架会把这些值**写回该凭据**（只接受插件在 manifest 里声明过的字段）。用于「用 refresh_token 换一组新令牌」这类必须原子保存的操作。写回值**不会**回传给前端（那是新明文令牌），前端只收到 `data_updated: [字段名]`，据此刷新列表并复验。见 §3.1.8 |

`run_action` 的返回：

- 普通：`{"ok": True, "output": "多行文本"}` 或 `{"ok": False, "error": "人话原因"}`
  —— **失败时 `error` 会原样展示给用户**，别写「执行失败」这种没信息量的。
- **列表**：`{"ok": True, "rows": [{"<列key>": 值, ...}], "columns"?, "row_actions"?, "empty_text"?, "total"?, "has_more"?}`
  —— `rows` 里每项的键就是 `columns` 的 `key`；后四个可以在返回里**覆盖声明**（做动态列）。

#### 3.1.1 `kind: "list"` 长什么样

```
╔══════════════════════════════════════════╗
║ 站点列表                          [刷新] ║   ← submit 文案
╠══════════════════════════════════════════╣
║ 站点       记录数  状态      操作        ║   ← columns / row_actions
║ a.com          12  正常     [解析][新增] ║
║ b.com           3  待续费   [解析][新增] ║
╚══════════════════════════════════════════╝
```
点「解析」会**下钻**到另一个 `list`（前端自动加面包屑与「返回」，返回时直接还原上一帧、不重新请求）；
点「新增」会打开一个 `form`（自动预填当前行的域名）。加载中、空、出错各有独立表现，出错**不关弹窗**并提供「重试」。

#### 3.1.2 `row_actions`：行内按钮与下钻

```python
{"id": "list_records",          # 目标 action 的 id（必须在同一个 actions() 里声明）
 "label": "解析",               # 按钮文字
 "params": {"zone": "$row.zone", "type": "A"},   # 字面量 + `$row.<列key>` 模板
 "write": True}                 # 可选：目标操作是写操作时带上
```
- `params` 里字符串形式的 `$row.<列key>` 会被替换成**该行**的值；**解析不到的行，按钮自动置灰**（绝不会把 `undefined` 传给你的插件）。
- 点下去后按**目标 action 的 `kind`** 分派：`form` → 打开表单并预填；`list` → 下钻；其它 → 直接执行。
- 所以**同一个 action 既可以被菜单直接调用、又可以被某一行调用**，靠 `params` 里有没有值来区分。例如
  `add_record` 直接调用时让用户填域名，从某一行调用时自动带上域名。
- **纯下钻操作不进顶层菜单**：若某个 action 只被别的 action 当 `row_action` 目标、且自身没有任何
  `fields`（点了也没参数可填），前端会把它从「操作」菜单里隐去——否则用户点进去只会得到一句
  「缺少 xxx」。它仍照常被行内按钮调用；要让它也能单独用，就给它加上可填的 `fields`
  （例如 `list_records` 加一个 `domain` 输入框）。

#### 3.1.3 `save_to`：下载类操作（会自动弹「另存为」）

```python
{"id": "download_repo", "name": "打包下载", "kind": "form",
 "fields": [{"key": "repo", "label": "仓库", "required": True}],
 "save_to": {"key": "path", "default_name": "$row.name.zip"}}
```
执行顺序由框架保证：**先确认（若 `write`）→ 前端弹系统「另存为」拿到路径 → 路径塞进 `params` → 调 `run_action`**。
插件侧只负责把内容写进那个路径，用各类别 `*_common.py` 里的原子落盘辅助：

```python
path = (params.get("path") or "").strip()
if not path:
    return {"ok": False, "error": "未指定保存路径"}
with make_client() as client:
    with client.stream("GET", url, headers=headers) as resp:
        ok, message = download_to_file(resp, path)
        if not ok:
            return {"ok": False, "error": message}
return {"ok": True, "output": f"已保存到 {path}"}
```

- `download_to_file(response, path) -> (ok, message)`：**非 200 直接失败、不碰磁盘**；先写 `.part` 再 `os.replace` 原子替换，
  避免留下「名字正常、内容截断」的半成品。**不要去插件里自己 open/write。**
- **插件绝不要弹任何对话框**：流式路径上插件跑在新建线程里，跨线程弹窗会卡界面；而且对话框属于界面层的事。

#### 3.1.4 `capabilities`：权限探测（隐藏用不了的操作）

```python
def capabilities(data: dict) -> dict:
    """{"available": ["list_buckets", "list_instances"], "note": "具备云主机与对象存储权限"}"""
```
- 前端**只在点「操作」时调一次**（按凭据缓存，不落 localStorage），所以这里可以联网——但要**串行、每个都 try、给足超时**，别让用户等太久。
- `available` 列出**当前凭据真正可用**的操作 id：**没权限的操作会被隐藏**；`available == []` 时前端把 `note` 展示给用户。
- **探测失败必须 fail-open**：拿不准时**抛异常**（框架会兜住并当作「未知 → 全部可用」）或返回 `{"available": None}`。
  **千万不要为了省事返回空列表** —— 那等于一次网络抖动就把用户能用的功能全藏了。
- 三种状态在前端是可区分的：**没有操作** / **有操作但没权限（显示 note）** / **探测失败（全部展示）**。

#### 3.1.5 可复用的辅助（各类别 `*_common.py` 里都有）

| 辅助 | 用途 |
|---|---|
| `make_client()` / `safe_json()` / `body_snippet()` / `guarded()` / `timeout()` | 原有 |
| `first_list(payload, *keys) -> list` | 从各家五花八门的 JSON 信封里捞第一个列表（`items`/`list`/`data`/`ZoneList`…）。**拿不准就显式传 keys** |
| `download_to_file(response, path) -> (ok, message)` | 原子落盘（见 §3.1.3） |

#### 3.1.6 一个可以照抄的最小骨架

```python
def actions(data: dict) -> list[dict]:
    if not (data or {}).get("api_key"):
        return []
    return [
        {"id": "list_things", "name": "资源列表", "kind": "list", "group": "查看", "order": 10,
         "submit": "刷新",
         "columns": [{"key": "name", "label": "名称"}, {"key": "id", "label": "ID", "mono": True}],
         "row_actions": [{"id": "add_thing", "label": "新增", "params": {"name": "$row.name"}}],
         "empty_text": "还没有资源"},
        {"id": "add_thing", "name": "新增资源", "kind": "form", "group": "管理", "order": 20,
         "write": True, "submit": "创建",
         "fields": [{"key": "name", "label": "名称", "required": True},
                    {"key": "type", "label": "类型", "type": "select",
                     "options": [{"value": "A", "label": "A 记录"}, {"value": "CNAME", "label": "CNAME"}]}]},
    ]


def capabilities(data: dict) -> dict:
    available, notes = [], []
    try:
        _call(data, "GET", "/ping")           # 只读探测
        available.append("list_things")
        available.append("add_thing")
    except Exception:
        notes.append("缺少读权限")
    return {"available": available, "note": "；".join(notes)}


def run_action(action_id: str, params: dict, data: dict) -> dict:
    params = params or {}
    try:
        if action_id == "list_things":
            payload = _call(data, "GET", "/things")
            rows = [{"name": item.get("name", ""), "id": item.get("id", "")}
                    for item in first_list(payload, "items", "data")]
            return {"ok": True, "rows": rows, "total": len(rows)}
        if action_id == "add_thing":
            _call(data, "POST", "/things", json={"name": params.get("name"), "type": params.get("type")})
            return {"ok": True, "output": f"已创建 {params.get('name')}"}
        return {"ok": False, "error": "未知操作"}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底
        return {"ok": False, "error": f"执行失败：{type(exc).__name__}"}
```

#### 3.1.7 输出净化（后端统一兜底）

即使插件未自行脱敏，`backend/api.py` 也会在返回前**递归**把该凭据所有 `sensitive` 字段的明文
（长度 ≥ 6）替换成 `***` —— 覆盖 `output` / `error` / 流式 `delta`，**以及 `rows` 等任意嵌套结构**。
插件侧仍应尽量不自造含密钥的错误串。

#### 3.1.8 把结果写回凭据（`updated_data`）

```python
def run_action(action_id, params, data):
    ...
    return {"ok": True,
            "output": "已刷新 access_token；refresh_token 已轮换并写回本凭据",
            "updated_data": {"access_token": 新值, "refresh_token": 新值}}
```

`backend/api.py` 拿到 `updated_data` 后：按该插件 manifest 里**声明过的字段**过滤（其它键丢弃）→
合并进凭据明文 → 重加密落库 → 回给前端一个 `data_updated: [字段名]`（**新值本身不回传**，那是明文令牌）
→ 前端刷新列表并自动复验一次。

**这是「写凭据」的能力，只在必要时用**。典型场景是**刷新令牌**：用 refresh_token 换新 access_token 时
上游会**轮换** refresh_token，若不把新值原子写回，库里与本机 CLI 的登录态就永久错位了。
注意配套要求：

- 操作必须标 `write: true`（前端会二次确认），并用 `confirm` 把不可逆后果写清楚；
- 只读校验路径**绝不能**走这条（换令牌是写副作用）；
- 详见 `plugins/llm/openai/plugin.py` 的 `_do_refresh`。

#### 3.1.9 长操作报进度（`progress` + `on_progress`）

下载 / 打包这类要跑十几秒的操作，声明 `"progress": True` 后前端会改走**流式入口**并在弹窗里
渲染进度条：

```python
{"id": "download_prefix", "name": "打包下载", "kind": "form", "progress": True,
 "save_to": {...}, "fields": [...]}

def stream_action(action_id, params, data, on_delta, on_progress=None):
    """progress 类操作必须实现它，并把 on_progress 一路传到下载循环里。"""
    if action_id not in ("download_object", "download_prefix"):
        return run_action(action_id, params, data)      # 其它操作照旧
    return _run_download(..., progress=on_progress)

def on_progress(current: int, total: int, text: str = "") -> None:
    """total <= 0 表示总量未知（前端只显示已完成量）；text 是给用户看的一句话。"""
```

约定与注意事项：

- **`on_progress` 必须是关键字参数且带默认值**（`on_progress=None`）。框架用
  `inspect.signature` 判断插件认不认这个名字，**不认就不传**——所以只写
  `stream_action(a, p, d, on_delta)` 的老插件照常工作，不会被新参数打断。
- **节流**：别在 `iter_bytes` 的每个 chunk 上回调（大文件会推出几万条事件）。
  按每 ~256KB 或每 0.2 秒报一次即可。
- 报什么：单对象下载按**字节**报（`current=已写, total=Content-Length`）；多对象打包按
  **对象个数**报（跨对象累加字节不直观）。
- 进度是**尽力而为**：后端推送出错不会让操作失败；`current/total` 会被夹到合法区间。
- 终态仍由框架的 `done` 事件给出（`output` / `error`），插件不必自己报「完成」。

**协作式取消**：同一条通道上还有第二个可选关键字 `should_cancel`——

```python
def stream_action(action_id, params, data, on_delta, on_progress=None, should_cancel=None):
    ...
    for chunk in resp.iter_bytes(65536):
        if should_cancel and should_cancel():        # True = 用户点了取消
            os.remove(tmp)                           # 半成品要删干净
            return {"ok": False, "error": "下载已取消"}
```

- 取消是**协作式**的：框架只置一个标志位（`backend/tasks.py`），真正停下靠插件在循环里查。
  做不到立刻杀线程，但会在下一个检查点（每个 chunk / 每个对象）停下。
- **插件只负责干净地停下并返回 ok=False**，不要去猜「取消还是失败」——框架持有取消标志，
  会据实把任务标成「已取消」（刚好在取消前跑完的仍算成功）。
- 一定要**清掉 `.part` 之类的临时文件**，别留下半成品。

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

**llm/ 类别（19 个）**：openai（含 Codex/ChatGPT 登录态模式）、anthropic、deepseek、openrouter、
gemini、minimax、nvidia、moonshot、siliconflow（硅基流动）、dashscope（阿里百炼）、huawei_maas、
glm（智谱）、doubao（豆包）、tongyi（通义）、wenxin（文心）、
openai_compatible（第三方中转）、newapi（New API 中转站）、
sensenova（商汤日日新，OpenAI 兼容）、codebuddy（腾讯 CodeBuddy 客户端登录态）。

> **同一厂商的多种接口格式不拆分插件**，而是合成一个。用不用「格式」字段取决于**key 是否按协议分权**：
>
> - **不分权就不设字段**——`glm`（智谱）即如此：2026-10-05 实测同一把 key 在 OpenAI 端点与
>   Anthropic 端点都能通过（智谱把 `claude-sonnet-4` 之类模型名也做了映射），所以插件只有一条
>   路径（OpenAI 兼容端点），既不设开关、也不对旧地址做特判。普通平台用默认地址，Coding Plan
>   在 Base URL 里填 `/api/coding/paas/v4`。
> - **部署形态真的不同才设字段**——`openai_compatible`（第三方中转）保留 `api_style` 下拉：
>   中转站千差万别，有只暴露 `/v1/messages` 的 Claude 专属站，光靠 URL 猜不可靠。
>
> 格式字段一律**只认显式值**（manifest 给默认），不做 Base URL 嗅探，也不为旧凭据类型写数据迁移
> ——**1.0 之前不考虑向下兼容（含旧版本数据库）**，旧的 `llm_glm_anthropic` /
> `llm_anthropic_compatible` 类型不再有对应插件，那类凭据直接在界面上删掉、按新插件重建即可。

**api/ 类别（7 个）**：firecrawl（网页抓取）、tavily（搜索）、metaso（秘塔 AI 搜索）、coze（字节扣子）、
dify（Dify 应用 / 工作流）、app_secret（通用应用密钥，一对 key/secret）、gateway（短信网关 / 接码服务商，
字段按各厂商叫法通用化——`app_key` / `app_secret` 装不下的一律塞 `extra_params`；实测有可用只读校验接口的
8 家：Engagelab、Infobip、CM、laaffic、nxcloud、buka、Ultramsg——Infobip 印度 200 有效、香港那把在该
入口 403「无权限」故判 `error`；Ultramsg 域名对任意实例/口令都返回同一段文案，只能报「未验证」；其余 8 家
没有只读接口，一律如实报「未验证」）。

**dns/ 类别（4 个）**：dnspod、duckdns、godaddy、cloudflare（从 cloud/ 移入：它就是域名解析类）。

**dev/ 类别（3 个）**：github（令牌）、huggingface、wandb（Weights & Biases）。

**im/ 类别（8 个）**：feishu（飞书）、dingtalk（钉钉）、telegram（Bot）、discord（Webhook）、
qq_bot（QQ 频道机器人）、wechat_ilink（微信 iLink Bot）、telegram_apihash（Telegram API ID / Hash）、
webhook（通用群机器人 Webhook）。

**cloud/ 类别（6 个）**：aliyun（阿里云 AK/SK）、tencentcloud（腾讯云 SecretId/Key）、aws（AWS Access Key）、
gcp（GCP 服务账号）、huaweicloud（华为云 AK/SK）、firebase（Firebase 管理密钥）。
服务账号这类凭据按「一行一项」收 **邮箱 + 私钥**，另两个字段选填（密钥 ID 只当 JWT 头的 `kid`、
项目 ID 只喂额外探测），**不接收整段 JSON**——见 §2「字段输入约定」。
`google_sa.py` 是协议级共享模块：gcp 与 firebase 都要把邮箱 + 私钥签成 RS256 JWT 换 access_token，
这步两家完全一致，故单独成模块（判定「什么状态码算有效」仍在各自 plugin.py 内）。
2026-10-06 用交付目录里的 adminsdk 密钥实测：私钥在单行框里被压掉换行后照样换得到 token；
`private_key_id` 缺失不影响；只填 `client_email` + `private_key` 也能通过。
（Cloudflare 已移到 `dns/` —— 它本质是域名解析类。）

**exchange/ 类别（2 个）**：okx、binance。

**mail/ 类别（1 个）**：smtp（通用 SMTP，覆盖 Gmail / QQ / 163 / 126 等）。

> 说明：标记 `batch_verify: false`（只能手动单个验证）的有三个：`duckdns` 的校验本身是写操作
> （先写入 `1.1.1.1` 确认生效再还原原值）；`telegram_apihash` 与 `api/gateway` 则是判定常为
> 「未验证」（前者要登录态、后者多数厂商没有只读接口），批量跑会把人工判定的状态刷成「未验证」。

> 本节的 50 个是 `plugins/` 下**默认启用**的全部。另有 `plugins_optin/` 里 34 个
> **默认不启用、尚无实例验证**的插件（含 airtable / censys / shodan / stripe / mistral 等主流
> 服务），清单见 §1.1 —— 它们装了不生效，须在「插件管理 → 可添加」里手动添加。

### 内置「简易操作」一览

凭据齐全时，凭据行会出现「操作」按钮（见 §3.1）。一个凭据有多个操作时先弹选择菜单，
**只有一个就直接进**；带「（写）」的是 `write: true`，提交前会二次确认。

- **llm**：18 个厂商 —— 简单对话（`chat`，流式）；其中 **openai** 另有「认证方式」下拉，
  选「ChatGPT 登录态（Codex）」时改为「登录态详情」（`token_info`，只读解析本地 JWT，**不回显任何令牌**）；
  `codebuddy` 是纯登录态类凭据，同样只有 `token_info`。
  > 登录态**按字段分开填**（`access_token` 必需；`account_id` / `id_token` / `refresh_token` 可选），
  > 不要求粘贴整份 `~/.codex/auth.json`——既不便于维护，也会把不必要的字段一起存下来。
  > 字段按模式显隐（`show_if`）：选 API Key 时只看到 API Key 那组，选 ChatGPT 登录态时只看到登录态那组。
  > **只读校验绝不换令牌**（只有 `GET /v1/models` 与 `GET /backend-api/me` 两个只读请求）。
  > 填了 `refresh_token` 时会多出「刷新 access_token」这个**手动**操作：它 `write: true`、
  > 带自定义确认文案，成功后新值经 `updated_data` **自动写回本凭据**——因为上游换令牌会**轮换**
  > refresh_token，不写回就会让本机 CLI 与库里的登录态永久错位。
  >
  > `codebuddy`（纯登录态类）同样是分字段填写：**必需** `access_token`、`uid`（探针请求头
  > `X-User-Id`）；**可选** `refresh_token`（只存不用）、`enterprise_id`、`domain`
  > （默认 `www.codebuddy.cn`）。它**没有**刷新操作——刷新端点未实测确认，
  > 拿猜的接口去轮换用户的令牌风险太大。
- **dns**：
  - dnspod / godaddy / cloudflare —— **域名（站点）列表 → 行内「解析」下钻到解析记录 → 行内「修改」**，
    以及行内「新增」；前两者是 `write`，Cloudflare 用同一个表单（留空 `record_id` 即新增）
  - duckdns —— 当前解析（`list`：A / TXT 两行）→ 行内「修改」下钻到「更新解析」（`write`），
    另有「清除 TXT」（`write`）。DuckDNS 没有域名列表接口（一个 token 对应自有子域名），
    查询经 Cloudflare DoH；更新/清除走其 `update` 接口
- **cloud**：aliyun / tencentcloud 同构 ——
  `whoami`（身份 + **探测到的权限**）· `list_regions`（**地域列表**，含中文名）→ 行内「实例」下钻
  `list_instances`（云主机，`region` 可预填可手填）· `list_buckets`（**对象存储**桶列表）→
  行内「预览」下钻 `list_objects`（**区分文件夹前缀与文件**，可继续进入前缀）；
  文件行「下载」（`save_to` 单对象）、文件夹行「打包下载」（`download_prefix`，本地打成 zip 后 `save_to`，
  对象数/总字节有上限，超出会如实报错而非静默截断）
- **dev**：github —— 账号信息（含 `X-OAuth-Scopes` 权限）· **仓库列表 → 行内「打包下载」（`save_to`，弹另存为）**
  与行内「详情」下钻；huggingface / wandb —— 查看账号信息
- **im**：feishu —— 应用信息与权限 · 机器人信息 · 用户详情（按 `open_id` 或邮箱）· 群组 · 知识库 · 云文档；
  telegram（机器人信息、发送消息（写））· discord（Webhook 信息）·
  wechat_ilink —— 拉取消息（`list`，短轮询）→ 行内「下载」下钻到附件下载（图片 / 语音 / 文件 / 视频，
  CDN 密文本地 AES 解密后经 `save_to` 写盘）
- **exchange**：okx（账户余额）· binance（账户信息、交易所时间）
- **api**：coze（工作区列表）· tavily（执行搜索）· firecrawl（抓取网页）·
  metaso（搜索（`list`，行内「阅读」下钻）· 网页解析）
- **mail**：smtp —— 收件箱（`list`，可填拉取封数/文件夹/IMAP 主机与端口）→ 行内「看正文」下钻

> 以上按**声明了操作（`actions()`）**的插件列举。新加入的 `api/app_secret`、`api/dify`、`cloud/aws`、
> `cloud/gcp`、`cloud/huaweicloud`、`cloud/firebase`、`im/telegram_apihash`、`im/webhook`、`api/gateway`
> 目前只做凭据验证，未声明任何「操作」。

> **按权限显隐**：aliyun / tencentcloud / feishu / github 实现了 `capabilities()`，没权限的操作不会出现在菜单里；
> 一个都没有时给出说明。**探测失败一律 fail-open**（全都展示）——只有上游**明确**说「密钥无效」才判定为无可用操作。

#### 附：阿里云 OSS 与腾讯云 COS 的签名（自建在各自 plugin.py 内）

两者都**不复用**云 API 的签名，写新对象存储插件时别搞混：

- **OSS V1**：`StringToSign = VERB\nContent-MD5\nContent-Type\nDate\nCanonicalizedOSSHeaders + CanonicalizedResource`，
  `Signature = base64(HMAC-SHA1(AccessKeySecret, StringToSign))`（**key 不加 `&`**，这点与云 API RPC v1 不同），
  请求头 `Date`(RFC1123 GMT) + `Authorization: OSS <AK>:<Sig>`；`max-keys`/`prefix`/`marker` **不进**规范串。
- **COS**：`q-sign-algorithm=sha1`。`signKey = HMAC-SHA1(SecretKey, signTime)` —— **单次** HMAC！
  （曾误写成 `HMAC(HMAC(SecretKey, signTime), keyTime)` 的双重形式，那会让所有请求 403。）
  `httpString = <method 小写>\n<path>\n<params>\n<headers>\n`（k/v 都 `quote(x, safe="-_.~")`、按字典序 `&` 连接），
  `stringToSign = "sha1\n" + signTime + "\n" + sha1(httpString) + "\n"`，`signature = HMAC-SHA1(signKey, stringToSign)`。
- 两家的响应都是 **XML**（用 `response.text` + `xml.etree.ElementTree`，用 `tag.rsplit('}', 1)[-1]` 绕开 namespace），
  **不要**用 `safe_json`。

---
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
