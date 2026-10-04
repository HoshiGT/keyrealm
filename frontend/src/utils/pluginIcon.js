// 插件图标工具：manifest 的 icon 既可以是 emoji，也可以是指向插件目录内图片的
// 文件名（如 logo.png）。后者由后端读取为 icon_image（data URL）单独展示，
// 绝不能当作文字渲染，否则会出现 "logo.png DeepSeek" 这类标签。

const IMAGE_EXT_RE = /\.(png|jpe?g|svg|webp|ico|gif)$/i

/** icon 是否为图片文件名（而非 emoji 等内联字符）。 */
export function isImageIcon(icon) {
  return typeof icon === 'string' && IMAGE_EXT_RE.test(icon.trim())
}

/** 返回可直接内联显示的图标字符；若 icon 是图片文件名或为空，则回退到 fallback。 */
export function inlineIcon(icon, fallback = '🔑') {
  const value = typeof icon === 'string' ? icon.trim() : ''
  return value && !isImageIcon(value) ? value : fallback
}
