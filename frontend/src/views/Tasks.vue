<script setup>
/**
 * 任务（后台任务管理）。
 *
 * 为什么独立成页：下载/打包这类长操作跑在后台线程里，关掉发起它的弹窗也继续跑。
 * 用户需要一个**固定入口**回来看「还在跑哪些、进度多少、成没成」，并能随时取消。
 * 弹窗里那条 `vault:action_stream` 是一次性事件流，弹窗一关就没人接了，撑不起这个需求。
 *
 * 数据契约：call('list_tasks') 直接返回数组，且**已按「进行中在前、其内按创建时间倒序」
 * 排好**（排序在后端做，前端照序渲染即可，别自己再排一遍——两边规则不一致会打架）。
 * 增量更新走 `vault:tasks` 事件，detail 是**发生变化的那一条快照**（不是全量列表），
 * 因此这里就地更新/插入对应行，避免每次进度推送都拉一次全量。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete, Refresh } from '@element-plus/icons-vue'

import { api, EVENTS } from '@/api'

const tasks = ref([])
const loading = ref(false)

// 状态 → 标签文案/颜色。后端状态是固定集合，这里兜底 unknown 免得出现空白标签。
const STATUS_META = {
  running: { label: '进行中', type: 'primary' },
  done: { label: '已完成', type: 'success' },
  failed: { label: '失败', type: 'danger' },
  canceled: { label: '已取消', type: 'info' },
}

const runningCount = computed(() => tasks.value.filter((t) => t.status === 'running').length)

function statusMeta(task) {
  // 「已请求取消」是进行中的子状态：插件还没跑到检查点，结果未定，所以单独给文案与颜色，
  // 避免用户以为点了取消却没生效。
  if (task.cancel_requested && task.status === 'running') {
    return { label: '正在取消…', type: 'warning' }
  }
  return STATUS_META[task.status] || { label: task.status || '未知', type: 'info' }
}

/** total<=0 表示「总量未知」（如分块下载拿不到 Content-Length），此时进度条走不定态。 */
function progressPercent(task) {
  if (!task.total || task.total <= 0) return 0
  return Math.min(100, Math.round((task.current / task.total) * 100))
}

function formatTime(iso) {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleString()
}

/** 终态行只展示一行摘要：失败优先看 error，成功/取消看 output。 */
function resultLine(task) {
  if (task.status === 'running') return ''
  return task.error || task.output || ''
}

async function load() {
  loading.value = true
  try {
    const list = await api.listTasks()
    tasks.value = Array.isArray(list) ? list : []
  } catch (err) {
    ElMessage.error(err.message || '加载任务失败')
  } finally {
    loading.value = false
  }
}

/**
 * 把一条任务快照落到列表里。
 *
 * 已存在就整行替换（后端推的是最新快照，字段齐全）；新任务（本页没见过的）按后端的
 * 排序规则插入，保证「进行中在前」这个不变量不被破坏。
 */
function applySnapshot(snapshot) {
  if (!snapshot || !snapshot.id) return false
  const list = tasks.value
  const index = list.findIndex((t) => t.id === snapshot.id)
  if (index >= 0) {
    list[index] = snapshot
    return true
  }
  if (snapshot.status === 'running') {
    list.unshift(snapshot)
  } else {
    // 已结束的新记录（本页错过了它的进行中阶段）：插到「最后一条进行中任务」之后。
    const firstFinished = list.findIndex((t) => t.status !== 'running')
    if (firstFinished < 0) list.push(snapshot)
    else list.splice(firstFinished, 0, snapshot)
  }
  return true
}

function onTasksEvent(event) {
  const snapshot = event && event.detail
  // 拿不到快照（后端结构变了等）就整表兜底，宁可多拉一次也别让列表卡在旧状态。
  if (!applySnapshot(snapshot)) load()
}

async function cancelTask(task) {
  try {
    await ElMessageBox.confirm(
      `将停止「${task.title}」并清理未完成的临时文件，确定取消吗？`,
      '取消任务',
      { type: 'warning', confirmButtonText: '取消任务', cancelButtonText: '返回' },
    )
  } catch {
    return
  }
  try {
    await api.cancelTask(task.id)
    // 不主动刷新：取消是协作式的，状态要等插件跑到检查点才变，
    // 由 vault:tasks 推送回来更新，这里抢跑会显示成「取消没生效」。
    ElMessage.success('已请求取消')
  } catch (err) {
    ElMessage.error(err.message || '取消失败')
  }
}

async function clearFinished() {
  try {
    await ElMessageBox.confirm(
      '将移除所有已结束（已完成 / 失败 / 已取消）的任务记录，进行中的任务不受影响。',
      '清空已完成',
      { type: 'warning', confirmButtonText: '清空', cancelButtonText: '取消', confirmButtonClass: 'el-button--danger' },
    )
  } catch {
    return
  }
  try {
    await api.clearFinishedTasks()
    ElMessage.success('已清空已结束的任务')
    await load()
  } catch (err) {
    ElMessage.error(err.message || '清空失败')
  }
}

onMounted(async () => {
  window.addEventListener(EVENTS.tasksChanged, onTasksEvent)
  await load()
})

onBeforeUnmount(() => {
  // 组件销毁必须摘掉监听，否则每次进出页面都会叠一层 handler（推送一来就重复更新）。
  window.removeEventListener(EVENTS.tasksChanged, onTasksEvent)
})
</script>

<template>
  <div class="page">
    <div class="page-toolbar">
      <h2 class="section-title">任务</h2>
      <span v-if="runningCount" class="text-muted">{{ runningCount }} 个进行中</span>
      <div class="spacer" />
      <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
    </div>

    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>后台任务</span>
          <el-button type="danger" plain :icon="Delete" @click="clearFinished">清空已完成</el-button>
        </div>
      </template>

      <div v-loading="loading">
        <div v-if="tasks.length" class="task-list">
          <div v-for="task in tasks" :key="task.id" class="task-row">
            <div class="task-head">
              <span class="task-title" :title="task.title">{{ task.title }}</span>
              <el-tag :type="statusMeta(task).type" size="small" effect="light">
                {{ statusMeta(task).label }}
              </el-tag>
              <div class="spacer" />
              <span class="task-time">{{ formatTime(task.created_at) }}</span>
            </div>

            <!-- 进度条只在进行中显示：终态留在那里只会让人以为还在跑 -->
            <div v-if="task.status === 'running'" class="task-progress">
              <el-progress
                class="progress-bar"
                :percentage="progressPercent(task)"
                :indeterminate="!task.total || task.total <= 0"
                :show-text="Boolean(task.total && task.total > 0)"
                :stroke-width="10"
              />
              <span v-if="task.text" class="progress-text">{{ task.text }}</span>
            </div>

            <!-- 终态摘要：一行灰字，装不下省略（hover 看全文），别把行撑高 -->
            <div v-if="resultLine(task)" class="task-result">
              <el-tooltip :content="resultLine(task)" placement="top" :show-after="400">
                <span class="result-text" :class="{ 'is-error': task.status === 'failed' }">
                  {{ resultLine(task) }}
                </span>
              </el-tooltip>
            </div>

            <div v-if="task.status === 'running'" class="task-actions">
              <el-button
                size="small"
                :disabled="task.cancel_requested"
                @click="cancelTask(task)"
              >
                {{ task.cancel_requested ? '正在取消…' : '取消' }}
              </el-button>
            </div>
          </div>
        </div>

        <el-empty v-else description="暂无任务" :image-size="60" />
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

.task-list {
  display: flex;
  flex-direction: column;
}

.task-row {
  padding: 12px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.task-row:last-child {
  border-bottom: none;
}

.task-head {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.task-title {
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.task-time {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  white-space: nowrap;
}

.task-progress {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 10px;
}

.progress-bar {
  flex: 1;
  min-width: 0;
}

.progress-text {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  white-space: nowrap;
}

.task-result {
  margin-top: 8px;
  min-width: 0;
}

.result-text {
  display: block;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  cursor: default;
}

.result-text.is-error {
  color: var(--el-color-danger);
}

.task-actions {
  margin-top: 10px;
  display: flex;
  justify-content: flex-end;
}
</style>
