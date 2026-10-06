import { defineStore } from 'pinia'
import { api } from '@/api'

export const useMcpStore = defineStore('mcp', {
  state: () => ({
    status: { running: false, host: '127.0.0.1', port: 8765, active_tokens: 0, url: '' },
    permissions: [],
    token: null,
    busy: false,
  }),

  getters: {
    running: (state) => !!state.status.running,
    serverUrl: (state) =>
      state.status.url ||
      `http://${state.status.host || '127.0.0.1'}:${state.status.port || 8765}/sse`,
    clientConfig: (state) => () =>
      JSON.stringify(
        {
          mcpServers: {
            'credential-vault': {
              url:
                state.status.url ||
                `http://${state.status.host || '127.0.0.1'}:${state.status.port || 8765}/sse`,
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
      this.status = { running: false, host: '127.0.0.1', port: 8765, active_tokens: 0, url: '' }
      this.permissions = []
      this.token = null
    },

    async fetchStatus() {
      const status = await api.getMcpStatus()
      this.status = {
        running: !!(status && status.running),
        host: (status && status.host) || '127.0.0.1',
        port: (status && status.port) || 8765,
        active_tokens: (status && status.active_tokens) || 0,
        url: (status && status.url) || '',
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

    async fetchToken() {
      const result = await api.getMcpToken()
      this.token = result && result.exists ? result : null
      return this.token
    },

    async rotateToken() {
      const rotated = await api.rotateMcpToken()
      await Promise.all([this.fetchToken(), this.fetchStatus()])
      return rotated
    },

    async refreshAll() {
      await Promise.all([this.fetchStatus(), this.fetchPermissions(), this.fetchToken()])
    },

    applyStatusEvent(detail) {
      if (!detail) return
      if (detail.running !== undefined) this.status.running = !!detail.running
      if (detail.host !== undefined) this.status.host = detail.host
      if (detail.port !== undefined) this.status.port = detail.port
      if (detail.active_tokens !== undefined) this.status.active_tokens = detail.active_tokens
    },
  },
})