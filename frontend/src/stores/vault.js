import { defineStore } from 'pinia'
import { api } from '@/api'
import { inlineIcon } from '@/utils/pluginIcon'

const emptyFilters = () => ({
  group_id: null,
  credential_type: null,
  credential_types: null,
  status: null,
  search: '',
})

const emptyBatch = () => ({
  running: false,
  done: 0,
  total: 0,
  currentName: '',
  status: '',
  // 本次要验证的凭据 id（由 batch_started 事件给）。让对应行的「验证」按钮转成 loading，
  // 免得用户对着已被后台处理的条目重复点。
  ids: [],
  // 谁触发的：manual / mcp / schedule。**只用于决定要不要整页互斥**——只有用户自己点的
  // （manual）才锁整页；MCP 与定时任务都在用户没看着的时候跑，锁住界面只会让人莫名点不动。
  trigger: '',
  result: null,
})

export const useVaultStore = defineStore('vault', {
  state: () => ({
    credentials: [],
    groups: [],
    plugins: [],
    loadingCredentials: false,
    filters: emptyFilters(),
    batch: emptyBatch(),
  }),

  getters: {
    enabledPlugins: (state) => state.plugins.filter((plugin) => plugin.enabled),

    pluginByType: (state) => {
      const map = {}
      state.plugins.forEach((plugin) => {
        map[plugin.credential_type] = plugin
      })
      return map
    },

    // 类型下拉一律只列**已启用**的插件：禁用插件不该出现在「新建凭据」的类型里
    // （选不到就不会新建出「一建好就验证不了」的凭据），
    // 也不该出现在系统设置的「指定类型」里（定时验证本就跳过禁用插件）。
    // 注意：这只是**下拉可选项**的收窄；已禁用插件名下**已有**凭据的渲染（编辑表单的
    // fields_schema、凭据列表的置灰与状态）一律走 pluginByType，不受这里影响。
    typeOptions() {
      return this.enabledPlugins.map((plugin) => ({
        label: `${inlineIcon(plugin.icon, '')} ${plugin.name}`.trim(),
        value: plugin.credential_type,
      }))
    },

    /** 按类别分组的类型选项（供可搜索下拉的分组展示） */
    typeGroups() {
      const groups = {}
      this.enabledPlugins.forEach((plugin) => {
        const key = plugin.category || ''
        if (!groups[key]) {
          groups[key] = { label: plugin.category_name || plugin.category || '其他', options: [] }
        }
        groups[key].options.push({
          label: `${inlineIcon(plugin.icon, '')} ${plugin.name}`.trim(),
          value: plugin.credential_type,
        })
      })
      return Object.values(groups)
    },

    /** 左侧目录树：类别节点 → 服务节点（与 plugins/ 目录层次一致） */
    pluginTree: (state) => {
      const categories = {}
      state.plugins.forEach((plugin) => {
        // 被禁用且名下没有凭据的插件：目录树里不再显示它（避免树里挂一堆点进去没内容的空节点）
        if (!plugin.enabled && !(plugin.credential_count || 0)) return
        const key = plugin.category || ''
        if (!categories[key]) {
          categories[key] = {
            id: `cat:${key || 'root'}`,
            label: plugin.category_name || plugin.category || '其他',
            icon: plugin.category_icon || '📁',
            category: key,
            rel_path: key,
            children: [],
          }
        }
        categories[key].children.push({
          id: `type:${plugin.credential_type}`,
          label: plugin.name,
          icon: inlineIcon(plugin.icon),
          icon_image: plugin.icon_image || null,
          credential_type: plugin.credential_type,
          category: key,
          rel_path: plugin.rel_path || '',
          count: plugin.credential_count || 0,
        })
      })
      return Object.values(categories).map((cat) => ({
        ...cat,
        count: cat.children.reduce((sum, child) => sum + child.count, 0),
        types: cat.children.map((child) => child.credential_type),
      }))
    },

    groupOptions: (state) =>
      state.groups.map((group) => ({ label: group.name, value: group.id })),
  },

  actions: {
    clear() {
      this.credentials = []
      this.groups = []
      this.plugins = []
      this.filters = emptyFilters()
      this.batch = emptyBatch()
    },

    // ── 凭据 ──────────────────────────────────────────────
    async fetchCredentials() {
      this.loadingCredentials = true
      try {
        const items = await api.listCredentials(this.filters)
        this.credentials = Array.isArray(items) ? items : []
      } finally {
        this.loadingCredentials = false
      }
      return this.credentials
    },

    async createCredential(payload) {
      const created = await api.createCredential(payload)
      if (created && created.id != null) this.upsertCredential(created)
      await Promise.all([this.fetchGroups(), this.fetchPlugins()])
      return created
    },

    async updateCredential(id, payload) {
      const updated = await api.updateCredential(id, payload)
      if (updated && updated.id != null) this.upsertCredential(updated)
      await Promise.all([this.fetchGroups(), this.fetchPlugins()])
      return updated
    },

    async deleteCredential(id) {
      const result = await api.deleteCredential(id)
      this.credentials = this.credentials.filter((item) => item.id !== id)
      await Promise.all([this.fetchGroups(), this.fetchPlugins()])
      return result
    },

    async verifyCredential(id) {
      const result = await api.verifyCredential(id)
      const target = this.credentials.find((item) => item.id === id)
      if (target) {
        const now = new Date().toISOString()
        target.status = result.status
        target.status_detail = { ...(target.status_detail || {}), message: result.message }
        if (result.account_info) target.status_detail.account_info = result.account_info
        target.last_verified_at = now
        if (result.status === 'valid') target.last_valid_at = now
      }
      return result
    },

    /** 批量验证：进度与结果通过后端事件驱动（见 applyBatchProgress / applyBatchDone）。
     *  concurrency / timeout 来自「批量验证」对话框，传了即随本次验证落库。 */
    async batchVerify(scope = 'all', groupId = null, credentialType = null, credentialIds = null,
      concurrency = null, timeout = null) {
      // 先把 trigger 写成 manual：这里的入口只有用户在对话框里点「验证 N 条」一种，
      // 而 batch_started 事件要等后端线程起来才到——中间那段空窗期若 trigger 为空，
      // 整页互斥会滞后一拍。写死反而准确。
      this.batch = { ...emptyBatch(), running: true, trigger: 'manual' }
      try {
        return await api.batchVerify(scope, groupId, credentialType, credentialIds,
          concurrency, timeout)
      } catch (err) {
        this.batch.running = false
        throw err
      }
    },

    /** 批量验证的运行参数（并发线程数 / 请求超时）与取值范围，用于对话框回填。 */
    async fetchBatchOptions() {
      return await api.getBatchOptions()
    },

    // ── 分组 ──────────────────────────────────────────────
    async fetchGroups() {
      const items = await api.listGroups()
      this.groups = Array.isArray(items) ? items : []
      return this.groups
    },

    async createGroup(payload) {
      const created = await api.createGroup(payload.name, payload.description || '', payload.color || '#409EFF')
      await this.fetchGroups()
      return created
    },

    async updateGroup(id, payload) {
      const updated = await api.updateGroup(id, payload)
      await this.fetchGroups()
      return updated
    },

    async deleteGroup(id) {
      const result = await api.deleteGroup(id)
      await Promise.all([this.fetchGroups(), this.fetchCredentials()])
      return result
    },

    // ── 插件（放入 plugins/ 目录即自动加载，仅拉取列表）────
    async fetchPlugins() {
      const items = await api.listPlugins()
      this.plugins = (Array.isArray(items) ? items : []).map((plugin) => ({
        ...plugin,
        enabled: !!plugin.enabled,
      }))
      return this.plugins
    },

    /** 启用/禁用插件；随后刷新插件列表（凭据行的置灰由 pluginByType 派生，无需重拉凭据） */
    async setPluginEnabled(credentialType, enabled) {
      const result = await api.setPluginEnabled(credentialType, enabled)
      await this.fetchPlugins()
      return result
    },

    // ── 后端事件 ──────────────────────────────────────────
    upsertCredential(credential) {
      const index = this.credentials.findIndex((item) => item.id === credential.id)
      if (index === -1) this.credentials.unshift(credential)
      else this.credentials.splice(index, 1, { ...this.credentials[index], ...credential })
    },

    applyCredentialUpdated(credential) {
      if (!credential || credential.id == null) return
      this.upsertCredential(credential)
    },

    applyBatchProgress(detail) {
      if (!detail) return
      this.batch.running = true
      this.batch.done = detail.current ?? this.batch.done
      this.batch.total = detail.total ?? this.batch.total
      this.batch.currentName = detail.credential_name || ''
      this.batch.status = detail.status || ''
    },

    /** 批量验证开始：记下本次涉及的凭据 id（行内按钮据此转 loading），并顺手重置进度，
     *  免得上一次的计数在第一条进度到达前先显示出来。 */
    applyBatchStarted(detail) {
      if (!detail) return
      this.batch.running = true
      this.batch.done = 0
      this.batch.total = detail.total ?? 0
      this.batch.currentName = ''
      this.batch.status = ''
      this.batch.trigger = detail.trigger || ''
      this.batch.ids = Array.isArray(detail.credentials) ? detail.credentials : []
    },

    async applyBatchDone(detail) {
      this.batch.running = false
      // 结束时必须清掉：留着会让整批条目一直显示 loading，直到下一次批量验证
      this.batch.ids = []
      this.batch.trigger = ''
      this.batch.result = detail || null
      await this.fetchCredentials()
      return this.batch.result
    },

    /** 重载页面后把仍在跑的批量验证接回界面。
     *  started / progress 都是「推送」事件，重载后已经错过，只能拿一次任务快照兜底。
     *  任务里不存凭据 id，所以行内 loading 恢复不了——这里只保证横幅还能显示进度。
     *  也同样不存触发方，故**不置 trigger**：重载后默认不锁整页（拿不准时宁可放过，
     *  锁错了用户会莫名点不动；后端另有 in-flight 保护，重复点也不会真的重跑）。 */
    async resumeBatchFromTasks() {
      if (this.batch.running) return
      let list = []
      try {
        list = await api.listTasks()
      } catch {
        // 未解锁 / 后端未就绪：静默，批量验证本来就是后台自己的事
        return
      }
      const task = (Array.isArray(list) ? list : []).find(
        (item) => item && item.kind === 'batch_verify' && item.status === 'running',
      )
      if (!task) return
      this.batch.running = true
      this.batch.done = Number(task.current) || 0
      this.batch.total = Number(task.total) || 0
      this.batch.currentName = task.text || ''
    },
  },
})