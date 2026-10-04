<script setup>
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  Connection,
  Expand,
  Fold,
  FolderOpened,
  InfoFilled,
  Key,
  Setting,
  Lock,
  Minus,
} from '@element-plus/icons-vue'

import { useAppStore } from '@/stores/app'
import { useVaultStore } from '@/stores/vault'

const route = useRoute()
const router = useRouter()
const appStore = useAppStore()
const vaultStore = useVaultStore()

const collapsed = ref(localStorage.getItem('vault.layout.asideCollapsed') === '1')
const batchDialog = ref(false)

watch(collapsed, (value) => {
  localStorage.setItem('vault.layout.asideCollapsed', value ? '1' : '0')
})

const navItems = [
  { path: '/credentials', title: '凭据管理', icon: Key },
  { path: '/groups', title: '分组管理', icon: FolderOpened },
  { path: '/mcp', title: 'MCP 设置', icon: Connection },
  { path: '/settings', title: '系统设置', icon: Setting },
  { path: '/about', title: '关于', icon: InfoFilled },
]

const activePath = computed(() => route.path)

const pageTitle = computed(() => route.meta.title || 'Credential Vault')

const batchPercent = computed(() => {
  const { done, total } = vaultStore.batch
  if (!total) return 0
  return Math.min(100, Math.round((done / total) * 100))
})

watch(
  () => vaultStore.batch.running,
  (running) => {
    if (running) batchDialog.value = true
  },
)

watch(
  () => vaultStore.batch.result,
  (result) => {
    if (result) batchDialog.value = true
  },
)

function closeBatchDialog() {
  vaultStore.batch.result = null
}

async function handleLock() {
  try {
    await appStore.lock()
    router.push('/unlock')
  } catch (err) {
    ElMessage.error(err.message || '锁定失败')
  }
}

async function handleMinimize() {
  try {
    await appStore.minimizeToTray()
    ElMessage.success('已最小化到系统托盘，后台任务继续运行')
  } catch (err) {
    ElMessage.error(err.message || '最小化失败')
  }
}
</script>

<template>
  <el-container class="layout">
    <el-aside :width="collapsed ? '64px' : '152px'" class="layout-aside">
      <div class="brand" :class="{ 'is-collapsed': collapsed }">
        <img src="/favicon.svg" class="brand-logo" alt="Credential Vault" />
        <span v-show="!collapsed" class="brand-text">Credential Vault</span>
      </div>

      <el-menu :default-active="activePath" :collapse="collapsed" :collapse-transition="false" router class="layout-menu">
        <el-menu-item v-for="item in navItems" :key="item.path" :index="item.path">
          <el-icon><component :is="item.icon" /></el-icon>
          <template #title>{{ item.title }}</template>
        </el-menu-item>
      </el-menu>

      <div class="aside-footer">
        <el-button text class="collapse-btn" @click="collapsed = !collapsed">
          <el-icon><component :is="collapsed ? Expand : Fold" /></el-icon>
          <span v-show="!collapsed">收起侧栏</span>
        </el-button>
      </div>
    </el-aside>

    <el-container>
      <el-header class="layout-header">
        <div class="header-title">{{ pageTitle }}</div>
        <div class="header-actions">
          <el-tooltip :content="appStore.tray.mcp_running ? 'MCP 服务运行中' : 'MCP 服务已停止'" placement="bottom">
            <span class="mcp-indicator" :class="appStore.tray.mcp_running ? 'is-on' : 'is-off'">
              <i class="dot" />
              MCP {{ appStore.tray.mcp_running ? '运行中' : '已停止' }}
              <template v-if="appStore.tray.active_tokens">· {{ appStore.tray.active_tokens }} Token</template>
            </span>
          </el-tooltip>

          <el-divider direction="vertical" />

          <el-button text :icon="Minus" @click="handleMinimize">最小化到托盘</el-button>
          <el-button type="primary" plain :icon="Lock" @click="handleLock">锁定</el-button>
        </div>
      </el-header>

      <el-main class="layout-main">
        <router-view v-slot="{ Component }">
          <component :is="Component" />
        </router-view>
      </el-main>
    </el-container>

    <el-dialog
      v-model="batchDialog"
      title="批量验证"
      width="520px"
      :close-on-click-modal="false"
      :close-on-press-escape="!vaultStore.batch.running"
      :show-close="!vaultStore.batch.running"
      @closed="closeBatchDialog"
    >
      <div v-if="vaultStore.batch.running" class="batch-progress">
        <el-progress :percentage="batchPercent" :stroke-width="14" striped striped-flow />
        <p class="batch-line">
          正在验证：<strong>{{ vaultStore.batch.currentName || '准备中…' }}</strong>
          <span class="batch-count">{{ vaultStore.batch.done }} / {{ vaultStore.batch.total || '—' }}</span>
        </p>
        <p class="batch-hint">验证过程中请勿关闭应用，可在后台继续运行。</p>
      </div>

      <div v-else-if="vaultStore.batch.result" class="batch-result">
        <el-result icon="success" title="批量验证完成">
          <template #sub-title>
            <div class="result-grid">
              <span>总计 <strong>{{ vaultStore.batch.result.total }}</strong></span>
              <span class="is-valid">有效 {{ vaultStore.batch.result.valid }}</span>
              <span class="is-invalid">失效 {{ vaultStore.batch.result.invalid }}</span>
              <span class="is-error">错误 {{ vaultStore.batch.result.error }}</span>
              <span v-if="vaultStore.batch.result.skipped" class="is-skipped">
                跳过 {{ vaultStore.batch.result.skipped }}（需手动验证）
              </span>
            </div>
          </template>
        </el-result>
      </div>

      <template #footer>
        <el-button v-if="vaultStore.batch.running" disabled loading>验证中…</el-button>
        <el-button v-else type="primary" @click="batchDialog = false">关闭</el-button>
      </template>
    </el-dialog>
  </el-container>
</template>

<style scoped>
.layout {
  height: 100vh;
  overflow: hidden;
}

.layout-aside {
  display: flex;
  flex-direction: column;
  background: var(--el-bg-color-overlay);
  border-right: 1px solid var(--el-border-color);
  transition: width 0.22s ease;
  overflow: hidden;
}

.brand {
  display: flex;
  align-items: center;
  gap: 6px;
  height: 60px;
  padding: 0 8px;
  font-weight: 700;
  font-size: 13px;
  white-space: nowrap;
  border-bottom: 1px solid var(--el-border-color);
}

.brand-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}

.layout-menu :deep(.el-menu-item) {
  justify-content: center;
  padding: 0 !important;
}

.layout-menu :deep(.el-menu-item .el-icon) {
  margin-right: 6px;
}

.layout-menu :deep(.el-menu-item.is-active) {
  padding-left: 0 !important;
}

.brand.is-collapsed {
  justify-content: center;
  padding: 0;
}

.brand-logo {
  width: 22px;
  height: 22px;
  border-radius: 6px;
  flex-shrink: 0;
}

.layout-menu {
  flex: 1;
  border-right: none;
  overflow-y: auto;
}

.aside-footer {
  border-top: 1px solid var(--el-border-color);
  padding: 8px;
}

.collapse-btn {
  width: 100%;
  justify-content: center;
}

.layout-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  height: 60px;
  border-bottom: 1px solid var(--el-border-color);
  background: var(--el-bg-color-overlay);
}

.header-title {
  font-size: 17px;
  font-weight: 600;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 6px;
}

.mcp-indicator {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.mcp-indicator .dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--el-color-info);
}

.mcp-indicator.is-on .dot {
  background: var(--el-color-success);
  box-shadow: 0 0 0 3px rgba(103, 194, 58, 0.18);
}

.mcp-indicator.is-off .dot {
  background: var(--el-color-danger);
}

.layout-main {
  padding: 20px;
  overflow-y: auto;
  background: var(--el-bg-color-page);
}

.batch-progress {
  padding: 4px 2px;
}

.batch-line {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin: 16px 0 4px;
}

.batch-count {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.batch-hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  margin: 0;
}

.result-grid {
  display: flex;
  gap: 18px;
  justify-content: center;
  flex-wrap: wrap;
}

.result-grid .is-valid {
  color: var(--el-color-success);
}

.result-grid .is-invalid {
  color: var(--el-color-danger);
}

.result-grid .is-error {
  color: var(--el-color-warning);
}

.result-grid .is-skipped {
  color: var(--el-text-color-secondary);
}
</style>