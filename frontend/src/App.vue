<script setup>
import { onBeforeUnmount, onMounted, watch } from 'vue'
import { useRouter } from 'vue-router'

import { EVENTS, onEvent } from '@/api'
import { useAppStore } from '@/stores/app'
import { useVaultStore } from '@/stores/vault'
import { useMcpStore } from '@/stores/mcp'

const router = useRouter()
const appStore = useAppStore()
const vaultStore = useVaultStore()
const mcpStore = useMcpStore()

const disposers = []

function guard(promise) {
  if (promise && typeof promise.catch === 'function') promise.catch(() => {})
}

onMounted(() => {
  disposers.push(
    onEvent(EVENTS.credentialUpdated, (detail) => vaultStore.applyCredentialUpdated(detail)),
  )
  disposers.push(
    onEvent(EVENTS.credentialsChanged, () => guard(vaultStore.fetchCredentials())),
  )
  disposers.push(onEvent(EVENTS.batchProgress, (detail) => vaultStore.applyBatchProgress(detail)))
  disposers.push(
    onEvent(EVENTS.batchDone, (detail) => guard(vaultStore.applyBatchDone(detail))),
  )
  disposers.push(onEvent(EVENTS.pluginInstalled, () => guard(vaultStore.fetchPlugins())))
  disposers.push(
    onEvent(EVENTS.mcpStatus, (detail) => {
      mcpStore.applyStatusEvent(detail)
      guard(appStore.refreshTray())
    }),
  )

  guard(appStore.refreshTray())
})

onBeforeUnmount(() => {
  disposers.forEach((dispose) => dispose())
  disposers.length = 0
})

// 保险库被锁定（例如托盘菜单触发）时，强制回到解锁页。
watch(
  () => appStore.unlocked,
  (unlocked) => {
    if (appStore.initialized && !unlocked && router.currentRoute.value.name !== 'unlock') {
      router.push('/unlock')
    }
  },
)
</script>

<template>
  <router-view />
</template>

<style>
:root {
  font-family: 'Inter', 'PingFang SC', 'Microsoft YaHei', system-ui, -apple-system, 'Segoe UI', sans-serif;
  font-synthesis: none;
  text-rendering: optimizeLegibility;
}

html,
body,
#app {
  height: 100%;
  margin: 0;
  padding: 0;
}

body {
  background: var(--el-bg-color-page);
  color: var(--el-text-color-primary);
  overflow: hidden;
  -webkit-font-smoothing: antialiased;
}

* {
  box-sizing: border-box;
}

::-webkit-scrollbar {
  width: 8px;
  height: 8px;
}

::-webkit-scrollbar-thumb {
  background: var(--el-border-color-darker);
  border-radius: 4px;
}

::-webkit-scrollbar-track {
  background: transparent;
}

.page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.page-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.page-toolbar .spacer {
  flex: 1;
}

.text-muted {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
</style>