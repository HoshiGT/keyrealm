import { getTheme, readSavedTheme, THEME_STORAGE_KEY } from './themes'

/**
 * 把主题写到 <html> 上。
 *
 * 三处副作用都在这里，别处不要重复：
 *   · `data-theme`      → 命中 `:root[data-theme='x']` 的令牌块
 *   · `.dark` class     → Element Plus 内置深色变量表按此生效
 *   · `color-scheme`    → 让原生控件（滚动条、日期选择器）跟着变深浅
 */
export function applyTheme(id, { persist = true } = {}) {
  const theme = getTheme(id)
  const root = document.documentElement

  root.dataset.theme = theme.id
  root.classList.toggle('dark', theme.mode === 'dark')
  root.style.colorScheme = theme.mode
  root.style.background = ''

  if (persist) {
    try {
      localStorage.setItem(THEME_STORAGE_KEY, theme.id)
    } catch {
      // 隐私模式下 localStorage 可能不可写；主题退化为「本次会话有效」。
    }
  }

  return theme
}

/** 启动时按上次选择还原（不重复写存储）。 */
export function initTheme() {
  return applyTheme(readSavedTheme(), { persist: false })
}
