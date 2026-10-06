import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import * as ElementPlusIcons from '@element-plus/icons-vue'

import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
// 主题层：必须排在 Element Plus 之后，才能盖住它的默认变量与组件样式。
import '@/styles/index.css'

import App from './App.vue'
import router from './router'
import { initTheme } from '@/theme/applyTheme'

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
  app.use(ElementPlus, { locale: zhCn })

  for (const [name, component] of Object.entries(ElementPlusIcons)) {
    app.component(name, component)
  }

  app.config.errorHandler = (err, _instance, info) => {
    console.error('[Credential Vault] 未捕获的前端异常:', err, info)
  }

  app.mount('#app')
}

bootstrap()