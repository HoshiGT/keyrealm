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
        label: `${inlineIcon(plugin.icon, '')} ${plugin.name}`.trim(),
        value: plugin.credential_type,
      })),

    /** 按类别分组的类型选项（供可搜索下拉的分组展示） */
    typeGroups: (state) => {
      const groups = {}
      state.plugins.forEach((plugin) => {
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
        const now = new Date().toISOString()
        target.status = result.status
        target.status_detail = { ...(target.status_detail || {}), message: result.message }
        if (result.account_info) target.status_detail.account_info = result.account_info
        target.last_verified_at = now
        if (result.status === 'valid') target.last_valid_at = now
      }
      return result
    },

    /** 批量验证：进度与结果通过后端事件驱动（见 applyBatchProgress / applyBatchDone）。 */
    async batchVerify(scope = 'all', groupId = null, credentialType = null, credentialIds = null) {
      this.batch = { ...emptyBatch(), running: true }
      try {
        return await api.batchVerify(scope, groupId, credentialType, credentialIds)
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

    // ── 插件（放入 plugins/ 目录即自动加载，仅拉取列表）────
    async fetchPlugins() {
      const items = await api.listPlugins()
      this.plugins = (Array.isArray(items) ? items : []).map((plugin) => ({
        ...plugin,
        enabled: !!plugin.enabled,
      }))
      return this.plugins
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