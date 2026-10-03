<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CircleCheck, CircleClose, Plus, Refresh, VideoPause, VideoPlay } from '@element-plus/icons-vue'

import { useMcpStore } from '@/stores/mcp'
import { useAppStore } from '@/stores/app'
import { formatDateTime, formatRelative } from '@/utils/format'
import { copyText } from '@/utils/clipboard'
import TokenDialog from '@/components/TokenDialog.vue'

const mcpStore = useMcpStore()
const appStore = useAppStore()

const tokenDialog = ref(false)
const scopeDialog = ref(false)
const scopeToken = ref(null)
const scopeTools = ref([])

onMounted(() => {
  mcpStore.refreshAll().catch((err) => ElMessage.error(err.message || '加载 MCP 状态失败'))
})

const serverUrl = computed(() => mcpStore.serverUrl)

async function toggleServer() {
  try {
    if (mcpStore.running) {
      await mcpStore.stop()
      ElMessage.success('MCP 服务已停止')
    } else {
      const result = await mcpStore.start()
      ElMessage.success(`MCP 服务已启动：${result.url}`)
    }
    await appStore.refreshTray()
  } catch (err) {
    ElMessage.error(err.message || '操作失败')
  }
}

async function togglePermission(toolName, enabled) {
  try {
    await mcpStore.setPermission(toolName, enabled)
  } catch (err) {
    ElMessage.error(err.message || '设置失败')
    await mcpStore.fetchPermissions()
  }
}

async function handleRevoke(token) {
  try {
    await ElMessageBox.confirm(
      `确认撤销 Token「${token.name}」？撤销后使用该 Token 的 Agent 将立即失去访问权限。`,
      '撤销 Token',
      { type: 'warning', confirmButtonText: '确认撤销', cancelButtonText: '取消' },
    )
    await mcpStore.revokeToken(token.id)
    await appStore.refreshTray()
    ElMessage.success('Token 已撤销')
  } catch (err) {
    if (err !== 'cancel') ElMessage.error(err.message || '撤销失败')
  }
}

function openScopeDialog(token) {
  scopeToken.value = token
  scopeTools.value = [...(token.tools_allowed || [])]
  scopeDialog.value = true
}

async function saveScope() {
  try {
    await mcpStore.updateTokenScope(scopeToken.value.id, scopeTools.value)
    ElMessage.success('Token 权限范围已更新')
    scopeDialog.value = false
  } catch (err) {
    ElMessage.error(err.message || '更新失败')
  }
}

async function copyUrl() {
  const ok = await copyText(serverUrl.value)
  if (ok) ElMessage.success('服务地址已复制')
}

async function copyClientConfig() {
  const ok = await copyText(mcpStore.clientConfig())
  if (ok) ElMessage.success('客户端配置已复制')
}
</script>

<template>
  <div class="page">
    <!-- 服务状态 -->
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>MCP 服务</span>
          <el-tag :type="mcpStore.running ? 'success' : 'danger'" effect="dark" round>
            {{ mcpStore.running ? '运行中' : '已停止' }}
          </el-tag>
        </div>
      </template>

      <el-descriptions :column="2" border size="small">
        <el-descriptions-item label="服务地址">
          <code class="mono">{{ mcpStore.running ? serverUrl : '—' }}</code>
          <el-button v-if="mcpStore.running" text size="small" @click="copyUrl">复制</el-button>
        </el-descriptions-item>
        <el-descriptions-item label="活跃 Token">{{ mcpStore.status.active_tokens }} 个</el-descriptions-item>
        <el-descriptions-item label="传输协议">SSE (Server-Sent Events)</el-descriptions-item>
        <el-descriptions-item label="鉴权方式">Bearer Token</el-descriptions-item>
      </el-descriptions>

      <div class="server-actions">
        <el-button
          :type="mcpStore.running ? 'danger' : 'success'"
          :icon="mcpStore.running ? VideoPause : VideoPlay"
          :loading="mcpStore.busy"
          @click="toggleServer"
        >
          {{ mcpStore.running ? '停止服务' : '启动服务' }}
        </el-button>
        <el-button :icon="Refresh" @click="mcpStore.fetchStatus()">刷新</el-button>
        <el-button text @click="copyClientConfig">复制客户端配置</el-button>
      </div>

      <el-alert type="info" :closable="false" class="mt-12">
        <template #title>
          客户端配置示例（Claude Desktop / Cursor）：将下方 JSON 合并到 MCP 配置文件中，并替换 Token。
        </template>
        <pre class="config-pre">{{ mcpStore.clientConfig() }}</pre>
      </el-alert>
    </el-card>

    <!-- 工具权限 -->
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>工具权限（全局开关）</span>
          <span class="text-muted">关闭后所有 Token 均无法调用该工具</span>
        </div>
      </template>

      <el-table :data="mcpStore.permissions" size="small" stripe>
        <el-table-column prop="tool_name" label="工具名" min-width="200">
          <template #default="{ row }">
            <code class="mono">{{ row.tool_name }}</code>
          </template>
        </el-table-column>
        <el-table-column prop="description" label="说明" min-width="240" />
        <el-table-column label="状态" width="100" align="center">
          <template #default="{ row }">
            <el-switch
              :model-value="!!row.enabled"
              size="small"
              @change="(val) => togglePermission(row.tool_name, val)"
            />
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- Token 管理 -->
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>访问 Token</span>
          <el-button type="primary" size="small" :icon="Plus" @click="tokenDialog = true">
            生成 Token
          </el-button>
        </div>
      </template>

      <el-empty v-if="!mcpStore.tokens.length" description="暂无 Token，点击上方按钮生成" />

      <el-table v-else :data="mcpStore.tokens" size="small" stripe>
        <el-table-column prop="name" label="名称" min-width="140" />
        <el-table-column label="Token 前缀" width="130">
          <template #default="{ row }">
            <code class="mono">{{ row.token_prefix }}…</code>
          </template>
        </el-table-column>
        <el-table-column label="工具范围" min-width="180">
          <template #default="{ row }">
            <el-tag v-if="!row.tools_allowed || !row.tools_allowed.length" size="small" type="info">
              继承全局
            </el-tag>
            <el-tooltip
              v-else
              :content="row.tools_allowed.join(', ')"
              placement="top"
            >
              <el-tag size="small">{{ row.tools_allowed.length }} 个工具</el-tag>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="150">
          <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="最后使用" width="120">
          <template #default="{ row }">{{ formatRelative(row.last_used_at, '从未') }}</template>
        </el-table-column>
        <el-table-column label="过期时间" width="150">
          <template #default="{ row }">
            <span v-if="!row.expires_at" class="text-muted">永不过期</span>
            <span v-else>{{ formatDateTime(row.expires_at) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="140" fixed="right">
          <template #default="{ row }">
            <el-button text size="small" @click="openScopeDialog(row)">范围</el-button>
            <el-button text size="small" type="danger" @click="handleRevoke(row)">撤销</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- Token 生成对话框 -->
    <TokenDialog v-model="tokenDialog" @created="appStore.refreshTray()" />

    <!-- 工具范围编辑对话框 -->
    <el-dialog v-model="scopeDialog" title="编辑 Token 工具范围" width="520px">
      <el-alert type="info" :closable="false" class="mb-12">
        不选择任何工具 = 继承全局权限（全局开关决定）。选择后仅允许调用的工具。
      </el-alert>
      <el-select
        v-model="scopeTools"
        multiple
        filterable
        collapse-tags
        collapse-tags-tooltip
        placeholder="不选 = 继承全局"
        class="full-width"
      >
        <el-option
          v-for="option in mcpStore.toolOptions"
          :key="option.value"
          :label="option.label"
          :value="option.value"
        >
          <span>{{ option.label }}</span>
          <span class="option-desc">{{ option.description }}</span>
        </el-option>
      </el-select>
      <template #footer>
        <el-button @click="scopeDialog = false">取消</el-button>
        <el-button type="primary" @click="saveScope">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
}

.server-actions {
  display: flex;
  gap: 8px;
  margin-top: 16px;
  flex-wrap: wrap;
}

.config-pre {
  margin: 8px 0 0;
  padding: 10px;
  background: var(--el-fill-color-dark);
  border-radius: 6px;
  font-size: 12px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  overflow-x: auto;
  white-space: pre;
}

.mt-12 {
  margin-top: 12px;
}

.mb-12 {
  margin-bottom: 12px;
}

.full-width {
  width: 100%;
}

.option-desc {
  float: right;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
