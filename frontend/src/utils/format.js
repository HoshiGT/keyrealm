/**
 * 时间格式化工具。
 *
 * 后端返回的 datetime 由 pydantic 序列化为 ISO 字符串（可能不带时区），
 * 这里统一按本地时间解析。
 */

export function parseDate(value) {
  if (!value) return null
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value
  const text = String(value).trim()
  if (!text) return null
  const date = new Date(text)
  return Number.isNaN(date.getTime()) ? null : date
}

function pad(n) {
  return String(n).padStart(2, '0')
}

/** 2026-10-03 14:22 */
export function formatDateTime(value, fallback = '—') {
  const date = parseDate(value)
  if (!date) return fallback
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

/** 2026-10-03 14:22:07 */
export function formatDateTimeFull(value, fallback = '—') {
  const date = parseDate(value)
  if (!date) return fallback
  return `${formatDateTime(date)}:${pad(date.getSeconds())}`
}

/** 刚刚 / 5 分钟前 / 3 小时前 / 2 天前 / 2026-10-03 */
export function formatRelative(value, fallback = '从未') {
  const date = parseDate(value)
  if (!date) return fallback
  const diff = Date.now() - date.getTime()
  if (diff < 0) {
    const ahead = Math.abs(diff)
    if (ahead < 60_000) return '即将'
    if (ahead < 3_600_000) return `${Math.floor(ahead / 60_000)} 分钟后`
    if (ahead < 86_400_000) return `${Math.floor(ahead / 3_600_000)} 小时后`
    return `${Math.floor(ahead / 86_400_000)} 天后`
  }
  if (diff < 10_000) return '刚刚'
  if (diff < 60_000) return `${Math.floor(diff / 1000)} 秒前`
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`
  if (diff < 30 * 86_400_000) return `${Math.floor(diff / 86_400_000)} 天前`
  return formatDateTime(date)
}

/** 12.4 ms / 1.2 s */
export function formatDuration(ms) {
  const value = Number(ms)
  if (!Number.isFinite(value) || value <= 0) return '—'
  if (value < 1000) return `${Math.round(value)} ms`
  return `${(value / 1000).toFixed(1)} s`
}

const STATUS_TEXT = {
  valid: '有效',
  invalid: '失效',
  error: '错误',
  unknown: '未验证',
}

export function statusText(status) {
  return STATUS_TEXT[status] || status || '未验证'
}

export function statusTagType(status) {
  switch (status) {
    case 'valid':
      return 'success'
    case 'invalid':
      return 'danger'
    case 'error':
      return 'warning'
    default:
      return 'info'
  }
}

export const STATUS_OPTIONS = [
  { label: '有效', value: 'valid' },
  { label: '失效', value: 'invalid' },
  { label: '错误', value: 'error' },
  { label: '未验证', value: 'unknown' },
]

/**
 * 归一化 manifest 里 `select` 字段的 options。
 *
 * 插件的 options 允许两种写法：
 *   - 纯字符串数组：`["api_key", "chatgpt"]` → 值与显示都是它自己；
 *   - 对象数组：`[{ value: "chatgpt", label: "ChatGPT 登录态" }]` → 显示 label、存 value。
 * 早期只支持字符串，导致「值必须可读」——想让用户看懂就只能把中文当值存库（既难看又难匹配）。
 * 统一在这里归一，渲染处一律用 value/label。
 */
export function normalizeOptions(options) {
  if (!Array.isArray(options)) return []
  return options.map((option) => {
    if (option && typeof option === 'object') {
      return { value: option.value, label: option.label ?? String(option.value ?? '') }
    }
    return { value: option, label: String(option) }
  })
}