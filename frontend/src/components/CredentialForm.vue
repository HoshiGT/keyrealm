<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

import { useVaultStore } from '@/stores/vault'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  credential: { type: Object, default: null },
  defaultGroupId: { type: [Number, String], default: null },
  defaultType: { type: String, default: '' },
})

const emit = defineEmits(['update:modelValue', 'saved'])

const vaultStore = useVaultStore()

const formRef = ref(null)
const submitting = ref(false)

const form = reactive({
  name: '',
  credential_type: '',
  group_id: null,
  auto_verify: true,
  data: {},
})

const isEdit = computed(() => !!(props.credential && props.credential.id))

const plugins = computed(() => vaultStore.enabledPlugins)

const groupOptions = computed(() => vaultStore.groupOptions)

const typeOptions = computed(() =>
  plugins.value.map((plugin) => ({
    label: `${plugin.icon || '🔑'} ${plugin.name}`,
    value: plugin.credential_type,
  })),
)

const currentPlugin = computed(
  () => plugins.value.find((plugin) => plugin.credential_type === form.credential_type) || null,
)

const schema = computed(() => (currentPlugin.value && currentPlugin.value.fields_schema) || [])

const rules = computed(() => {
  const result = {
    name: [
      { required: true, message: '请输入凭据名称', trigger: 'blur' },
      { min: 1, max: 100, message: '名称长度需在 1-100 个字符之间', trigger: 'blur' },
    ],
    credential_type: [{ required: true, message: '请选择凭据类型', trigger: 'change' }],
  }
  schema.value.forEach((field) => {
    if (!field.required) return
    const trigger = field.type === 'select' || field.type === 'checkbox' ? 'change' : 'blur'
    result[`data.${field.key}`] = [
      { required: true, message: `请填写${field.label}`, trigger },
    ]
  })
  return result
})

function coerce(field, value) {
  if (field.type === 'checkbox') return value === true || value === 'true'
  if (field.type === 'number') {
    if (value === '' || value === null || value === undefined) return null
    const parsed = Number(value)
    return Number.isFinite(parsed) ? parsed : null
  }
  return value === null || value === undefined ? '' : value
}

function schemaDefaults(list, source = {}) {
  const data = {}
  list.forEach((field) => {
    if (Object.prototype.hasOwnProperty.call(source, field.key)) {
      data[field.key] = coerce(field, source[field.key])
      return
    }
    if (field.type === 'checkbox') {
      data[field.key] = field.default === true || field.default === 'true'
      return
    }
    if (field.type === 'number') {
      data[field.key] = field.default === null || field.default === undefined ? null : Number(field.default)
      return
    }
    data[field.key] = field.default === null || field.default === undefined ? '' : field.default
  })
  return data
}

function resetForm() {
  const existing = props.credential
  if (existing && existing.id) {
    form.name = existing.name || ''
    form.credential_type = existing.credential_type || ''
    form.group_id = existing.group_id ?? null
    form.auto_verify = true
    const plugin = vaultStore.pluginByType[existing.credential_type]
    form.data = schemaDefaults((plugin && plugin.fields_schema) || [], existing.data_plain || {})
  } else {
    const initialType = props.defaultType || (plugins.value[0] && plugins.value[0].credential_type) || ''
    form.name = ''
    form.credential_type = initialType
    form.group_id = props.defaultGroupId ?? null
    form.auto_verify = true
    const plugin = plugins.value.find((item) => item.credential_type === initialType)
    form.data = schemaDefaults((plugin && plugin.fields_schema) || [])
  }
  if (formRef.value) formRef.value.clearValidate()
}

watch(
  () => props.modelValue,
  (visible) => {
    if (visible) resetForm()
  },
  { immediate: true },
)

// 创建模式下切换凭据类型时，按新插件的 fields_schema 重建字段。
watch(
  () => form.credential_type,
  (next, prev) => {
    if (!props.modelValue || isEdit.value) return
    if (!prev || next === prev) return
    const plugin = plugins.value.find((item) => item.credential_type === next)
    form.data = schemaDefaults((plugin && plugin.fields_schema) || [])
    if (formRef.value) formRef.value.clearValidate()
  },
)

function buildData() {
  const data = {}
  schema.value.forEach((field) => {
    data[field.key] = coerce(field, form.data[field.key])
  })
  return data
}

function close() {
  emit('update:modelValue', false)
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
      credential_type: form.credential_type,
      group_id: form.group_id ?? null,
      data: buildData(),
      auto_verify: form.auto_verify,
    }

    let saved
    if (isEdit.value) {
      saved = await vaultStore.updateCredential(props.credential.id, {
        name: payload.name,
        group_id: payload.group_id,
        data: payload.data,
      })
      if (form.auto_verify) {
        try {
          await vaultStore.verifyCredential(props.credential.id)
        } catch (err) {
          ElMessage.warning(`凭据已保存，但验证失败：${err.message}`)
        }
      }
    } else {
      saved = await vaultStore.createCredential(payload)
    }

    ElMessage.success(isEdit.value ? '凭据已更新' : '凭据已创建')
    emit('saved', saved)
    close()
  } catch (err) {
    ElMessage.error(err.message || '保存失败')
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    :title="isEdit ? '编辑凭据' : '添加凭据'"
    width="640px"
    top="6vh"
    :close-on-click-modal="false"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <el-form ref="formRef" :model="form" :rules="rules" label-position="top" class="credential-form">
      <el-form-item label="凭据名称" prop="name">
        <el-input v-model="form.name" placeholder="例如：OpenAI 生产账号" clearable maxlength="100" />
      </el-form-item>

      <el-form-item label="凭据类型" prop="credential_type">
        <el-select v-model="form.credential_type" placeholder="请选择凭据类型" :disabled="isEdit" class="full-width">
          <el-option v-for="option in typeOptions" :key="option.value" :label="option.label" :value="option.value" />
        </el-select>
      </el-form-item>

      <el-form-item label="所属分组">
        <el-select v-model="form.group_id" placeholder="未分组" clearable class="full-width">
          <el-option v-for="option in groupOptions" :key="option.value" :label="option.label" :value="option.value" />
        </el-select>
      </el-form-item>

      <el-divider v-if="schema.length" content-position="left">
        <span class="divider-text">以下字段由插件 {{ currentPlugin?.name }} 定义</span>
      </el-divider>

      <template v-for="field in schema" :key="field.key">
        <el-form-item :label="field.label" :prop="`data.${field.key}`">
          <el-input
            v-if="field.type === 'text' || field.type === 'url'"
            v-model="form.data[field.key]"
            :placeholder="field.placeholder || ''"
            clearable
          />

          <el-input
            v-else-if="field.type === 'password' || field.sensitive"
            v-model="form.data[field.key]"
            type="password"
            show-password
            :placeholder="field.placeholder || '请输入'"
          />

          <el-input
            v-else-if="field.type === 'textarea'"
            v-model="form.data[field.key]"
            type="textarea"
            :rows="3"
            :placeholder="field.placeholder || ''"
          />

          <el-select
            v-else-if="field.type === 'select'"
            v-model="form.data[field.key]"
            :placeholder="field.placeholder || '请选择'"
            class="full-width"
          >
            <el-option v-for="option in field.options || []" :key="option" :label="option" :value="option" />
          </el-select>

          <el-input-number
            v-else-if="field.type === 'number'"
            v-model="form.data[field.key]"
            :controls-position="'right'"
            class="full-width"
          />

          <el-switch v-else-if="field.type === 'checkbox'" v-model="form.data[field.key]" />

          <el-input v-else v-model="form.data[field.key]" :placeholder="field.placeholder || ''" clearable />

          <div v-if="field.sensitive" class="field-hint">敏感字段，加密存储且不会在列表中展示</div>
        </el-form-item>
      </template>

      <el-form-item>
        <el-checkbox v-model="form.auto_verify">
          {{ isEdit ? '保存后重新验证' : '保存后自动验证' }}
        </el-checkbox>
      </el-form-item>
    </el-form>

    <template #footer>
      <el-button @click="close">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="submit">保存</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.full-width {
  width: 100%;
}

.divider-text {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.field-hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  line-height: 1.6;
  margin-top: 2px;
}

.credential-form :deep(.el-form-item) {
  margin-bottom: 16px;
}
</style>