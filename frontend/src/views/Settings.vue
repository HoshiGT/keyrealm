<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Connection, Delete, Edit, Key, Lock, Plus, Refresh } from '@element-plus/icons-vue'

import { api } from '@/api'
import ThemePicker from '@/components/ThemePicker.vue'
import { useAppStore } from '@/stores/app'
import { useProxyStore } from '@/stores/proxy'

const appStore = useAppStore()

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

/* ── 空闲自动锁定 ───────────────────────────────────── */
const autoLockMinutes = ref(0)
const autoLockOptions = ref([
  { value: 0, label: '关闭' },
  { value: 1, label: '1 分钟' },
  { value: 5, label: '5 分钟' },
  { value: 10, label: '10 分钟' },
  { value: 15, label: '15 分钟' },
  { value: 30, label: '30 分钟' },
  { value: 60, label: '60 分钟' },
])
const autoLockSaving = ref(false)

async function loadAutoLock() {
  try {
    const config = await api.getAutoLock()
    if (typeof config.minutes === 'number') autoLockMinutes.value = config.minutes
    if (Array.isArray(config.options) && config.options.length) {
      autoLockOptions.value = config.options
    }
  } catch (err) {
    ElMessage.error(err.message || '加载空闲锁定设置失败')
  }
}

async function applyAutoLock(value) {
  autoLockSaving.value = true
  try {
    await api.setAutoLock(value)
    ElMessage.success(value ? `已开启：空闲 ${value} 分钟后自动锁定` : '已关闭空闲自动锁定')
  } catch (err) {
    ElMessage.error(err.message || '设置空闲自动锁定失败')
    await loadAutoLock()
  } finally {
    autoLockSaving.value = false
  }
}

/* ── 轻量模式 ───────────────────────────────────────── */
// 开关只影响后端「隐藏到托盘」时的行为，前端不需要跟着变什么。
const lightMode = ref(false)
const lightModeSaving = ref(false)

async function loadLightMode() {
  try {
    const config = await api.getLightMode()
    lightMode.value = Boolean(config.enabled)
  } catch (err) {
    ElMessage.error(err.message || '加载轻量模式设置失败')
  }
}

async function applyLightMode(value) {
  lightModeSaving.value = true
  try {
    await api.setLightMode(value)
    ElMessage.success(
      value ? '已开启轻量模式：最小化到托盘时释放界面' : '已关闭轻量模式',
    )
  } catch (err) {
    ElMessage.error(err.message || '设置轻量模式失败')
    await loadLightMode()
  } finally {
    lightModeSaving.value = false
  }
}

/* ── 开机自启动 ────────────────────────────────────── */
// 状态以**操作系统**的自启条目为准（后端每次现读），前端不做本地记忆，
// 免得用户手动删掉系统条目后界面还显示"已开启"。
const autostart = ref({ supported: true, enabled: false, platform: '', target: '' })
const autostartSaving = ref(false)

async function loadAutostart() {
  try {
    const config = await api.getAutostart()
    autostart.value = {
      supported: Boolean(config.supported),
      enabled: Boolean(config.enabled),
      platform: config.platform || '',
      target: config.target || '',
    }
  } catch (err) {
    ElMessage.error(err.message || '加载开机自启动设置失败')
  }
}

async function applyAutostart(value) {
  autostartSaving.value = true
  try {
    const res = await api.setAutostart(value)
    // 回读系统侧的真实结果（写入可能被系统策略改写）
    if (res && typeof res.enabled === 'boolean') autostart.value.enabled = res.enabled
    ElMessage.success(autostart.value.enabled ? '已开启开机自启动' : '已关闭开机自启动')
  } catch (err) {
    ElMessage.error(err.message || '设置开机自启动失败')
    await loadAutostart()
  } finally {
    autostartSaving.value = false
  }
}

const refreshing = ref(false)

/**
 * 重新拉取本页各项设置：首屏加载与顶部「刷新」共用同一份清单，
 * 免得以后新增设置项时只改了其中一处。
 * @param {boolean} showSpinner 首屏不必转菊花 —— 页面本来就是空的
 */
async function refreshAll(showSpinner = true) {
  if (showSpinner) refreshing.value = true
  try {
    await Promise.all([
      loadCloseAction(),
      loadAutoLock(),
      loadLightMode(),
      loadAutostart(),
      proxyStore.fetch().catch(() => {}),
      appStore.refreshTray().catch(() => {}),
    ])
  } finally {
    if (showSpinner) refreshing.value = false
  }
}

onMounted(() => refreshAll(false))
</script>

<template>
  <div class="page">
    <div class="page-toolbar">
      <h2 class="section-title">系统设置</h2>
      <div class="spacer" />
      <el-button :icon="Refresh" :loading="refreshing" @click="refreshAll">刷新</el-button>
    </div>

    <el-card shadow="never">
      <template #header><span>外观</span></template>

      <div class="setting-info">
        <div class="setting-label">界面主题</div>
      </div>

      <ThemePicker />
    </el-card>

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

      <el-divider />

      <div class="setting-row">
        <div class="setting-info">
          <div class="setting-label">空闲自动锁定</div>
          <div class="setting-desc">
            一段时间无操作后自动清除内存中的密钥。若当时后台仍有验证任务在跑，则顺延到任务结束后再锁。
          </div>
        </div>
        <el-select
          v-model="autoLockMinutes"
          class="close-select"
          :loading="autoLockSaving"
          @change="applyAutoLock"
        >
          <el-option
            v-for="opt in autoLockOptions"
            :key="opt.value"
            :label="opt.label"
            :value="opt.value"
          />
        </el-select>
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

      <el-divider />

      <div class="setting-row">
        <div class="setting-info">
          <div class="setting-label">轻量模式</div>
          <div class="setting-desc">
            最小化到托盘时释放界面（销毁当前网页）以省内存，从托盘打开窗口时重新加载。
            代价：重新打开需要等待界面重建，且释放时未保存的编辑内容会丢失；
            后台的 MCP 服务与定时任务不受影响。
          </div>
        </div>
        <el-switch v-model="lightMode" :loading="lightModeSaving" @change="applyLightMode" />
      </div>
    </el-card>

    <el-card shadow="never">
      <template #header><span>启动</span></template>

      <div class="setting-row">
        <div class="setting-info">
          <div class="setting-label">开机自启动</div>
          <div class="setting-desc">
            {{
              autostart.supported
                ? '开机后自动启动本程序。开关直接读写操作系统的自启条目，不依赖本程序自身的配置。'
                : '当前平台不支持设置开机自启动。'
            }}
          </div>
          <div v-if="autostart.supported && autostart.target" class="setting-path">
            系统落点：{{ autostart.target }}
          </div>
        </div>
        <el-switch
          v-model="autostart.enabled"
          :loading="autostartSaving"
          :disabled="!autostart.supported"
          @change="applyAutostart"
        />
      </div>
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
          <el-input v-model="proxyForm.name" placeholder="例如：示例代理 / 本机 Clash" clearable />
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
  </div>
</template>

<style scoped>
.section-title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
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

.setting-path {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  margin-top: 4px;
  word-break: break-all;
}

.inline-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
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