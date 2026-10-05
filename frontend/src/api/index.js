/**
 * pywebview JS-API 桥接层。
 *
 * pywebview 会把后端 Python 对象注入为 `window.pywebview.api`，
 * 所有方法均为异步（返回 Promise）。注入完成时会派发 `pywebviewready` 事件。
 *
 * 约定：
 *  - 后端返回 `{ ok: false, message }` 或 `{ error: '错误码', message?/details? }`
 *    均视为业务失败，这里统一抛出 ApiError，
 *    调用方只需 try/catch 即可（无需逐处判断 res.ok）。
 *  - 后端抛出的 Python 异常会以 rejected Promise 形式到达，同样转成 ApiError。
 *  - pywebview 桥接只支持位置参数，各方法传参顺序必须与 main.py 中
 *    VaultApi 对应方法的签名严格一致（dev mock 也保持同样签名）。
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
  if (result && typeof result === 'object' && !Array.isArray(result)) {
    // 后端约定一：{ ok: false, message } 表示业务失败
    if (result.ok === false) {
      throw new ApiError(result.message || '操作失败', method)
    }
    // 后端约定二：{ error: '错误码', message?/details? } 同样表示失败
    // （如 vault_locked / validation_failed / not_found），不能当成成功返回，
    // 否则调用方会误报成功（例如凭据校验失败却提示"已创建"）。
    if (typeof result.error === 'string') {
      const detail = Array.isArray(result.details) ? result.details.join('；') : ''
      throw new ApiError(result.message || detail || `操作失败（${result.error}）`, method)
    }
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
  pluginsChanged: 'vault:plugins_changed',
  actionStream: 'vault:action_stream',
  batchProgress: 'vault:batch_verify_progress',
  batchDone: 'vault:batch_verify_done',
  mcpStatus: 'vault:mcp_status',
  closeConfirm: 'vault:close_confirm',
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
      filters.credential_types ?? null,
    ),
  getCredential: (id) => call('get_credential', id),
  // 注意：pywebview 桥接只支持位置参数，顺序必须与后端
  // VaultApi.create_credential(name, credential_type, data, group_id, auto_verify) 严格一致
  createCredential: (payload) =>
    call(
      'create_credential',
      payload.name,
      payload.credential_type,
      payload.data ?? {},
      payload.group_id ?? null,
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
  listCredentialActions: (id) => call('list_credential_actions', id),
  runCredentialAction: (id, actionId, params) => call('run_credential_action', id, actionId, params),
  listCredentialModels: (id) => call('list_credential_models', id),
  streamCredentialAction: (id, actionId, params) => call('stream_credential_action', id, actionId, params),
  batchVerify: (scope = 'all', groupId = null, credentialType = null, credentialIds = null) =>
    call('batch_verify', scope, groupId, credentialType, credentialIds),
  checkDuplicateCredential: (credentialType, data) =>
    call('check_duplicate_credential', credentialType, data),

  // ── 分组 ────────────────────────────────────────────────
  listGroups: () => call('list_groups'),
  createGroup: (name, description = '', color = '#409EFF') =>
    call('create_group', name, description, color),
  updateGroup: (id, payload = {}) =>
    call('update_group', id, payload.name ?? null, payload.description ?? null, payload.color ?? null),
  deleteGroup: (id) => call('delete_group', id),

  // ── 插件（放入 plugins/ 目录即自动加载，无需手动管理）────
  listPlugins: () => call('list_plugins'),
  deletePlugin: (credentialType) => call('delete_plugin', credentialType),
  listAvailablePlugins: () => call('list_available_plugins'),
  installPlugin: (category, service) => call('install_plugin', category, service),

  // ── MCP ─────────────────────────────────────────────────
  getMcpStatus: () => call('get_mcp_status'),
  startMcp: () => call('start_mcp'),
  stopMcp: () => call('stop_mcp'),
  listMcpPermissions: () => call('list_mcp_permissions'),
  setMcpPermission: (toolName, enabled) => call('set_mcp_permission', toolName, enabled),
  getMcpToken: () => call('get_mcp_token'),
  rotateMcpToken: () => call('rotate_mcp_token'),

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

  // ── 托盘 ────────────────────────────────────────────────
  getTrayStatus: () => call('get_tray_status'),
  minimizeToTray: () => call('minimize_to_tray'),

  // ── 关闭窗口行为 ────────────────────────────────────────
  getCloseAction: () => call('get_close_action'),
  setCloseAction: (action) => call('set_close_action', action),
  confirmCloseAction: (action, remember = false) =>
    call('confirm_close_action', action, remember),

  // ── 验证代理 ────────────────────────────────────────────
  getProxy: () => call('get_proxy'),
  setProxy: (proxy) => call('set_proxy', proxy),

  // ── 备份 / 恢复 ─────────────────────────────────────────
  exportBackup: (scope = 'all', encrypted = true) => call('export_backup', scope, encrypted),
  importBackup: () => call('import_backup'),

  // ── 日志 ────────────────────────────────────────────────
  getLogConfig: () => call('get_log_config'),
  setLogLevel: (level) => call('set_log_level', level),
  getLogs: (limit = 200, level = '') => call('get_logs', limit, level),
  clearLogs: () => call('clear_logs'),
}

export default api