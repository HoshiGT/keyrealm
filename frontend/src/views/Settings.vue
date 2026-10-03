<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Delete, Refresh } from '@element-plus/icons-vue'

import { api } from '@/api'
import { useAppStore } from '@/stores/app'
import { formatDateTime } from '@/utils/format'

const appStore = useAppStore()

// ── 修改主密码 ────────────────────────────────────────────
const pwdForm = reactive({ oldPassword: '', newPassword: '', confirmPassword: '' })
const pwdLoading = ref(false)

async function changePassword() {
  if (!pwdForm.oldPassword || !pwdForm.newPassword) {
    ElMessage.warning('请填写完整')
    return
  }
  if (pwdForm.newPassword.length < 6) {
    ElMessage.warning('新密码至少 6 位')
    return
  }
  if (pwdForm.newPassword !== pwdForm.confirmPassword) {
    ElMessage.warning('两次输入的新密码不一致')
    return
  }
  pwdLoading.value = true
  try {
    const result = await api.changeMasterPassword(pwdForm.oldPassword, pwdForm.newPassword)
    if (result.ok === false) {
      ElMessage.error(result.message || '修改失败')
    } else {
      ElMessage.success('主密码修改成功，所有凭据已重新加密')
      pwdForm.oldPassword = ''
      pwdForm.newPassword = ''
      pwdForm.confirmPassword = ''
    }
  } catch (err) {
    ElMessage.error(err.message || '修改失败')
  } finally {
    pwdLoading.value = false
  }
}

// ── 定时计划 ──────────────────────────────────────────────
const schedules = ref([])
const schedLoading = ref(false)
const schedDialog = ref(false)
const schedForm = reactive({
  name: '',
  trigger_type: 'interval',
  interval_hours: 24,
  cron_expression: '0 2 * * *',
  target_scope: 'all',
  target_id: null,
})

async function fetchSchedules() {
  schedLoading.value = true
  try {
    const items = await api.listSchedules()
    schedules.value = Array.isArray(items) ? items : []
  } catch (err) {
    ElMessage.error(err.message || '加载计划失败')
  } finally {
    schedLoading.value = false
  }
}

onMounted(fetchSchedules)

function openSchedDialog() {
  schedForm.name = ''
  schedForm.trigger_type = 'interval'
  schedForm.interval_hours = 24
  schedForm.cron_expression = '0 2 * * *'
  schedForm.target_scope = 'all'
  schedForm.target_id = null
  schedDialog.value = true
}

async function saveSchedule() {
  if (!schedForm.name.trim()) {
    ElMessage.warning('请输入计划名称')
    return
  }
  const trigger_args =
    schedForm.trigger_type === 'interval'
      ? { hours: Number(schedForm.interval_hours) || 24 }
      : { expression: schedForm.cron_expression }

  try {
    await api.createSchedule({
      name: schedForm.name.trim(),
      trigger_type: schedForm.trigger_type,
      trigger_args,
      target_scope: schedForm.target_scope,
      target_id: schedForm.target_id,
    })
    ElMessage.success('定时计划已创建')
    schedDialog.value = false
    await fetchSchedules()
  } catch (err) {
    ElMessage.error(err.message || '创建失败')
  }
}

async function toggleSchedule(schedule) {
  try {
    await api.updateSchedule(schedule.id, { enabled: !schedule.enabled })
    await fetchSchedules()
  } catch (err) {
    ElMessage.error(err.message || '操作失败')
  }
}

async function deleteSchedule(schedule) {
  try {
    await ElMessageBox.confirm(`确认删除定时计划「${schedule.name}」？`, '删除计划', { type: 'warning' })
    await api.deleteSchedule(schedule.id)
    ElMessage.success('已删除')
    await fetchSchedules()
  } catch (err) {
    if (err !== 'cancel') ElMessage.error(err.message || '删除失败')
  }
}

function triggerLabel(schedule) {
  const args = schedule.trigger_args || {}
  if (schedule.trigger_type === 'interval') return `每 ${args.hours || '?'} 小时`
  return `Cron: ${args.expression || '?'}`
}

function scopeLabel(schedule) {
  if (schedule.target_scope === 'all') return '全部凭据'
  if (schedule.target_scope === 'group') return `分组 #${schedule.target_id}`
  return `类型: ${schedule.target_scope}`
}

// ── 关于 ──────────────────────────────────────────────────
const version = '1.0.0'
</script>

<template>
  <div class="page">
    <!-- 修改主密码 -->
    <el-card shadow="never">
      <template #header><span>修改主密码</span></template>
      <el-alert type="warning" :closable="false" class="mb-16">
        修改主密码后，所有已存储的凭据将使用新密码重新加密。请牢记新密码，忘记密码将无法恢复数据。
      </el-alert>
      <el-form label-position="top" class="pwd-form">
        <el-form-item label="当前密码">
          <el-input v-model="pwdForm.oldPassword" type="password" show-password placeholder="请输入当前主密码" />
        </el-form-item>
        <el-form-item label="新密码">
          <el-input v-model="pwdForm.newPassword" type="password" show-password placeholder="至少 6 位" />
        </el-form-item>
        <el-form-item label="确认新密码">
          <el-input v-model="pwdForm.confirmPassword" type="password" show-password placeholder="再次输入新密码" />
        </el-form-item>
        <el-button type="primary" :loading="pwdLoading" @click="changePassword">修改密码</el-button>
      </el-form>
    </el-card>

    <!-- 定时验证计划 -->
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>定时验证计划</span>
          <div class="header-btns">
            <el-button size="small" :icon="Refresh" @click="fetchSchedules">刷新</el-button>
            <el-button size="small" type="primary" :icon="Plus" @click="openSchedDialog">新建计划</el-button>
          </div>
        </div>
      </template>

      <el-empty v-if="!schedules.length && !schedLoading" description="暂无定时计划" />

      <el-table v-else :data="schedules" size="small" stripe v-loading="schedLoading">
        <el-table-column prop="name" label="名称" min-width="140" />
        <el-table-column label="触发规则" min-width="140">
          <template #default="{ row }">{{ triggerLabel(row) }}</template>
        </el-table-column>
        <el-table-column label="验证范围" min-width="120">
          <template #default="{ row }">{{ scopeLabel(row) }}</template>
        </el-table-column>
        <el-table-column label="上次执行" width="150">
          <template #default="{ row }">{{ formatDateTime(row.last_run_at, '从未') }}</template>
        </el-table-column>
        <el-table-column label="下次执行" width="150">
          <template #default="{ row }">{{ formatDateTime(row.next_run_at, '—') }}</template>
        </el-table-column>
        <el-table-column label="启用" width="80" align="center">
          <template #default="{ row }">
            <el-switch :model-value="!!row.enabled" size="small" @change="toggleSchedule(row)" />
          </template>
        </el-table-column>
        <el-table-column label="操作" width="80" fixed="right">
          <template #default="{ row }">
            <el-button text size="small" type="danger" :icon="Delete" @click="deleteSchedule(row)" />
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 关于 -->
    <el-card shadow="never">
      <template #header><span>关于</span></template>
      <el-descriptions :column="2" border size="small">
        <el-descriptions-item label="应用名称">Credential Vault</el-descriptions-item>
        <el-descriptions-item label="版本">{{ version }}</el-descriptions-item>
        <el-descriptions-item label="存储方式">AES-256-GCM 加密，SQLite 本地数据库</el-descriptions-item>
        <el-descriptions-item label="MCP 协议">SSE 传输，Bearer Token 鉴权</el-descriptions-item>
        <el-descriptions-item label="数据库位置">./data/credentials.db（项目目录内）</el-descriptions-item>
        <el-descriptions-item label="插件目录">./data/installed_plugins/</el-descriptions-item>
      </el-descriptions>
    </el-card>

    <!-- 新建计划对话框 -->
    <el-dialog v-model="schedDialog" title="新建定时验证计划" width="520px">
      <el-form label-position="top">
        <el-form-item label="计划名称">
          <el-input v-model="schedForm.name" placeholder="例如：每日凌晨全量验证" clearable />
        </el-form-item>

        <el-form-item label="触发方式">
          <el-radio-group v-model="schedForm.trigger_type">
            <el-radio value="interval">固定间隔</el-radio>
            <el-radio value="cron">Cron 表达式</el-radio>
          </el-radio-group>
        </el-form-item>

        <el-form-item v-if="schedForm.trigger_type === 'interval'" label="间隔（小时）">
          <el-input-number v-model="schedForm.interval_hours" :min="1" :max="168" controls-position="right" />
        </el-form-item>

        <el-form-item v-else label="Cron 表达式">
          <el-input v-model="schedForm.cron_expression" placeholder="0 2 * * *（每天凌晨 2 点）" />
          <div class="field-hint">格式：分 时 日 月 周（标准 5 段 Cron）</div>
        </el-form-item>

        <el-form-item label="验证范围">
          <el-select v-model="schedForm.target_scope" class="full-width">
            <el-option label="全部凭据" value="all" />
            <el-option label="按分组" value="group" />
          </el-select>
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="schedDialog = false">取消</el-button>
        <el-button type="primary" @click="saveSchedule">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.header-btns {
  display: flex;
  gap: 8px;
}

.pwd-form {
  max-width: 400px;
}

.mb-16 {
  margin-bottom: 16px;
}

.full-width {
  width: 100%;
}

.field-hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-top: 4px;
}
</style>
