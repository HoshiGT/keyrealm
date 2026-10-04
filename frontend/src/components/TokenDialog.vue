<script setup>
import { ElMessage } from 'element-plus'
import { DocumentCopy, WarningFilled } from '@element-plus/icons-vue'

import { copyText } from '@/utils/clipboard'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  token: { type: Object, default: null },
})

const emit = defineEmits(['update:modelValue'])

async function handleCopy() {
  const ok = await copyText(props.token && props.token.token)
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
    title="Token 已轮换"
    width="560px"
    :close-on-click-modal="false"
    :close-on-press-escape="false"
    :show-close="false"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div class="reveal">
      <el-alert type="warning" :closable="false" show-icon>
        <template #title>请立即复制，关闭后无法再次查看该 Token 明文</template>
      </el-alert>

      <div class="token-row">
        <el-input :model-value="token?.token || ''" readonly class="token-input" />
        <el-button type="primary" :icon="DocumentCopy" @click="handleCopy">复制</el-button>
      </div>

      <div class="token-meta">
        <p><span>前缀：</span>{{ token?.token_prefix }}…</p>
        <p class="warn-text">
          <el-icon><WarningFilled /></el-icon>
          Token 等同于密码，请勿写入代码仓库或公开渠道。
        </p>
      </div>
    </div>

    <template #footer>
      <el-button type="primary" @click="close">我已保存，关闭</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
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
</style>
