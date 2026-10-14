<script setup>
import { computed, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Download, Upload } from '@element-plus/icons-vue'

import api from '@/api'
import CredentialTreePicker from '@/components/CredentialTreePicker.vue'

// 备份范围：all = **整库**（凭据 + 分组 + 代理 + 预设 + 定时计划 + 设置 + 保险库 KDF + MCP + 插件启停）；
//           selected = 仅勾选的凭据（不含整库档那部分）
const scope = ref('all')
const encrypted = ref(true)
const exporting = ref(false)
const importing = ref(false)
const lastResult = ref('')
const lastResultType = ref('success')

// 「仅选中的凭据」的勾选结果（凭据 id 数组）。目录树的取数、搜索、全选/清空、
// 已选计数都在 CredentialTreePicker 里 —— 与「定时计划 → 指定凭据」共用同一个组件。
const selectedIds = ref([])

// 最近一次导入的完整结果（含逐条失败原因与两份日志路径），驱动下面的结果弹窗
const report = ref(null)
const reportVisible = ref(false)

const canExport = computed(
  () => scope.value === 'all' || selectedIds.value.length > 0,
)

/** 结果弹窗里的一句话总结 */
const reportSummary = computed(() => {
  const r = report.value
  if (!r) return ''
  const parts = [`已导入 ${r.imported} 条`]
  if (r.skipped_duplicate) parts.push(`重复跳过 ${r.skipped_duplicate} 条`)
  if (r.failed_count) parts.push(`失败 ${r.failed_count} 条`)
  let text = parts.join('，') + '。'
  const extra = []
  if (r.created_groups) extra.push(`新建分组 ${r.created_groups} 个`)
  if (r.created_proxies) extra.push(`新建代理 ${r.created_proxies} 个`)
  if (extra.length) text += `（${extra.join('、')}）`
  if (r.failed_count) text += ' 失败条目未写入数据库，原因见下表，也可对照失败日志手动处理。'
  return text
})

async function doExport() {
  exporting.value = true
  try {
    const ids = scope.value === 'selected' ? selectedIds.value : null
    // 仅选中时后端 scope 用 credentials（不带分组/偏好设置）
    const apiScope = scope.value === 'selected' ? 'credentials' : 'all'
    const res = await api.exportBackup(apiScope, encrypted.value, ids)
    if (res && res.ok === false) {
      ElMessage.info(res.message || '已取消')
      return
    }
    const extras = res.scope === 'all' ? '，含分组 / 代理 / 预设 / 定时计划 / 设置 / 插件启停（整库）' : ''
    lastResult.value =
      `已导出 ${res.count} 条凭据（${res.encrypted ? '加密' : '明文'}${extras}）→ ${res.path}`
    lastResultType.value = 'success'
    ElMessage.success('备份已导出')
  } catch (err) {
    ElMessage.error(err.message || '导出失败')
  } finally {
    exporting.value = false
  }
}

async function doImport() {
  try {
    await ElMessageBox.confirm(
      '导入会把备份中的凭据按当前插件字段表填充进库（相同类型且敏感值相同的会跳过）。' +
        '插件未安装/已禁用、含当前插件不认识的字段、缺少必填字段的条目会导入失败并逐条列出原因。' +
        '整库备份还会一并恢复分组 / 代理 / 预设 / 定时计划 / 设置 / MCP / 插件启停。' +
        '加密备份自带盐：**同一保险库**直接导入；来自**别的保险库**时会再让你输入它当时的主密码。是否继续？',
      '导入备份',
      { type: 'warning', confirmButtonText: '选择文件并导入', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  importing.value = true
  try {
    let res = await api.importBackup()
    // 备份来自**别的保险库**（自带 salt）：补问原主密码，并带着**同一个文件**重试
    while (res && res.need_password) {
      let password
      try {
        ({ value: password } = await ElMessageBox.prompt(
          res.message || '这份备份来自另一个保险库，请输入它当时的主密码',
          '需要原主密码',
          { inputType: 'password', confirmButtonText: '解密并导入', cancelButtonText: '取消' },
        ))
      } catch {
        return
      }
      if (!password) return
      res = await api.importBackup(password, res.path)
    }
    if (res && res.ok === false) {
      ElMessage.info(res.message || '已取消')
      return
    }
    const failed = res.failed_count || 0
    lastResultType.value = failed ? 'warning' : 'success'
    lastResult.value =
      `已导入 ${res.imported} 条` +
      (failed ? `，失败 ${failed} 条` : '') +
      (res.skipped_duplicate ? `（重复跳过 ${res.skipped_duplicate} 条）` : '') +
      `；新建分组 ${res.created_groups ?? 0}、新建代理 ${res.created_proxies ?? 0}` +
      (res.restored && res.restored.schedules
        ? `；恢复定时计划 ${res.restored.schedules} 条`
        : '')

    // 有失败（或至少生成了日志）就弹详情：日志路径必须让用户看见，失败得能一键打开
    if (failed || res.log_file) {
      report.value = res
      reportVisible.value = true
    }
    if (failed) ElMessage.warning(`导入完成，${failed} 条失败`)
    else ElMessage.success('导入完成')
  } catch (err) {
    ElMessage.error(err.message || '导入失败')
  } finally {
    importing.value = false
  }
}

/** 用系统默认程序打开 `data/logs/` 下的日志文件（后端只接受纯文件名） */
async function openLog(field) {
  const path = report.value?.[field]
  if (!path) return
  const name = String(path).split(/[\\/]/).pop()
  try {
    const res = await api.openLogFile(name)
    if (res && res.ok === false) ElMessage.error(res.message || '打开失败')
  } catch (err) {
    ElMessage.error(err.message || '打开失败')
  }
}
</script>

<template>
  <div class="page">
    <div class="page-toolbar">
      <h2 class="section-title">备份</h2>
    </div>

    <el-card shadow="never">
      <template #header><span>导出备份</span></template>

      <el-form label-width="90px">
        <el-form-item label="备份范围">
          <el-radio-group v-model="scope">
            <el-radio value="all">整库（含分组 / 代理 / 预设 / 定时计划 / 设置 / 插件启停）</el-radio>
            <el-radio value="selected">仅选中的凭据</el-radio>
          </el-radio-group>
        </el-form-item>

        <el-form-item v-if="scope === 'selected'" label="选择凭据">
          <!-- 与「系统设置 → 定时计划 → 指定凭据」同一个组件（勾选语义完全相同） -->
          <CredentialTreePicker v-model="selectedIds" />
        </el-form-item>

        <el-form-item label="是否加密">
          <el-radio-group v-model="encrypted">
            <el-radio :value="true">加密（默认，需同一主密码导入）</el-radio>
            <el-radio :value="false">不加密（敏感值明文，慎用）</el-radio>
          </el-radio-group>
        </el-form-item>
      </el-form>

      <el-alert type="info" :closable="false" show-icon class="tip">
        加密备份的敏感字段保持密文、不落明文，但只能用同一主密码导入；
        不加密备份为明文，可在不同主密码的库上导入。
        <br />
        「整库」会额外带上分组 / 代理 / 预设 / 定时计划 / 设置 / **保险库 KDF（盐）** / MCP / 插件启停，
        并因此**自包含**——换台机器、或原库损坏丢失，导入它都能还原（来自别的保险库时会再问当时的主密码）；
        「仅选中的凭据」只导出勾选的条目，导入时不会改动上述整库档内容。
      </el-alert>

      <div class="actions">
        <el-button
          type="primary"
          :icon="Download"
          :loading="exporting"
          :disabled="!canExport"
          @click="doExport"
        >
          导出备份
        </el-button>
      </div>
    </el-card>

    <el-card shadow="never">
      <template #header><span>导入备份</span></template>
      <p class="desc text-muted">
        选择备份文件导入，会自动识别加密 / 明文。导入按<strong>当前系统的插件字段表</strong>填充：
        插件新增的字段留空即可；反之，插件未安装 / 已禁用、备份里出现当前插件不认识的字段、
        必填字段在备份里没有着落（且插件也没给默认值）的条目，都会<strong>整条判失败</strong>而不入库。
      </p>
      <p class="desc text-muted">
        每次导入都会在 <code>data/logs/</code> 下留一份完整导入日志（含成功条目，敏感值已脱敏）；
        若有失败，另生成一份失败日志（只含失败条目、<strong>保留明文</strong>，供人工比对补录）。
        失败原因会在导入后弹窗逐条列出，并可一键打开日志。
      </p>
      <div class="actions">
        <el-button :icon="Upload" :loading="importing" @click="doImport">导入备份</el-button>
      </div>
    </el-card>

    <el-alert
      v-if="lastResult"
      :type="lastResultType"
      :closable="false"
      class="tip"
    >{{ lastResult }}</el-alert>

    <!-- 导入结果：有失败就逐条列原因，并把两份日志的路径摆出来 -->
    <el-dialog v-model="reportVisible" title="导入结果" width="780px" append-to-body>
      <template v-if="report">
        <el-alert
          :type="report.failed_count ? 'warning' : 'success'"
          :closable="false"
          show-icon
          class="report-head"
        >
          {{ reportSummary }}
        </el-alert>

        <template v-if="report.failed_count">
          <p class="report-label">失败条目（{{ report.failed_count }} 条）</p>
          <el-table :data="report.failed || []" size="small" border max-height="240">
            <el-table-column prop="name" label="名称" min-width="150" show-overflow-tooltip />
            <el-table-column prop="credential_type" label="类型" min-width="150" show-overflow-tooltip />
            <el-table-column prop="reason" label="失败原因" min-width="280" show-overflow-tooltip />
          </el-table>
          <p v-if="report.failed_truncated" class="desc text-muted report-truncated">
            另有 {{ report.failed_truncated }} 条未在此列出，完整清单见失败日志。
          </p>
        </template>

        <div class="report-logs">
          <p class="report-label">日志文件</p>
          <div v-if="report.log_file" class="report-log-line">
            <span class="text-muted">完整导入日志（敏感值已脱敏）：</span>
            <code>{{ report.log_file }}</code>
          </div>
          <div v-else-if="report.log_error" class="report-log-line text-muted">
            {{ report.log_error }}
          </div>
          <div v-if="report.failed_log_file" class="report-log-line">
            <span class="text-muted">导入失败日志（含明文）：</span>
            <code>{{ report.failed_log_file }}</code>
          </div>
        </div>
      </template>

      <template #footer>
        <el-button
          v-if="report && report.failed_log_file"
          type="primary"
          @click="openLog('failed_log_file')"
        >
          打开失败日志
        </el-button>
        <el-button v-if="report && report.log_file" @click="openLog('log_file')">
          打开完整日志
        </el-button>
        <el-button @click="reportVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.tip {
  margin-top: 12px;
}

.actions {
  margin-top: 12px;
}

.desc {
  font-size: 13px;
  margin: 0 0 4px;
}

.text-muted {
  color: var(--el-text-color-secondary);
}

.report-head {
  margin-bottom: 14px;
}

.report-label {
  font-size: 13px;
  font-weight: 600;
  margin: 0 0 6px;
}

.report-truncated {
  margin-top: 6px;
}

.report-logs {
  margin-top: 16px;
}

/* 日志路径可能很长，允许在任意字符处折行，别把弹窗撑宽 */
.report-log-line {
  font-size: 13px;
  line-height: 1.6;
}

.report-log-line code {
  word-break: break-all;
}
</style>
