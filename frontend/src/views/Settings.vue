<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Connection, Delete, Edit, Key, Lock, Plus, Refresh, Timer, VideoPlay } from '@element-plus/icons-vue'

import { api } from '@/api'
import { useAppStore } from '@/stores/app'
import { useProxyStore } from '@/stores/proxy'
import { useVaultStore } from '@/stores/vault'
import { formatDateTime, formatRelative } from '@/utils/format'

const appStore = useAppStore()
const vaultStore = useVaultStore()

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

/* ── 验证代理（多代理注册表 + 全局默认） ────────────────
   代理一律按 id 引用；测试的进行态与结果放在 store 里，切页不会丢。 */

const proxyStore = useProxyStore()

const proxyDialog = ref(false)
const proxySaving = ref(false)
const proxyForm = reactive({ id: null, name: '', url: '' })

const globalProxyValue = computed(() => proxyStore.globalId ?? '')

function openCreateProxy() {
  proxyForm.id = null
  proxyForm.name = ''
  proxyForm.url = ''
  proxyDialog.value = true
}

function openEditProxy(item) {
  proxyForm.id = item.id
  proxyForm.name = item.name
  proxyForm.url = item.url
  proxyDialog.value = true
}

async function saveProxy() {
  const name = proxyForm.name.trim()
  const url = proxyForm.url.trim()
  if (!name) {
    ElMessage.warning('请填写代理名称')
    return
  }
  if (!url) {
    ElMessage.warning('请填写代理地址')
    return
  }
  proxySaving.value = true
  try {
    if (proxyForm.id) {
      await proxyStore.update(proxyForm.id, name, url)
      ElMessage.success('代理已更新')
    } else {
      await proxyStore.create(name, url)
      ElMessage.success('代理已新增')
    }
    // 地址变了旧结论就失效，避免用户对着过期结果下判断
    proxyStore.clearResult()
    proxyDialog.value = false
  } catch (err) {
    ElMessage.error(err.message || '保存失败')
  } finally {
    proxySaving.value = false
  }
}

async function removeProxy(item) {
  try {
    await ElMessageBox.confirm(
      `确定删除代理「${item.name}」吗？引用它的凭据会自动回退到全局代理。`,
      '删除代理',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消', confirmButtonClass: 'el-button--danger' },
    )
  } catch {
    return
  }
  try {
    await proxyStore.remove(item.id)
    ElMessage.success('代理已删除')
  } catch (err) {
    ElMessage.error(err.message || '删除失败')
  }
}

async function changeGlobalProxy(value) {
  try {
    await proxyStore.setGlobal(value === '' ? null : value)
    ElMessage.success(value === '' ? '全局已切换为直连' : '已设为全局默认代理')
  } catch (err) {
    ElMessage.error(err.message || '设置失败')
    await proxyStore.fetch().catch(() => {})
  }
}

/* 代理连通性测试：后端同一批探测分别走直连与代理，前端只负责展示对照结果 */
async function runProxyTest(id = null) {
  try {
    const result = await proxyStore.runTest(id)
    if (result && result.ok === false) {
      ElMessage.error(result.message || '代理测试失败')
    }
  } catch (err) {
    ElMessage.error(err.message || '代理测试失败')
  }
}

function verdictType(level) {
  return level === 'ok' ? 'success' : level === 'fail' ? 'error' : 'warning'
}

function probeClass(section) {
  if (!section) return ''
  return section.ok ? 'probe-ok' : 'probe-bad'
}

function probeSiteText(section) {
  if (!section) return '—'
  return section.ok ? `可达（${section.ms}ms）` : '不可达'
}

function probeIpText(info) {
  if (!info) return '—'
  if (!info.ok) return `查询失败：${info.detail || '未知原因'}`
  const geo = [info.country, info.region, info.city].filter(Boolean).join(' ')
  const isp = info.isp ? ` · ${info.isp}` : ''
  return geo ? `${info.ip}（${geo}${isp}）` : info.ip
}

/* ── 关闭窗口行为 ───────────────────────────────────── */
const closeAction = ref('ask')
const closeActionOptions = ref([
  { value: 'ask', label: '每次询问我' },
  { value: 'quit', label: '直接退出程序' },
  { value: 'background', label: '后台运行（最小化到托盘）' },
])
const closeSaving = ref(false)

async function loadCloseAction() {
  try {
    const config = await api.getCloseAction()
    if (config.action) closeAction.value = config.action
    if (Array.isArray(config.options) && config.options.length) {
      closeActionOptions.value = config.options
    }
  } catch (err) {
    ElMessage.error(err.message || '加载关闭设置失败')
  }
}

async function applyCloseAction(value) {
  closeSaving.value = true
  try {
    await api.setCloseAction(value)
    ElMessage.success('关闭行为已更新')
  } catch (err) {
    ElMessage.error(err.message || '设置关闭行为失败')
    await loadCloseAction()
  } finally {
    closeSaving.value = false
  }
}

onMounted(async () => {
  await Promise.all([
    loadSchedules(),
    loadCloseAction(),
    proxyStore.fetch().catch(() => {}),
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
      <template #header><span>窗口与关闭</span></template>

      <div class="setting-row">
        <div class="setting-info">
          <div class="setting-label">关闭主窗口时</div>
          <div class="setting-desc">
            点击窗口关闭按钮时的默认行为。选择"后台运行"时程序会最小化到系统托盘，MCP 服务与定时任务继续执行。
          </div>
        </div>
        <el-select
          v-model="closeAction"
          class="close-select"
          :loading="closeSaving"
          @change="applyCloseAction"
        >
          <el-option
            v-for="opt in closeActionOptions"
            :key="opt.value"
            :label="opt.label"
            :value="opt.value"
          />
        </el-select>
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
      <template #header><span>网络与代理</span></template>

      <!-- 全局默认代理：凭据没单独指定时用它 -->
      <div class="setting-row">
        <div class="setting-info">
          <div class="setting-label">默认代理</div>
          <div class="setting-desc">
            凭据没有单独指定代理时用它。可在下面维护多个代理、各自取名；其它模块一律按 id 引用。
            支持 http / https / socks5。环境变量 VAULT_PROXY 仍可覆盖（外部变量优先）。
          </div>
        </div>
        <div class="inline-row">
          <el-select v-model="globalProxyValue" class="proxy-input" @change="changeGlobalProxy">
            <el-option value="" label="直连（不使用代理）" />
            <el-option
              v-for="item in proxyStore.items"
              :key="item.id"
              :value="item.id"
              :label="`${item.name}（${item.url}）`"
            />
          </el-select>
          <el-button
            :icon="Connection"
            :loading="proxyStore.testing && proxyStore.testingId === null"
            @click="runProxyTest(null)"
          >测试当前</el-button>
        </div>
      </div>

      <el-divider />

      <div class="proxy-list-head">
        <span class="setting-label">代理列表</span>
        <el-button size="small" :icon="Plus" @click="openCreateProxy">新增代理</el-button>
      </div>

      <el-empty
        v-if="!proxyStore.items.length"
        description="还没有代理，点「新增代理」添加"
        :image-size="48"
      />
      <div v-else class="proxy-list">
        <div v-for="item in proxyStore.items" :key="item.id" class="proxy-item">
          <el-tag v-if="proxyStore.globalId === item.id" size="small" type="success" effect="plain">默认</el-tag>
          <span class="proxy-name">{{ item.name }}</span>
          <code class="mono proxy-url">{{ item.url }}</code>
          <el-button
            size="small"
            :loading="proxyStore.testing && proxyStore.testingId === item.id"
            @click="runProxyTest(item.id)"
          >测试</el-button>
          <el-button size="small" :icon="Edit" @click="openEditProxy(item)" />
          <el-button size="small" type="danger" plain :icon="Delete" @click="removeProxy(item)" />
        </div>
      </div>

      <!-- 测试结果：直连 / 代理两组对照，外加综合结论（状态在 store 里，切页不丢） -->
      <template v-if="proxyStore.result || proxyStore.testing">
        <el-divider />

        <div v-if="proxyStore.testing && !proxyStore.result" class="probe-loading">
          正在探测直连与代理出口，最多约 10 秒…
        </div>

        <el-alert
          v-if="proxyStore.result && proxyStore.result.verdict"
          :type="verdictType(proxyStore.result.verdict.level)"
          :title="proxyStore.result.verdict.headline"
          :closable="false"
          show-icon
        >
          <ul class="verdict-list">
            <li v-for="(line, index) in proxyStore.result.verdict.details" :key="index">{{ line }}</li>
          </ul>
        </el-alert>

        <div v-if="proxyStore.result" class="probe-grid">
          <div class="probe-card">
            <div class="probe-title">不挂代理（直连）</div>
            <div class="probe-row">
              <span class="probe-k">出口 IP</span>
              <span class="probe-v">{{ probeIpText(proxyStore.result.direct && proxyStore.result.direct.ip) }}</span>
            </div>
            <div class="probe-row">
              <span class="probe-k">国内站点</span>
              <span class="probe-v" :class="probeClass(proxyStore.result.direct && proxyStore.result.direct.domestic)">
                {{ probeSiteText(proxyStore.result.direct && proxyStore.result.direct.domestic) }}
              </span>
            </div>
            <div class="probe-row">
              <span class="probe-k">国外站点</span>
              <span class="probe-v" :class="probeClass(proxyStore.result.direct && proxyStore.result.direct.foreign)">
                {{ probeSiteText(proxyStore.result.direct && proxyStore.result.direct.foreign) }}
              </span>
            </div>
          </div>

          <div class="probe-card">
            <div class="probe-title">
              挂代理
              <span v-if="proxyStore.result.proxy" class="text-muted">
                （{{ proxyStore.result.proxy }}）
              </span>
            </div>
            <template v-if="proxyStore.result.proxied">
              <div class="probe-row">
                <span class="probe-k">出口 IP</span>
                <span class="probe-v">{{ probeIpText(proxyStore.result.proxied.ip) }}</span>
              </div>
              <div class="probe-row">
                <span class="probe-k">国内站点</span>
                <span class="probe-v" :class="probeClass(proxyStore.result.proxied.domestic)">
                  {{ probeSiteText(proxyStore.result.proxied.domestic) }}
                </span>
              </div>
              <div class="probe-row">
                <span class="probe-k">国外站点</span>
                <span class="probe-v" :class="probeClass(proxyStore.result.proxied.foreign)">
                  {{ probeSiteText(proxyStore.result.proxied.foreign) }}
                </span>
              </div>
            </template>
            <el-empty v-else description="未配置代理" :image-size="48" />
          </div>
        </div>
      </template>
    </el-card>

    <!-- 新增 / 编辑代理 -->
    <el-dialog
      v-model="proxyDialog"
      :title="proxyForm.id ? '编辑代理' : '新增代理'"
      width="460px"
      :close-on-click-modal="false"
    >
      <el-form label-position="top">
        <el-form-item label="名称">
          <el-input v-model="proxyForm.name" placeholder="例如：生产梯子 / 本机 Clash" clearable />
        </el-form-item>
        <el-form-item label="代理地址">
          <el-input v-model="proxyForm.url" placeholder="socks5://127.0.0.1:10808" clearable />
          <div class="field-hint">需带协议前缀，支持 http / https / socks4 / socks5 / socks5h</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="proxyDialog = false">取消</el-button>
        <el-button type="primary" :loading="proxySaving" @click="saveProxy">保存</el-button>
      </template>
    </el-dialog>

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

.proxy-input {
  width: 280px;
}

/* ── 代理列表 ───────────────────────────────────────── */
.proxy-list-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.proxy-list {
  display: flex;
  flex-direction: column;
}

.proxy-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.proxy-item:last-child {
  border-bottom: none;
}

.proxy-name {
  min-width: 110px;
  font-size: 13px;
}

.proxy-url {
  flex: 1;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.close-select {
  width: 220px;
}

/* ── 代理测试结果 ───────────────────────────────────── */
.verdict-list {
  margin: 6px 0 0;
  padding-left: 18px;
  line-height: 1.7;
}

.probe-grid {
  margin-top: 14px;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 12px;
}

.probe-loading {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.probe-card {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 10px 12px;
}

.probe-title {
  font-weight: 600;
  margin-bottom: 8px;
  font-size: 13px;
}

.probe-row {
  display: flex;
  gap: 8px;
  font-size: 12px;
  line-height: 1.8;
}

.probe-k {
  flex: 0 0 62px;
  color: var(--el-text-color-secondary);
}

.probe-v {
  flex: 1;
  word-break: break-all;
}

.probe-v.probe-ok {
  color: var(--el-color-success);
}

.probe-v.probe-bad {
  color: var(--el-color-danger);
}
</style>