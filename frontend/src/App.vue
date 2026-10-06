<script setup>
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import api, { EVENTS, onEvent } from '@/api'
import { useAppStore } from '@/stores/app'
import { useVaultStore } from '@/stores/vault'
import { useMcpStore } from '@/stores/mcp'

const router = useRouter()
const appStore = useAppStore()
const vaultStore = useVaultStore()
const mcpStore = useMcpStore()

const disposers = []

// 关闭窗口确认弹窗（由后端关闭事件 vault:close_confirm 触发）
const closeDialog = ref(false)
const closeRemember = ref(false)
const closeBusy = ref(false)

async function handleCloseChoice(action) {
  closeBusy.value = true
  try {
    await api.confirmCloseAction(action, closeRemember.value)
    closeDialog.value = false
  } catch (err) {
    ElMessage.error(err.message || '操作失败')
  } finally {
    closeBusy.value = false
  }
}

function guard(promise) {
  if (promise && typeof promise.catch === 'function') promise.catch(() => {})
}

/** 窗口重新获得焦点/可见时刷新数据（覆盖后台 MCP 操作后前台未实时更新的场景） */
function refreshOnActive() {
  if (!appStore.unlocked) return
  guard(vaultStore.fetchCredentials())
  guard(vaultStore.fetchGroups())
}
function handleVisibility() {
  if (document.visibilityState !== 'visible') return
  handleActivity()
  refreshOnActive()
}

/** 用户活动上报（供后端「空闲自动锁定」计时）。
 *  这里只做转发，节流在 store 内（30s）——本函数会被 mousemove 高频调用，
 *  所以在 store 里拦截比在这里判断更省事，也不必给每个事件各配一套节流。 */
function handleActivity() {
  appStore.reportActivity()
}

// 覆盖键鼠/滚轮/触摸四类；mousemove 与 wheel 高频，标 passive 避免拖慢滚动。
const ACTIVITY_EVENTS = [
  ['mousemove', { passive: true }],
  ['mousedown', { passive: true }],
  ['wheel', { passive: true }],
  ['touchstart', { passive: true }],
  ['keydown', undefined],
]

onMounted(() => {
  disposers.push(
    onEvent(EVENTS.credentialUpdated, (detail) => vaultStore.applyCredentialUpdated(detail)),
  )
  disposers.push(
    onEvent(EVENTS.credentialsChanged, () => guard(vaultStore.fetchCredentials())),
  )
  disposers.push(
    onEvent(EVENTS.pluginsChanged, () => guard(vaultStore.fetchPlugins())),
  )
  disposers.push(onEvent(EVENTS.batchStarted, (detail) => vaultStore.applyBatchStarted(detail)))
  disposers.push(onEvent(EVENTS.batchProgress, (detail) => vaultStore.applyBatchProgress(detail)))
  disposers.push(
    onEvent(EVENTS.batchDone, (detail) => guard(vaultStore.applyBatchDone(detail))),
  )
  disposers.push(
    onEvent(EVENTS.mcpStatus, (detail) => {
      mcpStore.applyStatusEvent(detail)
      guard(appStore.refreshTray())
    }),
  )
  disposers.push(
    onEvent(EVENTS.closeConfirm, () => {
      closeRemember.value = false
      closeDialog.value = true
    }),
  )
  // 后端加锁（界面按钮 / 托盘 / 空闲自动锁）都会广播此事件，统一在这里清空明文。
  // 随后的 watch(appStore.unlocked) 会把路由推回 /unlock。
  disposers.push(onEvent(EVENTS.vaultLocked, () => appStore.applyLocked()))

  const handleFocus = () => {
    handleActivity()
    refreshOnActive()
  }
  window.addEventListener('focus', handleFocus)
  document.addEventListener('visibilitychange', handleVisibility)
  disposers.push(() => window.removeEventListener('focus', handleFocus))
  disposers.push(() => document.removeEventListener('visibilitychange', handleVisibility))

  for (const [name, opts] of ACTIVITY_EVENTS) {
    window.addEventListener(name, handleActivity, opts)
    disposers.push(() => window.removeEventListener(name, handleActivity, opts))
  }

  guard(appStore.refreshTray())
  // 页面重载会丢掉已推送的批量验证事件；这里从任务列表兜一次底，避免横幅「凭空消失」
  guard(vaultStore.resumeBatchFromTasks())
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

  <el-dialog
    v-model="closeDialog"
    title="关闭窗口"
    width="440px"
    align-center
    :close-on-click-modal="false"
    :close-on-press-escape="false"
    :show-close="false"
  >
    <p class="close-ask">要直接退出程序，还是让程序继续在后台运行？</p>
    <p class="close-hint">后台运行时窗口会最小化到系统托盘，MCP 服务与定时任务继续执行。</p>
    <el-checkbox v-model="closeRemember">记住我的选择（可在"系统设置"中修改）</el-checkbox>
    <template #footer>
      <el-button :disabled="closeBusy" @click="handleCloseChoice('background')">
        后台运行
      </el-button>
      <el-button type="primary" :disabled="closeBusy" @click="handleCloseChoice('quit')">
        直接退出
      </el-button>
    </template>
  </el-dialog>
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

.close-ask {
  margin: 0 0 6px;
  font-size: 14px;
}

.close-hint {
  margin: 0 0 14px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
</style>