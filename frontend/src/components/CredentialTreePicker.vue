<script setup>
/**
 * 凭据目录树勾选器：类别 → 服务 → 凭据三层，可搜索、可全选 / 清空、实时显示已选计数。
 *
 * 「备份 → 仅选中的凭据」与「系统设置 → 定时计划 → 指定凭据」共用本组件。两处要的是
 * **同一套勾选语义**，各写一份必然长歪（改了一处忘了另一处，界面上就出现两种「已选 n / m」）。
 * 勾选结果经 `v-model` 以**凭据 id 数组**双向绑定（类别/服务节点不会混进这个数组）。
 */
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { Search } from '@element-plus/icons-vue'

import { api } from '@/api'
import { useVaultStore } from '@/stores/vault'

const props = defineProps({
  modelValue: { type: Array, default: () => [] },
  /** 树区域的 max-height（px）：备份页放得开（260），定时计划对话框里要更矮 */
  maxHeight: { type: Number, default: 260 },
})
const emit = defineEmits(['update:modelValue'])

const vaultStore = useVaultStore()

const treeRef = ref(null)
const search = ref('')
const loading = ref(false)

// 目录树始终展示**全部**凭据：不能复用 vaultStore.credentials —— 那份是「凭据管理」页
// 按当前分组/筛选查出来的结果，会随那边的选择变化，导致这里只看到过滤后的子集。
// 同理也不能复用「凭据管理」的分页结果（那是纯展示层切片）。
const credentials = ref([])
const credentialCount = computed(() => credentials.value.length)
const selectedCount = computed(() => props.modelValue.length)

/** 只保留「有凭据」的服务节点，避免一堆空节点把树撑长 */
const treeData = computed(() => {
  const byType = new Map()
  credentials.value.forEach((cred) => {
    if (!byType.has(cred.credential_type)) byType.set(cred.credential_type, [])
    byType.get(cred.credential_type).push(cred)
  })
  const tree = []
  vaultStore.pluginTree.forEach((category) => {
    const services = []
    category.children.forEach((plugin) => {
      const leaves = byType.get(plugin.credential_type) || []
      if (!leaves.length) return
      services.push({
        id: plugin.id,
        label: plugin.label,
        icon: plugin.icon,
        icon_image: plugin.icon_image,
        icon_lift: plugin.icon_lift,
        count: leaves.length,
        children: leaves.map((cred) => ({
          id: `cred:${cred.id}`,
          label: cred.name,
          icon: '🔑',
        })),
      })
    })
    if (services.length) {
      tree.push({ id: category.id, label: category.label, icon: category.icon, children: services })
    }
  })
  return tree
})

/** 树上当前勾中的**凭据** id（只读叶子，故类别/服务节点不会混进来） */
function checkedIds() {
  const keys = treeRef.value ? treeRef.value.getCheckedKeys(true) : []
  return keys
    .filter((key) => String(key).startsWith('cred:'))
    .map((key) => Number(String(key).slice('cred:'.length)))
}

/** 两个 id 数组是否同一批（无序、去重、按数字比较）—— 用于识别「自己 emit 出去的回声」 */
function sameIds(a, b) {
  const norm = (list) => [...new Set((list || []).map(Number))].sort((x, y) => x - y)
  return JSON.stringify(norm(a)) === JSON.stringify(norm(b))
}

function syncChecked() {
  emit('update:modelValue', checkedIds())
}

/** 外部送进来的已选 id（编辑既有计划、或父组件重置）同步到树上 */
function applySelection(ids) {
  if (!treeRef.value) return
  treeRef.value.setCheckedKeys((ids || []).map((id) => `cred:${id}`))
}

/** 「全选」只勾**当前可见**的凭据：搜索过滤生效时不该把被隐藏的也勾上 */
function visibleLeafKeys() {
  const keyword = search.value.trim().toLowerCase()
  const keys = []
  treeData.value.forEach((category) => {
    const categoryHit = !keyword || String(category.label).toLowerCase().includes(keyword)
    category.children.forEach((service) => {
      const serviceHit = categoryHit || String(service.label).toLowerCase().includes(keyword)
      service.children.forEach((leaf) => {
        if (serviceHit || String(leaf.label).toLowerCase().includes(keyword)) keys.push(leaf.id)
      })
    })
  })
  return keys
}

function selectAll() {
  if (!treeRef.value) return
  treeRef.value.setCheckedKeys(visibleLeafKeys())
  syncChecked()
}

function clearAll() {
  if (treeRef.value) treeRef.value.setCheckedKeys([])
  emit('update:modelValue', [])
}

function filterNode(value, data) {
  if (!value) return true
  return String(data.label || '').toLowerCase().includes(String(value).toLowerCase())
}

// 「全选」在搜索态下只认可见节点，所以搜索词一变就得让筛选真正生效（el-tree 的 filter 是命令式）
watch(search, (value) => {
  if (treeRef.value) treeRef.value.filter(value)
})

// 外部改动已选值（编辑另一条计划 / 父组件清空）时同步树。自己 emit 出去的回声要忽略：
// 以 props 为唯一事实来源、单向同步，否则会来回抖。
watch(() => props.modelValue, (ids) => {
  if (sameIds(ids, checkedIds())) return
  applySelection(ids)
})

async function load() {
  loading.value = true
  try {
    // 空筛选 → 后端返回全部凭据；结果只放本地，不写回 store，免得影响「凭据管理」页。
    const items = await api.listCredentials({})
    credentials.value = Array.isArray(items) ? items : []
  } catch {
    credentials.value = []
  } finally {
    loading.value = false
  }
  // 树是 v-if 渲染的：本 tick 里 treeRef 还是空的，等 DOM 出来再回填勾选
  await nextTick()
  applySelection(props.modelValue)
}

onMounted(load)
</script>

<template>
  <div class="pick">
    <div class="pick-head">
      <el-input
        v-model="search"
        size="small"
        placeholder="搜索名称 / 类型 / 分组"
        clearable
        :prefix-icon="Search"
        class="pick-search"
      />
      <el-button size="small" @click="selectAll">全选</el-button>
      <el-button size="small" @click="clearAll">清空</el-button>
      <span class="text-muted">已选 {{ selectedCount }} / {{ credentialCount }}</span>
    </div>

    <div v-if="loading" class="text-muted pick-empty">正在加载凭据…</div>
    <el-tree
      v-else-if="treeData.length"
      ref="treeRef"
      :data="treeData"
      :props="{ label: 'label', children: 'children' }"
      node-key="id"
      show-checkbox
      default-expand-all
      :filter-node-method="filterNode"
      :style="{ maxHeight: `${maxHeight}px` }"
      class="pick-tree"
      @check="syncChecked"
    >
      <template #default="{ data }">
        <span class="pick-node">
          <img
            v-if="data.icon_image"
            class="pick-node-icon"
            :class="{ 'icon-lift': data.icon_lift }"
            :src="data.icon_image"
            alt=""
          />
          <span v-else class="pick-node-icon">{{ data.icon }}</span>
          <span>{{ data.label }}</span>
          <span v-if="data.count" class="text-muted">（{{ data.count }}）</span>
        </span>
      </template>
    </el-tree>
    <div v-else class="text-muted pick-empty">没有可选凭据</div>
  </div>
</template>

<style scoped>
.pick {
  width: 100%;
}

.pick-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.pick-search {
  width: 240px;
}

/* max-height 由 maxHeight 属性内联给出（同一套组件在备份页与定时计划里高度需求不同） */
.pick-tree {
  overflow-y: auto;
  padding: 6px 8px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
}

.pick-node {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 13px;
}

.pick-node-icon {
  width: 16px;
  height: 16px;
  object-fit: contain;
}

.pick-empty {
  font-size: 13px;
  padding: 8px 0;
}
</style>
