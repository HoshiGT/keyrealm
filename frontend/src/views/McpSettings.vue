<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CircleCheck, CircleClose, Refresh, VideoPause, VideoPlay } from '@element-plus/icons-vue'

import { useMcpStore } from '@/stores/mcp'
import { useAppStore } from '@/stores/app'
import { formatDateTime, formatRelative } from '@/utils/format'
import { copyText } from '@/utils/clipboard'
import TokenDialog from '@/components/TokenDialog.vue'

const mcpStore = useMcpStore()
const appStore = useAppStore()

const tokenDialog = ref(false)
const scopeDialog = ref(false)
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

// 高危工具：开启前需二次确认
const SENSITIVE_TOOLS = new Set([
  'vault_get_secret',
  'vault_create_credential',
  'vault_update_credential',
  'vault_delete_credential',
  'vault_install_plugin',
])

async function togglePermission(toolName, enabled) {
  // 开启高危工具时要求确认，避免误点导致 Agent 获得敏感能力
  if (enabled && SENSITIVE_TOOLS.has(toolName)) {
    try {
      await ElMessageBox.confirm(
        `「${toolName}」是高危工具，开启后持有有效 Token 的 Agent 均可调用。确认开启？`,
        '开启高危工具',
        { type: 'warning', confirmButtonText: '确认开启', cancelButtonText: '取消' },
      )
    } catch {
      // 用户取消：回滚开关显示
      await mcpStore.fetchPermissions()
      return
    }
  }
  try {
    await mcpStore.setPermission(toolName, enabled)
    ElMessage.success(enabled ? `已开启 ${toolName}` : `已关闭 ${toolName}`)
  } catch (err) {
    ElMessage.error(err.message || '设置失败')
    await mcpStore.fetchPermissions()
  }
}

async function handleRevoke() {
  try {
    await ElMessageBox.confirm(
      '确认撤销当前 Token？撤销后使用该 Token 的 Agent 将立即失去访问权限。',
      '撤销 Token',
      { type: 'warning', confirmButtonText: '确认撤销', cancelButtonText: '取消' },
    )
    await mcpStore.revokeToken()
    await appStore.refreshTray()
    ElMessage.success('Token 已撤销')
  } catch (err) {
    if (err !== 'cancel') ElMessage.error(err.message || '撤销失败')
  }
}

function openScopeDialog() {
  scopeTools.value = [...((mcpStore.token && mcpStore.token.tools_allowed) || [])]
  scopeDialog.value = true
}

async function saveScope() {
  try {
    await mcpStore.updateTokenScope(scopeTools.value)
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
            <el-tag
              v-if="SENSITIVE_TOOLS.has(row.tool_name)"
              type="danger"
              size="small"
              effect="plain"
              class="ml-6"
            >
              高危
            </el-tag>
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

    <!-- 访问 Token（单 Token + 轮换） -->
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>访问 Token</span>
          <el-button type="primary" size="small" :icon="Refresh" @click="tokenDialog = true">
            {{ mcpStore.token ? '轮换 Token' : '生成 Token' }}
          </el-button>
        </div>
      </template>

      <el-empty v-if="!mcpStore.token" description="暂无活跃 Token，点击右上角生成" />

      <template v-else>
        <el-alert type="warning" :closable="false" class="mb-12">
          Token 明文仅在生成 / 轮换时展示一次；轮换后旧 Token 立即失效，数据库只保存其哈希与前缀。
        </el-alert>

        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="Token 前缀">
            <code class="mono">{{ mcpStore.token.token_prefix }}…</code>
          </el-descriptions-item>
          <el-descriptions-item label="工具范围">
            <el-tag
              v-if="!mcpStore.token.tools_allowed || !mcpStore.token.tools_allowed.length"
              size="small"
              type="info"
            >
              继承全局
            </el-tag>
            <el-tooltip v-else :content="mcpStore.token.tools_allowed.join(', ')" placement="top">
              <el-tag size="small">{{ mcpStore.token.tools_allowed.length }} 个工具</el-tag>
            </el-tooltip>
          </el-descriptions-item>
          <el-descriptions-item label="创建时间">
            {{ formatDateTime(mcpStore.token.created_at) }}
          </el-descriptions-item>
          <el-descriptions-item label="最后使用">
            {{ formatRelative(mcpStore.token.last_used_at, '从未') }}
          </el-descriptions-item>
          <el-descriptions-item label="过期时间">
            <span v-if="!mcpStore.token.expires_at" class="text-muted">永不过期</span>
            <span v-else>{{ formatDateTime(mcpStore.token.expires_at) }}</span>
          </el-descriptions-item>
        </el-descriptions>

        <div class="server-actions">
          <el-button @click="openScopeDialog">编辑范围</el-button>
          <el-button type="danger" plain @click="handleRevoke">撤销 Token</el-button>
        </div>
      </template>
    </el-card>

    <!-- Token 生成 / 轮换对话框 -->
    <TokenDialog
      v-model="tokenDialog"
      :rotating="!!mcpStore.token"
      @created="appStore.refreshTray()"
    />

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

.ml-6 {
  margin-left: 6px;
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
