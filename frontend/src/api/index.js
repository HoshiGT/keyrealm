/**
 * pywebview JS-API 桥接层。
 *
 * pywebview 会把后端 Python 对象注入为 `window.pywebview.api`，
 * 所有方法均为异步（返回 Promise）。注入完成时会派发 `pywebviewready` 事件。
 *
 * 约定：
 *  - 后端返回 `{ ok: false, message }` 视为业务失败，这里统一抛出 ApiError，
 *    调用方只需 try/catch 即可（无需逐处判断 res.ok）。
 *  - 后端抛出的 Python 异常会以 rejected Promise 形式到达，同样转成 ApiError。
 */

const READY_EVENT = 'pywebviewready'
const READY_TIMEOUT = 20000

export class ApiError extends Error {
  constructor(message, method) {
    super(message)
    this.name = 'ApiError'
    this.method = method
  }
}

function getBridge() {
  return window.pywebview && window.pywebview.api ? window.pywebview.api : null
}

let readyPromise = null

/** 等待 pywebview 注入完成。 */
export function whenReady() {
  const bridge = getBridge()
  if (bridge) return Promise.resolve(bridge)
  if (readyPromise) return readyPromise

  readyPromise = new Promise((resolve, reject) => {
    let timer = null
    const cleanup = () => {
      window.removeEventListener(READY_EVENT, onReady)
      if (timer) clearTimeout(timer)
    }
    const onReady = () => {
      cleanup()
      resolve(getBridge())
    }
    window.addEventListener(READY_EVENT, onReady)
    timer = setTimeout(() => {
      cleanup()
      readyPromise = null
      const late = getBridge()
      if (late) resolve(late)
      else reject(new ApiError('无法连接后端服务：pywebview 未就绪', 'whenReady'))
    }, READY_TIMEOUT)
  })
  return readyPromise
}

function unwrap(result, method) {
  if (result && typeof result === 'object' && result.ok === false) {
    throw new ApiError(result.message || '操作失败', method)
  }
  return result
}

/** 调用任意后端方法。 */
export async function call(method, ...args) {
  const bridge = await whenReady()
  const fn = bridge[method]
  if (typeof fn !== 'function') {
    throw new ApiError(`后端未实现接口：${method}`, method)
  }
  let result
  try {
    result = await fn(...args)
  } catch (err) {
    const message = err && err.message ? err.message : String(err)
    throw new ApiError(message || `调用 ${method} 失败`, method)
  }
  return unwrap(result, method)
}

/** 监听后端推送事件，返回取消订阅函数。 */
export function onEvent(name, handler) {
  const listener = (event) => handler(event && event.detail)
  window.addEventListener(name, listener)
  return () => window.removeEventListener(name, listener)
}

export const EVENTS = {
  credentialUpdated: 'vault:credential_updated',
  credentialsChanged: 'vault:credentials_changed',
  batchProgress: 'vault:batch_verify_progress',
  batchDone: 'vault:batch_verify_done',
  pluginInstalled: 'vault:plugin_installed',
  mcpStatus: 'vault:mcp_status',
}

export const api = {
  // ── 认证 / 锁 ────────────────────────────────────────────
  setupMasterPassword: (password) => call('setup_master_password', password),
  unlock: (password) => call('unlock', password),
  lockVault: () => call('lock_vault'),
  isInitialized: () => call('is_initialized'),
  isUnlocked: () => call('is_unlocked'),
  changeMasterPassword: (oldPassword, newPassword) =>
    call('change_master_password', oldPassword, newPassword),

  // ── 凭据 ────────────────────────────────────────────────
  listCredentials: (filters = {}) =>
    call(
      'list_credentials',
      filters.group_id ?? null,
      filters.credential_type ?? null,
      filters.status ?? null,
      filters.search ? filters.search.trim() : null,
    ),
  getCredential: (id) => call('get_credential', id),
  createCredential: (payload) =>
    call(
      'create_credential',
      payload.name,
      payload.credential_type,
      payload.group_id ?? null,
      payload.data ?? {},
      payload.auto_verify !== false,
    ),
  updateCredential: (id, payload) =>
    call(
      'update_credential',
      id,
      payload.name ?? null,
      payload.group_id ?? null,
      payload.data ?? null,
    ),
  deleteCredential: (id) => call('delete_credential', id),
  verifyCredential: (id) => call('verify_credential', id),
  batchVerify: (scope = 'all', groupId = null, credentialType = null) =>
    call('batch_verify', scope, groupId, credentialType),
  getVerificationHistory: (credentialId, limit = 20) =>
    call('get_verification_history', credentialId, limit),

  // ── 分组 ────────────────────────────────────────────────
  listGroups: () => call('list_groups'),
  createGroup: (name, description = '', color = '#409EFF') =>
    call('create_group', name, description, color),
  updateGroup: (id, payload = {}) =>
    call('update_group', id, payload.name ?? null, payload.description ?? null, payload.color ?? null),
  deleteGroup: (id) => call('delete_group', id),

  // ── 插件 ────────────────────────────────────────────────
  listPlugins: () => call('list_plugins'),
  installPlugin: (zipPath) => call('install_plugin', zipPath),
  uninstallPlugin: (credentialType) => call('uninstall_plugin', credentialType),
  togglePlugin: (credentialType, enabled) => call('toggle_plugin', credentialType, enabled),

  // ── MCP ─────────────────────────────────────────────────
  getMcpStatus: () => call('get_mcp_status'),
  startMcp: () => call('start_mcp'),
  stopMcp: () => call('stop_mcp'),
  listMcpPermissions: () => call('list_mcp_permissions'),
  setMcpPermission: (toolName, enabled) => call('set_mcp_permission', toolName, enabled),
  listMcpTokens: () => call('list_mcp_tokens'),
  createMcpToken: (name, toolsAllowed = [], expiresDays = null) =>
    call('create_mcp_token', name, toolsAllowed, expiresDays),
  revokeMcpToken: (tokenId) => call('revoke_mcp_token', tokenId),
  updateTokenScope: (tokenId, toolsAllowed) =>
    call('update_token_scope', tokenId, toolsAllowed),

  // ── 定时计划 ────────────────────────────────────────────
  listSchedules: () => call('list_schedules'),
  createSchedule: (payload) =>
    call(
      'create_schedule',
      payload.name,
      payload.trigger_type,
      payload.trigger_args ?? {},
      payload.target_scope ?? 'all',
      payload.target_id ?? null,
    ),
  updateSchedule: (id, payload) => call('update_schedule', id, payload),
  deleteSchedule: (id) => call('delete_schedule', id),

  // ── 仪表盘 ──────────────────────────────────────────────
  getDashboardStats: () => call('get_dashboard_stats'),

  // ── 托盘 ────────────────────────────────────────────────
  getTrayStatus: () => call('get_tray_status'),
  minimizeToTray: () => call('minimize_to_tray'),
}

export default api