<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Download, Search, Upload } from '@element-plus/icons-vue'

import api from '@/api'
import { useVaultStore } from '@/stores/vault'

const vaultStore = useVaultStore()

// 备份范围：all = 全部凭据（含分组与偏好设置）；selected = 仅勾选的凭据（不含分组/偏好）
const scope = ref('all')
const encrypted = ref(true)
const exporting = ref(false)
const importing = ref(false)
const lastResult = ref('')

// 「仅选中的凭据」用目录树勾选：类别 → 服务 → 凭据三层，与「凭据管理」的目录树同源
const pickTreeRef = ref(null)
const selectedIds = ref([])
const credSearch = ref('')
const loadingCreds = ref(false)

// 目录树始终展示**全部**凭据：不能复用 vaultStore.credentials——那份是「凭据管理」页
// 按当前分组/筛选查出来的结果，会随那边的选择变化，导致这里只看到过滤后的子集。
const credentials = ref([])
const credentialCount = computed(() => credentials.value.length)

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

/** 「全选」只勾**当前可见**的凭据：搜索过滤生效时不该把被隐藏的也勾上 */
function visibleLeafKeys() {
  const keyword = credSearch.value.trim().toLowerCase()
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

/** 勾选状态由树来管，这里同步出真正的凭据 id 供导出使用 */
function syncChecked() {
  const keys = pickTreeRef.value ? pickTreeRef.value.getCheckedKeys(true) : []
  selectedIds.value = keys
    .filter((key) => String(key).startsWith('cred:'))
    .map((key) => Number(String(key).slice('cred:'.length)))
}

function selectAll() {
  if (!pickTreeRef.value) return
  pickTreeRef.value.setCheckedKeys(visibleLeafKeys())
  syncChecked()
}

function clearAll() {
  if (pickTreeRef.value) pickTreeRef.value.setCheckedKeys([])
  selectedIds.value = []
}

function filterPickNode(value, data) {
  if (!value) return true
  return String(data.label || '').toLowerCase().includes(String(value).toLowerCase())
}

watch(credSearch, (value) => {
  if (pickTreeRef.value) pickTreeRef.value.filter(value)
})

async function loadCredentials() {
  loadingCreds.value = true
  try {
    // 空 filters → 后端返回全部凭据；结果只放本地，不写回 store，免得影响「凭据管理」页。
    const items = await api.listCredentials({})
    credentials.value = Array.isArray(items) ? items : []
  } catch {
    credentials.value = []
  } finally {
    loadingCreds.value = false
  }
}

const canExport = computed(
  () => scope.value === 'all' || selectedIds.value.length > 0,
)

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
    const extras = res.scope === 'all' ? '，含分组与偏好设置' : ''
    lastResult.value =
      `已导出 ${res.count} 条凭据（${res.encrypted ? '加密' : '明文'}${extras}）→ ${res.path}`
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
      '导入会把备份中的凭据合并到当前库（相同类型且敏感值相同的会跳过）。加密备份需与当前主密码一致。是否继续？',
      '导入备份',
      { type: 'warning', confirmButtonText: '选择文件并导入', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  importing.value = true
  try {
    const res = await api.importBackup()
    if (res && res.ok === false) {
      ElMessage.info(res.message || '已取消')
      return
    }
    lastResult.value =
      `已导入 ${res.imported} 条（跳过：插件未安装 ${res.skipped_plugin}、重复 ${res.skipped_duplicate}；` +
      `新建分组 ${res.created_groups ?? 0}、新建代理 ${res.created_proxies ?? 0}）`
    ElMessage.success('导入完成')
  } catch (err) {
    ElMessage.error(err.message || '导入失败')
  } finally {
    importing.value = false
  }
}

onMounted(loadCredentials)
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
            <el-radio value="all">全部凭据（含分组与偏好设置）</el-radio>
            <el-radio value="selected">仅选中的凭据</el-radio>
          </el-radio-group>
        </el-form-item>

        <el-form-item v-if="scope === 'selected'" label="选择凭据">
          <div class="pick">
            <div class="pick-head">
              <el-input
                v-model="credSearch"
                size="small"
                placeholder="搜索名称 / 类型 / 分组"
                clearable
                :prefix-icon="Search"
                class="pick-search"
              />
              <el-button size="small" @click="selectAll">全选</el-button>
              <el-button size="small" @click="clearAll">清空</el-button>
              <span class="text-muted">
                已选 {{ selectedIds.length }} / {{ credentialCount }}
              </span>
            </div>
            <div v-if="loadingCreds" class="text-muted pick-empty">正在加载凭据…</div>
            <el-tree
              v-else-if="treeData.length"
              ref="pickTreeRef"
              :data="treeData"
              :props="{ label: 'label', children: 'children' }"
              node-key="id"
              show-checkbox
              default-expand-all
              :filter-node-method="filterPickNode"
              class="pick-tree"
              @check="syncChecked"
            >
              <template #default="{ data }">
                <span class="pick-node">
                  <img v-if="data.icon_image" class="pick-node-icon" :src="data.icon_image" alt="" />
                  <span v-else class="pick-node-icon">{{ data.icon }}</span>
                  <span>{{ data.label }}</span>
                  <span v-if="data.count" class="text-muted">（{{ data.count }}）</span>
                </span>
              </template>
            </el-tree>
            <div v-else class="text-muted pick-empty">没有可选凭据</div>
          </div>
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
        「全部凭据」会额外带上分组与偏好设置（代理 / 日志级别 / 关闭行为）；
        「仅选中的凭据」只导出勾选的条目，导入时不会改动分组与偏好设置。
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
        选择备份文件导入，会自动识别加密 / 明文。导入为合并：相同类型且敏感值相同的凭据会跳过，
        插件未安装的条目也会跳过。
      </p>
      <div class="actions">
        <el-button :icon="Upload" :loading="importing" @click="doImport">导入备份</el-button>
      </div>
    </el-card>

    <el-alert v-if="lastResult" type="success" :closable="false" class="tip">{{ lastResult }}</el-alert>
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

.pick-tree {
  max-height: 260px;
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
