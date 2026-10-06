/**
 * 主题注册表 —— 全部配色方案清单。
 *
 * 每个主题在 `styles/themes/<id>.css` 里以 `:root[data-theme='<id>']` 声明一套 `--v-*`
 * 令牌；这里只登记「元信息 + 色卡」，供「系统设置 → 外观」的 `ThemePicker.vue` 渲染
 * 与 localStorage 持久化。
 *
 * 增删一套主题要动三处：加/删 `styles/themes/<id>.css`、在本文件登记一项、
 * 在 `styles/index.css` 里加/删 import。深色主题还要同步 `index.html` 首帧内联脚本
 * 里的 `DARK` 表（脚本无法 import 模块），否则该主题首帧会白闪一下。
 */

export const THEMES = [
  {
    id: 'aurora',
    name: '极光',
    en: 'Aurora',
    mode: 'light',
    desc: '墨蓝紫 + 青紫极光渐变，毛玻璃面板',
    swatch: ['#4f46e5', '#a855f7', '#06b6d4'],
  },
  {
    id: 'paper',
    name: '素笺',
    en: 'Paper',
    mode: 'light',
    desc: '暖白纸感 + 墨色/陶土，近乎扁平、克制',
    swatch: ['#2b2f36', '#c2703d', '#5b7a6b'],
  },
  {
    id: 'mint',
    name: '薄荷',
    en: 'Mint',
    mode: 'light',
    desc: '青绿主色 + 大圆角 + 柔光，轻快亲和',
    swatch: ['#0f9d8c', '#34d399', '#38bdf8'],
  },
  {
    id: 'midnight',
    name: '午夜',
    en: 'Midnight',
    mode: 'dark',
    desc: '深墨蓝 + 厚毛玻璃 + 长春花紫，macOS 通透感',
    swatch: ['#7c8cf8', '#c084fc', '#67e8f9'],
  },
  {
    id: 'obsidian',
    name: '黑曜',
    en: 'Obsidian',
    mode: 'dark',
    desc: '近黑实底 + 细描边 + 青柠点缀，锐利高对比',
    swatch: ['#a3e635', '#4ade80', '#22d3ee'],
  },
  {
    id: 'nebula',
    name: '星云',
    en: 'Nebula',
    mode: 'dark',
    desc: '紫黑底 + 紫粉蓝大光晕 + 发光描边，华丽',
    swatch: ['#a78bfa', '#f472b6', '#38bdf8'],
  },
]

export const DEFAULT_THEME = 'obsidian'

/** 纯界面偏好，存 localStorage（不进后端库）。 */
export const THEME_STORAGE_KEY = 'vault.ui.theme'

export function getTheme(id) {
  return THEMES.find((t) => t.id === id) || THEMES.find((t) => t.id === DEFAULT_THEME)
}

/** 读用户上次选的主题；非法值一律回落默认。 */
export function readSavedTheme() {
  try {
    const saved = localStorage.getItem(THEME_STORAGE_KEY)
    return getTheme(saved).id
  } catch {
    return DEFAULT_THEME
  }
}
