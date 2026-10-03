<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Grid, Key, Refresh } from '@element-plus/icons-vue'

import StatusBadge from '@/components/StatusBadge.vue'
import { api } from '@/api'
import { useVaultStore } from '@/stores/vault'
import { formatDuration, formatRelative, statusText } from '@/utils/format'

const router = useRouter()
const vaultStore = useVaultStore()

const stats = ref({ total: 0, valid: 0, invalid: 0, error: 0, unknown: 0, recent_activity: [] })
const loading = ref(true)
const refreshing = ref(false)
const batchStarting = ref(false)

const cards = computed(() => [
  { key: 'total', label: '总计', value: stats.value.total, color: 'var(--el-color-primary)' },
  { key: 'valid', label: '有效', value: stats.value.valid, color: 'var(--el-color-success)' },
  { key: 'invalid', label: '失效', value: stats.value.invalid, color: 'var(--el-color-danger)' },
  { key: 'error', label: '错误', value: stats.value.error, color: 'var(--el-color-warning)' },
  { key: 'unknown', label: '未验证', value: stats.value.unknown, color: 'var(--el-text-color-secondary)' },
])

const credentialNames = computed(() => {
  const map = {}
  vaultStore.credentials.forEach((item) => {
    map[item.id] = item.name
  })
  return map
})

const signature = computed(() =>
  vaultStore.credentials.map((item) => `${item.id}:${item.status}`).join(','),
)

function activityName(record) {
  return record.credential_name || credentialNames.value[record.credential_id] || `#${record.credential_id}`
}

async function fetchStats() {
  try {
    const result = await api.getDashboardStats()
    stats.value = {
      total: result?.total ?? 0,
      valid: result?.valid ?? 0,
      invalid: result?.invalid ?? 0,
      error: result?.error ?? 0,
      unknown: result?.unknown ?? 0,
      recent_activity: Array.isArray(result?.recent_activity) ? result.recent_activity : [],
    }
  } catch (err) {
    ElMessage.error(err.message || '加载统计数据失败')
  }
}

async function reload() {
  refreshing.value = true
  try {
    await Promise.all([vaultStore.fetchCredentials(), vaultStore.fetchGroups(), vaultStore.fetchPlugins()])
    await fetchStats()
  } catch (err) {
    ElMessage.error(err.message || '刷新失败')
  } finally {
    refreshing.value = false
    loading.value = false
  }
}

async function batchVerifyAll() {
  batchStarting.value = true
  try {
    await vaultStore.batchVerify('all')
  } catch (err) {
    ElMessage.error(err.message || '批量验证启动失败')
  } finally {
    batchStarting.value = false
  }
}

watch(signature, () => {
  fetchStats()
})

onMounted(reload)
</script>

<template>
  <div class="page">
    <div class="page-toolbar">
      <h2 class="section-title">凭据总览</h2>
      <div class="spacer" />
      <el-button :icon="Refresh" :loading="refreshing" @click="reload">刷新</el-button>
    </div>

    <div class="stat-grid" v-loading="loading">
      <el-card v-for="card in cards" :key="card.key" class="stat-card" shadow="hover">
        <div class="stat-value" :style="{ color: card.color }">{{ card.value }}</div>
        <div class="stat-label">{{ card.label }}</div>
      </el-card>
    </div>

    <el-card shadow="never" class="activity-card">
      <template #header>
        <div class="card-header">
          <span>最近验证活动</span>
          <el-button text type="primary" @click="router.push('/credentials')">查看全部凭据</el-button>
        </div>
      </template>

      <el-table :data="stats.recent_activity" size="default" empty-text="暂无验证记录" style="width: 100%">
        <el-table-column label="凭据" min-width="200">
          <template #default="{ row }">
            <span class="activity-name">{{ activityName(row) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="结果" width="110">
          <template #default="{ row }">
            <StatusBadge :status="row.status" />
          </template>
        </el-table-column>
        <el-table-column label="说明" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="text-muted">{{ row.error_message || (row.result_data && row.result_data.message) || statusText(row.status) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="耗时" width="100">
          <template #default="{ row }">
            <span class="text-muted">{{ formatDuration(row.duration_ms) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="时间" width="130">
          <template #default="{ row }">
            <span class="text-muted">{{ formatRelative(row.verified_at) }}</span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card shadow="never">
      <template #header><span>快捷操作</span></template>
      <div class="quick-actions">
        <el-button type="primary" :icon="Key" @click="router.push({ path: '/credentials', query: { new: '1' } })">
          添加凭据
        </el-button>
        <el-button :icon="Refresh" :loading="batchStarting" @click="batchVerifyAll">批量验证全部</el-button>
        <el-button :icon="Grid" @click="router.push('/plugins')">管理插件</el-button>
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.section-title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
}

.stat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 14px;
}

.stat-card {
  text-align: center;
}

.stat-value {
  font-size: 30px;
  font-weight: 700;
  line-height: 1.2;
}

.stat-label {
  margin-top: 6px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.activity-name {
  font-weight: 500;
}

.quick-actions {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
}
</style>