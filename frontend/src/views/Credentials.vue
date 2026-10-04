<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete, Edit, Expand, Fold, Plus, Refresh, Search } from '@element-plus/icons-vue'

import CredentialForm from '@/components/CredentialForm.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { useVaultStore } from '@/stores/vault'
import { STATUS_OPTIONS, formatRelative } from '@/utils/format'

const route = useRoute()
const router = useRouter()
const vaultStore = useVaultStore()

const formVisible = ref(false)
const editing = ref(null)
const verifyingId = ref(null)
const batchStarting = ref(false)

const filters = vaultStore.filters

const statusOptions = STATUS_OPTIONS

const groupOptions = computed(() => vaultStore.groupOptions)
const hasPlugins = computed(() => vaultStore.plugins.length > 0)

const scopeArgs = computed(() => {
  if (filters.credential_type) return ['type', null, filters.credential_type]
  if (filters.group_id) return ['group', filters.group_id, null]
  return ['all', null, null]
})

function pluginFor(type) {
  return vaultStore.pluginByType[type] || null
}

function typeLabel(type) {
  const plugin = pluginFor(type)
  return plugin ? plugin.name : type
}

function typeIcon(type) {
  const plugin = pluginFor(type)
  return plugin && plugin.icon ? plugin.icon : '🔑'
}

/** 凭据单元格副标题：掩码后的敏感值（如 sk-123…cdef） */
function maskedSubtitle(row) {
  const masked = row.data_masked
  if (!masked || typeof masked !== 'object') return ''
  const values = Object.values(masked).filter(Boolean)
  return values[0] || ''
}

/** 附加信息列：账户信息（余额、组织等） */
function extraInfo(row) {
  const info = row.status_detail && row.status_detail.account_info
  if (!info || typeof info !== 'object') return ''
  const parts = []
  if (info.balance !== undefined && info.balance !== null && info.balance !== '') {
    parts.push(`余额 ${info.balance}${info.currency ? ` ${info.currency}` : ''}`)
  }
  if (info.organization) parts.push(info.organization)
  Object.keys(info).forEach((key) => {
    if (['balance', 'currency', 'organization'].includes(key)) return
    const value = info[key]
    if (value === null || value === undefined || value === '' || typeof value === 'object') return
    parts.push(String(value))
  })
  return parts.join(' · ')
}

function notesOf(row) {
  return (row.data_plain && row.data_plain.notes) || ''
}

// ── 左侧插件目录树 ────────────────────────────────────────
// 与 plugins/ 文件系统层次一致：类别目录 → 服务子插件。
// 根目录平铺插件（无类别）直接挂在「全部凭据」下。

const treeRef = ref(null)
const selectedNode = ref('all')

// 树的展开/收起状态由本组件自行维护（持久化 localStorage），
// 避免凭据保存后 treeData 重建导致已收起的节点被重新展开。
const TREE_EXPANDED_KEY = 'vault.credentials.treeExpanded'
const TREE_COLLAPSED_KEY = 'vault.credentials.treeCollapsed'

const expandedKeys = ref(JSON.parse(localStorage.getItem(TREE_EXPANDED_KEY) || '[]'))
const treeCollapsed = ref(localStorage.getItem(TREE_COLLAPSED_KEY) === '1')

function persistExpanded() {
  localStorage.setItem(TREE_EXPANDED_KEY, JSON.stringify(expandedKeys.value))
}

function collectExpandable(nodes, acc = []) {
  nodes.forEach((node) => {
    if (node.children && node.children.length) {
      acc.push(node.id)
      collectExpandable(node.children, acc)
    }
  })
  return acc
}

function handleNodeExpand(node) {
  if (!expandedKeys.value.includes(node.id)) {
    expandedKeys.value.push(node.id)
    persistExpanded()
  }
}

function handleNodeCollapse(node) {
  expandedKeys.value = expandedKeys.value.filter((key) => key !== node.id)
  persistExpanded()
}

function toggleTreeCollapsed() {
  treeCollapsed.value = !treeCollapsed.value
  localStorage.setItem(TREE_COLLAPSED_KEY, treeCollapsed.value ? '1' : '0')
}

const treeData = computed(() => {
  const children = []
  vaultStore.pluginTree.forEach((cat) => {
    if (cat.category === '') children.push(...cat.children)
    else children.push(cat)
  })
  return [{ id: 'all', label: '全部凭据', icon: '🗂️', children }]
})

const treeProps = { label: 'label', children: 'children' }

function handleNodeClick(node) {
  selectedNode.value = node.id
  if (node.id === 'all') {
    filters.credential_type = null
    filters.credential_types = null
  } else if (node.types) {
    // 类别节点：展示该层级及其子层级的全部条目
    filters.credential_type = null
    filters.credential_types = node.types
  } else {
    filters.credential_type = node.credential_type
    filters.credential_types = null
  }
  applyFilters()
}

function resetTree() {
  selectedNode.value = 'all'
  filters.credential_type = null
  filters.credential_types = null
  if (treeRef.value) treeRef.value.setCurrentKey('all')
}

// ── 表头拖拽排序（原生 HTML5 DnD，顺序持久化 localStorage）──

const COLUMN_DEFS = [
  { key: 'credential', label: '凭据', minWidth: 230 },
  { key: 'type', label: '类型', minWidth: 140 },
  { key: 'notes', label: '备注', minWidth: 130 },
  { key: 'extra', label: '附加信息', minWidth: 170 },
  { key: 'group', label: '分组', width: 110 },
  { key: 'status', label: '状态', width: 100 },
  { key: 'created_at', label: '添加时间', width: 120 },
  { key: 'last_verified', label: '最后验证', width: 120 },
  { key: 'last_valid', label: '最后有效', width: 120 },
  { key: 'actions', label: '操作', width: 190 },
]

const COLUMN_ORDER_KEY = 'vault.credentials.columnOrder'

function loadColumnOrder() {
  try {
    const saved = JSON.parse(localStorage.getItem(COLUMN_ORDER_KEY) || '[]')
    if (Array.isArray(saved)) {
      const known = saved.filter((key) => COLUMN_DEFS.some((def) => def.key === key))
      const missing = COLUMN_DEFS.map((def) => def.key).filter((key) => !known.includes(key))
      if (known.length) return [...known, ...missing]
    }
  } catch {
    // 忽略损坏的本地配置
  }
  return COLUMN_DEFS.map((def) => def.key)
}

const columnOrder = ref(loadColumnOrder())
const orderedColumns = computed(() =>
  columnOrder.value.map((key) => COLUMN_DEFS.find((def) => def.key === key)).filter(Boolean),
)

const tableRef = ref(null)
let dragCleanup = []

function setupHeaderDrag() {
  dragCleanup.forEach((fn) => fn())
  dragCleanup = []
  const tableEl = tableRef.value && tableRef.value.$el
  if (!tableEl) return
  const ths = Array.from(
    tableEl.querySelectorAll('.el-table__header-wrapper thead th'),
  ).filter((th) => !th.classList.contains('gutter'))
  ths.forEach((th, index) => {
    th.draggable = true
    th.classList.add('is-draggable')
    const onDragStart = (event) => {
      event.dataTransfer.effectAllowed = 'move'
      event.dataTransfer.setData('text/plain', String(index))
      th.classList.add('is-dragging')
    }
    const onDragEnd = () => th.classList.remove('is-dragging')
    const onDragOver = (event) => {
      event.preventDefault()
      event.dataTransfer.dropEffect = 'move'
      th.classList.add('is-drag-over')
    }
    const onDragLeave = () => th.classList.remove('is-drag-over')
    const onDrop = (event) => {
      event.preventDefault()
      th.classList.remove('is-drag-over')
      const from = Number(event.dataTransfer.getData('text/plain'))
      if (!Number.isInteger(from) || from === index) return
      const next = [...columnOrder.value]
      const [moved] = next.splice(from, 1)
      next.splice(index, 0, moved)
      columnOrder.value = next
      localStorage.setItem(COLUMN_ORDER_KEY, JSON.stringify(next))
      nextTick(setupHeaderDrag)
    }
    th.addEventListener('dragstart', onDragStart)
    th.addEventListener('dragend', onDragEnd)
    th.addEventListener('dragover', onDragOver)
    th.addEventListener('dragleave', onDragLeave)
    th.addEventListener('drop', onDrop)
    dragCleanup.push(() => {
      th.removeEventListener('dragstart', onDragStart)
      th.removeEventListener('dragend', onDragEnd)
      th.removeEventListener('dragover', onDragOver)
      th.removeEventListener('dragleave', onDragLeave)
      th.removeEventListener('drop', onDrop)
    })
  })
}

onBeforeUnmount(() => {
  dragCleanup.forEach((fn) => fn())
  dragCleanup = []
})

// ── 数据加载与操作 ────────────────────────────────────────

async function load() {
  try {
    await Promise.all([vaultStore.fetchCredentials(), vaultStore.fetchGroups(), vaultStore.fetchPlugins()])
  } catch (err) {
    ElMessage.error(err.message || '加载凭据失败')
  }
}

let searchTimer = null
watch(
  () => filters.search,
  () => {
    if (searchTimer) clearTimeout(searchTimer)
    searchTimer = setTimeout(() => {
      vaultStore.fetchCredentials().catch((err) => ElMessage.error(err.message || '搜索失败'))
    }, 350)
  },
)

function applyFilters() {
  vaultStore.fetchCredentials().catch((err) => ElMessage.error(err.message || '筛选失败'))
}

function resetFilters() {
  filters.group_id = null
  filters.status = null
  filters.search = ''
  resetTree()
  applyFilters()
}

function openCreate() {
  if (!hasPlugins.value) {
    ElMessage.warning('未检测到可用插件，请将插件目录放入 plugins/ 文件夹后重启应用')
    return
  }
  editing.value = null
  formVisible.value = true
}

function openEdit(row) {
  editing.value = row
  formVisible.value = true
}

async function verifyRow(row) {
  verifyingId.value = row.id
  try {
    const result = await vaultStore.verifyCredential(row.id)
    if (result.status === 'valid') ElMessage.success(`${row.name}：验证通过`)
    else ElMessage.warning(`${row.name}：${result.message || '验证未通过'}`)
  } catch (err) {
    ElMessage.error(err.message || '验证失败')
  } finally {
    verifyingId.value = null
  }
}

async function removeRow(row) {
  try {
    await ElMessageBox.confirm(
      `确定要删除凭据「${row.name}」吗？该操作不可撤销。`,
      '删除凭据',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消', confirmButtonClass: 'el-button--danger' },
    )
  } catch {
    return
  }
  try {
    await vaultStore.deleteCredential(row.id)
    ElMessage.success('凭据已删除')
  } catch (err) {
    ElMessage.error(err.message || '删除失败')
  }
}

async function batchVerify() {
  batchStarting.value = true
  try {
    await vaultStore.batchVerify(...scopeArgs.value)
  } catch (err) {
    ElMessage.error(err.message || '批量验证启动失败')
  } finally {
    batchStarting.value = false
  }
}

watch(
  () => route.query.new,
  (value) => {
    if (value === '1') {
      openCreate()
      router.replace({ path: '/credentials' })
    }
  },
)

onMounted(async () => {
  await load()
  // 首次使用（无本地记录）时默认展开全部节点
  if (!expandedKeys.value.length) {
    expandedKeys.value = collectExpandable(treeData.value)
    persistExpanded()
  }
  nextTick(() => {
    if (treeRef.value) treeRef.value.setCurrentKey('all')
    setupHeaderDrag()
  })
  if (route.query.new === '1') {
    openCreate()
    router.replace({ path: '/credentials' })
  }
})
</script>

<template>
  <div class="page">
    <div class="page-toolbar">
      <h2 class="section-title">凭据管理</h2>
      <span class="text-muted">共 {{ vaultStore.credentials.length }} 条</span>
      <div class="spacer" />
      <el-button :icon="Refresh" :loading="batchStarting" @click="batchVerify">批量验证</el-button>
      <el-button type="primary" :icon="Plus" @click="openCreate">添加凭据</el-button>
    </div>

    <el-card shadow="never">
      <div class="filter-bar">
        <el-input
          v-model="filters.search"
          :prefix-icon="Search"
          placeholder="搜索名称、类型或分组"
          clearable
          class="filter-search"
        />
        <el-select v-model="filters.group_id" placeholder="全部分组" clearable class="filter-item" @change="applyFilters">
          <el-option v-for="option in groupOptions" :key="option.value" :label="option.label" :value="option.value" />
        </el-select>
        <el-select v-model="filters.status" placeholder="全部状态" clearable class="filter-item" @change="applyFilters">
          <el-option v-for="option in statusOptions" :key="option.value" :label="option.label" :value="option.value" />
        </el-select>
        <el-button text @click="resetFilters">重置</el-button>
      </div>
    </el-card>

    <div class="content-row">
      <el-card shadow="never" class="tree-card" :class="{ collapsed: treeCollapsed }">
        <div class="tree-header">
          <span v-if="!treeCollapsed" class="tree-title">凭据目录</span>
          <el-button
            text
            size="small"
            :icon="treeCollapsed ? Expand : Fold"
            :title="treeCollapsed ? '展开目录' : '收起目录'"
            @click="toggleTreeCollapsed"
          />
        </div>
        <el-tree
          v-show="!treeCollapsed"
          ref="treeRef"
          :data="treeData"
          :props="treeProps"
          node-key="id"
          highlight-current
          :default-expanded-keys="expandedKeys"
          :expand-on-click-node="false"
          @node-click="handleNodeClick"
          @node-expand="handleNodeExpand"
          @node-collapse="handleNodeCollapse"
        >
          <template #default="{ data }">
            <span class="tree-node">
              <span class="tree-icon">{{ data.icon }}</span>
              <span class="tree-label">{{ data.label }}</span>
              <span v-if="data.count !== undefined" class="tree-count">{{ data.count }}</span>
            </span>
          </template>
        </el-tree>
      </el-card>

      <el-card shadow="never" class="table-card">
        <el-table
          ref="tableRef"
          v-loading="vaultStore.loadingCredentials"
          :data="vaultStore.credentials"
          empty-text="暂无凭据，点击右上角「添加凭据」开始"
          style="width: 100%"
        >
          <el-table-column
            v-for="col in orderedColumns"
            :key="col.key"
            :label="col.label"
            :width="col.width"
            :min-width="col.minWidth"
          >
            <template v-if="col.key === 'credential'" #default="{ row }">
              <div class="credential-cell">
                <span class="credential-icon">{{ typeIcon(row.credential_type) }}</span>
                <div class="credential-text">
                  <div class="credential-name">{{ row.name }}</div>
                  <div class="credential-sub mono">{{ maskedSubtitle(row) || row.credential_type }}</div>
                </div>
              </div>
            </template>

            <template v-else-if="col.key === 'type'" #default="{ row }">
              <span class="text-muted">{{ typeLabel(row.credential_type) }}</span>
            </template>

            <template v-else-if="col.key === 'notes'" #default="{ row }">
              <span class="text-muted">{{ notesOf(row) || '—' }}</span>
            </template>

            <template v-else-if="col.key === 'extra'" #default="{ row }">
              <span class="text-muted">{{ extraInfo(row) || '—' }}</span>
            </template>

            <template v-else-if="col.key === 'group'" #default="{ row }">
              <el-tag v-if="row.group_name" size="small" effect="plain">{{ row.group_name }}</el-tag>
              <span v-else class="text-muted">未分组</span>
            </template>

            <template v-else-if="col.key === 'status'" #default="{ row }">
              <StatusBadge :status="row.status" :detail="row.status_detail" />
            </template>

            <template v-else-if="col.key === 'created_at'" #default="{ row }">
              <span class="text-muted">{{ formatRelative(row.created_at) }}</span>
            </template>

            <template v-else-if="col.key === 'last_verified'" #default="{ row }">
              <span class="text-muted">{{ formatRelative(row.last_verified_at) }}</span>
            </template>

            <template v-else-if="col.key === 'last_valid'" #default="{ row }">
              <span class="text-muted">{{ formatRelative(row.last_valid_at) }}</span>
            </template>

            <template v-else-if="col.key === 'actions'" #default="{ row }">
              <div class="action-cell">
                <el-button
                  size="small"
                  type="primary"
                  plain
                  :loading="verifyingId === row.id"
                  @click="verifyRow(row)"
                >
                  验证
                </el-button>
                <el-button size="small" :icon="Edit" @click="openEdit(row)">编辑</el-button>
                <el-button size="small" type="danger" plain :icon="Delete" @click="removeRow(row)" />
              </div>
            </template>
          </el-table-column>
        </el-table>
      </el-card>
    </div>

    <CredentialForm v-model="formVisible" :credential="editing" @saved="load" />
  </div>
</template>

<style scoped>
.section-title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
}

.filter-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.filter-search {
  width: 260px;
}

.filter-item {
  width: 170px;
}

.content-row {
  display: flex;
  gap: 16px;
  align-items: flex-start;
}

.tree-card {
  width: 230px;
  flex-shrink: 0;
  transition: width 0.2s ease;
}

.tree-card.collapsed {
  width: 48px;
}

.tree-card.collapsed :deep(.el-card__body) {
  padding: 8px 6px;
}

.tree-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
}

.tree-card.collapsed .tree-header {
  justify-content: center;
  margin-bottom: 0;
}

.tree-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-secondary);
}

.tree-card :deep(.el-card__body) {
  padding: 8px;
}

.tree-node {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 1;
  overflow: hidden;
}

.tree-icon {
  line-height: 1;
}

.tree-label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tree-count {
  margin-left: auto;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.table-card {
  flex: 1;
  min-width: 0;
}

.credential-cell {
  display: flex;
  align-items: center;
  gap: 10px;
}

.credential-icon {
  font-size: 20px;
  line-height: 1;
}

.credential-name {
  font-weight: 600;
}

.credential-sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-top: 2px;
}

.mono {
  font-family: 'JetBrains Mono', 'Consolas', monospace;
}

/* 操作列按钮：flex 布局对齐，清除 Element Plus 相邻按钮的默认左间距 */
.action-cell {
  display: flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
}

.action-cell .el-button {
  margin-left: 0;
}

/* 表头拖拽提示样式 */
.table-card :deep(.el-table__header-wrapper th.is-draggable) {
  cursor: grab;
  user-select: none;
}

.table-card :deep(.el-table__header-wrapper th.is-dragging) {
  opacity: 0.5;
}

.table-card :deep(.el-table__header-wrapper th.is-drag-over) {
  border-left: 2px solid var(--el-color-primary);
}
</style>
