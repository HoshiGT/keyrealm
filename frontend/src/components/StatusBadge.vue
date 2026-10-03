<script setup>
import { computed } from 'vue'
import { statusTagType, statusText } from '@/utils/format'

const props = defineProps({
  status: { type: String, default: 'unknown' },
  detail: { type: Object, default: () => ({}) },
  size: { type: String, default: 'default' },
})

const tagType = computed(() => statusTagType(props.status))
const text = computed(() => statusText(props.status))
const hint = computed(() => (props.detail && props.detail.message ? String(props.detail.message) : ''))
</script>

<template>
  <el-tooltip :content="hint" :disabled="!hint" placement="top">
    <el-tag :type="tagType" :size="size" effect="dark" round class="status-badge">{{ text }}</el-tag>
  </el-tooltip>
</template>

<style scoped>
.status-badge {
  font-weight: 600;
  letter-spacing: 0.02em;
}
</style>