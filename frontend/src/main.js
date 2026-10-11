import { createApp } from 'vue'
import { createPinia } from 'pinia'

// Element Plus 的组件本体与样式由 unplugin-vue-components 按需引入（见 vite.config.js），
// 这里不再 app.use(ElementPlus)、也不再引 element-plus/dist/index.css。
//
// 例外：命令式服务 ElMessage / ElMessageBox 是 JS 调用而非模板标签，解析器扫不到它们，
// 必须手动引入样式入口。es/components/<name>/style/css 会自动带上该组件的依赖
// （base / badge / input / button / overlay），不必逐个补。
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
// 暗色主题变量：全局一份，先于样式层。
import 'element-plus/theme-chalk/dark/css-vars.css'

import App from './App.vue'
import router from './router'
import { initTheme } from '@/theme/applyTheme'

// ⚠ 主题层必须是**最后一个** import，别往上挪。
// 按需引入后，Element Plus 各组件的样式挂在各自的 SFC 上，随 App.vue / router 子树
// 进入模块图。只有让这一行排在它们之后，我们的覆盖才会落到打包 CSS 的末尾，
// 从而在同优先级下压住 Element Plus 的默认样式。提前会导致大面积覆盖失效。
import '@/styles/index.css'

async function bootstrap() {
  // 开发模式下若没有 pywebview，安装内存 Mock 后端，便于浏览器中联调。
  if (import.meta.env.DEV) {
    const { installDevBridge } = await import('./api/mock')
    await installDevBridge()
  }

  // 尽早在挂载前定主题：index.html 里已有一份同样的内联脚本兜首屏，
  // 这里负责把「读 localStorage + 落 data-theme/.dark」的逻辑收敛到一处。
  initTheme()

  const app = createApp(App)

  app.use(createPinia())
  app.use(router)

  app.config.errorHandler = (err, _instance, info) => {
    console.error('[Keyrealm] 未捕获的前端异常:', err, info)
  }

  app.mount('#app')
}

bootstrap()
