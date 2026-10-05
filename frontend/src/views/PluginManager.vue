<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowDown, ArrowRight, Delete, Plus, Refresh, Search, SetUp } from '@element-plus/icons-vue'

import ScopeDefaultsDialog from '@/components/ScopeDefaultsDialog.vue'
import { useVaultStore } from '@/stores/vault'

const vaultStore = useVaultStore()
const loading = ref(false)
const available = ref([])
const pluginSearch = ref('')

const installedGroups = computed(() => {
  const groups = {}
  vaultStore.plugins.forEach((p) => {
    const key = p.category || '(根目录)'
    if (!groups[key]) {
      groups[key] = { key, name: p.category_name || key, category_icon: p.category_icon || null, items: [] }
    }
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

// 「可添加」按类别分组。折叠 key 加 avail: 前缀，与已安装插件的类别 key 区分，共用同一份持久化。
const AVAIL_KEY_PREFIX = 'avail:'

const availableGroups = computed(() => {
  const groups = {}
  filteredAvailable.value.forEach((item) => {
    const key = item.category || '(根目录)'
    if (!groups[key]) {
      groups[key] = {
        key,
        collapseKey: AVAIL_KEY_PREFIX + key,
        name: item.category_name || key,
        category_icon: item.category_icon || null,
        items: [],
      }
    }
    groups[key].items.push(item)
  })
  return Object.values(groups)
})

// ── 类别折叠：状态持久化到 localStorage（键内同时存放已安装与可添加两类 key）──
const COLLAPSED_CATS_KEY = 'vault.plugins.collapsedCats'

function readCollapsedCats() {
  try {
    const saved = JSON.parse(localStorage.getItem(COLLAPSED_CATS_KEY) || '[]')
    return Array.isArray(saved) ? saved : []
  } catch {
    return []
  }
}

const collapsedCats = ref(readCollapsedCats())

function persistCollapsedCats() {
  try {
    localStorage.setItem(COLLAPSED_CATS_KEY, JSON.stringify(collapsedCats.value))
  } catch {
    // 隐私模式等写入失败时静默降级，仅影响持久化
  }
}

// 搜索时忽略折叠状态、强制展开命中类别，避免「搜到了却看不见」
function isCatCollapsed(key) {
  if (pluginSearch.value.trim()) return false
  return collapsedCats.value.includes(key)
}

function toggleCat(key) {
  const index = collapsedCats.value.indexOf(key)
  if (index === -1) collapsedCats.value.push(key)
  else collapsedCats.value.splice(index, 1)
  persistCollapsedCats()
}

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

// ── 类别 / 插件默认设置（与「凭据目录」右键共用同一个对话框）──
const scopeDialog = ref({ visible: false, scope: 'plugin', scopeKey: '', credentialType: '', title: '' })

function openDefaults(scope, scopeKey, credentialType, name) {
  if (!scopeKey) return
  scopeDialog.value = {
    visible: true,
    scope,
    scopeKey,
    credentialType,
    title: `${scope === 'category' ? '类别' : '插件'}默认设置：${name}`,
  }
}
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
        <div class="pm-cat" @click="toggleCat(group.key)">
          <el-icon class="pm-cat-arrow">
            <ArrowDown v-if="!isCatCollapsed(group.key)" />
            <ArrowRight v-else />
          </el-icon>
          <span class="pm-cat-icon">
            <img
              v-if="!group.category_icon && group.items[0].icon_image"
              :src="group.items[0].icon_image"
              alt=""
            />
            <template v-else>{{ group.category_icon || group.items[0].icon || '📁' }}</template>
          </span>
          <span class="pm-cat-name">{{ group.name }}（{{ group.items.length }}）</span>
          <span class="pm-cat-actions" @click.stop>
            <el-tooltip content="类别默认设置（新建凭据时预填）" placement="top">
              <el-button
                text
                size="small"
                :icon="SetUp"
                @click="openDefaults('category', group.key, '', group.name)"
              />
            </el-tooltip>
          </span>
        </div>
        <template v-if="!isCatCollapsed(group.key)">
          <div v-for="plugin in group.items" :key="plugin.credential_type" class="pm-row">
            <img v-if="plugin.icon_image" class="pm-icon" :src="plugin.icon_image" alt="" />
            <span v-else class="pm-icon-emoji">{{ plugin.icon }}</span>
            <div class="pm-main">
              <div class="pm-name">{{ plugin.name }}</div>
              <div class="pm-sub text-muted">
                {{ plugin.credential_type }} · {{ plugin.credential_count || 0 }} 条凭据
              </div>
            </div>
            <el-tooltip content="插件默认设置（覆盖类别级）" placement="top">
              <el-button
                text
                size="small"
                :icon="SetUp"
                @click="openDefaults('plugin', plugin.credential_type, plugin.credential_type, plugin.name)"
              />
            </el-tooltip>
            <el-button type="danger" plain size="small" :icon="Delete" @click="remove(plugin)">
              删除
            </el-button>
          </div>
        </template>
      </div>

      <el-empty v-if="!vaultStore.plugins.length" description="暂无已安装插件" :image-size="60" />
      <el-empty
        v-else-if="noInstalledMatch"
        description="没有匹配的已安装插件"
        :image-size="60"
      />
    </el-card>

    <el-card v-if="availableGroups.length || noAvailableMatch" shadow="never">
      <template #header><span>可添加（内置源）</span></template>
      <el-empty
        v-if="noAvailableMatch"
        description="内置源里没有匹配的插件"
        :image-size="60"
      />
      <div v-for="group in availableGroups" :key="group.collapseKey" class="pm-group">
        <div class="pm-cat" @click="toggleCat(group.collapseKey)">
          <el-icon class="pm-cat-arrow">
            <ArrowDown v-if="!isCatCollapsed(group.collapseKey)" />
            <ArrowRight v-else />
          </el-icon>
          <span class="pm-cat-icon">
            <img
              v-if="!group.category_icon && group.items[0].icon_image"
              :src="group.items[0].icon_image"
              alt=""
            />
            <template v-else>{{ group.category_icon || group.items[0].icon || '📁' }}</template>
          </span>
          <span class="pm-cat-name">{{ group.name }}（{{ group.items.length }}）</span>
          <!-- 预留：类别默认设置按钮 -->
          <span class="pm-cat-actions" @click.stop />
        </div>
        <template v-if="!isCatCollapsed(group.collapseKey)">
          <div v-for="item in group.items" :key="item.credential_type" class="pm-row">
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
        </template>
      </div>
    </el-card>

    <ScopeDefaultsDialog
      v-model="scopeDialog.visible"
      :scope="scopeDialog.scope"
      :scope-key="scopeDialog.scopeKey"
      :credential-type="scopeDialog.credentialType"
      :title="scopeDialog.title"
    />
  </div>
</template>

<style scoped>
.pm-group + .pm-group {
  margin-top: 12px;
}

.pm-cat {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 4px;
  padding: 4px;
  font-size: 12px;
  font-weight: 600;
  color: var(--el-text-color-secondary);
  cursor: pointer;
  user-select: none;
  border-radius: 4px;
}

.pm-cat:hover {
  background: var(--el-fill-color-light);
}

.pm-cat-arrow {
  font-size: 12px;
  color: var(--el-text-color-placeholder);
}

.pm-cat-icon {
  display: inline-flex;
  align-items: center;
}

.pm-cat-icon img {
  width: 16px;
  height: 16px;
  object-fit: contain;
}

.pm-cat-actions {
  margin-left: auto;
  display: flex;
  gap: 4px;
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
