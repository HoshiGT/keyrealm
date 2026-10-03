import { defineStore } from 'pinia'
import { api } from '@/api'
import { useVaultStore } from './vault'
import { useMcpStore } from './mcp'

let bootstrapPromise = null

export const useAppStore = defineStore('app', {
  state: () => ({
    bootstrapped: false,
    initialized: false,
    unlocked: false,
    bridgeError: '',
    tray: {
      mcp_running: false,
      mcp_port: 8765,
      active_tokens: 0,
      is_locked: true,
    },
  }),

  getters: {
    ready: (state) => state.bootstrapped && !state.bridgeError,
    locked: (state) => state.initialized && !state.unlocked,
  },

  actions: {
    /** 读取初始化/解锁状态。可安全重复调用（并发去重 + 结果缓存）。 */
    async bootstrap(force = false) {
      if (this.bootstrapped && !force) return
      if (bootstrapPromise && !force) return bootstrapPromise

      const task = (async () => {
        try {
          const initResult = await api.isInitialized()
          this.initialized = !!(initResult && initResult.initialized)
          if (this.initialized) {
            const unlockedResult = await api.isUnlocked()
            this.unlocked = !!(unlockedResult && unlockedResult.unlocked)
          } else {
            this.unlocked = false
          }
          this.bridgeError = ''
        } catch (err) {
          this.initialized = false
          this.unlocked = false
          this.bridgeError = err && err.message ? err.message : '无法连接后端服务'
        } finally {
          this.bootstrapped = true
        }
      })()

      bootstrapPromise = task
      try {
        await task
      } finally {
        bootstrapPromise = null
      }
    },

    async setupMasterPassword(password) {
      const result = await api.setupMasterPassword(password)
      this.initialized = true
      this.unlocked = true
      this.bridgeError = ''
      await this.refreshTray()
      return result
    },

    async unlock(password) {
      const result = await api.unlock(password)
      this.unlocked = true
      await this.refreshTray()
      return result
    },

    /** 锁定保险库并清空内存中的明文数据。 */
    async lock() {
      try {
        await api.lockVault()
      } finally {
        this.unlocked = false
        this.tray.is_locked = true
        useVaultStore().clear()
        useMcpStore().clear()
      }
    },

    async refreshTray() {
      try {
        const status = await api.getTrayStatus()
        if (status) {
          this.tray = {
            mcp_running: !!status.mcp_running,
            mcp_port: status.mcp_port ?? 8765,
            active_tokens: status.active_tokens ?? 0,
            is_locked: !!status.is_locked,
          }
        }
      } catch {
        // 托盘状态为辅助信息，失败不阻塞主流程
      }
    },

    async minimizeToTray() {
      const result = await api.minimizeToTray()
      await this.refreshTray()
      return result
    },
  },
})