import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import * as ElementPlusIcons from '@element-plus/icons-vue'

import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'

import App from './App.vue'
import router from './router'

async function bootstrap() {
  // 开发模式下若没有 pywebview，安装内存 Mock 后端，便于浏览器中联调。
  if (import.meta.env.DEV) {
    const { installDevBridge } = await import('./api/mock')
    await installDevBridge()
  }

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