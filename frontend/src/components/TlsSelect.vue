<script setup>
/**
 * 「TLS 版本」下拉 —— 凭据 / 作用域预设 / 系统设置三处共用。
 *
 * 值写进保留键 `_tls_version`：空串 = 跟随上层；`auto` = 先标准协商、握手失败回退 1.2；
 * `1.0`~`1.3` = 锁死版本。选项列表由后端下发（`tls_versions`）。
 */
const props = defineProps({
  modelValue: { type: String, default: '' },
  disabled: { type: Boolean, default: false },
  // 「跟随」那一项怎么写。凭据里写「跟随上层设置」，作用域预设里写「不在此层指定」。
  followLabel: { type: String, default: '跟随上层设置' },
  // 括注：跟随之后实际会用哪个版本（由后端按「插件预设 > 类别预设 > 全局」解析好）
  inheritHint: { type: String, default: '' },
  versions: { type: Array, default: () => ['auto', '1.0', '1.1', '1.2', '1.3'] },
})

const emit = defineEmits(['update:modelValue'])

const LABELS = {
  auto: 'auto（自动：先 TLS 1.3，握手失败回退 1.2）',
  '1.0': '固定 TLS 1.0',
  '1.1': '固定 TLS 1.1',
  '1.2': '固定 TLS 1.2',
  '1.3': '固定 TLS 1.3',
}

const labelOf = (value) => LABELS[value] || String(value)
</script>

<template>
  <el-select
    :model-value="modelValue"
    :disabled="disabled"
    class="full-width"
    :placeholder="followLabel"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <el-option value="" :label="`${followLabel}（${inheritHint || '全局默认'}）`" />
    <el-option v-for="v in versions" :key="v" :value="v" :label="labelOf(v)" />
  </el-select>
</template>
