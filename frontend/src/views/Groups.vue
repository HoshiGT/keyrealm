<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete, Edit, Plus, Refresh } from '@element-plus/icons-vue'

import { useVaultStore } from '@/stores/vault'
import { formatDateTime } from '@/utils/format'

const vaultStore = useVaultStore()

const PRESET_COLORS = ['#409EFF', '#67C23A', '#E6A23C', '#F56C6C', '#909399', '#9B59B6', '#1ABC9C']

const dialogVisible = ref(false)
const submitting = ref(false)
const editingId = ref(null)
const formRef = ref(null)

const form = reactive({ name: '', description: '', color: '#409EFF' })

const rules = {
  name: [
    { required: true, message: '请输入分组名称', trigger: 'blur' },
    { min: 1, max: 40, message: '名称长度需在 1-40 个字符之间', trigger: 'blur' },
  ],
}

const ungroupedCount = computed(
  () => vaultStore.credentials.filter((item) => item.group_id === null || item.group_id === undefined).length,
)

const isEdit = computed(() => editingId.value !== null)

function openCreate() {
  editingId.value = null
  form.name = ''
  form.description = ''
  form.color = '#409EFF'
  dialogVisible.value = true
}

function openEdit(row) {
  editingId.value = row.id
  form.name = row.name
  form.description = row.description || ''
  form.color = row.color || '#409EFF'
  dialogVisible.value = true
}

async function submit() {
  if (!formRef.value) return
  try {
    await formRef.value.validate()
  } catch {
    return
  }

  submitting.value = true
  try {
    const payload = {
      name: form.name.trim(),
      description: form.description.trim(),
      color: form.color,
    }
    if (isEdit.value) {
      await vaultStore.updateGroup(editingId.value, payload)
      ElMessage.success('分组已更新')
    } else {
      await vaultStore.createGroup(payload)
      ElMessage.success('分组已创建')
    }
    dialogVisible.value = false
  } catch (err) {
    ElMessage.error(err.message || '保存失败')
  } finally {
    submitting.value = false
  }
}

async function remove(row) {
  try {
    await ElMessageBox.confirm(
      row.credential_count
        ? `分组「${row.name}」下有 ${row.credential_count} 条凭据，删除后这些凭据将变为「未分组」。确定删除吗？`
        : `确定要删除分组「${row.name}」吗？`,
      '删除分组',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消', confirmButtonClass: 'el-button--danger' },
    )
  } catch {
    return
  }

  try {
    await vaultStore.deleteGroup(row.id)
    ElMessage.success('分组已删除')
  } catch (err) {
    ElMessage.error(err.message || '删除失败')
  }
}

async function reload() {
  try {
    await Promise.all([vaultStore.fetchGroups(), vaultStore.fetchCredentials()])
  } catch (err) {
    ElMessage.error(err.message || '加载分组失败')
  }
}

onMounted(reload)
</script>

<template>
  <div class="page">
    <div class="page-toolbar">
      <h2 class="section-title">分组管理</h2>
      <el-tag v-if="ungroupedCount" type="info" effect="plain" size="small">未分组 {{ ungroupedCount }} 条</el-tag>
      <div class="spacer" />
      <el-button :icon="Refresh" @click="reload">刷新</el-button>
      <el-button type="primary" :icon="Plus" @click="openCreate">新建分组</el-button>
    </div>

    <el-card shadow="never">
      <el-table :data="vaultStore.groups" empty-text="暂无分组" style="width: 100%">
        <el-table-column label="分组" min-width="220">
          <template #default="{ row }">
            <div class="group-cell">
              <span class="color-dot" :style="{ background: row.color || '#409EFF' }" />
              <span class="group-name">{{ row.name }}</span>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="描述" min-width="240">
          <template #default="{ row }">
            <span class="text-muted">{{ row.description || '—' }}</span>
          </template>
        </el-table-column>

        <el-table-column label="凭据数" width="110">
          <template #default="{ row }">
            <el-tag size="small" effect="plain" type="primary">{{ row.credential_count || 0 }}</el-tag>
          </template>
        </el-table-column>

        <el-table-column label="创建时间" width="170">
          <template #default="{ row }">
            <span class="text-muted">{{ formatDateTime(row.created_at) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="180" fixed="right">
          <template #default="{ row }">
            <el-button size="small" :icon="Edit" @click="openEdit(row)">编辑</el-button>
            <el-button size="small" type="danger" plain :icon="Delete" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog
      v-model="dialogVisible"
      :title="isEdit ? '编辑分组' : '新建分组'"
      width="480px"
      :close-on-click-modal="false"
    >
      <el-form ref="formRef" :model="form" :rules="rules" label-position="top">
        <el-form-item label="分组名称" prop="name">
          <el-input v-model="form.name" placeholder="例如：工作项目" clearable maxlength="40" />
        </el-form-item>

        <el-form-item label="描述">
          <el-input v-model="form.description" type="textarea" :rows="3" placeholder="可选，说明该分组的用途" />
        </el-form-item>

        <el-form-item label="颜色标记">
          <el-color-picker v-model="form.color" :predefine="PRESET_COLORS" />
          <span class="color-hint">{{ form.color }}</span>
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.section-title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
}

.group-cell {
  display: flex;
  align-items: center;
  gap: 10px;
}

.color-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  flex: none;
}

.group-name {
  font-weight: 600;
}

.color-hint {
  margin-left: 10px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
</style>