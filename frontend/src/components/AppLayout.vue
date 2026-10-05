<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Connection,
  Document,
  Download,
  Expand,
  Fold,
  FolderOpened,
  Grid,
  InfoFilled,
  Key,
  List,
  RefreshLeft,
  Setting,
  Lock,
  Minus,
} from '@element-plus/icons-vue'

import { api, EVENTS } from '@/api'
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

// 导航项默认顺序。用户可在侧栏直接拖拽调整，顺序存 localStorage（纯界面偏好，不进后端）。
const NAV_ITEMS = [
  { path: '/credentials', title: '凭据管理', icon: Key },
  { path: '/plugins', title: '插件管理', icon: Grid },
  { path: '/groups', title: '分组管理', icon: FolderOpened },
  { path: '/mcp', title: 'MCP 设置', icon: Connection },
  { path: '/logs', title: '日志', icon: Document },
  { path: '/tasks', title: '任务', icon: List },
  { path: '/settings', title: '系统设置', icon: Setting },
  { path: '/backup', title: '备份', icon: Download },
  { path: '/about', title: '关于', icon: InfoFilled },
]

const NAV_ORDER_KEY = 'vault.layout.navOrder'

function readSavedOrder() {
  try {
    const raw = JSON.parse(localStorage.getItem(NAV_ORDER_KEY) || 'null')
    return Array.isArray(raw) ? raw.filter((p) => typeof p === 'string') : []
  } catch {
    return []
  }
}

/** 按已保存的顺序排列；保存顺序里没有的项（如新增页面）追加到末尾，已移除的项直接忽略。 */
function applyOrder(saved) {
  if (!saved.length) return NAV_ITEMS.slice()
  const pending = new Map(NAV_ITEMS.map((item) => [item.path, item]))
  const ordered = []
  for (const path of saved) {
    const item = pending.get(path)
    if (item) {
      ordered.push(item)
      pending.delete(path)
    }
  }
  for (const item of NAV_ITEMS) {
    if (pending.has(item.path)) ordered.push(item)
  }
  return ordered
}

const navItems = ref(applyOrder(readSavedOrder()))

const isCustomOrder = computed(
  () => navItems.value.some((item, i) => item.path !== NAV_ITEMS[i].path),
)

const dragPath = ref('')
const dropPath = ref('')
const dropAfter = ref(false)

function resetDrag() {
  dragPath.value = ''
  dropPath.value = ''
  dropAfter.value = false
}

function onDragStart(item, event) {
  if (collapsed.value) return
  dragPath.value = item.path
  event.dataTransfer.effectAllowed = 'move'
  // Firefox 必须有 setData 才会真正开始拖拽
  event.dataTransfer.setData('text/plain', item.path)
}

function onDragOver(item, event) {
  if (!dragPath.value || item.path === dragPath.value) return
  event.preventDefault()
  event.dataTransfer.dropEffect = 'move'
  const rect = event.currentTarget.getBoundingClientRect()
  dropPath.value = item.path
  dropAfter.value = event.clientY > rect.top + rect.height / 2
}

function onDrop(item, event) {
  event.preventDefault()
  const from = dragPath.value
  const to = item.path
  const after = dropAfter.value
  resetDrag()
  if (!from || from === to) return

  const list = navItems.value.slice()
  const fromIndex = list.findIndex((i) => i.path === from)
  if (fromIndex < 0) return
  const [moved] = list.splice(fromIndex, 1)
  let toIndex = list.findIndex((i) => i.path === to)
  if (toIndex < 0) return
  if (after) toIndex += 1
  list.splice(toIndex, 0, moved)

  navItems.value = list
  localStorage.setItem(NAV_ORDER_KEY, JSON.stringify(list.map((i) => i.path)))
}

async function resetNavOrder() {
  try {
    await ElMessageBox.confirm(
      '将把侧栏菜单恢复到默认顺序，当前的自定义排列会被清除。是否继续？',
      '恢复默认顺序',
      { type: 'warning', confirmButtonText: '恢复', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  localStorage.removeItem(NAV_ORDER_KEY)
  navItems.value = NAV_ITEMS.slice()
  resetDrag()
  ElMessage.success('已恢复默认顺序')
}

const activePath = computed(() => route.path)

/* ── 后台任务角标 ───────────────────────────────────────
 * 侧栏上要能一眼看到「还有几个任务在跑」，所以这里自己维护一个计数。
 * 关键是**别每次进度推送都拉全量**：进度事件一秒可能好几条，拉全量会白打后端。
 * 于是记一份「任务 id → 状态」，靠快照的前后对比算出增减：
 * 状态从非进行中变成进行中 → +1，反之 → -1，其余（同一状态内的进度刷新）不动。
 * 拿不准的场合（拿到还没见过的任务）只影响增量，不影响准确性——计数与 id 表始终一致。
 */
const runningCount = ref(0)
const taskStatusById = new Map()

function applyTaskCountSnapshot(snapshot) {
  if (!snapshot || !snapshot.id) return
  const wasRunning = taskStatusById.get(snapshot.id) === 'running'
  const isRunning = snapshot.status === 'running'
  taskStatusById.set(snapshot.id, snapshot.status)
  if (isRunning && !wasRunning) runningCount.value += 1
  else if (!isRunning && wasRunning) runningCount.value = Math.max(0, runningCount.value - 1)
}

/** 整表重算（首次进入、以及事件给的快照用不上时的兜底）。 */
async function refreshTaskCount() {
  try {
    const list = await api.listTasks()
    taskStatusById.clear()
    let count = 0
    for (const task of Array.isArray(list) ? list : []) {
      if (!task || !task.id) continue
      taskStatusById.set(task.id, task.status)
      if (task.status === 'running') count += 1
    }
    runningCount.value = count
  } catch {
    // 未解锁 / 后端未就绪时静默：角标只是提示，不该弹错误打扰用户。
  }
}

function onTasksChanged(event) {
  const snapshot = event && event.detail
  if (!snapshot || !snapshot.id) {
    refreshTaskCount()
    return
  }
  applyTaskCountSnapshot(snapshot)
}

onMounted(() => {
  window.addEventListener(EVENTS.tasksChanged, onTasksChanged)
  refreshTaskCount()
})

onBeforeUnmount(() => {
  window.removeEventListener(EVENTS.tasksChanged, onTasksChanged)
})

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
    <el-aside
      :width="collapsed ? '64px' : '152px'"
      class="layout-aside"
      :class="{ 'is-collapsed': collapsed }"
    >
      <div class="brand" :class="{ 'is-collapsed': collapsed }">
        <img src="/favicon.svg" class="brand-logo" alt="Credential Vault" />
        <span v-show="!collapsed" class="brand-text">Credential Vault</span>
      </div>

      <!-- 预留位：后续功能按钮加在这里 -->
      <div class="aside-toolbar">
        <el-tooltip
          :content="collapsed ? '展开侧栏' : '收起侧栏'"
          :placement="collapsed ? 'right' : 'bottom'"
        >
          <el-button text @click="collapsed = !collapsed">
            <el-icon><component :is="collapsed ? Expand : Fold" /></el-icon>
          </el-button>
        </el-tooltip>
        <el-tooltip v-if="isCustomOrder" content="恢复默认顺序" placement="bottom">
          <el-button text :icon="RefreshLeft" @click="resetNavOrder" />
        </el-tooltip>
        <span class="toolbar-spacer" />
      </div>

      <el-menu :default-active="activePath" :collapse="collapsed" :collapse-transition="false" router class="layout-menu">
        <el-menu-item
          v-for="item in navItems"
          :key="item.path"
          :index="item.path"
          :draggable="!collapsed"
          :class="{
            'is-dragging': dragPath === item.path,
            'is-drop-before': dropPath === item.path && !dropAfter,
            'is-drop-after': dropPath === item.path && dropAfter,
          }"
          @dragstart="onDragStart(item, $event)"
          @dragover="onDragOver(item, $event)"
          @drop="onDrop(item, $event)"
          @dragend="resetDrag"
        >
          <el-icon><component :is="item.icon" /></el-icon>
          <template #title>{{ item.title }}</template>
          <!-- 进行中任务数角标：为 0 时不渲染（不占位、不显示「0」） -->
          <span
            v-if="item.path === '/tasks' && runningCount > 0"
            class="nav-badge"
          >{{ runningCount }}</span>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header class="layout-header">
        <div class="header-title">{{ pageTitle }}</div>
        <div class="header-actions">
          <el-tooltip :content="appStore.tray.mcp_running ? 'MCP 服务运行中' : 'MCP 服务已停止'" placement="bottom">
            <span class="mcp-indicator" :class="appStore.tray.mcp_running ? 'is-on' : 'is-off'">
              <i class="dot" />
              MCP {{ appStore.tray.mcp_running ? '运行中' : '已停止' }}
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
  position: relative;
  justify-content: center;
  padding: 0 !important;
}

.layout-menu :deep(.el-menu-item .el-icon) {
  margin-right: 6px;
}

.layout-menu :deep(.el-menu-item.is-active) {
  padding-left: 0 !important;
}

/* 拖拽排序：展开态显示抓手、可拖、悬停高亮插入位 */
.layout-aside:not(.is-collapsed) .layout-menu :deep(.el-menu-item) {
  cursor: grab;
}

.layout-aside:not(.is-collapsed) .layout-menu :deep(.el-menu-item)::after {
  content: '';
  position: absolute;
  right: 8px;
  top: 50%;
  width: 10px;
  height: 12px;
  margin-top: -6px;
  background-image: radial-gradient(circle, currentColor 1px, transparent 1.2px);
  background-size: 5px 5px;
  background-position: 2px 1px;
  opacity: 0;
  transition: opacity 0.15s;
  pointer-events: none;
}

.layout-aside:not(.is-collapsed) .layout-menu :deep(.el-menu-item:hover)::after {
  opacity: 0.35;
}

.layout-menu :deep(.el-menu-item.is-dragging) {
  opacity: 0.4;
}

.layout-menu :deep(.el-menu-item.is-drop-before)::before,
.layout-menu :deep(.el-menu-item.is-drop-after)::before {
  content: '';
  position: absolute;
  left: 6px;
  right: 6px;
  height: 2px;
  border-radius: 1px;
  background: var(--el-color-primary);
}

.layout-menu :deep(.el-menu-item.is-drop-before)::before {
  top: -1px;
}

.layout-menu :deep(.el-menu-item.is-drop-after)::before {
  bottom: -1px;
}

/* 进行中任务角标。菜单项本身已是 position: relative，这里直接绝对定位；
   收起态没有标题文字，就把角标挪到图标右上角，免得无处可依。 */
.nav-badge {
  position: absolute;
  right: 8px;
  top: 50%;
  transform: translateY(-50%);
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  box-sizing: border-box;
  border-radius: 8px;
  background: var(--el-color-primary);
  color: #fff;
  font-size: 11px;
  line-height: 16px;
  text-align: center;
  pointer-events: none;
}

.layout-aside.is-collapsed .nav-badge {
  right: 6px;
  top: 6px;
  transform: none;
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

.aside-toolbar {
  display: flex;
  align-items: center;
  gap: 2px;
  padding: 6px 8px;
  border-bottom: 1px solid var(--el-border-color);
}

.aside-toolbar .el-button {
  padding: 6px;
}

.toolbar-spacer {
  margin-left: auto;
}

.layout-aside.is-collapsed .aside-toolbar {
  justify-content: center;
  padding: 6px 2px;
}

.layout-aside.is-collapsed .toolbar-spacer {
  display: none;
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