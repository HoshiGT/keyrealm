<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete, Plus, Refresh, Search } from '@element-plus/icons-vue'

import { useVaultStore } from '@/stores/vault'

const vaultStore = useVaultStore()
const loading = ref(false)
const available = ref([])
const pluginSearch = ref('')

const installedGroups = computed(() => {
  const groups = {}
  vaultStore.plugins.forEach((p) => {
    const key = p.category || '(根目录)'
    if (!groups[key]) groups[key] = { key, name: p.category_name || key, items: [] }
    groups[key].items.push(p)
  })
  return Object.values(groups)
})

// 搜索词按空格拆分为多个关键词，需全部命中（AND）
const searchTerms = computed(() =>
  pluginSearch.value.trim().toLowerCase().split(/\s+/).filter(Boolean),
)

function matchesSearch(item) {
  if (!searchTerms.value.length) return true
  const haystack = [item.name, item.credential_type, item.description, item.category_name]
    .filter(Boolean)
    .join(' ')
    .toLowerCase()
  return searchTerms.value.every((term) => haystack.includes(term))
}

// 过滤只影响展示：组内无匹配项时整组丢弃，不触碰任何选中集合（本页无选择状态）
const filteredInstalledGroups = computed(() =>
  installedGroups.value
    .map((group) => ({ ...group, items: group.items.filter(matchesSearch) }))
    .filter((group) => group.items.length),
)

const filteredAvailable = computed(() => available.value.filter(matchesSearch))

// 两个卡片各自判断「有内容但全被过滤掉」，分别给空状态提示（避免出现只有标题的空卡片）
const noInstalledMatch = computed(
  () => searchTerms.value.length > 0 && vaultStore.plugins.length > 0 && !filteredInstalledGroups.value.length,
)

const noAvailableMatch = computed(
  () => searchTerms.value.length > 0 && available.value.length > 0 && !filteredAvailable.value.length,
)

async function load() {
  loading.value = true
  try {
    await vaultStore.fetchPlugins()
    available.value = await vaultStore.fetchAvailablePlugins()
  } catch (err) {
    ElMessage.error(err.message || '加载插件失败')
  } finally {
    loading.value = false
  }
}

async function remove(plugin) {
  try {
    await ElMessageBox.confirm(
      `将删除插件「${plugin.name}」及其全部凭据（不可恢复，除非稍后重新添加该插件）。`,
      '删除插件',
      {
        type: 'warning',
        confirmButtonText: '删除',
        cancelButtonText: '取消',
        confirmButtonClass: 'el-button--danger',
      },
    )
  } catch {
    return
  }
  try {
    const result = await vaultStore.deletePlugin(plugin.credential_type)
    ElMessage.success(
      `已删除「${result.name || plugin.name}」，同时删除 ${result.deleted_credentials} 条凭据`,
    )
    await load()
  } catch (err) {
    ElMessage.error(err.message || '删除失败')
  }
}

async function add(item) {
  try {
    await vaultStore.installPlugin(item.category, item.service)
    ElMessage.success(`已添加「${item.name}」`)
    await load()
  } catch (err) {
    ElMessage.error(err.message || '添加失败')
  }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="page-toolbar">
      <h2 class="section-title">插件管理</h2>
      <span class="text-muted">已安装 {{ vaultStore.plugins.length }} 个</span>
      <div class="spacer" />
      <el-input
        v-model="pluginSearch"
        class="plugin-search"
        placeholder="搜索插件名 / 标识 / 描述"
        clearable
        :prefix-icon="Search"
      />
      <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
    </div>

    <el-card v-loading="loading" shadow="never">
      <template #header><span>已安装插件</span></template>

      <div v-for="group in filteredInstalledGroups" :key="group.key" class="pm-group">
        <div class="pm-cat">{{ group.name }}</div>
        <div v-for="plugin in group.items" :key="plugin.credential_type" class="pm-row">
          <img v-if="plugin.icon_image" class="pm-icon" :src="plugin.icon_image" alt="" />
          <span v-else class="pm-icon-emoji">{{ plugin.icon }}</span>
          <div class="pm-main">
            <div class="pm-name">{{ plugin.name }}</div>
            <div class="pm-sub text-muted">
              {{ plugin.credential_type }} · {{ plugin.credential_count || 0 }} 条凭据
            </div>
          </div>
          <el-button type="danger" plain size="small" :icon="Delete" @click="remove(plugin)">
            删除
          </el-button>
        </div>
      </div>

      <el-empty v-if="!vaultStore.plugins.length" description="暂无已安装插件" :image-size="60" />
      <el-empty
        v-else-if="noInstalledMatch"
        description="没有匹配的已安装插件"
        :image-size="60"
      />
    </el-card>

    <el-card v-if="filteredAvailable.length || noAvailableMatch" shadow="never">
      <template #header><span>可添加（内置源）</span></template>
      <el-empty
        v-if="noAvailableMatch"
        description="内置源里没有匹配的插件"
        :image-size="60"
      />
      <div v-for="item in filteredAvailable" :key="item.credential_type" class="pm-row">
        <img v-if="item.icon_image" class="pm-icon" :src="item.icon_image" alt="" />
        <span v-else class="pm-icon-emoji">{{ item.icon }}</span>
        <div class="pm-main">
          <div class="pm-name">{{ item.name }}</div>
          <div class="pm-sub text-muted">
            {{ item.credential_type }} · {{ item.category_name }}
          </div>
        </div>
        <el-button type="primary" plain size="small" :icon="Plus" @click="add(item)">
          添加
        </el-button>
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.pm-group + .pm-group {
  margin-top: 12px;
}

.pm-cat {
  margin-bottom: 4px;
  font-size: 12px;
  font-weight: 600;
  color: var(--el-text-color-secondary);
}

.pm-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 4px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.pm-row:last-child {
  border-bottom: none;
}

.pm-icon {
  width: 22px;
  height: 22px;
  object-fit: contain;
  flex-shrink: 0;
}

.pm-icon-emoji {
  font-size: 18px;
  line-height: 1;
  flex-shrink: 0;
}

.pm-main {
  flex: 1;
  min-width: 0;
}

.pm-name {
  font-weight: 500;
}

.pm-sub {
  font-size: 12px;
  margin-top: 2px;
}

.plugin-search {
  width: 240px;
}
</style>
