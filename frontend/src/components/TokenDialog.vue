<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { DocumentCopy, WarningFilled } from '@element-plus/icons-vue'

import { useMcpStore } from '@/stores/mcp'
import { copyText } from '@/utils/clipboard'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  rotating: { type: Boolean, default: false },
})

const emit = defineEmits(['update:modelValue', 'created'])

const mcpStore = useMcpStore()

const formRef = ref(null)
const step = ref('form')
const submitting = ref(false)
const createdToken = ref(null)

const form = reactive({
  scopeMode: 'inherit',
  tools_allowed: [],
  expiresMode: 'never',
  customDays: 30,
})

const rules = {}

const toolOptions = computed(() => mcpStore.toolOptions)

const expiresDays = computed(() => {
  if (form.expiresMode === 'never') return null
  if (form.expiresMode === 'custom') return Number(form.customDays) || null
  return Number(form.expiresMode)
})

function reset() {
  step.value = 'form'
  createdToken.value = null
  const current = mcpStore.token && mcpStore.token.tools_allowed
  form.scopeMode = current && current.length ? 'custom' : 'inherit'
  form.tools_allowed = current ? [...current] : []
  form.expiresMode = 'never'
  form.customDays = 30
  if (formRef.value) formRef.value.clearValidate()
}

watch(
  () => props.modelValue,
  (visible) => {
    if (visible) reset()
  },
)

async function submit() {
  submitting.value = true
  try {
    const result = await mcpStore.rotateToken({
      tools_allowed: form.scopeMode === 'custom' ? [...form.tools_allowed] : [],
      expires_days: expiresDays.value,
    })
    createdToken.value = result
    step.value = 'reveal'
    emit('created', result)
  } catch (err) {
    ElMessage.error(err.message || (props.rotating ? '轮换 Token 失败' : '生成 Token 失败'))
  } finally {
    submitting.value = false
  }
}

async function handleCopy() {
  const ok = await copyText(createdToken.value && createdToken.value.token)
  if (ok) ElMessage.success('Token 已复制到剪贴板')
  else ElMessage.warning('复制失败，请手动选中并复制')
}

function close() {
  emit('update:modelValue', false)
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    :title="step === 'form'
      ? (rotating ? '轮换 Token' : '生成 Token')
      : (rotating ? 'Token 已轮换' : 'Token 已生成')"
    width="560px"
    :close-on-click-modal="false"
    :close-on-press-escape="step === 'form'"
    :show-close="step === 'form'"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <el-form v-if="step === 'form'" ref="formRef" :model="form" :rules="rules" label-position="top">
      <el-alert v-if="rotating" type="warning" :closable="false" class="mb-12">
        <template #title>轮换后当前 Token 立即失效，请及时更新已配置的客户端。</template>
      </el-alert>

      <el-form-item label="工具范围">
        <el-radio-group v-model="form.scopeMode">
          <el-radio value="inherit">继承全局权限（推荐）</el-radio>
          <el-radio value="custom">自定义范围</el-radio>
        </el-radio-group>
      </el-form-item>

      <el-form-item v-if="form.scopeMode === 'custom'" label="允许调用的工具">
        <el-select
          v-model="form.tools_allowed"
          multiple
          filterable
          collapse-tags
          collapse-tags-tooltip
          placeholder="请选择允许的工具"
          class="full-width"
        >
          <el-option
            v-for="option in toolOptions"
            :key="option.value"
            :label="option.label"
            :value="option.value"
          >
            <span>{{ option.label }}</span>
            <span class="option-desc">{{ option.description }}</span>
          </el-option>
        </el-select>
      </el-form-item>

      <el-form-item label="过期时间">
        <el-radio-group v-model="form.expiresMode">
          <el-radio value="never">永不过期</el-radio>
          <el-radio value="30">30 天</el-radio>
          <el-radio value="90">90 天</el-radio>
          <el-radio value="custom">自定义</el-radio>
        </el-radio-group>
        <el-input-number
          v-if="form.expiresMode === 'custom'"
          v-model="form.customDays"
          :min="1"
          :max="3650"
          :controls-position="'right'"
          class="custom-days"
        />
        <span v-if="form.expiresMode === 'custom'" class="inline-hint">天后过期</span>
      </el-form-item>
    </el-form>

    <div v-else class="reveal">
      <el-alert type="warning" :closable="false" show-icon>
        <template #title>请立即复制，关闭后无法再次查看该 Token 明文</template>
      </el-alert>

      <div class="token-row">
        <el-input :model-value="createdToken?.token || ''" readonly class="token-input" />
        <el-button type="primary" :icon="DocumentCopy" @click="handleCopy">复制</el-button>
      </div>

      <div class="token-meta">
        <p><span>前缀：</span>{{ createdToken?.token_prefix }}…</p>
        <p class="warn-text">
          <el-icon><WarningFilled /></el-icon>
          Token 等同于密码，请勿写入代码仓库或公开渠道。
        </p>
      </div>
    </div>

    <template #footer>
      <template v-if="step === 'form'">
        <el-button @click="close">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submit">
          {{ rotating ? '轮换' : '生成' }}
        </el-button>
      </template>
      <el-button v-else type="primary" @click="close">我已保存，关闭</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.full-width {
  width: 100%;
}

.option-desc {
  float: right;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.custom-days {
  margin-left: 12px;
  width: 130px;
}

.inline-hint {
  margin-left: 8px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.reveal {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.token-row {
  display: flex;
  gap: 8px;
}

.token-input :deep(.el-input__inner) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}

.token-meta p {
  margin: 4px 0;
  font-size: 13px;
  color: var(--el-text-color-regular);
}

.token-meta span {
  color: var(--el-text-color-secondary);
}

.warn-text {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--el-color-warning);
}

.mb-12 {
  margin-bottom: 12px;
}
</style>