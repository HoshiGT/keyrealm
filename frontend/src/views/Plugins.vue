<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete, Refresh, UploadFilled } from '@element-plus/icons-vue'

import { useVaultStore } from '@/stores/vault'
import { formatDateTime } from '@/utils/format'

const vaultStore = useVaultStore()

const uploadRef = ref(null)
const installing = ref(false)
const togglingType = ref('')
const manualPath = ref('')
const showManual = ref(false)

const plugins = computed(() => vaultStore.plugins)

const builtinCount = computed(() => plugins.value.filter((plugin) => plugin.is_builtin).length)

/**
 * 从拖拽/选择得到的 File 对象中取出本地绝对路径。
 * pywebview 会给 File 对象附加 path 属性；普通浏览器出于安全限制没有该属性。
 */
function extractPath(file) {
  const raw = file && file.raw ? file.raw : file
  if (!raw) return ''
  return raw.path || raw.fullPath || raw.filePath || file.path || ''
}

async function installFromPath(zipPath) {
  if (!zipPath) {
    ElMessage.error('无法获取文件路径，请使用桌面端拖拽，或在下方手动输入 .zip 的完整路径')
    showManual.value = true
    return
  }
  installing.value = true
  try {
    const result = await vaultStore.installPlugin(zipPath)
    ElMessage.success(result.message || '插件安装成功')
    manualPath.value = ''
  } catch (err) {
    ElMessage.error(err.message || '插件安装失败')
  } finally {
    installing.value = false
    if (uploadRef.value) uploadRef.value.clearFiles()
  }
}

function handleFileChange(file) {
  const path = extractPath(file)
  installFromPath(path)
}

function installManual() {
  const path = manualPath.value.trim()
  if (!path) {
    ElMessage.warning('请输入插件包路径')
    return
  }
  installFromPath(path)
}

async function togglePlugin(plugin, enabled) {
  togglingType.value = plugin.credential_type
  try {
    await vaultStore.togglePlugin(plugin.credential_type, enabled)
    ElMessage.success(`${plugin.name} 已${enabled ? '启用' : '停用'}`)
  } catch (err) {
    ElMessage.error(err.message || '操作失败')
    await vaultStore.fetchPlugins()
  } finally {
    togglingType.value = ''
  }
}

async function uninstall(plugin) {
  try {
    await ElMessageBox.confirm(
      `确定卸载插件「${plugin.name}」吗？该插件下的 ${plugin.credential_count || 0} 条凭据也会一并移除。`,
      '卸载插件',
      { type: 'warning', confirmButtonText: '卸载', cancelButtonText: '取消', confirmButtonClass: 'el-button--danger' },
    )
  } catch {
    return
  }

  try {
    const result = await vaultStore.uninstallPlugin(plugin.credential_type)
    ElMessage.success(result.message || '插件已卸载')
    await vaultStore.fetchCredentials()
  } catch (err) {
    ElMessage.error(err.message || '卸载失败')
  }
}

async function reload() {
  try {
    await vaultStore.fetchPlugins()
  } catch (err) {
    ElMessage.error(err.message || '加载插件失败')
  }
}

onMounted(async () => {
  await reload()
  await vaultStore.fetchCredentials().catch(() => {})
})
</script>

<template>
  <div class="page">
    <div class="page-toolbar">
      <h2 class="section-title">插件管理</h2>
      <span class="text-muted">已安装 {{ plugins.length }} 个（内置 {{ builtinCount }} 个）</span>
      <div class="spacer" />
      <el-button :icon="Refresh" @click="reload">刷新</el-button>
    </div>

    <el-card shadow="never">
      <el-upload
        ref="uploadRef"
        drag
        accept=".zip"
        :auto-upload="false"
        :show-file-list="false"
        :limit="1"
        :disabled="installing"
        :on-change="handleFileChange"
        class="plugin-upload"
      >
        <el-icon class="upload-icon"><UploadFilled /></el-icon>
        <div class="upload-text">拖拽 .zip 插件包到此处安装</div>
        <div class="upload-sub">或点击选择文件（安装后无需重启即可使用）</div>
      </el-upload>

      <div class="manual-row">
        <el-button text type="primary" @click="showManual = !showManual">
          {{ showManual ? '收起手动安装' : '无法拖拽？手动输入路径' }}
        </el-button>
        <div v-if="showManual" class="manual-input">
          <el-input v-model="manualPath" placeholder="例如：/home/user/plugins/my_plugin.zip" clearable />
          <el-button type="primary" :loading="installing" @click="installManual">安装</el-button>
        </div>
      </div>
    </el-card>

    <el-card shadow="never">
      <template #header><span>已安装插件</span></template>

      <el-empty v-if="!plugins.length" description="暂无插件" />

      <div v-else class="plugin-list">
        <div v-for="plugin in plugins" :key="plugin.credential_type" class="plugin-item">
          <div class="plugin-icon">{{ plugin.icon || '🔑' }}</div>

          <div class="plugin-body">
            <div class="plugin-head">
              <span class="plugin-name">{{ plugin.name }}</span>
              <el-tag size="small" effect="plain">v{{ plugin.version }}</el-tag>
              <el-tag size="small" :type="plugin.is_builtin ? 'warning' : 'info'" effect="plain">
                {{ plugin.is_builtin ? '内置' : '用户' }}
              </el-tag>
              <el-tag v-if="!plugin.enabled" size="small" type="danger" effect="plain">已停用</el-tag>
            </div>
            <div class="plugin-desc">{{ plugin.description || '暂无描述' }}</div>
            <div class="plugin-meta">
              <span>类型：{{ plugin.credential_type }}</span>
              <span v-if="plugin.author">作者：{{ plugin.author }}</span>
              <span>凭据：{{ plugin.credential_count || 0 }} 条</span>
              <span>安装于 {{ formatDateTime(plugin.installed_at) }}</span>
            </div>
          </div>

          <div class="plugin-actions">
            <el-switch
              :model-value="plugin.enabled"
              :loading="togglingType === plugin.credential_type"
              inline-prompt
              active-text="启用"
              inactive-text="停用"
              @change="(value) => togglePlugin(plugin, value)"
            />
            <el-button
              v-if="!plugin.is_builtin"
              size="small"
              type="danger"
              plain
              :icon="Delete"
              @click="uninstall(plugin)"
            >
              卸载
            </el-button>
          </div>
        </div>
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

.plugin-upload :deep(.el-upload-dragger) {
  padding: 28px 20px;
}

.upload-icon {
  font-size: 40px;
  color: var(--el-text-color-secondary);
}

.upload-text {
  margin-top: 8px;
  font-size: 14px;
  color: var(--el-text-color-regular);
}

.upload-sub {
  margin-top: 4px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.manual-row {
  margin-top: 12px;
}

.manual-input {
  display: flex;
  gap: 10px;
  margin-top: 8px;
}

.plugin-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.plugin-item {
  display: flex;
  align-items: flex-start;
  gap: 14px;
  padding: 14px;
  border: 1px solid var(--el-border-color);
  border-radius: 8px;
  background: var(--el-fill-color-blank);
}

.plugin-icon {
  font-size: 28px;
  line-height: 1.1;
}

.plugin-body {
  flex: 1;
  min-width: 0;
}

.plugin-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.plugin-name {
  font-weight: 600;
  font-size: 15px;
}

.plugin-desc {
  margin-top: 6px;
  color: var(--el-text-color-regular);
  font-size: 13px;
}

.plugin-meta {
  margin-top: 8px;
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.plugin-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  flex: none;
}
</style>