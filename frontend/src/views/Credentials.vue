<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete, Edit, Expand, Fold, Plus, Refresh, Search, SetUp } from '@element-plus/icons-vue'

import CredentialForm from '@/components/CredentialForm.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { useVaultStore } from '@/stores/vault'
import { STATUS_OPTIONS, formatDateTimeFull } from '@/utils/format'
import { inlineIcon } from '@/utils/pluginIcon'

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

// 添加凭据时的默认服务：跟随目录树当前选中项
// （选中具体服务 → 该服务；选中类别 → 类别下第一个服务）
const createDefaultType = computed(() => {
  if (filters.credential_type) return filters.credential_type
  if (filters.credential_types && filters.credential_types.length) return filters.credential_types[0]
  return ''
})

// ── 批量验证：手动勾选（含一键全选）后并发验证 ──
const batchPickVisible = ref(false)
const batchPickIds = ref([])

/** 该类型是否允许批量验证（manifest batch_verify=false 的类型只能手动单条验证） */
function canBatchVerify(type) {
  const plugin = pluginFor(type)
  return !plugin || plugin.batch_verify !== false
}

const pickableCredentials = computed(() =>
  vaultStore.credentials.filter((c) => canBatchVerify(c.credential_type)),
)
const pickAllChecked = computed(
  () =>
    pickableCredentials.value.length > 0 &&
    batchPickIds.value.length === pickableCredentials.value.length,
)
const pickIndeterminate = computed(
  () =>
    batchPickIds.value.length > 0 &&
    batchPickIds.value.length < pickableCredentials.value.length,
)

function openBatchPick() {
  batchPickIds.value = pickableCredentials.value.map((c) => c.id)
  batchPickVisible.value = true
}

function onPickAll(checked) {
  batchPickIds.value = checked ? pickableCredentials.value.map((c) => c.id) : []
}

async function startPickedBatch() {
  const ids = [...batchPickIds.value]
  if (!ids.length) return
  batchPickVisible.value = false
  batchStarting.value = true
  try {
    await vaultStore.batchVerify('all', null, null, ids)
  } catch (err) {
    ElMessage.error(err.message || '批量验证启动失败')
  } finally {
    batchStarting.value = false
  }
}

function pluginFor(type) {
  return vaultStore.pluginByType[type] || null
}

function typeLabel(type) {
  const plugin = pluginFor(type)
  return plugin ? plugin.name : type
}

function typeIcon(type) {
  const plugin = pluginFor(type)
  return inlineIcon(plugin && plugin.icon)
}

/** 插件自定义图标（manifest icon 指向图片文件时后端返回 data URL） */
function iconImage(type) {
  const plugin = pluginFor(type)
  return plugin && plugin.icon_image ? plugin.icon_image : ''
}

/** 凭据单元格副标题：掩码后的敏感值（如 sk-123…cdef） */
function maskedSubtitle(row) {
  const masked = row.data_masked
  if (!masked || typeof masked !== 'object') return ''
  const values = Object.values(masked).filter(Boolean)
  return values[0] || ''
}

// account_info 常见键的中文标签（插件返回的原始英文键 → 可读标签）
const ACCOUNT_INFO_LABELS = {
  domain_count: '域名数',
  model_count: '模型数',
  workspace_count: '工作区数',
  account_id: '账号 ID',
  user_id: '用户 ID',
  accountId: '账号 ID',
  userId: '用户 ID',
  arn: 'ARN',
  email: '邮箱',
  login: '账号',
  username: '账号',
  name: '名称',
  first_name: '名称',
  scopes: '权限',
  plan: '套餐',
  accountType: '账户类型',
  canTrade: '可交易',
  canWithdraw: '可提现',
  canDeposit: '可充值',
  totalEq: '总权益',
  isoEq: '持仓权益',
  uTime: '更新时间',
  server: '服务器',
  port: '端口',
  channel_id: '频道 ID',
  guild_id: '服务器 ID',
  id: 'ID',
  label: '标签',
  usage: '用量',
  limit: '额度',
  limit_remaining: '剩余额度',
  org_id: '组织 ID',
  ram_user: 'RAM 用户',
  token_id: 'Token ID',
  token_status: 'Token 状态',
  app_id: 'App ID',
  app_key: 'App Key',
  tenant: '租户',
  account: '账号',
}

// 除余额/使用率/组织外，其余字段的展示优先级（只取最关键的少量，保持简洁）
const ACCOUNT_INFO_PRIORITY = [
  'domain_count', 'workspace_count', 'model_count',
  'login', 'username', 'name', 'email',
  'account_id', 'user_id', 'totalEq', 'channel_id', 'server',
]
const EXTRA_MAX = 2

/** 把 account_info 的单个值格式化为可读文本（毫秒时间戳转日期） */
function formatAccountValue(key, value) {
  if ((key === 'uTime' || key === 'updated_at') && /^\d{12,}$/.test(String(value))) {
    return formatDateTimeFull(new Date(Number(value)).toISOString())
  }
  return String(value)
}

/** 附加信息列：把 account_info 渲染为「标签 值 · …」的可读文本 */
function extraInfo(row) {
  const info = row.status_detail && row.status_detail.account_info
  if (!info || typeof info !== 'object') return ''
  const parts = []
  if (info.balance !== undefined && info.balance !== null && info.balance !== '') {
    const num = Number(info.balance)
    const shown = Number.isFinite(num) ? num.toFixed(2) : String(info.balance)
    parts.push(`余额 ${shown}${info.currency ? ` ${info.currency}` : ''}`)
  }
  if (info.usage_rate !== undefined && info.usage_rate !== null && info.usage_rate !== '') {
    const pct = Math.round(Number(info.usage_rate) * 100)
    if (!Number.isNaN(pct)) parts.push(`使用率 ${pct}%`)
  }
  if (info.organization) parts.push(`组织 ${info.organization}`)
  // 其余字段按优先级挑选，最多 EXTRA_MAX 项，超长值跳过——保持简洁
  const skip = new Set(['balance', 'currency', 'organization', 'usage_rate', 'usage_label'])
  const rest = []
  const candidates = [...ACCOUNT_INFO_PRIORITY, ...Object.keys(info)]
  const used = new Set()
  for (const key of candidates) {
    if (rest.length >= EXTRA_MAX) break
    if (skip.has(key) || used.has(key)) continue
    used.add(key)
    const value = info[key]
    if (value === null || value === undefined || value === '' || typeof value === 'object') continue
    const text = formatAccountValue(key, value)
    if (text.length > 40) continue
    rest.push(`${ACCOUNT_INFO_LABELS[key] || key} ${text}`)
  }
  return [...parts, ...rest].join(' · ')
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

// ── 树宽度：拖拽右侧边框调整并持久化 ──
const TREE_WIDTH_KEY = 'vault.credentials.treeWidth'
const TREE_WIDTH_MIN = 160
const TREE_WIDTH_MAX = 560

function clampTreeWidth(value) {
  return Math.min(TREE_WIDTH_MAX, Math.max(TREE_WIDTH_MIN, Math.round(value)))
}

const treeWidth = ref(clampTreeWidth(Number(localStorage.getItem(TREE_WIDTH_KEY)) || 230))
const treeResizing = ref(false)

function startTreeResize(event) {
  event.preventDefault()
  treeResizing.value = true
  const startX = event.clientX
  const startWidth = treeWidth.value
  const onMove = (e) => {
    treeWidth.value = clampTreeWidth(startWidth + (e.clientX - startX))
  }
  const onUp = () => {
    treeResizing.value = false
    localStorage.setItem(TREE_WIDTH_KEY, String(treeWidth.value))
    document.removeEventListener('mousemove', onMove)
    document.removeEventListener('mouseup', onUp)
    document.body.style.userSelect = ''
  }
  document.body.style.userSelect = 'none'
  document.addEventListener('mousemove', onMove)
  document.addEventListener('mouseup', onUp)
}

// ── 树搜索：按名称或相对路径过滤，并展开命中分支 ──
const treeSearch = ref('')

function treeNodeMatches(query, data) {
  const q = query.toLowerCase()
  return (data.label || '').toLowerCase().includes(q) || (data.rel_path || '').toLowerCase().includes(q)
}

function filterTreeNode(value, data) {
  const q = (value || '').trim().toLowerCase()
  if (!q) return true
  if (treeNodeMatches(q, data)) return true
  return (data.children || []).some((child) => filterTreeNode(value, child))
}

function applyTreeSearch() {
  const tree = treeRef.value
  if (!tree) return
  tree.filter(treeSearch.value)
  const q = treeSearch.value.trim().toLowerCase()
  if (!q) return
  const expandMatches = (nodes) => {
    nodes.forEach((node) => {
      const matched =
        treeNodeMatches(q, node) || (node.children || []).some((child) => treeNodeMatches(q, child))
      if (matched) tree.getNode(node.id)?.expand()
      if (node.children) expandMatches(node.children)
    })
  }
  expandMatches(treeData.value)
}

watch(treeSearch, () => {
  nextTick(applyTreeSearch)
})

const treeData = computed(() => {
  const children = []
  vaultStore.pluginTree.forEach((cat) => {
    if (cat.category === '') children.push(...cat.children)
    else children.push(cat)
  })
  return [{ id: 'all', label: '全部凭据', icon: '🗂️', children }]
})

const treeProps = { label: 'label', children: 'children' }

/** 选中节点的相对路径（如 llm/deepseek），用于在目录顶部展示提示 */
const selectedRelPath = computed(() => {
  const map = {}
  const walk = (nodes) =>
    nodes.forEach((node) => {
      map[node.id] = node.rel_path || ''
      if (node.children) walk(node.children)
    })
  walk(treeData.value)
  return map[selectedNode.value] || ''
})

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

// ── 列配置：顺序（拖拽）与可见性（列设置），均持久化 localStorage ──

const COLUMN_DEFS = [
  { key: 'credential', label: '凭据', minWidth: 230, locked: true },
  { key: 'type', label: '类型', minWidth: 140 },
  { key: 'notes', label: '备注', minWidth: 130 },
  { key: 'extra', label: '附加信息', minWidth: 170 },
  { key: 'group', label: '分组', width: 110 },
  { key: 'status', label: '状态', width: 100 },
  { key: 'created_at', label: '添加时间', width: 170 },
  { key: 'last_verified', label: '最后验证', width: 170 },
  { key: 'last_valid', label: '最后有效', width: 170 },
  { key: 'actions', label: '操作', width: 190, locked: true },
]

const COLUMN_ORDER_KEY = 'vault.credentials.columnOrder'
const COLUMN_VISIBLE_KEY = 'vault.credentials.visibleColumns'
const ALL_COLUMN_KEYS = COLUMN_DEFS.map((def) => def.key)

function loadColumnOrder() {
  try {
    const saved = JSON.parse(localStorage.getItem(COLUMN_ORDER_KEY) || '[]')
    if (Array.isArray(saved)) {
      const known = saved.filter((key) => ALL_COLUMN_KEYS.includes(key))
      const missing = ALL_COLUMN_KEYS.filter((key) => !known.includes(key))
      if (known.length) return [...known, ...missing]
    }
  } catch {
    // 忽略损坏的本地配置
  }
  return [...ALL_COLUMN_KEYS]
}

function loadVisibleColumns() {
  try {
    const saved = JSON.parse(localStorage.getItem(COLUMN_VISIBLE_KEY) || 'null')
    if (Array.isArray(saved)) {
      const known = saved.filter((key) => ALL_COLUMN_KEYS.includes(key))
      if (known.length) return known
    }
  } catch {
    // 忽略损坏的本地配置
  }
  return [...ALL_COLUMN_KEYS]
}

const columnOrder = ref(loadColumnOrder())
const visibleKeys = ref(loadVisibleColumns())

// 锁定列（凭据/操作）始终可见
const effectiveVisible = computed(() => {
  const locked = COLUMN_DEFS.filter((def) => def.locked).map((def) => def.key)
  return [...new Set([...visibleKeys.value, ...locked])]
})

const orderedColumns = computed(() =>
  columnOrder.value
    .filter((key) => effectiveVisible.value.includes(key))
    .map((key) => COLUMN_DEFS.find((def) => def.key === key))
    .filter(Boolean),
)

function handleVisibleChange(values) {
  visibleKeys.value = values
  localStorage.setItem(COLUMN_VISIBLE_KEY, JSON.stringify(values))
  nextTick(setupHeaderDrag)
}

// ── 表头拖拽排序：以列 key 标识（label-class-name），避免索引错位 ──

const tableRef = ref(null)
let dragCleanup = []

function columnKeyOf(th) {
  const cls = Array.from(th.classList).find((name) => name.startsWith('vault-th-'))
  return cls ? cls.slice('vault-th-'.length) : null
}

function setupHeaderDrag() {
  dragCleanup.forEach((fn) => fn())
  dragCleanup = []
  const tableEl = tableRef.value && tableRef.value.$el
  if (!tableEl) return
  const ths = Array.from(
    tableEl.querySelectorAll('.el-table__header-wrapper thead th'),
  ).filter((th) => columnKeyOf(th))
  ths.forEach((th) => {
    const targetKey = columnKeyOf(th)
    th.draggable = true
    th.classList.add('is-draggable')
    const onDragStart = (event) => {
      event.dataTransfer.effectAllowed = 'move'
      event.dataTransfer.setData('text/plain', targetKey)
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
      const fromKey = event.dataTransfer.getData('text/plain')
      if (!fromKey || fromKey === targetKey) return
      const next = [...columnOrder.value]
      const fromIndex = next.indexOf(fromKey)
      const toIndex = next.indexOf(targetKey)
      if (fromIndex === -1 || toIndex === -1) return
      next.splice(fromIndex, 1)
      next.splice(next.indexOf(targetKey), 0, fromKey)
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
      <el-popover placement="bottom-end" :width="200" trigger="click">
        <template #reference>
          <el-button :icon="SetUp">列设置</el-button>
        </template>
        <div class="column-settings">
          <div class="column-settings-title">显示列</div>
          <el-checkbox-group
            :model-value="visibleKeys"
            @update:model-value="handleVisibleChange"
          >
            <el-checkbox
              v-for="def in COLUMN_DEFS"
              :key="def.key"
              :value="def.key"
              :disabled="!!def.locked"
              class="column-settings-item"
            >
              {{ def.label }}
            </el-checkbox>
          </el-checkbox-group>
          <div class="column-settings-hint">表头可拖拽调整顺序</div>
        </div>
      </el-popover>
      <el-button :icon="Refresh" :loading="batchStarting" @click="openBatchPick">批量验证</el-button>
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
      <el-card
        shadow="never"
        class="tree-card"
        :class="{ collapsed: treeCollapsed, resizing: treeResizing }"
        :style="treeCollapsed ? null : { width: treeWidth + 'px' }"
      >
        <div class="tree-header">
          <span v-if="!treeCollapsed" class="tree-title">凭据目录</span>
          <el-input
            v-if="!treeCollapsed"
            v-model="treeSearch"
            class="tree-search"
            size="small"
            placeholder="搜索服务"
            clearable
            :prefix-icon="Search"
          />
          <el-button
            text
            size="small"
            :icon="treeCollapsed ? Expand : Fold"
            :title="treeCollapsed ? '展开目录' : '收起目录'"
            @click="toggleTreeCollapsed"
          />
        </div>
        <div
          v-if="!treeCollapsed && selectedRelPath"
          class="tree-path-hint"
          :title="`plugins/${selectedRelPath}`"
        >
          {{ selectedRelPath }}
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
          :filter-node-method="filterTreeNode"
          @node-click="handleNodeClick"
          @node-expand="handleNodeExpand"
          @node-collapse="handleNodeCollapse"
        >
          <template #default="{ data }">
            <el-tooltip
              :content="`plugins/${data.rel_path}`"
              placement="right"
              :disabled="!data.rel_path"
              :show-after="300"
            >
              <span class="tree-node">
                <img v-if="data.icon_image" class="tree-icon-img" :src="data.icon_image" alt="" />
                <span v-else class="tree-icon">{{ data.icon }}</span>
                <span class="tree-label">{{ data.label }}</span>
                <span v-if="data.count !== undefined" class="tree-count">{{ data.count }}</span>
              </span>
            </el-tooltip>
          </template>
        </el-tree>
        <div v-if="!treeCollapsed" class="tree-resizer" @mousedown="startTreeResize" />
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
            :label-class-name="`vault-th-${col.key}`"
          >
            <template v-if="col.key === 'credential'" #default="{ row }">
              <div class="credential-cell">
                <img v-if="iconImage(row.credential_type)" class="credential-icon-img" :src="iconImage(row.credential_type)" alt="" />
                <span v-else class="credential-icon">{{ typeIcon(row.credential_type) }}</span>
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
              <span class="text-muted mono">{{ formatDateTimeFull(row.created_at) }}</span>
            </template>

            <template v-else-if="col.key === 'last_verified'" #default="{ row }">
              <span class="text-muted mono">{{ formatDateTimeFull(row.last_verified_at) }}</span>
            </template>

            <template v-else-if="col.key === 'last_valid'" #default="{ row }">
              <span class="text-muted mono">{{ formatDateTimeFull(row.last_valid_at) }}</span>
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

    <el-dialog
      v-model="batchPickVisible"
      title="批量验证"
      width="560px"
      :close-on-click-modal="false"
    >
      <div class="pick-toolbar">
        <el-checkbox
          :model-value="pickAllChecked"
          :indeterminate="pickIndeterminate"
          @change="onPickAll"
        >全选</el-checkbox>
        <span class="pick-count text-muted">
          已选 {{ batchPickIds.length }} / {{ pickableCredentials.length }}
        </span>
      </div>
      <div class="pick-list">
        <el-checkbox-group v-model="batchPickIds">
          <div
            v-for="row in vaultStore.credentials"
            :key="row.id"
            class="pick-row"
            :class="{ 'is-disabled': !canBatchVerify(row.credential_type) }"
          >
            <el-checkbox :value="row.id" :disabled="!canBatchVerify(row.credential_type)">
              <span class="pick-name">{{ row.name }}</span>
              <span class="pick-type text-muted">{{ typeLabel(row.credential_type) }}</span>
            </el-checkbox>
            <el-tag v-if="!canBatchVerify(row.credential_type)" size="small" type="info">
              需手动验证
            </el-tag>
          </div>
        </el-checkbox-group>
        <el-empty
          v-if="!vaultStore.credentials.length"
          description="暂无可验证的凭据"
          :image-size="60"
        />
      </div>
      <template #footer>
        <el-button @click="batchPickVisible = false">取消</el-button>
        <el-button type="primary" :disabled="!batchPickIds.length" @click="startPickedBatch">
          验证 {{ batchPickIds.length }} 条
        </el-button>
      </template>
    </el-dialog>

    <CredentialForm v-model="formVisible" :credential="editing" :default-type="createDefaultType" @saved="load" />
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
  flex-shrink: 0;
  position: relative;
  transition: width 0.2s ease;
}

.tree-card.resizing {
  transition: none;
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
  gap: 6px;
  margin-bottom: 6px;
}

.tree-card.collapsed .tree-header {
  justify-content: center;
  margin-bottom: 0;
}

.tree-title {
  flex-shrink: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-secondary);
}

.tree-search {
  flex: 1;
  min-width: 0;
}

.tree-search :deep(.el-input__wrapper) {
  padding: 0 8px;
}

.tree-path-hint {
  margin-bottom: 6px;
  font-family: 'JetBrains Mono', 'Consolas', monospace;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tree-resizer {
  position: absolute;
  top: 0;
  right: 0;
  width: 6px;
  height: 100%;
  cursor: col-resize;
  z-index: 5;
}

.tree-resizer:hover,
.tree-card.resizing .tree-resizer {
  background: var(--el-color-primary-light-7);
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

.credential-icon-img {
  width: 22px;
  height: 22px;
  object-fit: contain;
  flex-shrink: 0;
}

.tree-icon-img {
  width: 16px;
  height: 16px;
  object-fit: contain;
  flex-shrink: 0;
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

.pick-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.pick-count {
  font-size: 12px;
}

.pick-list {
  max-height: 46vh;
  overflow-y: auto;
  padding: 4px 10px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
}

.pick-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 4px 0;
}

.pick-row.is-disabled {
  opacity: 0.6;
}

.pick-name {
  font-weight: 500;
}

.pick-type {
  margin-left: 8px;
  font-size: 12px;
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

/* 列设置弹层 */
.column-settings-title {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 8px;
}

.column-settings-item {
  display: flex;
  margin-right: 0;
  height: 28px;
}

.column-settings-hint {
  margin-top: 8px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
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
