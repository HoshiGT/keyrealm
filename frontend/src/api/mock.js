/**
 * 仅开发模式使用的内存 Mock 后端。
 *
 * 目的：在没有 pywebview 的普通浏览器里跑 `npm run dev` 时，前端仍然可以完整
 * 走通所有页面与交互，便于开发与联调。生产构建中该模块通过 `import.meta.env.DEV`
 * 守卫被完全摇树移除，不会出现在 dist 里。
 *
 * Mock 主密码：demo1234
 */

import { PLUGIN_ICON_IMAGES } from './pluginIcons'

const MOCK_PASSWORD = 'demo1234'
const LATENCY = 120

const delay = (ms = LATENCY) => new Promise((resolve) => setTimeout(resolve, ms))
const iso = (date = new Date()) => date.toISOString()
const daysAgo = (n) => iso(new Date(Date.now() - n * 86_400_000))
const minutesAgo = (n) => iso(new Date(Date.now() - n * 60_000))
const minutesDate = (n) => new Date(Date.now() - n * 60_000)

const LOG_LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
const LOG_LEVEL_VALUES = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 }
const MOCK_LOG_FILE = '<开发模式>/data/logs/credential_vault.log'

/** 生成一行与后端格式一致的日志文本：YYYY-MM-DD HH:MM:SS [LEVEL] name: message */
function logLine(level, name, message, date = new Date()) {
  const pad = (n) => String(n).padStart(2, '0')
  const ts =
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  return `${ts} [${level}] ${name}: ${message}`
}

function emit(name, detail) {
  window.dispatchEvent(new CustomEvent(name, { detail }))
}

function clone(value) {
  return value == null ? value : JSON.parse(JSON.stringify(value))
}

/** 与后端 crypto.mask_secret 保持一致的掩码规则 */
function maskSecret(value) {
  const text = String(value || '')
  if (!text) return ''
  if (text.length <= 10) return `${text.slice(0, 2)}…`
  return `${text.slice(0, 6)}…${text.slice(-4)}`
}

// ── Mock 子插件定义（与 plugins/<类别>/<服务>/ 目录结构一致）─────────────
const LLM_VENDORS = [
  ['openai', 'OpenAI'], ['anthropic', 'Anthropic'], ['deepseek', 'DeepSeek'],
  ['openrouter', 'OpenRouter'], ['gemini', 'Google Gemini'], ['minimax', 'MiniMax'],
  ['nvidia', 'NVIDIA NIM'], ['moonshot', 'Moonshot Kimi'], ['siliconflow', 'SiliconFlow'],
  ['dashscope', '阿里云百炼'], ['huawei_maas', '华为云 MaaS'], ['glm', '智谱 GLM'],
  ['glm_anthropic', '智谱 GLM（Anthropic 兼容）'], ['ark', '火山方舟'],
  ['tongyi', '通义千问'], ['wenxin', '百度文心'],
  ['openai_compatible', 'OpenAI 兼容接口'], ['anthropic_compatible', 'Anthropic 兼容接口'],
]
const API_SERVICES = [
  ['firecrawl', 'Firecrawl'], ['tavily', 'Tavily'], ['coze', 'Coze'],
]

// 其余类别子插件：[类别, 类别名, 类别图标, 服务目录, 显示名, 图标, 是否支持批量验证]
const EXTRA_PLUGINS = [
  ['dns', '域名解析', '🌐', 'dnspod', 'DNSPod', '🅳', true],
  ['dns', '域名解析', '🌐', 'duckdns', 'DuckDNS', '🦆', false],
  ['dns', '域名解析', '🌐', 'godaddy', 'GoDaddy', '🛒', true],
  ['dev', '开发者平台', '🧰', 'github', 'GitHub', '🐙', true],
  ['dev', '开发者平台', '🧰', 'huggingface', 'HuggingFace', '🤗', true],
  ['dev', '开发者平台', '🧰', 'wandb', 'Weights & Biases', '📊', true],
  ['im', '即时通讯', '💬', 'feishu', '飞书', '🕊️', true],
  ['im', '即时通讯', '💬', 'dingtalk', '钉钉', '📌', true],
  ['im', '即时通讯', '💬', 'telegram', 'Telegram', '✈️', true],
  ['im', '即时通讯', '💬', 'discord', 'Discord', '🎮', true],
  ['im', '即时通讯', '💬', 'qq_bot', 'QQ 频道机器人', '🐧', true],
  ['im', '即时通讯', '💬', 'wechat_ilink', '微信 iLink Bot', '📲', false],
  ['cloud', '云服务商', '☁️', 'aliyun', '阿里云', '☁️', true],
  ['cloud', '云服务商', '☁️', 'tencentcloud', '腾讯云', '🐧', true],
  ['cloud', '云服务商', '☁️', 'cloudflare', 'Cloudflare', '🛡️', true],
  ['exchange', '交易所', '📈', 'ht', '华泰证券（涨乐）', '📈', false],
  ['exchange', '交易所', '📈', 'okx', 'OKX', '📉', true],
  ['exchange', '交易所', '📈', 'binance', 'Binance', '🟡', true],
  ['mail', '邮箱', '✉️', 'smtp', '通用 SMTP', '✉️', true],
]

function makeFieldsSchema(isCompatible) {
  return [
    { key: 'base_url', label: 'Base URL', type: 'url', required: !!isCompatible, sensitive: false, default: null, placeholder: 'https://api.example.com/v1', options: null },
    { key: 'api_key', label: 'API Key', type: 'password', required: true, sensitive: true, default: null, placeholder: 'sk-...', options: null },
    { key: 'notes', label: '备注', type: 'textarea', required: false, sensitive: false, default: null, placeholder: '用途说明', options: null },
  ]
}

function buildPlugins() {
  const plugins = []
  let id = 0
  for (const [vendor, name] of LLM_VENDORS) {
    id += 1
    plugins.push({
      id,
      name,
      version: '1.0.0',
      author: 'Credential Vault',
      description: `${name} API Key 管理与验证`,
      credential_type: `llm_${vendor}`,
      icon: PLUGIN_ICON_IMAGES[vendor] ? 'logo.png' : '🤖',
      icon_image: PLUGIN_ICON_IMAGES[vendor] || null,
      icon_lift: false,
      category: 'llm',
      category_name: 'LLM 模型',
      category_icon: '🤖',
      rel_path: `llm/${vendor}`,
      batch_verify: true,
      has_actions: true,      enabled: true,
      installed_at: daysAgo(30),
      credential_count: 0,
      fields_schema: makeFieldsSchema(vendor.endsWith('_compatible')),
    })
  }
  for (const [service, name] of API_SERVICES) {
    id += 1
    plugins.push({
      id,
      name,
      version: '1.0.0',
      author: 'Credential Vault',
      description: `${name} API Key 管理与验证`,
      credential_type: `api_${service}`,
      icon: PLUGIN_ICON_IMAGES[service] ? 'logo.png' : '🔌',
      icon_image: PLUGIN_ICON_IMAGES[service] || null,
      icon_lift: false,
      category: 'api',
      category_name: 'API 服务',
      category_icon: '🔌',
      rel_path: `api/${service}`,
      batch_verify: true,      enabled: true,
      installed_at: daysAgo(30),
      credential_count: 0,
      fields_schema: makeFieldsSchema(false),
    })
  }
  for (const [cat, catName, catIcon, service, name, icon, batch] of EXTRA_PLUGINS) {
    id += 1
    plugins.push({
      id,
      name,
      version: '1.0.0',
      author: 'Credential Vault',
      description: `${name} 凭据管理与验证`,
      credential_type: `${cat}_${service}`,
      icon,
      icon_image: null,
      icon_lift: false,
      category: cat,
      category_name: catName,
      category_icon: catIcon,
      rel_path: `${cat}/${service}`,
      batch_verify: batch,
      has_actions: service === 'smtp',      enabled: true,
      installed_at: daysAgo(30),
      credential_count: 0,
      fields_schema: makeFieldsSchema(false),
    })
  }
  return plugins
}

function createState() {
  const groups = [
    { id: 1, name: '工作项目', description: '2026 年 Q4 核心项目', color: '#409EFF', sort_order: 0, created_at: daysAgo(30), credential_count: 3 },
    { id: 2, name: '个人', description: '个人测试用途', color: '#67C23A', sort_order: 1, created_at: daysAgo(20), credential_count: 2 },
    { id: 3, name: '基础设施', description: '云资源访问密钥', color: '#E6A23C', sort_order: 2, created_at: daysAgo(12), credential_count: 1 },
  ]

  const plugins = buildPlugins()

  const credentials = [
    { id: 1, name: 'OpenAI 生产账号', credential_type: 'llm_openai', group_id: 1, data_plain: { base_url: 'https://api.openai.com/v1', notes: '生产环境主账号' }, secret: 'sk-proj-demo-aaaaaaaaaaaaaaaa', status: 'valid', status_detail: { message: '验证通过', account_info: { balance: 2.419652, currency: 'USD', usage_rate: 0.5160696 }, duration_ms: 320 }, last_verified_at: minutesAgo(2), last_valid_at: minutesAgo(2), created_at: daysAgo(30), updated_at: minutesAgo(2) },
    { id: 2, name: 'Claude 主账号', credential_type: 'llm_anthropic', group_id: 1, data_plain: { base_url: 'https://api.anthropic.com', notes: '内部工具调用' }, secret: 'sk-ant-demo-bbbbbbbbbbbb', status: 'invalid', status_detail: { message: '401 Unauthorized', duration_ms: 210 }, last_verified_at: minutesAgo(60), last_valid_at: daysAgo(4), created_at: daysAgo(25), updated_at: minutesAgo(60) },
    { id: 3, name: 'DeepSeek 测试', credential_type: 'llm_deepseek', group_id: null, data_plain: { base_url: 'https://api.deepseek.com/v1', notes: '' }, secret: 'sk-demo-cccccccccccc', status: 'valid', status_detail: { message: '验证通过', account_info: { balance: 12.34, currency: 'CNY' }, duration_ms: 180 }, last_verified_at: minutesAgo(10), last_valid_at: minutesAgo(10), created_at: daysAgo(10), updated_at: minutesAgo(10) },
    { id: 4, name: 'Gemini 个人', credential_type: 'llm_gemini', group_id: 2, data_plain: { notes: '' }, secret: 'AIza-demo-dddddddddddd', status: 'error', status_detail: { message: '连接超时', duration_ms: 20000 }, last_verified_at: minutesAgo(180), last_valid_at: null, created_at: daysAgo(3), updated_at: minutesAgo(180) },
    { id: 5, name: 'Tavily 搜索', credential_type: 'api_tavily', group_id: 3, data_plain: { notes: '搜索 API' }, secret: 'tvly-demo-eeeeeeeeeeee', status: 'unknown', status_detail: {}, last_verified_at: null, last_valid_at: null, created_at: daysAgo(7), updated_at: daysAgo(7) },
    { id: 6, name: '硅基流动', credential_type: 'llm_siliconflow', group_id: 2, data_plain: { notes: '' }, secret: 'sk-demo-ffffffffffffff', status: 'valid', status_detail: { message: '验证通过', account_info: { balance: 8.5, currency: 'CNY' }, duration_ms: 260 }, last_verified_at: minutesAgo(45), last_valid_at: minutesAgo(45), created_at: daysAgo(5), updated_at: minutesAgo(45) },
    { id: 7, name: 'SMTP - QQ 邮箱', credential_type: 'mail_smtp', group_id: null, data_plain: { host: 'smtp.qq.com', port: 465, ssl: true, username: 'demo@qq.com' }, secret: 'authcode-demo', status: 'valid', status_detail: { message: '凭证有效：SMTP 登录成功', account_info: { server: 'smtp.qq.com', port: 465 }, duration_ms: 300 }, last_verified_at: minutesAgo(30), last_valid_at: minutesAgo(30), created_at: daysAgo(2), updated_at: minutesAgo(30) },
  ]

  const mcpPermissions = [
    { tool_name: 'vault_list_credentials', enabled: true, description: '列出凭据' },
    { tool_name: 'vault_verify_credential', enabled: true, description: '触发验证' },
    { tool_name: 'vault_list_groups', enabled: true, description: '列出分组' },
    { tool_name: 'vault_list_plugins', enabled: true, description: '列出插件' },
    { tool_name: 'vault_get_credential', enabled: false, description: '获取凭据详情' },
    { tool_name: 'vault_create_credential', enabled: false, description: '创建凭据' },
    { tool_name: 'vault_update_credential', enabled: false, description: '修改凭据' },
    { tool_name: 'vault_delete_credential', enabled: false, description: '删除凭据' },
    { tool_name: 'vault_batch_verify', enabled: false, description: '批量验证' },
    { tool_name: 'vault_create_group', enabled: false, description: '创建分组' },
    { tool_name: 'vault_get_secret', enabled: false, description: '获取明文密钥（高危）' },
  ]

  return {
    initialized: true,
    unlocked: false,
    mcpRunning: false,
    mcpPort: 8765,
    groups,
    plugins,
    credentials,
    mcpPermissions,
    mcpToken: {
      id: 1,
      token_prefix: 'cv_a3f2',
      created_at: daysAgo(2),
      last_used_at: minutesAgo(2),
    },
    schedules: [
      { id: 1, name: '每日全量验证', trigger_type: 'cron', trigger_args: { hour: 2, minute: 0 }, target_scope: 'all', target_id: null, credential_ids: [], enabled: true, last_run_at: daysAgo(1), next_run_at: daysAgo(-1), created_at: daysAgo(20) },
      { id: 2, name: '工作分组巡检', trigger_type: 'interval', trigger_args: { hours: 6 }, target_scope: 'group', target_id: 1, credential_ids: [], enabled: false, last_run_at: null, next_run_at: null, created_at: daysAgo(10) },
      { id: 3, name: '重点凭据加密巡检', trigger_type: 'interval', trigger_args: { hours: 12 }, target_scope: 'credentials', target_id: null, credential_ids: [1, 3, 6], enabled: true, last_run_at: null, next_run_at: daysAgo(-0.5), created_at: daysAgo(4) },
    ],
    logLevel: 'INFO',
    closeAction: 'ask',
    autoLockMinutes: 0,
    lightMode: false,
    autostartEnabled: true,
    logs: [
      { level: 'INFO', text: logLine('INFO', 'main', '应用启动（frozen=False，日志级别=INFO，数据目录=…）', minutesDate(32)) },
      { level: 'DEBUG', text: logLine('DEBUG', 'backend.scheduler', '调度器已启动，加载 2 个计划', minutesDate(32)) },
      { level: 'INFO', text: logLine('INFO', 'backend.plugin_manager', '扫描到 40 个子插件（7 个类别）', minutesDate(30)) },
      { level: 'WARNING', text: logLine('WARNING', 'backend.api', '事件推送失败 vault:mcp_status: window not ready', minutesDate(14)) },
      { level: 'ERROR', text: logLine('ERROR', 'backend.scheduler', '加载计划失败 id=2: 触发器参数无效', minutesDate(6)) },
    ],
    sequence: 1000,
  }
}

export function createMockApi() {
  const state = createState()

  const nextId = () => ++state.sequence

  const groupName = (groupId) => {
    const group = state.groups.find((item) => item.id === groupId)
    return group ? group.name : null
  }

  const pluginOf = (credentialType) =>
    state.plugins.find((p) => p.credential_type === credentialType)

  /** 根据插件 fields_schema 计算敏感字段掩码（模拟后端 data_masked） */
  const maskedOf = (credential) => {
    const plugin = pluginOf(credential.credential_type)
    if (!plugin) return {}
    const masked = {}
    plugin.fields_schema
      .filter((f) => f.sensitive)
      .forEach((f) => {
        if (credential.secret) masked[f.key] = maskSecret(credential.secret)
      })
    return masked
  }

  const decorate = (credential) => ({
    group_name: groupName(credential.group_id),
    data_masked: maskedOf(credential),
    ...credential,
  })

  const recomputeCounts = () => {
    state.groups.forEach((group) => {
      group.credential_count = state.credentials.filter((item) => item.group_id === group.id).length
    })
    state.plugins.forEach((plugin) => {
      plugin.credential_count = state.credentials.filter((item) => item.credential_type === plugin.credential_type).length
    })
  }

  const requireUnlocked = () => {
    if (!state.unlocked) {
      const error = new Error('保险库已锁定')
      throw error
    }
  }

  const mockLog = (name, level, message) => {
    state.logs.push({ level, text: logLine(level, name, message) })
  }

  return {
    // ── 认证 / 锁 ──────────────────────────────────────────
    async is_initialized() {
      await delay(30)
      return { initialized: state.initialized }
    },

    async is_unlocked() {
      await delay(30)
      return { unlocked: state.unlocked }
    },

    async setup_master_password(password) {
      await delay()
      if (!password || String(password).length < 8) {
        return { ok: false, message: '主密码至少需要 8 个字符' }
      }
      state.initialized = true
      state.unlocked = true
      return { ok: true, message: '保险库创建成功' }
    },

    async unlock(password) {
      await delay()
      if (String(password) !== MOCK_PASSWORD) {
        return { ok: false, message: '主密码错误，请重试' }
      }
      state.unlocked = true
      return { ok: true, message: '解锁成功' }
    },

    async lock_vault() {
      await delay(40)
      state.unlocked = false
      return { ok: true }
    },

    async change_master_password(oldPassword, newPassword) {
      await delay(200)
      if (String(oldPassword) !== MOCK_PASSWORD) {
        return { ok: false, message: '原密码错误' }
      }
      if (!newPassword || String(newPassword).length < 8) {
        return { ok: false, message: '新密码至少需要 8 个字符' }
      }
      return { ok: true, message: '主密码已更新，所有凭据已重新加密' }
    },

    // ── 凭据 ──────────────────────────────────────────────
    async list_credentials(groupId = null, credentialType = null, status = null, search = null, credentialTypes = null) {
      await delay()
      requireUnlocked()
      const keyword = search ? String(search).toLowerCase() : null
      const typeSet = Array.isArray(credentialTypes) && credentialTypes.length ? new Set(credentialTypes) : null
      const items = state.credentials
        .filter((item) => (groupId == null ? true : item.group_id === groupId))
        .filter((item) => (credentialType == null ? true : item.credential_type === credentialType))
        .filter((item) => (typeSet == null ? true : typeSet.has(item.credential_type)))
        .filter((item) => (status == null ? true : item.status === status))
        .filter((item) => {
          if (!keyword) return true
          const plugin = pluginOf(item.credential_type)
          const haystack = [item.name, item.credential_type, plugin ? plugin.name : '', groupName(item.group_id) || '']
            .join(' ')
            .toLowerCase()
          return haystack.includes(keyword)
        })
        .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
      return clone(items.map(decorate))
    },

    async get_credential(id) {
      await delay(40)
      requireUnlocked()
      const found = state.credentials.find((item) => item.id === Number(id))
      if (!found) throw new Error(`凭据 #${id} 不存在`)
      // 编辑表单需要明文预填：mock 直接把 secret 放回 api_key
      const result = decorate(found)
      result.data = { ...(clone(found.data_plain) || {}), api_key: found.secret }
      return clone(result)
    },

    // 与真实后端签名保持一致：(name, credential_type, data, group_id, auto_verify)
    async create_credential(name, credentialType, data = {}, groupId = null, autoVerify = true) {
      await delay(180)
      requireUnlocked()
      const now = iso()
      const created = {
        id: nextId(),
        name,
        credential_type: credentialType,
        group_id: groupId == null ? null : Number(groupId),
        data_plain: clone(data) || {},
        secret: data.api_key || '',
        status: autoVerify ? 'valid' : 'unknown',
        status_detail: autoVerify ? { message: '验证通过', duration_ms: 200 } : {},
        last_verified_at: autoVerify ? now : null,
        last_valid_at: autoVerify ? now : null,
        created_at: now,
        updated_at: now,
      }
      delete created.data_plain.api_key
      state.credentials.unshift(created)
      recomputeCounts()
      emit('vault:credentials_changed', {})
      return clone(decorate(created))
    },

    async update_credential(id, name = null, groupId = null, data = null) {
      await delay(160)
      requireUnlocked()
      const target = state.credentials.find((item) => item.id === Number(id))
      if (!target) throw new Error(`凭据 #${id} 不存在`)
      if (name != null) target.name = name
      if (groupId != null) target.group_id = Number(groupId)
      if (data != null) {
        if (data.api_key) target.secret = data.api_key
        const plain = clone(data)
        delete plain.api_key
        target.data_plain = plain
      }
      target.updated_at = iso()
      recomputeCounts()
      emit('vault:credentials_changed', {})
      return clone(decorate(target))
    },

    async delete_credential(id) {
      await delay(140)
      requireUnlocked()
      const index = state.credentials.findIndex((item) => item.id === Number(id))
      if (index === -1) throw new Error(`凭据 #${id} 不存在`)
      state.credentials.splice(index, 1)
      recomputeCounts()
      emit('vault:credentials_changed', {})
      return { ok: true }
    },

    // 与真实后端签名保持一致：delete_credentials(ids) → { ok, deleted }
    async delete_credentials(ids) {
      await delay(140)
      requireUnlocked()
      const wanted = new Set((Array.isArray(ids) ? ids : []).map(Number))
      if (!wanted.size) return { ok: true, deleted: 0 }
      const before = state.credentials.length
      state.credentials = state.credentials.filter((item) => !wanted.has(item.id))
      const deleted = before - state.credentials.length
      recomputeCounts()
      emit('vault:credentials_changed', {})
      return { ok: true, deleted }
    },

    async verify_credential(id) {
      await delay(500)
      requireUnlocked()
      const target = state.credentials.find((item) => item.id === Number(id))
      if (!target) throw new Error(`凭据 #${id} 不存在`)
      const roll = Math.random()
      const status = roll > 0.7 ? 'invalid' : roll > 0.55 ? 'error' : 'valid'
      const message = status === 'valid' ? '验证通过' : status === 'invalid' ? '401 Unauthorized' : '连接超时'
      const now = iso()
      target.status = status
      target.status_detail = { message, duration_ms: 200 + Math.round(Math.random() * 300) }
      target.last_verified_at = now
      if (status === 'valid') target.last_valid_at = now
      target.updated_at = now
      emit('vault:credential_updated', clone(decorate(target)))
      return { status, message, account_info: status === 'valid' ? { organization: 'Mock Org' } : null }
    },

    async batch_verify(scope = 'all', groupId = null, credentialType = null, credentialIds = null) {
      requireUnlocked()
      const idSet = Array.isArray(credentialIds) && credentialIds.length
        ? new Set(credentialIds.map(Number))
        : null
      let picked = idSet
        ? state.credentials.filter((item) => idSet.has(item.id))
        : state.credentials
            .filter((item) => (scope === 'group' ? item.group_id === Number(groupId) : true))
            .filter((item) => (scope === 'type' ? item.credential_type === credentialType : true))
      // 与后端一致：跳过禁用批量验证的类型，以及插件本身被禁用的类型
      const noBatch = new Set(state.plugins
        .filter((p) => p.batch_verify === false || p.enabled === false)
        .map((p) => p.credential_type))
      const skipped = picked.filter((item) => noBatch.has(item.credential_type)).length
      const targets = picked.filter((item) => !noBatch.has(item.credential_type))
      let valid = 0
      let invalid = 0
      let error = 0
      for (let index = 0; index < targets.length; index += 1) {
        const target = targets[index]
        await delay(220)
        const roll = Math.random()
        const status = roll > 0.75 ? 'invalid' : roll > 0.6 ? 'error' : 'valid'
        const now = iso()
        target.status = status
        target.status_detail = { message: status === 'valid' ? '验证通过' : status === 'invalid' ? '401 Unauthorized' : '连接超时', duration_ms: 220 }
        target.last_verified_at = now
        if (status === 'valid') target.last_valid_at = now
        if (status === 'valid') valid += 1
        else if (status === 'invalid') invalid += 1
        else error += 1
        emit('vault:batch_verify_progress', {
          current: index + 1,
          total: targets.length,
          credential_name: target.name,
          status,
        })
      }
      emit('vault:batch_verify_done', { total: targets.length, valid, invalid, error, skipped })
      emit('vault:credentials_changed', {})
      return { ok: true, total: targets.length }
    },

    async check_duplicate_credential(credentialType, data = {}) {
      await delay(120)
      requireUnlocked()
      const plugin = pluginOf(credentialType)
      const newValues = new Set(
        (plugin ? plugin.fields_schema : [])
          .filter((f) => f.sensitive)
          .map((f) => String(data[f.key] || '').trim())
          .filter(Boolean),
      )
      if (!newValues.size) return []
      return state.credentials
        .filter((item) => item.secret && newValues.has(item.secret))
        .map((item) => ({ id: item.id, name: item.name, credential_type: item.credential_type }))
    },

    // ── 分组 ──────────────────────────────────────────────
    async list_groups() {
      await delay()
      recomputeCounts()
      return clone([...state.groups].sort((a, b) => a.sort_order - b.sort_order))
    },

    async create_group(name, description = '', color = '#409EFF') {
      await delay(140)
      const created = { id: nextId(), name, description, color, sort_order: state.groups.length, created_at: iso(), credential_count: 0 }
      state.groups.push(created)
      return clone(created)
    },

    async update_group(id, name = null, description = null, color = null) {
      await delay(140)
      const target = state.groups.find((item) => item.id === Number(id))
      if (!target) throw new Error(`分组 #${id} 不存在`)
      if (name != null) target.name = name
      if (description != null) target.description = description
      if (color != null) target.color = color
      recomputeCounts()
      return clone(target)
    },

    async delete_group(id) {
      await delay(140)
      const index = state.groups.findIndex((item) => item.id === Number(id))
      if (index === -1) throw new Error(`分组 #${id} 不存在`)
      state.groups.splice(index, 1)
      state.credentials.forEach((item) => {
        if (item.group_id === Number(id)) item.group_id = null
      })
      recomputeCounts()
      emit('vault:credentials_changed', {})
      return { ok: true }
    },

    // ── 插件（放入 plugins/ 目录即自动加载）────────────────
    async list_plugins() {
      await delay()
      recomputeCounts()
      return clone(state.plugins)
    },

    // 启用/禁用插件：只切换状态位，凭据一律保留（禁用后依赖插件的能力不可用）
    async set_plugin_enabled(credentialType, enabled) {
      await delay(120)
      requireUnlocked()
      const plugin = state.plugins.find((p) => p.credential_type === credentialType)
      if (!plugin) return { ok: false, message: '插件不存在' }
      plugin.enabled = !!enabled
      emit('vault:plugins_changed', {})
      return { ok: true, credential_type: credentialType, enabled: !!enabled }
    },

    // ── MCP ───────────────────────────────────────────────
    async get_version() {
      return { version: '1.0.0', build: 'mock-dev', schema_version: '1.0.0',
        display: '1.0.0 (mock-dev)' }
    },

    async get_mcp_status() {
      await delay(60)
      return {
        running: state.mcpRunning,
        port: state.mcpPort,
        active_tokens: state.mcpToken ? 1 : 0,
        url: `http://localhost:${state.mcpPort}/sse`,
      }
    },

    async start_mcp() {
      await delay(500)
      state.mcpRunning = true
      emit('vault:mcp_status', { running: true, port: state.mcpPort, active_tokens: 0 })
      return { ok: true, port: state.mcpPort, url: `http://localhost:${state.mcpPort}/sse` }
    },

    async stop_mcp() {
      await delay(300)
      state.mcpRunning = false
      emit('vault:mcp_status', { running: false, port: state.mcpPort, active_tokens: 0 })
      return { ok: true }
    },

    async list_mcp_permissions() {
      await delay()
      return clone(state.mcpPermissions)
    },

    async set_mcp_permission(toolName, enabled) {
      await delay(100)
      const target = state.mcpPermissions.find((item) => item.tool_name === toolName)
      if (!target) return { ok: false, message: '工具不存在' }
      target.enabled = !!enabled
      return { ok: true }
    },

    async get_mcp_token() {
      await delay()
      if (!state.mcpToken) {
        state.mcpToken = {
          id: nextId(),
          token_prefix: `cv_${Math.random().toString(36).slice(2, 6)}`,
          created_at: iso(),
          last_used_at: null,
        }
      }
      return { exists: true, ...clone(state.mcpToken) }
    },

    async rotate_mcp_token() {
      await delay(300)
      const secret = `cv_${Math.random().toString(36).slice(2)}${Math.random().toString(36).slice(2)}`
      state.mcpToken = {
        id: nextId(),
        token_prefix: secret.slice(0, 7),
        created_at: iso(),
        last_used_at: null,
      }
      return {
        id: state.mcpToken.id,
        token: secret,                   // 明文仅此一次
        token_prefix: state.mcpToken.token_prefix,
      }
    },

    // ── 定时计划 ──────────────────────────────────────────
    async list_schedules() {
      await delay()
      return clone(state.schedules)
    },

    async create_schedule(name, triggerType, triggerArgs = {}, targetScope = 'all', targetId = null,
      credentialIds = []) {
      await delay(160)
      const created = {
        id: nextId(),
        name,
        trigger_type: triggerType,
        trigger_args: clone(triggerArgs) || {},
        target_scope: targetScope,
        target_id: targetId == null ? null : Number(targetId),
        // 仅 target_scope === 'credentials' 时有值（目录树勾选的具体凭据 id）
        credential_ids: (credentialIds || []).map(Number),
        enabled: true,
        last_run_at: null,
        next_run_at: null,
        created_at: iso(),
      }
      state.schedules.push(created)
      return clone(created)
    },

    async update_schedule(id, payload = {}) {
      await delay(160)
      const target = state.schedules.find((item) => item.id === Number(id))
      if (!target) throw new Error(`计划 #${id} 不存在`)
      Object.assign(target, clone(payload) || {})
      return clone(target)
    },

    async delete_schedule(id) {
      await delay(120)
      const index = state.schedules.findIndex((item) => item.id === Number(id))
      if (index === -1) throw new Error(`计划 #${id} 不存在`)
      state.schedules.splice(index, 1)
      return { ok: true }
    },

    // ── 托盘 ──────────────────────────────────────────────
    async get_tray_status() {
      await delay(40)
      return {
        mcp_running: state.mcpRunning,
        mcp_port: state.mcpPort,
        active_tokens: state.mcpToken ? 1 : 0,
        is_locked: !state.unlocked,
      }
    },

    async minimize_to_tray() {
      await delay(40)
      return { ok: true }
    },

    // ── 关闭窗口行为 ──────────────────────────────────────
    async get_close_action() {
      await delay(40)
      return {
        action: state.closeAction,
        options: [
          { value: 'ask', label: '每次询问我' },
          { value: 'quit', label: '直接退出程序' },
          { value: 'background', label: '后台运行（最小化到托盘）' },
        ],
      }
    },

    async set_close_action(action) {
      await delay(40)
      if (!['ask', 'quit', 'background'].includes(action)) {
        return { ok: false, message: `无效的关闭行为：${action || '(空)'}` }
      }
      state.closeAction = action
      return { ok: true, action }
    },

    async confirm_close_action(action, remember = false) {
      await delay(40)
      if (remember && ['quit', 'background'].includes(action)) {
        state.closeAction = action
      }
      return { ok: true, action }
    },

    // ── 空闲自动锁定 ──────────────────────────────────────
    async get_auto_lock() {
      await delay(40)
      return {
        minutes: state.autoLockMinutes,
        options: [
          { value: 0, label: '关闭' },
          { value: 1, label: '1 分钟' },
          { value: 5, label: '5 分钟' },
          { value: 10, label: '10 分钟' },
          { value: 15, label: '15 分钟' },
          { value: 30, label: '30 分钟' },
          { value: 60, label: '60 分钟' },
        ],
      }
    },

    async set_auto_lock(minutes) {
      await delay(40)
      if (![0, 1, 5, 10, 15, 30, 60].includes(Number(minutes))) {
        return { ok: false, message: `不支持的档位：${minutes} 分钟` }
      }
      state.autoLockMinutes = Number(minutes)
      return { ok: true, minutes: state.autoLockMinutes }
    },

    async touch_activity() {
      return { ok: true }
    },

    // ── 轻量模式 ──────────────────────────────────────────
    async get_light_mode() {
      await delay(40)
      return { enabled: state.lightMode }
    },

    async set_light_mode(enabled) {
      await delay(40)
      state.lightMode = Boolean(enabled)
      return { ok: true, enabled: state.lightMode }
    },

    // ── 开机自启动 ────────────────────────────────────────
    async get_autostart() {
      await delay(40)
      return {
        supported: true,
        enabled: state.autostartEnabled,
        platform: 'mock',
        target: 'mock://autostart/credential-vault',
      }
    },

    async set_autostart(enabled) {
      await delay(40)
      state.autostartEnabled = Boolean(enabled)
      return { ok: true, enabled: state.autostartEnabled }
    },

    // ── 简易操作（插件可选能力）────────────────────────────
    async list_credential_actions(credentialId) {
      await delay(60)
      const cred = state.credentials.find((c) => c.id === Number(credentialId))
      if (!cred) return []
      if (cred.credential_type === 'mail_smtp') {
        return [{
          id: 'inbox', name: '查看收件箱', kind: 'form', submit: '拉取',
          fields: [{ key: 'count', label: '条数', type: 'text', default: '10' }],
        }]
      }
      const plugin = state.plugins.find((p) => p.credential_type === cred.credential_type)
      return plugin && plugin.has_actions
        ? [{ id: 'chat', name: '简单对话', kind: 'chat', model: 'mock-model' }]
        : []
    },

    async run_credential_action(credentialId, actionId, params) {
      await delay(400)
      requireUnlocked()
      if (actionId === 'inbox') {
        return {
          ok: true,
          output: '共 3 封，显示最近 3 封（INBOX）：\n\n1. [2026-10-05 09:12] alert@x.com\n   主题：登录提醒',
        }
      }
      const messages = (params && params.messages) || []
      const last = messages.length ? messages[messages.length - 1].content : ''
      return { ok: true, output: `（Mock 回复）你说的是：「${last}」` }
    },

    async list_credential_models(credentialId, force = false) {
      await delay(200)
      const cred = state.credentials.find((c) => c.id === Number(credentialId))
      if (!cred) return []
      return ['mock-model', 'mock-model-mini', 'mock-model-pro']
    },

    async stream_credential_action(credentialId, actionId, params) {
      requireUnlocked()
      const streamId = `mock-${Date.now()}`
      const messages = (params && params.messages) || []
      const last = messages.length ? messages[messages.length - 1].content : ''
      const reply = `（Mock 流式回复）你说的是：「${last}」`
      const chunks = reply.match(/.{1,4}/g) || []
      let index = 0
      const timer = setInterval(() => {
        if (index >= chunks.length) {
          clearInterval(timer)
          emit('vault:action_stream', { stream_id: streamId, done: true, ok: true, output: reply, error: '' })
          return
        }
        emit('vault:action_stream', { stream_id: streamId, delta: chunks[index] })
        index += 1
      }, 70)
      return { ok: true, stream_id: streamId }
    },

    // ── 备份 / 恢复 ────────────────────────────────────────
    async export_backup(scope = 'all', encrypted = true) {
      await delay(140)
      requireUnlocked()
      const count = scope === 'all' ? state.credentials.length : state.credentials.length
      return { ok: true, path: `/mock/credential-vault-backup-${Date.now()}.json`, count, encrypted: !!encrypted }
    },

    async import_backup() {
      await delay(220)
      requireUnlocked()
      return {
        ok: true, encrypted: false, imported: 0,
        skipped_plugin: 0, skipped_duplicate: 0, created_groups: 0,
        path: '/mock/credential-vault-backup.json',
      }
    },

    // ── 日志 ──────────────────────────────────────────────
    async get_log_config() {
      await delay(40)
      return { level: state.logLevel, levels: clone(LOG_LEVELS), log_file: MOCK_LOG_FILE }
    },

    async set_log_level(level) {
      await delay(80)
      const next = String(level || '').trim().toUpperCase()
      if (!LOG_LEVELS.includes(next)) {
        return { ok: false, message: `无效日志级别：${level || '(空)'}` }
      }
      state.logLevel = next
      mockLog('backend.api', 'INFO', `日志级别已设置为 ${next}`)
      return { ok: true, level: next }
    },

    async get_logs(limit = 200, level = '') {
      await delay(80)
      const max = Math.max(1, Math.min(Number(limit) || 200, 2000))
      const threshold = String(level || '').trim().toUpperCase()
      const filtered =
        threshold && LOG_LEVELS.includes(threshold)
          ? state.logs.filter((entry) => LOG_LEVEL_VALUES[entry.level] >= LOG_LEVEL_VALUES[threshold])
          : state.logs
      const entries = filtered.slice(-max).map((entry) => entry.text)
      return { ok: true, entries, count: entries.length, log_file: MOCK_LOG_FILE }
    },

    async clear_logs() {
      await delay(60)
      state.logs.length = 0
      mockLog('backend.api', 'INFO', '日志已清空')
      return { ok: true, message: '日志已清空' }
    },
  }
}

/** 在普通浏览器中安装 Mock 桥接（仅开发模式调用）。 */
export async function installDevBridge(gracePeriod = 800) {
  if (window.pywebview && window.pywebview.api) return false
  await delay(gracePeriod)
  if (window.pywebview && window.pywebview.api) return false

  window.pywebview = { api: createMockApi() }
  window.__VAULT_MOCK__ = { password: MOCK_PASSWORD }
  window.dispatchEvent(new CustomEvent('pywebviewready'))
  console.info(
    `[Credential Vault] 开发模式：未检测到 pywebview，已启用内存 Mock 后端。主密码：${MOCK_PASSWORD}`,
  )
  return true
}
