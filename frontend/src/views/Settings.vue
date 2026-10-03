<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete, Edit, Key, Lock, Plus, Refresh, Timer, VideoPlay } from '@element-plus/icons-vue'

import { api } from '@/api'
import { useAppStore } from '@/stores/app'
import { useVaultStore } from '@/stores/vault'
import { formatDateTime, formatRelative } from '@/utils/format'

const appStore = useAppStore()
const vaultStore = useVaultStore()

const APP_VERSION = '0.1.0'

const WEEKDAYS = [
  { label: '周一', value: 'mon' },
  { label: '周二', value: 'tue' },
  { label: '周三', value: 'wed' },
  { label: '周四', value: 'thu' },
  { label: '周五', value: 'fri' },
  { label: '周六', value: 'sat' },
  { label: '周日', value: 'sun' },
]

const INTERVAL_UNITS = [
  { label: '分钟', value: 'minutes' },
  { label: '小时', value: 'hours' },
  { label: '天', value: 'days' },
]

const WEEKDAY_LABEL = WEEKDAYS.reduce((acc, item) => {
  acc[item.value] = item.label
  return acc
}, {})

/* ── 主密码 ─────────────────────────────────────────── */

const passwordDialog = ref(false)
const passwordSubmitting = ref(false)
const passwordFormRef = ref(null)
const passwordForm = reactive({ old_password: '', new_password: '', confirm_password: '' })

const passwordStrength = computed(() => {
  const value = passwordForm.new_password
  if (!value) return { level: 0, label: '' }
  let score = 0
  if (value.length >= 8) score += 1
  if (value.length >= 12) score += 1
  if (/[a-z]/.test(value) && /[A-Z]/.test(value)) score += 1
  if (/\d/.test(value)) score += 1
  if (/[^A-Za-z0-9]/.test(value)) score += 1
  const level = Math.min(score, 4)
  const labels = ['很弱', '较弱', '一般', '较强', '很强']
  return { level, label: labels[level] }
})

const passwordRules = {
  old_password: [{ required: true, message: '请输入当前主密码', trigger: 'blur' }],
  new_password: [
    { required: true, message: '请输入新主密码', trigger: 'blur' },
    { min: 6, message: '新密码至少 6 位', trigger: 'blur' },
  ],
  confirm_password: [
    { required: true, message: '请再次输入新主密码', trigger: 'blur' },
    {
      validator: (rule, value, callback) => {
        if (value !== passwordForm.new_password) callback(new Error('两次输入的新密码不一致'))
        else callback()
      },
      trigger: 'blur',
    },
  ],
}

function openPasswordDialog() {
  passwordForm.old_password = ''
  passwordForm.new_password = ''
  passwordForm.confirm_password = ''
  passwordDialog.value = true
}

async function submitPassword() {
  if (!passwordFormRef.value) return
  try {
    await passwordFormRef.value.validate()
  } catch {
    return
  }

  passwordSubmitting.value = true
  try {
    await api.changeMasterPassword(passwordForm.old_password, passwordForm.new_password)
    ElMessage.success('主密码已更新，所有凭据已使用新密钥重新加密')
    passwordDialog.value = false
  } catch (err) {
    ElMessage.error(err.message || '修改主密码失败')
  } finally {
    passwordSubmitting.value = false
  }
}

async function lockNow() {
  await appStore.lock()
  ElMessage.success('保险库已锁定')
}

/* ── 定时计划 ───────────────────────────────────────── */

const schedules = ref([])
const schedulesLoading = ref(false)
const scheduleDialog = ref(false)
const scheduleSubmitting = ref(false)
const scheduleFormRef = ref(null)
const editingId = ref(null)
const togglingId = ref(null)
const runningId = ref(null)

const scheduleForm = reactive({
  name: '',
  trigger_type: 'interval',
  intervalValue: 6,
  intervalUnit: 'hours',
  cronMode: 'daily',
  cronTime: '03:00',
  cronWeekday: 'mon',
  advanced: false,
  triggerArgsText: '',
  target_scope: 'all',
  target_id: null,
})

const isEdit = computed(() => editingId.value !== null)

const scheduleRules = {
  name: [{ required: true, message: '请输入计划名称', trigger: 'blur' }],
}

const groupOptions = computed(() => vaultStore.groups.map((item) => ({ label: item.name, value: item.id })))
const typeOptions = computed(() => vaultStore.typeOptions)

const triggerArgsPreview = computed(() => {
  if (scheduleForm.advanced) return scheduleForm.triggerArgsText.trim() || '{}'
  return JSON.stringify(previewTriggerArgs(), null, 2)
})

function previewTriggerArgs() {
  if (scheduleForm.trigger_type === 'interval') {
    return { [scheduleForm.intervalUnit]: Number(scheduleForm.intervalValue) || 1 }
  }
  const [hour, minute] = String(scheduleForm.cronTime || '03:00').split(':').map((part) => Number(part) || 0)
  if (scheduleForm.cronMode === 'weekly') {
    return { day_of_week: scheduleForm.cronWeekday, hour, minute }
  }
  return { hour, minute }
}

function buildTriggerArgs() {
  if (!scheduleForm.advanced) return previewTriggerArgs()
  const text = scheduleForm.triggerArgsText.trim()
  if (!text) throw new Error('高级模式下触发器参数不能为空')
  let parsed
  try {
    parsed = JSON.parse(text)
  } catch {
    throw new Error('触发器参数不是合法的 JSON，请检查格式')
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('触发器参数必须是一个 JSON 对象')
  }
  return parsed
}

function describeTrigger(schedule) {
  const args = schedule.trigger_args || {}
  if (schedule.trigger_type === 'interval') {
    for (const unit of INTERVAL_UNITS) {
      if (args[unit.value] !== undefined) {
        const suffix = { minutes: '分钟', hours: '小时', days: '天' }[unit.value]
        return `每 ${args[unit.value]} ${suffix}`
      }
    }
    return '按间隔执行'
  }
  if (schedule.trigger_type === 'cron') {
    const hour = String(args.hour ?? 0).padStart(2, '0')
    const minute = String(args.minute ?? 0).padStart(2, '0')
    if (args.day_of_week !== undefined) {
      const label = WEEKDAY_LABEL[String(args.day_of_week)] || String(args.day_of_week)
      return `每${label} ${hour}:${minute}`
    }
    if (args.day !== undefined) return `每月 ${args.day} 日 ${hour}:${minute}`
    return `每天 ${hour}:${minute}`
  }
  return schedule.trigger_type
}

function describeTarget(schedule) {
  if (schedule.target_scope === 'group') {
    const group = vaultStore.groups.find((item) => item.id === schedule.target_id)
    return group ? `分组：${group.name}` : `分组 #${schedule.target_id}`
  }
  if (schedule.target_scope === 'type') {
    const found = vaultStore.plugins.find((item) => item.credential_type === schedule.target_id)
    return found ? `类型：${found.name}` : `类型：${schedule.target_id}`
  }
  return '全部凭据'
}

function resetScheduleForm() {
  scheduleForm.name = ''
  scheduleForm.trigger_type = 'interval'
  scheduleForm.intervalValue = 6
  scheduleForm.intervalUnit = 'hours'
  scheduleForm.cronMode = 'daily'
  scheduleForm.cronTime = '03:00'
  scheduleForm.cronWeekday = 'mon'
  scheduleForm.advanced = false
  scheduleForm.triggerArgsText = ''
  scheduleForm.target_scope = 'all'
  scheduleForm.target_id = null
}

function openCreate() {
  editingId.value = null
  resetScheduleForm()
  scheduleDialog.value = true
}

function openEdit(row) {
  editingId.value = row.id
  resetScheduleForm()
  scheduleForm.name = row.name
  scheduleForm.trigger_type = row.trigger_type
  scheduleForm.target_scope = row.target_scope || 'all'
  scheduleForm.target_id = row.target_id ?? null

  const args = row.trigger_args || {}
  if (row.trigger_type === 'interval') {
    const unit = INTERVAL_UNITS.find((item) => args[item.value] !== undefined)
    if (unit) {
      scheduleForm.intervalUnit = unit.value
      scheduleForm.intervalValue = args[unit.value]
    }
    scheduleForm.advanced = !unit
  } else if (row.trigger_type === 'cron') {
    const hour = String(args.hour ?? 0).padStart(2, '0')
    const minute = String(args.minute ?? 0).padStart(2, '0')
    scheduleForm.cronTime = `${hour}:${minute}`
    if (args.day_of_week !== undefined) {
      scheduleForm.cronMode = 'weekly'
      scheduleForm.cronWeekday = String(args.day_of_week)
    } else {
      scheduleForm.cronMode = 'daily'
    }
    scheduleForm.advanced = args.day !== undefined
  } else {
    scheduleForm.advanced = true
  }

  scheduleForm.triggerArgsText = JSON.stringify(args, null, 2)
  scheduleDialog.value = true
}

async function loadSchedules() {
  schedulesLoading.value = true
  try {
    const result = await api.listSchedules()
    schedules.value = Array.isArray(result) ? result : []
  } catch (err) {
    ElMessage.error(err.message || '加载定时计划失败')
  } finally {
    schedulesLoading.value = false
  }
}

async function submitSchedule() {
  if (!scheduleFormRef.value) return
  try {
    await scheduleFormRef.value.validate()
  } catch {
    return
  }

  let triggerArgs
  try {
    triggerArgs = buildTriggerArgs()
  } catch (err) {
    ElMessage.error(err.message)
    return
  }

  const payload = {
    name: scheduleForm.name.trim(),
    trigger_type: scheduleForm.trigger_type,
    trigger_args: triggerArgs,
    target_scope: scheduleForm.target_scope,
    target_id: scheduleForm.target_scope === 'all' ? null : scheduleForm.target_id,
  }

  if (payload.target_scope !== 'all' && payload.target_id === null) {
    ElMessage.warning('请选择验证范围对应的目标')
    return
  }

  scheduleSubmitting.value = true
  try {
    if (isEdit.value) {
      await api.updateSchedule(editingId.value, payload)
      ElMessage.success('计划已更新')
    } else {
      await api.createSchedule(payload)
      ElMessage.success('计划已创建')
    }
    scheduleDialog.value = false
    await loadSchedules()
  } catch (err) {
    ElMessage.error(err.message || '保存计划失败')
  } finally {
    scheduleSubmitting.value = false
  }
}

async function toggleSchedule(row, enabled) {
  togglingId.value = row.id
  try {
    await api.updateSchedule(row.id, { enabled })
    row.enabled = enabled
    ElMessage.success(`计划「${row.name}」已${enabled ? '启用' : '停用'}`)
    await loadSchedules()
  } catch (err) {
    ElMessage.error(err.message || '操作失败，请确认后端 update_schedule 支持字典参数')
  } finally {
    togglingId.value = null
  }
}

async function runScheduleNow(row) {
  runningId.value = row.id
  try {
    await vaultStore.batchVerify(
      row.target_scope || 'all',
      row.target_scope === 'group' ? row.target_id : null,
      row.target_scope === 'type' ? row.target_id : null,
    )
    ElMessage.success(`已按计划「${row.name}」的范围启动验证`)
  } catch (err) {
    ElMessage.error(err.message || '启动验证失败')
  } finally {
    runningId.value = null
  }
}

async function removeSchedule(row) {
  try {
    await ElMessageBox.confirm(`确定删除定时计划「${row.name}」吗？`, '删除计划', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      confirmButtonClass: 'el-button--danger',
    })
  } catch {
    return
  }

  try {
    await api.deleteSchedule(row.id)
    ElMessage.success('计划已删除')
    await loadSchedules()
  } catch (err) {
    ElMessage.error(err.message || '删除失败')
  }
}

/* ── 日志 ───────────────────────────────────────────── */

const LOG_LIMITS = [
  { label: '最近 100 条', value: 100 },
  { label: '最近 200 条', value: 200 },
  { label: '最近 500 条', value: 500 },
  { label: '最近 1000 条', value: 1000 },
]

const logConfig = ref({ level: 'INFO', levels: ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'], log_file: '' })
const selectedLevel = ref('INFO')
const levelSaving = ref(false)

const logs = ref([])
const logsLoading = ref(false)
const logFilterLevel = ref('')
const logLimit = ref(200)

const logFilterOptions = computed(() => [
  { label: '全部级别', value: '' },
  ...logConfig.value.levels.map((level) => ({ label: `${level} 及以上`, value: level })),
])

const logCount = computed(() => logs.value.length)

function logLineClass(entry) {
  const match = /\[(DEBUG|INFO|WARNING|ERROR|CRITICAL)\]/.exec(entry)
  return match ? `lvl-${match[1].toLowerCase()}` : ''
}

async function loadLogConfig() {
  try {
    const config = await api.getLogConfig()
    logConfig.value = {
      level: config.level || 'INFO',
      levels: Array.isArray(config.levels) && config.levels.length ? config.levels : logConfig.value.levels,
      log_file: config.log_file || '',
    }
    selectedLevel.value = logConfig.value.level
  } catch (err) {
    ElMessage.error(err.message || '加载日志配置失败')
  }
}

async function applyLogLevel() {
  levelSaving.value = true
  try {
    await api.setLogLevel(selectedLevel.value)
    logConfig.value.level = selectedLevel.value
    ElMessage.success(`日志级别已设置为 ${selectedLevel.value}`)
    await loadLogs()
  } catch (err) {
    ElMessage.error(err.message || '设置日志级别失败')
  } finally {
    levelSaving.value = false
  }
}

async function loadLogs() {
  logsLoading.value = true
  try {
    const result = await api.getLogs(logLimit.value, logFilterLevel.value)
    logs.value = Array.isArray(result.entries) ? result.entries : []
    if (result.log_file) logConfig.value.log_file = result.log_file
  } catch (err) {
    ElMessage.error(err.message || '加载日志失败')
  } finally {
    logsLoading.value = false
  }
}

async function clearLogs() {
  try {
    await ElMessageBox.confirm('确定清空全部日志吗？此操作不可撤销。', '清空日志', {
      type: 'warning',
      confirmButtonText: '清空',
      cancelButtonText: '取消',
      confirmButtonClass: 'el-button--danger',
    })
  } catch {
    return
  }
  try {
    await api.clearLogs()
    ElMessage.success('日志已清空')
    await loadLogs()
  } catch (err) {
    ElMessage.error(err.message || '清空日志失败')
  }
}

/* ── 关于 ───────────────────────────────────────────── */
const isDesktop = computed(() => typeof window !== 'undefined' && !!window.pywebview)
const runtimeLabel = computed(() => (isDesktop.value ? '桌面端（pywebview）' : '浏览器预览（开发模式）'))
const mcpLabel = computed(() => (appStore.tray.mcp_running ? `运行中 · 端口 ${appStore.tray.mcp_port}` : '已停止'))

onMounted(async () => {
  await Promise.all([
    loadSchedules(),
    loadLogConfig(),
    loadLogs(),
    vaultStore.fetchGroups().catch(() => {}),
    vaultStore.fetchPlugins().catch(() => {}),
    appStore.refreshTray().catch(() => {}),
  ])
})
</script>

<template>
  <div class="page">
    <div class="page-toolbar">
      <h2 class="section-title">系统设置</h2>
      <div class="spacer" />
      <el-button :icon="Refresh" :loading="schedulesLoading" @click="loadSchedules">刷新</el-button>
    </div>

    <el-card shadow="never">
      <template #header><span>安全</span></template>

      <div class="setting-row">
        <div class="setting-info">
          <div class="setting-label">主密码</div>
          <div class="setting-desc">
            主密码用于派生加密密钥。修改后将使用新密钥重新加密库中全部凭据，旧密码立即失效。
          </div>
        </div>
        <el-button :icon="Key" @click="openPasswordDialog">修改主密码</el-button>
      </div>

      <el-divider />

      <div class="setting-row">
        <div class="setting-info">
          <div class="setting-label">锁定保险库</div>
          <div class="setting-desc">立即清除内存中的解密密钥与明文数据，需重新输入主密码才能访问。</div>
        </div>
        <el-button :icon="Lock" @click="lockNow">立即锁定</el-button>
      </div>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>定时验证计划</span>
          <el-button type="primary" size="small" :icon="Plus" @click="openCreate">新建计划</el-button>
        </div>
      </template>

      <el-table v-loading="schedulesLoading" :data="schedules" empty-text="暂无定时计划" style="width: 100%">
        <el-table-column label="名称" min-width="160">
          <template #default="{ row }">
            <span class="plan-name">{{ row.name }}</span>
          </template>
        </el-table-column>

        <el-table-column label="触发规则" width="180">
          <template #default="{ row }">
            <el-tag size="small" effect="plain" :icon="Timer">{{ describeTrigger(row) }}</el-tag>
          </template>
        </el-table-column>

        <el-table-column label="验证范围" min-width="150">
          <template #default="{ row }">
            <span class="text-muted">{{ describeTarget(row) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="启用" width="90">
          <template #default="{ row }">
            <el-switch
              :model-value="row.enabled"
              size="small"
              :loading="togglingId === row.id"
              @change="(value) => toggleSchedule(row, value)"
            />
          </template>
        </el-table-column>

        <el-table-column label="上次运行" width="130">
          <template #default="{ row }">
            <span class="text-muted">{{ formatRelative(row.last_run_at, '从未运行') }}</span>
          </template>
        </el-table-column>

        <el-table-column label="下次运行" width="170">
          <template #default="{ row }">
            <span v-if="row.next_run_at" class="text-muted">{{ formatDateTime(row.next_run_at) }}</span>
            <span v-else class="text-muted">—</span>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{ row }">
            <el-button
              size="small"
              :icon="VideoPlay"
              :loading="runningId === row.id"
              @click="runScheduleNow(row)"
            >
              立即运行
            </el-button>
            <el-button size="small" :icon="Edit" @click="openEdit(row)">编辑</el-button>
            <el-button size="small" type="danger" plain :icon="Delete" @click="removeSchedule(row)" />
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card shadow="never">
      <template #header><span>日志</span></template>

      <div class="setting-row">
        <div class="setting-info">
          <div class="setting-label">日志级别</div>
          <div class="setting-desc">
            记录到日志文件的最低级别。级别越低越详细（DEBUG 最详细，CRITICAL 最精简）。
            设置立即生效并持久化，下次启动自动恢复。
          </div>
        </div>
        <div class="inline-row">
          <el-select v-model="selectedLevel" class="level-select">
            <el-option v-for="level in logConfig.levels" :key="level" :label="level" :value="level" />
          </el-select>
          <el-button
            type="primary"
            :loading="levelSaving"
            :disabled="selectedLevel === logConfig.level"
            @click="applyLogLevel"
          >
            应用
          </el-button>
        </div>
      </div>

      <el-divider />

      <div class="log-toolbar">
        <el-select v-model="logFilterLevel" class="level-select" @change="loadLogs">
          <el-option
            v-for="option in logFilterOptions"
            :key="option.value"
            :label="option.label"
            :value="option.value"
          />
        </el-select>
        <el-select v-model="logLimit" class="limit-select" @change="loadLogs">
          <el-option
            v-for="option in LOG_LIMITS"
            :key="option.value"
            :label="option.label"
            :value="option.value"
          />
        </el-select>
        <div class="spacer" />
        <span class="log-count">共 {{ logCount }} 条</span>
        <el-button :icon="Refresh" :loading="logsLoading" @click="loadLogs">刷新</el-button>
        <el-button type="danger" plain :icon="Delete" @click="clearLogs">清空</el-button>
      </div>

      <div v-loading="logsLoading" class="log-viewer">
        <pre v-if="logs.length" class="log-content"><span
          v-for="(entry, index) in logs"
          :key="index"
          class="log-line"
          :class="logLineClass(entry)"
        >{{ entry }}</span></pre>
        <el-empty v-else description="暂无日志" :image-size="60" />
      </div>

      <div v-if="logConfig.log_file" class="log-path text-muted">日志文件：{{ logConfig.log_file }}</div>
    </el-card>

    <el-card shadow="never">
      <template #header><span>关于</span></template>

      <el-descriptions :column="2" border size="small">
        <el-descriptions-item label="应用名称">Credential Vault</el-descriptions-item>
        <el-descriptions-item label="版本">v{{ APP_VERSION }}</el-descriptions-item>
        <el-descriptions-item label="运行环境">{{ runtimeLabel }}</el-descriptions-item>
        <el-descriptions-item label="MCP 服务">{{ mcpLabel }}</el-descriptions-item>
        <el-descriptions-item label="前端技术栈">Vue 3 · Element Plus · Pinia · Vite</el-descriptions-item>
        <el-descriptions-item label="当前状态">{{ appStore.unlocked ? '已解锁' : '已锁定' }}</el-descriptions-item>
      </el-descriptions>

      <el-alert type="info" :closable="false" show-icon class="about-alert">
        <template #title>数据存储</template>
        <template #default>
          所有凭据仅保存在本机数据库中，使用由主密码派生的密钥加密。除验证时向对应服务商发起请求外，
          任何凭据明文都不会离开本机；通过 MCP 提供给 AI Agent 的明文访问受工具权限与 Token 范围双重控制。
        </template>
      </el-alert>
    </el-card>

    <!-- 修改主密码 -->
    <el-dialog v-model="passwordDialog" title="修改主密码" width="480px" :close-on-click-modal="false">
      <el-alert type="warning" :closable="false" show-icon class="mb-12">
        修改主密码需要重新加密全部凭据，请勿在批量验证进行中执行。
      </el-alert>

      <el-form ref="passwordFormRef" :model="passwordForm" :rules="passwordRules" label-position="top">
        <el-form-item label="当前主密码" prop="old_password">
          <el-input v-model="passwordForm.old_password" type="password" show-password placeholder="请输入当前主密码" />
        </el-form-item>

        <el-form-item label="新主密码" prop="new_password">
          <el-input v-model="passwordForm.new_password" type="password" show-password placeholder="至少 6 位" />
          <div v-if="passwordForm.new_password" class="strength">
            <div class="strength-bars">
              <span
                v-for="index in 4"
                :key="index"
                class="strength-bar"
                :class="index <= passwordStrength.level ? `level-${passwordStrength.level}` : ''"
              />
            </div>
            <span class="strength-label">{{ passwordStrength.label }}</span>
          </div>
        </el-form-item>

        <el-form-item label="确认新主密码" prop="confirm_password">
          <el-input
            v-model="passwordForm.confirm_password"
            type="password"
            show-password
            placeholder="请再次输入新主密码"
            @keyup.enter="submitPassword"
          />
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="passwordDialog = false">取消</el-button>
        <el-button type="primary" :loading="passwordSubmitting" @click="submitPassword">确认修改</el-button>
      </template>
    </el-dialog>

    <!-- 新建 / 编辑计划 -->
    <el-dialog
      v-model="scheduleDialog"
      :title="isEdit ? '编辑定时计划' : '新建定时计划'"
      width="580px"
      :close-on-click-modal="false"
    >
      <el-form ref="scheduleFormRef" :model="scheduleForm" :rules="scheduleRules" label-position="top">
        <el-form-item label="计划名称" prop="name">
          <el-input v-model="scheduleForm.name" placeholder="例如：每晚检查所有 API Key" clearable maxlength="40" />
        </el-form-item>

        <el-form-item label="触发方式">
          <el-radio-group v-model="scheduleForm.trigger_type" :disabled="scheduleForm.advanced">
            <el-radio-button value="interval">固定间隔</el-radio-button>
            <el-radio-button value="cron">日历时间</el-radio-button>
          </el-radio-group>
        </el-form-item>

        <template v-if="!scheduleForm.advanced">
          <el-form-item v-if="scheduleForm.trigger_type === 'interval'" label="间隔">
            <div class="inline-row">
              <span class="inline-text">每</span>
              <el-input-number v-model="scheduleForm.intervalValue" :min="1" :max="1440" controls-position="right" />
              <el-select v-model="scheduleForm.intervalUnit" class="unit-select">
                <el-option v-for="unit in INTERVAL_UNITS" :key="unit.value" :label="unit.label" :value="unit.value" />
              </el-select>
            </div>
          </el-form-item>

          <template v-else>
            <el-form-item label="周期">
              <el-radio-group v-model="scheduleForm.cronMode">
                <el-radio value="daily">每天</el-radio>
                <el-radio value="weekly">每周</el-radio>
              </el-radio-group>
            </el-form-item>

            <el-form-item :label="scheduleForm.cronMode === 'weekly' ? '星期与时间' : '执行时间'">
              <div class="inline-row">
                <el-select v-if="scheduleForm.cronMode === 'weekly'" v-model="scheduleForm.cronWeekday" class="unit-select">
                  <el-option v-for="day in WEEKDAYS" :key="day.value" :label="day.label" :value="day.value" />
                </el-select>
                <el-time-picker
                  v-model="scheduleForm.cronTime"
                  value-format="HH:mm"
                  format="HH:mm"
                  placeholder="选择时间"
                  class="time-picker"
                />
              </div>
            </el-form-item>
          </template>
        </template>

        <el-form-item label="验证范围">
          <el-select v-model="scheduleForm.target_scope" class="full-width">
            <el-option label="全部凭据" value="all" />
            <el-option label="指定分组" value="group" :disabled="!groupOptions.length" />
            <el-option label="指定类型" value="type" :disabled="!typeOptions.length" />
          </el-select>
        </el-form-item>

        <el-form-item v-if="scheduleForm.target_scope === 'group'" label="选择分组">
          <el-select v-model="scheduleForm.target_id" placeholder="请选择分组" class="full-width">
            <el-option v-for="option in groupOptions" :key="option.value" :label="option.label" :value="option.value" />
          </el-select>
        </el-form-item>

        <el-form-item v-else-if="scheduleForm.target_scope === 'type'" label="选择类型">
          <el-select v-model="scheduleForm.target_id" placeholder="请选择凭据类型" class="full-width">
            <el-option v-for="option in typeOptions" :key="option.value" :label="option.label" :value="option.value" />
          </el-select>
        </el-form-item>

        <el-form-item>
          <el-checkbox v-model="scheduleForm.advanced">高级：直接编辑触发器参数（APScheduler JSON）</el-checkbox>
        </el-form-item>

        <el-form-item v-if="scheduleForm.advanced" label="触发器参数">
          <el-input v-model="scheduleForm.triggerArgsText" type="textarea" :rows="4" spellcheck="false" />
        </el-form-item>

        <el-form-item label="参数预览">
          <pre class="preview">{{ triggerArgsPreview }}</pre>
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="scheduleDialog = false">取消</el-button>
        <el-button type="primary" :loading="scheduleSubmitting" @click="submitSchedule">保存</el-button>
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

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.setting-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  flex-wrap: wrap;
}

.setting-info {
  min-width: 0;
}

.setting-label {
  font-weight: 600;
  margin-bottom: 4px;
}

.setting-desc {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  line-height: 1.6;
  max-width: 640px;
}

.plan-name {
  font-weight: 600;
}

.inline-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.inline-text {
  color: var(--el-text-color-regular);
}

.unit-select {
  width: 110px;
}

.time-picker {
  width: 160px;
}

.full-width {
  width: 100%;
}

.preview {
  margin: 0;
  padding: 10px 12px;
  width: 100%;
  background: var(--el-fill-color-dark);
  border-radius: 6px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
}

.mb-12 {
  margin-bottom: 12px;
}

.about-alert {
  margin-top: 14px;
}

.strength {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 8px;
  width: 100%;
}

.strength-bars {
  display: flex;
  gap: 4px;
  flex: 1;
}

.strength-bar {
  height: 4px;
  flex: 1;
  border-radius: 2px;
  background: var(--el-fill-color-dark);
  transition: background 0.2s;
}

.strength-bar.level-1 {
  background: var(--el-color-danger);
}

.strength-bar.level-2 {
  background: var(--el-color-warning);
}

.strength-bar.level-3 {
  background: var(--el-color-primary);
}

.strength-bar.level-4 {
  background: var(--el-color-success);
}

.strength-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  width: 32px;
  text-align: right;
}

.level-select {
  width: 150px;
}

.limit-select {
  width: 150px;
}

.log-toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.log-count {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.log-viewer {
  max-height: 360px;
  overflow: auto;
  background: var(--el-fill-color-dark);
  border-radius: 6px;
  padding: 10px 12px;
}

.log-content {
  margin: 0;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--el-text-color-primary);
}

.log-line {
  display: block;
}

.log-line.lvl-debug {
  color: var(--el-text-color-secondary);
}

.log-line.lvl-warning {
  color: var(--el-color-warning);
}

.log-line.lvl-error {
  color: var(--el-color-danger);
}

.log-line.lvl-critical {
  color: var(--el-color-danger);
  font-weight: 600;
}

.log-path {
  margin-top: 10px;
  font-size: 12px;
  word-break: break-all;
}
</style>