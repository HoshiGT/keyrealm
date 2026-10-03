import { defineStore } from 'pinia'
import { api } from '@/api'

export const useMcpStore = defineStore('mcp', {
  state: () => ({
    status: { running: false, port: 8765, active_tokens: 0, url: '' },
    permissions: [],
    tokens: [],
    loading: false,
    busy: false,
  }),

  getters: {
    running: (state) => !!state.status.running,
    serverUrl: (state) => state.status.url || `http://localhost:${state.status.port || 8765}/sse`,
    toolOptions: (state) =>
      state.permissions.map((permission) => ({
        label: permission.tool_name,
        value: permission.tool_name,
        description: permission.description,
      })),
    toolDescription: (state) => (toolName) => {
      const found = state.permissions.find((item) => item.tool_name === toolName)
      return found ? found.description : ''
    },
    clientConfig: (state) => () =>
      JSON.stringify(
        {
          mcpServers: {
            'credential-vault': {
              url: state.status.url || `http://localhost:${state.status.port || 8765}/sse`,
              headers: { Authorization: 'Bearer cv_<your_token>' },
            },
          },
        },
        null,
        2,
      ),
  },

  actions: {
    clear() {
      this.status = { running: false, port: 8765, active_tokens: 0, url: '' }
      this.permissions = []
      this.tokens = []
    },

    async fetchStatus() {
      const status = await api.getMcpStatus()
      this.status = {
        running: !!(status && status.running),
        port: (status && status.port) || 8765,
        active_tokens: (status && status.active_tokens) || 0,
        url: (status && status.url) || `http://localhost:${(status && status.port) || 8765}/sse`,
      }
      return this.status
    },

    async start() {
      this.busy = true
      try {
        const result = await api.startMcp()
        await this.fetchStatus()
        return result
      } finally {
        this.busy = false
      }
    },

    async stop() {
      this.busy = true
      try {
        const result = await api.stopMcp()
        await this.fetchStatus()
        return result
      } finally {
        this.busy = false
      }
    },

    async fetchPermissions() {
      const items = await api.listMcpPermissions()
      this.permissions = Array.isArray(items) ? items : []
      return this.permissions
    },

    async setPermission(toolName, enabled) {
      const result = await api.setMcpPermission(toolName, enabled)
      const target = this.permissions.find((item) => item.tool_name === toolName)
      if (target) target.enabled = !!enabled
      return result
    },

    async fetchTokens() {
      this.loading = true
      try {
        const items = await api.listMcpTokens()
        this.tokens = Array.isArray(items) ? items : []
      } finally {
        this.loading = false
      }
      return this.tokens
    },

    async createToken(payload) {
      const created = await api.createMcpToken(
        payload.name,
        payload.tools_allowed || [],
        payload.expires_days ?? null,
      )
      await this.fetchTokens()
      return created
    },

    async revokeToken(tokenId) {
      const result = await api.revokeMcpToken(tokenId)
      this.tokens = this.tokens.filter((item) => item.id !== tokenId)
      await this.fetchStatus()
      return result
    },

    async updateTokenScope(tokenId, toolsAllowed) {
      const result = await api.updateTokenScope(tokenId, toolsAllowed)
      const target = this.tokens.find((item) => item.id === tokenId)
      if (target) target.tools_allowed = [...toolsAllowed]
      return result
    },

    async refreshAll() {
      await Promise.all([this.fetchStatus(), this.fetchPermissions(), this.fetchTokens()])
    },

    applyStatusEvent(detail) {
      if (!detail) return
      if (detail.running !== undefined) this.status.running = !!detail.running
      if (detail.port !== undefined) this.status.port = detail.port
      if (detail.active_tokens !== undefined) this.status.active_tokens = detail.active_tokens
    },
  },
})