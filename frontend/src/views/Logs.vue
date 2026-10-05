<script setup>
/**
 * 日志（独立页面）。
 *
 * 为什么独立成页：日志是排查问题时的高频入口，原先埋在「系统设置」顶部卡片里，
 * 既要滚动查找，又和主密码/代理等配置混在一起。拆出来后拥有整屏高度，
 * 也方便把「级别筛选 + 关键词搜索」这类查询能力集中在一处。
 *
 * 数据契约：call('get_logs', limit, level, search)，
 * 由后端 read_logs 对整条记录（含 traceback 续行）做大小写不敏感匹配，
 * 多个空白分隔关键词需全部命中（AND）。
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CopyDocument, Delete, Refresh, Search } from '@element-plus/icons-vue'

import { api } from '@/api'
import { copyText } from '@/utils/clipboard'

const LOG_LIMITS = [
  { label: '最近 100 条', value: 100 },
  { label: '最近 200 条', value: 200 },
  { label: '最近 500 条', value: 500 },
  { label: '最近 1000 条', value: 1000 },
]

/* ── 日志配置 ───────────────────────────────────────── */

const logConfig = ref({
  level: 'INFO',
  levels: ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
  log_file: '',
})
const selectedLevel = ref('INFO')
const levelSaving = ref(false)

/* ── 日志内容 ───────────────────────────────────────── */

const logs = ref([])
const logsLoading = ref(false)
const logFilterLevel = ref('')
const logLimit = ref(200)
// 搜索关键词：由用户手动触发（回车/按钮）才请求，避免边打字边打后端。
const logSearch = ref('')

const logFilterOptions = computed(() => [
  { label: '全部级别', value: '' },
  ...logConfig.value.levels.map((level) => ({ label: `${level} 及以上`, value: level })),
])

const logCount = computed(() => logs.value.length)

// 是否处于「已输入查询条件」状态，用于给清空按钮/提示语做文案区分。
const hasQuery = computed(() => Boolean(logSearch.value.trim()) || Boolean(logFilterLevel.value))

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
    // 级别筛选与关键词搜索叠加：两者都作为过滤条件交给后端，命中结果取交集。
    const result = await api.getLogs(logLimit.value, logFilterLevel.value, logSearch.value.trim())
    logs.value = Array.isArray(result.entries) ? result.entries : []
    if (result.log_file) logConfig.value.log_file = result.log_file
  } catch (err) {
    ElMessage.error(err.message || '加载日志失败')
  } finally {
    logsLoading.value = false
  }
}

function resetSearch() {
  // el-input 的 clearable 点击后会先清空 v-model，这里只需重新拉取。
  loadLogs()
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

/** 复制当前列表里的日志（按行拼接），方便贴出去反馈；复制的是可见的这批筛选结果 */
async function copyLogs() {
  if (!logs.value.length) {
    ElMessage.info('没有可复制的日志')
    return
  }
  const ok = await copyText(logs.value.join('\n'))
  if (ok) ElMessage.success(`已复制 ${logs.value.length} 条日志`)
  else ElMessage.error('复制失败，请手动选中后复制')
}

onMounted(async () => {
  await Promise.all([loadLogConfig(), loadLogs()])
})
</script>

<template>
  <div class="page">
    <div class="page-toolbar">
      <h2 class="section-title">日志</h2>
      <div class="spacer" />
      <el-button :icon="Refresh" :loading="logsLoading" @click="loadLogs">刷新</el-button>
    </div>

    <el-card shadow="never">
      <template #header><span>日志配置</span></template>

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

      <el-descriptions :column="2" border>
        <el-descriptions-item label="日志文件">
          <span class="log-path">{{ logConfig.log_file || '—' }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="当前显示">
          {{ logCount }} 条
          <span v-if="hasQuery" class="text-muted">（已过滤）</span>
        </el-descriptions-item>
      </el-descriptions>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>日志内容</span>
          <el-button type="danger" plain :icon="Delete" @click="clearLogs">清空日志</el-button>
        </div>
      </template>

      <div class="log-toolbar">
        <el-input
          v-model="logSearch"
          class="log-search"
          placeholder="搜索日志（空格分隔多个关键词，需全部命中）"
          clearable
          :prefix-icon="Search"
          @keyup.enter="loadLogs"
          @clear="resetSearch"
        />
        <el-button type="primary" :icon="Search" :loading="logsLoading" @click="loadLogs">搜索</el-button>

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
        <el-button :icon="CopyDocument" :disabled="!logs.length" @click="copyLogs">复制</el-button>
        <span class="log-count">共 {{ logCount }} 条</span>
      </div>

      <div v-loading="logsLoading" class="log-viewer">
        <pre v-if="logs.length" class="log-content"><span
          v-for="(entry, index) in logs"
          :key="index"
          class="log-line"
          :class="logLineClass(entry)"
        >{{ entry }}</span></pre>
        <el-empty v-else :description="hasQuery ? '没有匹配的日志' : '暂无日志'" :image-size="60" />
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

.inline-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.level-select {
  width: 150px;
}

.limit-select {
  width: 150px;
}

.log-search {
  width: 320px;
}

.log-path {
  word-break: break-all;
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
  max-height: 60vh;
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
</style>
