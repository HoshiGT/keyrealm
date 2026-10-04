<script setup>
import { computed, onMounted } from 'vue'

import { useAppStore } from '@/stores/app'
import { useVaultStore } from '@/stores/vault'

const APP_VERSION = '0.1.0'

const appStore = useAppStore()
const vaultStore = useVaultStore()

const isDesktop = computed(() => typeof window !== 'undefined' && !!window.pywebview)
const runtimeLabel = computed(() =>
  isDesktop.value ? '桌面端（pywebview）' : '浏览器预览（开发模式）',
)
const categoryCount = computed(
  () => new Set(vaultStore.plugins.map((p) => p.category).filter(Boolean)).size,
)

onMounted(() => {
  if (!vaultStore.plugins.length) vaultStore.fetchPlugins().catch(() => {})
})
</script>

<template>
  <div class="page">
    <div class="page-toolbar">
      <h2 class="section-title">关于</h2>
    </div>

    <el-card shadow="never">
      <div class="about-brand">
        <img src="/favicon.svg" alt="Credential Vault" class="about-logo" />
        <div>
          <div class="about-name">Credential Vault</div>
          <div class="about-sub text-muted">本地凭据保险库 · 版本 v{{ APP_VERSION }}</div>
        </div>
      </div>

      <el-descriptions :column="2" border size="small" class="about-desc">
        <el-descriptions-item label="项目名称">Credential Vault（凭据保险库）</el-descriptions-item>
        <el-descriptions-item label="版本号">v{{ APP_VERSION }}</el-descriptions-item>
        <el-descriptions-item label="运行环境">{{ runtimeLabel }}</el-descriptions-item>
        <el-descriptions-item label="当前状态">
          {{ appStore.unlocked ? '已解锁' : '已锁定' }}
        </el-descriptions-item>
        <el-descriptions-item label="内置插件">
          {{ vaultStore.plugins.length }} 个 · {{ categoryCount }} 个类别
        </el-descriptions-item>
        <el-descriptions-item label="MCP 服务">
          {{ appStore.tray.mcp_running ? `运行中 · 端口 ${appStore.tray.mcp_port}` : '已停止' }}
        </el-descriptions-item>
        <el-descriptions-item label="前端技术栈">
          Vue 3 · Element Plus · Pinia · Vite
        </el-descriptions-item>
        <el-descriptions-item label="后端技术栈">
          Python 3.11+ · pywebview · SQLite · cryptography
        </el-descriptions-item>
      </el-descriptions>

      <el-alert type="info" :closable="false" show-icon class="about-alert">
        <template #title>数据存储与安全</template>
        <template #default>
          所有凭据仅保存在本机数据库中，使用由主密码派生的密钥加密（AES-256-GCM）。
          除验证时向对应服务商发起请求外，任何凭据明文都不会离开本机；
          通过 MCP 提供给 AI Agent 的明文访问受工具权限与 Token 范围双重控制。
        </template>
      </el-alert>
    </el-card>
  </div>
</template>

<style scoped>
.about-brand {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}

.about-logo {
  width: 40px;
  height: 40px;
  border-radius: 10px;
}

.about-name {
  font-size: 17px;
  font-weight: 700;
}

.about-sub {
  font-size: 12px;
  margin-top: 2px;
}

.about-alert {
  margin-top: 16px;
}
</style>
