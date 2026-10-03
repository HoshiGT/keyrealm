<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete, Edit, MoreFilled, Plus, Refresh, Search, View } from '@element-plus/icons-vue'

import CredentialForm from '@/components/CredentialForm.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { useVaultStore } from '@/stores/vault'
import { STATUS_OPTIONS, formatDateTime, formatDuration, formatRelative } from '@/utils/format'

const route = useRoute()
const router = useRouter()
const vaultStore = useVaultStore()

const formVisible = ref(false)
const editing = ref(null)
const verifyingId = ref(null)
const batchStarting = ref(false)

const historyVisible = ref(false)
const historyLoading = ref(false)
const historyRows = ref([])
const historyCredential = ref(null)

const filters = vaultStore.filters

const statusOptions = STATUS_OPTIONS

const typeOptions = computed(() => vaultStore.typeOptions)
const groupOptions = computed(() => vaultStore.groupOptions)
const hasPlugins = computed(() => vaultStore.enabledPlugins.length > 0)

const triggerLabels = {
  manual: '手动',
  auto: '自动',
  batch: '批量',
  schedule: '定时',
}

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

function subtitle(row) {
  const parts = []
  const plugin = pluginFor(row.credential_type)
  if (plugin && Array.isArray(plugin.fields_schema)) {
    plugin.fields_schema
      .filter((field) => !field.sensitive && field.type !== 'password' && field.type !== 'textarea' && field.type !== 'checkbox')
      .slice(0, 2)
      .forEach((field) => {
        const value = row.data_plain ? row.data_plain[field.key] : undefined
        if (value === null || value === undefined || value === '' || typeof value === 'object') return
        parts.push(String(value))
      })
  }
  if (row.group_name) parts.push(row.group_name)
  return parts.join(' · ')
}

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
  filters.credential_type = null
  filters.status = null
  filters.search = ''
  applyFilters()
}

function openCreate() {
  if (!hasPlugins.value) {
    ElMessage.warning('请先在插件管理中安装并启用至少一个插件')
    router.push('/plugins')
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

async function openHistory(row) {
  historyCredential.value = row
  historyVisible.value = true
  historyLoading.value = true
  historyRows.value = []
  try {
    historyRows.value = await vaultStore.getHistory(row.id, 20)
  } catch (err) {
    ElMessage.error(err.message || '加载验证历史失败')
  } finally {
    historyLoading.value = false
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

function handleCommand(command, row) {
  if (command === 'history') openHistory(row)
  else if (command === 'delete') removeRow(row)
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
        <el-select
          v-model="filters.credential_type"
          placeholder="全部类型"
          clearable
          class="filter-item"
          @change="applyFilters"
        >
          <el-option v-for="option in typeOptions" :key="option.value" :label="option.label" :value="option.value" />
        </el-select>
        <el-select v-model="filters.status" placeholder="全部状态" clearable class="filter-item" @change="applyFilters">
          <el-option v-for="option in statusOptions" :key="option.value" :label="option.label" :value="option.value" />
        </el-select>
        <el-button text @click="resetFilters">重置</el-button>
      </div>
    </el-card>

    <el-card shadow="never">
      <el-table
        v-loading="vaultStore.loadingCredentials"
        :data="vaultStore.credentials"
        empty-text="暂无凭据，点击右上角「添加凭据」开始"
        style="width: 100%"
      >
        <el-table-column label="凭据" min-width="260">
          <template #default="{ row }">
            <div class="credential-cell">
              <span class="credential-icon">{{ typeIcon(row.credential_type) }}</span>
              <div class="credential-text">
                <div class="credential-name">{{ row.name }}</div>
                <div class="credential-sub">{{ subtitle(row) || row.credential_type }}</div>
              </div>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="类型" min-width="150">
          <template #default="{ row }">
            <span class="text-muted">{{ typeLabel(row.credential_type) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="分组" width="130">
          <template #default="{ row }">
            <el-tag v-if="row.group_name" size="small" effect="plain">{{ row.group_name }}</el-tag>
            <span v-else class="text-muted">未分组</span>
          </template>
        </el-table-column>

        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <StatusBadge :status="row.status" :detail="row.status_detail" />
          </template>
        </el-table-column>

        <el-table-column label="最后验证" width="130">
          <template #default="{ row }">
            <span class="text-muted">{{ formatRelative(row.last_verified_at) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{ row }">
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
            <el-dropdown trigger="click" @command="(command) => handleCommand(command, row)">
              <el-button size="small" :icon="MoreFilled" />
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="history" :icon="View">验证历史</el-dropdown-item>
                  <el-dropdown-item command="delete" :icon="Delete" divided>删除</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <CredentialForm v-model="formVisible" :credential="editing" @saved="load" />

    <el-dialog v-model="historyVisible" title="验证历史" width="720px" top="8vh">
      <div class="history-header">
        <span class="history-name">{{ historyCredential?.name }}</span>
        <StatusBadge v-if="historyCredential" :status="historyCredential.status" :detail="historyCredential.status_detail" />
      </div>

      <el-table v-loading="historyLoading" :data="historyRows" empty-text="暂无验证记录" max-height="420">
        <el-table-column label="时间" width="180">
          <template #default="{ row }">
            <span class="text-muted">{{ formatDateTime(row.verified_at) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="结果" width="100">
          <template #default="{ row }">
            <StatusBadge :status="row.status" size="small" />
          </template>
        </el-table-column>
        <el-table-column label="触发方式" width="100">
          <template #default="{ row }">
            <span class="text-muted">{{ triggerLabels[row.trigger_type] || row.trigger_type }}</span>
          </template>
        </el-table-column>
        <el-table-column label="耗时" width="90">
          <template #default="{ row }">
            <span class="text-muted">{{ formatDuration(row.duration_ms) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="说明" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="text-muted">
              {{ row.error_message || (row.result_data && row.result_data.message) || '—' }}
            </span>
          </template>
        </el-table-column>
      </el-table>

      <template #footer>
        <el-button @click="historyVisible = false">关闭</el-button>
      </template>
    </el-dialog>
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

.history-header {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
}

.history-name {
  font-weight: 600;
}
</style>