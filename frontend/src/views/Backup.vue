<script setup>
import { ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Download, Upload } from '@element-plus/icons-vue'

import api from '@/api'

const scope = ref('all')
const encrypted = ref(true)
const exporting = ref(false)
const importing = ref(false)
const lastResult = ref('')

async function doExport() {
  exporting.value = true
  try {
    const res = await api.exportBackup(scope.value, encrypted.value)
    if (res && res.ok === false) {
      ElMessage.info(res.message || '已取消')
      return
    }
    lastResult.value = `已导出 ${res.count} 条凭据（${res.encrypted ? '加密' : '明文'}）→ ${res.path}`
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
      `新建分组 ${res.created_groups}）`
    ElMessage.success('导入完成')
  } catch (err) {
    ElMessage.error(err.message || '导入失败')
  } finally {
    importing.value = false
  }
}
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
            <el-radio value="all">全部（凭据 + 分组 + 偏好设置）</el-radio>
            <el-radio value="credentials">仅凭据</el-radio>
          </el-radio-group>
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
      </el-alert>

      <div class="actions">
        <el-button type="primary" :icon="Download" :loading="exporting" @click="doExport">
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
</style>
