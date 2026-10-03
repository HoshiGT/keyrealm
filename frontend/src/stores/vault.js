import { defineStore } from 'pinia'
import { api } from '@/api'

const emptyFilters = () => ({ group_id: null, credential_type: null, status: null, search: '' })

const emptyBatch = () => ({
  running: false,
  done: 0,
  total: 0,
  currentName: '',
  status: '',
  result: null,
})

export const useVaultStore = defineStore('vault', {
  state: () => ({
    credentials: [],
    groups: [],
    plugins: [],
    loadingCredentials: false,
    loaded: false,
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

    groupById: (state) => {
      const map = {}
      state.groups.forEach((group) => {
        map[group.id] = group
      })
      return map
    },

    typeOptions: (state) =>
      state.plugins.map((plugin) => ({
        label: `${plugin.icon || '🔑'} ${plugin.name}`,
        value: plugin.credential_type,
      })),

    groupOptions: (state) =>
      state.groups.map((group) => ({ label: group.name, value: group.id })),

    statusCounts: (state) => {
      const counts = { total: state.credentials.length, valid: 0, invalid: 0, error: 0, unknown: 0 }
      state.credentials.forEach((credential) => {
        if (counts[credential.status] === undefined) counts.unknown += 1
        else counts[credential.status] += 1
      })
      return counts
    },
  },

  actions: {
    clear() {
      this.credentials = []
      this.groups = []
      this.plugins = []
      this.loaded = false
      this.filters = emptyFilters()
      this.batch = emptyBatch()
    },

    // ── 凭据 ──────────────────────────────────────────────
    async fetchCredentials() {
      this.loadingCredentials = true
      try {
        const items = await api.listCredentials(this.filters)
        this.credentials = Array.isArray(items) ? items : []
        this.loaded = true
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
        target.status = result.status
        target.status_detail = { ...(target.status_detail || {}), message: result.message }
        if (result.account_info) target.status_detail.account_info = result.account_info
        target.last_verified_at = new Date().toISOString()
      }
      return result
    },

    async getHistory(credentialId, limit = 20) {
      const items = await api.getVerificationHistory(credentialId, limit)
      return Array.isArray(items) ? items : []
    },

    /** 批量验证：进度与结果通过后端事件驱动（见 applyBatchProgress / applyBatchDone）。 */
    async batchVerify(scope = 'all', groupId = null, credentialType = null) {
      this.batch = { ...emptyBatch(), running: true }
      try {
        return await api.batchVerify(scope, groupId, credentialType)
      } catch (err) {
        this.batch.running = false
        throw err
      }
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

    // ── 插件 ──────────────────────────────────────────────
    async fetchPlugins() {
      const items = await api.listPlugins()
      // 后端 enabled/is_builtin 源自 SQLite INTEGER(0/1)，此处归一为布尔。
      // 否则 el-switch 内部 [activeValue,inactiveValue].includes(0) 判为“非法值”，
      // 会在组件创建时自动 emit 一次 change(false)，导致未点击却弹出“已停用”。
      this.plugins = (Array.isArray(items) ? items : []).map((plugin) => ({
        ...plugin,
        enabled: !!plugin.enabled,
        is_builtin: !!plugin.is_builtin,
      }))
      return this.plugins
    },

    async installPlugin(zipPath) {
      const result = await api.installPlugin(zipPath)
      await this.fetchPlugins()
      return result
    },

    async uninstallPlugin(credentialType) {
      const result = await api.uninstallPlugin(credentialType)
      await this.fetchPlugins()
      return result
    },

    async togglePlugin(credentialType, enabled) {
      const result = await api.togglePlugin(credentialType, enabled)
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

    async applyBatchDone(detail) {
      this.batch.running = false
      this.batch.result = detail || null
      await this.fetchCredentials()
      return this.batch.result
    },
  },
})