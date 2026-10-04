/**
 * 仅开发模式使用的内存 Mock 后端。
 *
 * 目的：在没有 pywebview 的普通浏览器里跑 `npm run dev` 时，前端仍然可以完整
 * 走通所有页面与交互，便于开发与联调。生产构建中该模块通过 `import.meta.env.DEV`
 * 守卫被完全摇树移除，不会出现在 dist 里。
 *
 * Mock 主密码：demo1234
 */

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

function createState() {
  const groups = [
    { id: 1, name: '工作项目', description: '2026 年 Q4 核心项目', color: '#409EFF', sort_order: 0, created_at: daysAgo(30), credential_count: 3 },
    { id: 2, name: '个人', description: '个人测试用途', color: '#67C23A', sort_order: 1, created_at: daysAgo(20), credential_count: 2 },
    { id: 3, name: '基础设施', description: '云资源访问密钥', color: '#E6A23C', sort_order: 2, created_at: daysAgo(12), credential_count: 1 },
  ]

  const plugins = [
    {
      id: 1,
      name: 'LLM API Key',
      version: '1.0.0',
      author: 'Credential Vault',
      description: '管理和验证各大 LLM 厂商的 API Key',
      credential_type: 'llm_api_key',
      icon: '🤖',
      is_builtin: true,
      enabled: true,
      installed_at: daysAgo(30),
      credential_count: 4,
      fields_schema: [
        { key: 'vendor', label: '厂商', type: 'select', required: true, sensitive: false, default: 'openai', placeholder: null, options: ['openai', 'anthropic', 'google', 'azure', 'deepseek'] },
        { key: 'base_url', label: 'Base URL', type: 'url', required: true, sensitive: false, default: 'https://api.openai.com/v1', placeholder: 'https://api.openai.com/v1', options: null },
        { key: 'api_key', label: 'API Key', type: 'password', required: true, sensitive: true, default: null, placeholder: 'sk-...', options: null },
        { key: 'note', label: '备注', type: 'textarea', required: false, sensitive: false, default: null, placeholder: '用途说明', options: null },
      ],
    },
    {
      id: 2,
      name: 'Cloud AK/SK',
      version: '0.1.0',
      author: 'community',
      description: 'AWS / 阿里云 / 腾讯云 AK/SK 验证',
      credential_type: 'cloud_aksk',
      icon: '☁️',
      is_builtin: false,
      enabled: true,
      installed_at: daysAgo(8),
      credential_count: 2,
      fields_schema: [
        { key: 'provider', label: '云厂商', type: 'select', required: true, sensitive: false, default: 'aws', placeholder: null, options: ['aws', 'aliyun', 'tencent'] },
        { key: 'access_key_id', label: 'Access Key ID', type: 'text', required: true, sensitive: false, default: null, placeholder: 'AKIA...', options: null },
        { key: 'access_key_secret', label: 'Access Key Secret', type: 'password', required: true, sensitive: true, default: null, placeholder: null, options: null },
        { key: 'region', label: 'Region', type: 'text', required: false, sensitive: false, default: 'us-east-1', placeholder: 'us-east-1', options: null },
        { key: 'alert_threshold', label: '余额告警阈值', type: 'number', required: false, sensitive: false, default: '100', placeholder: '100', options: null },
        { key: 'enable_monitor', label: '启用监控', type: 'checkbox', required: false, sensitive: false, default: 'true', placeholder: null, options: null },
        { key: 'note', label: '备注', type: 'textarea', required: false, sensitive: false, default: null, placeholder: null, options: null },
      ],
    },
  ]

  const credentials = [
    { id: 1, name: 'OpenAI 生产账号', credential_type: 'llm_api_key', group_id: 1, data_plain: { vendor: 'openai', base_url: 'https://api.openai.com/v1', api_key: 'sk-proj-demo-aaaaaaaaaaaaaaaa', note: '生产环境主账号' }, status: 'valid', status_detail: { message: '验证通过', account_info: { organization: 'Acme Inc', balance: '$128.40' } }, last_verified_at: minutesAgo(2), created_at: daysAgo(30), updated_at: minutesAgo(2) },
    { id: 2, name: 'Claude 主账号', credential_type: 'llm_api_key', group_id: 1, data_plain: { vendor: 'anthropic', base_url: 'https://api.anthropic.com/v1', api_key: 'sk-ant-demo-bbbbbbbbbbbb', note: '内部工具调用' }, status: 'invalid', status_detail: { message: '401 Unauthorized' }, last_verified_at: minutesAgo(60), created_at: daysAgo(25), updated_at: minutesAgo(60) },
    { id: 3, name: '测试 Key', credential_type: 'llm_api_key', group_id: null, data_plain: { vendor: 'deepseek', base_url: 'https://api.deepseek.com/v1', api_key: 'sk-demo-cccccccccccc', note: '' }, status: 'error', status_detail: { message: '连接超时' }, last_verified_at: minutesAgo(180), created_at: daysAgo(10), updated_at: minutesAgo(180) },
    { id: 4, name: 'AWS 生产 AK', credential_type: 'cloud_aksk', group_id: 3, data_plain: { provider: 'aws', access_key_id: 'AKIAIOSFODNN7EXAMPLE', access_key_secret: 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY', region: 'us-east-1', alert_threshold: 100, enable_monitor: true, note: '生产账户' }, status: 'unknown', status_detail: {}, last_verified_at: null, created_at: daysAgo(7), updated_at: daysAgo(7) },
    { id: 5, name: '阿里云备份', credential_type: 'cloud_aksk', group_id: 2, data_plain: { provider: 'aliyun', access_key_id: 'LTAI5tExample', access_key_secret: 'demo-secret-value', region: 'cn-hangzhou', alert_threshold: 50, enable_monitor: false, note: '' }, status: 'valid', status_detail: { message: '验证通过' }, last_verified_at: minutesAgo(25), created_at: daysAgo(5), updated_at: minutesAgo(25) },
    { id: 6, name: 'Google Gemini', credential_type: 'llm_api_key', group_id: 2, data_plain: { vendor: 'google', base_url: 'https://generativelanguage.googleapis.com/v1beta', api_key: 'AIza-demo-dddddddddddd', note: '' }, status: 'valid', status_detail: { message: '验证通过' }, last_verified_at: minutesAgo(45), created_at: daysAgo(3), updated_at: minutesAgo(45) },
  ]

  const history = credentials.map((credential, index) => ({
    id: index + 1,
    credential_id: credential.id,
    status: credential.status === 'unknown' ? 'error' : credential.status,
    result_data: credential.status_detail,
    error_message: credential.status === 'invalid' ? '401 Unauthorized' : '',
    trigger_type: 'manual',
    duration_ms: 180 + index * 40,
    verified_at: credential.last_verified_at || daysAgo(7),
  }))

  const mcpPermissions = [
    { tool_name: 'vault_list_credentials', enabled: true, description: '列出凭据' },
    { tool_name: 'vault_verify_credential', enabled: true, description: '触发验证' },
    { tool_name: 'vault_list_groups', enabled: true, description: '列出分组' },
    { tool_name: 'vault_list_plugins', enabled: true, description: '列出插件' },
    { tool_name: 'vault_get_credential', enabled: false, description: '获取凭据详情（掩码）' },
    { tool_name: 'vault_create_credential', enabled: false, description: '创建凭据' },
    { tool_name: 'vault_update_credential', enabled: false, description: '修改凭据' },
    { tool_name: 'vault_delete_credential', enabled: false, description: '删除凭据' },
    { tool_name: 'vault_batch_verify', enabled: false, description: '批量验证' },
    { tool_name: 'vault_create_group', enabled: false, description: '创建分组' },
    { tool_name: 'vault_install_plugin', enabled: false, description: '安装插件' },
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
    history,
    mcpPermissions,
    mcpToken: {
      id: 1,
      token_prefix: 'cv_a3f2',
      created_at: daysAgo(2),
      last_used_at: minutesAgo(2),
    },
    schedules: [
      { id: 1, name: '每日全量验证', trigger_type: 'cron', trigger_args: { hour: 2, minute: 0 }, target_scope: 'all', target_id: null, enabled: true, last_run_at: daysAgo(1), next_run_at: daysAgo(-1), created_at: daysAgo(20) },
      { id: 2, name: '工作分组巡检', trigger_type: 'interval', trigger_args: { hours: 6 }, target_scope: 'group', target_id: 1, enabled: false, last_run_at: null, next_run_at: null, created_at: daysAgo(10) },
    ],
    logLevel: 'INFO',
    closeAction: 'ask',
    logs: [
      { level: 'INFO', text: logLine('INFO', 'main', '应用启动（frozen=False，日志级别=INFO，数据目录=…）', minutesDate(32)) },
      { level: 'DEBUG', text: logLine('DEBUG', 'backend.scheduler', '调度器已启动，加载 2 个计划', minutesDate(32)) },
      { level: 'INFO', text: logLine('INFO', 'backend.plugin_manager', '内置插件 llm_api_key 加载成功', minutesDate(30)) },
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

  const decorate = (credential) => ({
    group_name: groupName(credential.group_id),
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
    async list_credentials(groupId = null, credentialType = null, status = null, search = null) {
      await delay()
      requireUnlocked()
      const keyword = search ? String(search).toLowerCase() : null
      const items = state.credentials
        .filter((item) => (groupId == null ? true : item.group_id === groupId))
        .filter((item) => (credentialType == null ? true : item.credential_type === credentialType))
        .filter((item) => (status == null ? true : item.status === status))
        .filter((item) => {
          if (!keyword) return true
          const plugin = state.plugins.find((p) => p.credential_type === item.credential_type)
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
      return clone(decorate(found))
    },

    // 与真实后端签名保持一致：(name, credential_type, data, group_id, auto_verify)
    async create_credential(name, credentialType, data = {}, groupId = null, autoVerify = true) {
      await delay(180)
      requireUnlocked()
      const created = {
        id: nextId(),
        name,
        credential_type: credentialType,
        group_id: groupId == null ? null : Number(groupId),
        data_plain: clone(data) || {},
        status: autoVerify ? 'valid' : 'unknown',
        status_detail: autoVerify ? { message: '验证通过' } : {},
        last_verified_at: autoVerify ? iso() : null,
        created_at: iso(),
        updated_at: iso(),
      }
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
      if (data != null) target.data_plain = clone(data)
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

    async verify_credential(id) {
      await delay(500)
      requireUnlocked()
      const target = state.credentials.find((item) => item.id === Number(id))
      if (!target) throw new Error(`凭据 #${id} 不存在`)
      const roll = Math.random()
      const status = roll > 0.7 ? 'invalid' : roll > 0.55 ? 'error' : 'valid'
      const message = status === 'valid' ? '验证通过' : status === 'invalid' ? '401 Unauthorized' : '连接超时'
      target.status = status
      target.status_detail = { message }
      target.last_verified_at = iso()
      target.updated_at = iso()
      state.history.unshift({
        id: nextId(),
        credential_id: target.id,
        status,
        result_data: target.status_detail,
        error_message: status === 'valid' ? '' : message,
        trigger_type: 'manual',
        duration_ms: 200 + Math.round(Math.random() * 300),
        verified_at: iso(),
      })
      emit('vault:credential_updated', clone(decorate(target)))
      return { status, message, account_info: status === 'valid' ? { organization: 'Mock Org' } : null }
    },

    async batch_verify(scope = 'all', groupId = null, credentialType = null) {
      requireUnlocked()
      const targets = state.credentials
        .filter((item) => (scope === 'group' ? item.group_id === Number(groupId) : true))
        .filter((item) => (scope === 'type' ? item.credential_type === credentialType : true))
      let valid = 0
      let invalid = 0
      let error = 0
      for (let index = 0; index < targets.length; index += 1) {
        const target = targets[index]
        await delay(220)
        const roll = Math.random()
        const status = roll > 0.75 ? 'invalid' : roll > 0.6 ? 'error' : 'valid'
        target.status = status
        target.status_detail = { message: status === 'valid' ? '验证通过' : status === 'invalid' ? '401 Unauthorized' : '连接超时' }
        target.last_verified_at = iso()
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
      emit('vault:batch_verify_done', { total: targets.length, valid, invalid, error })
      emit('vault:credentials_changed', {})
      return { ok: true, total: targets.length }
    },

    async get_verification_history(credentialId, limit = 20) {
      await delay()
      requireUnlocked()
      const items = state.history
        .filter((item) => item.credential_id === Number(credentialId))
        .slice(0, Number(limit) || 20)
      return clone(items)
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

    // ── 插件 ──────────────────────────────────────────────
    async list_plugins() {
      await delay()
      recomputeCounts()
      return clone(state.plugins)
    },

    async install_plugin(zipPath) {
      await delay(600)
      const type = `mock_plugin_${nextId()}`
      const plugin = {
        id: nextId(),
        name: String(zipPath).split('/').pop().replace(/\.zip$/i, '') || 'Mock 插件',
        version: '0.1.0',
        author: 'mock',
        description: '开发模式安装了本地插件包（模拟）',
        credential_type: type,
        icon: '📦',
        is_builtin: false,
        enabled: true,
        installed_at: iso(),
        credential_count: 0,
        fields_schema: [
          { key: 'token', label: 'Token', type: 'password', required: true, sensitive: true, default: null, placeholder: null, options: null },
        ],
      }
      state.plugins.push(plugin)
      emit('vault:plugin_installed', clone(plugin))
      return { ok: true, message: `插件 ${plugin.name} 安装成功` }
    },

    async uninstall_plugin(credentialType) {
      await delay(300)
      const index = state.plugins.findIndex((item) => item.credential_type === credentialType)
      if (index === -1) return { ok: false, message: '插件不存在' }
      if (state.plugins[index].is_builtin) return { ok: false, message: '内置插件不可卸载' }
      state.plugins.splice(index, 1)
      state.credentials = state.credentials.filter((item) => item.credential_type !== credentialType)
      recomputeCounts()
      emit('vault:credentials_changed', {})
      return { ok: true, message: '插件已卸载' }
    },

    async toggle_plugin(credentialType, enabled) {
      await delay(120)
      const target = state.plugins.find((item) => item.credential_type === credentialType)
      if (!target) return { ok: false, message: '插件不存在' }
      target.enabled = !!enabled
      return { ok: true }
    },

    // ── MCP ───────────────────────────────────────────────
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

    async create_schedule(name, triggerType, triggerArgs = {}, targetScope = 'all', targetId = null) {
      await delay(160)
      const created = {
        id: nextId(),
        name,
        trigger_type: triggerType,
        trigger_args: clone(triggerArgs) || {},
        target_scope: targetScope,
        target_id: targetId == null ? null : Number(targetId),
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