import { createRouter, createWebHashHistory } from 'vue-router'
import { useAppStore } from '@/stores/app'

import AppLayout from '@/components/AppLayout.vue'
import Setup from '@/views/Setup.vue'
import Unlock from '@/views/Unlock.vue'
import Credentials from '@/views/Credentials.vue'
import PluginManager from '@/views/PluginManager.vue'
import Groups from '@/views/Groups.vue'
import McpSettings from '@/views/McpSettings.vue'
import Logs from '@/views/Logs.vue'
import Tasks from '@/views/Tasks.vue'
import Settings from '@/views/Settings.vue'
import Backup from '@/views/Backup.vue'
import About from '@/views/About.vue'

// pywebview 通过 file:// 加载，必须使用 hash 模式，否则刷新/跳转会 404。
const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    {
      path: '/',
      component: AppLayout,
      redirect: '/credentials',
      children: [
        { path: 'credentials', name: 'credentials', component: Credentials, meta: { title: '凭据管理' } },
        { path: 'plugins', name: 'plugins', component: PluginManager, meta: { title: '插件管理' } },
        { path: 'groups', name: 'groups', component: Groups, meta: { title: '分组管理' } },
        { path: 'mcp', name: 'mcp', component: McpSettings, meta: { title: 'MCP 设置' } },
        { path: 'logs', name: 'logs', component: Logs, meta: { title: '日志' } },
        { path: 'tasks', name: 'tasks', component: Tasks, meta: { title: '任务' } },
        { path: 'settings', name: 'settings', component: Settings, meta: { title: '系统设置' } },
        { path: 'backup', name: 'backup', component: Backup, meta: { title: '备份' } },
        { path: 'about', name: 'about', component: About, meta: { title: '关于' } },
      ],
    },
    { path: '/setup', name: 'setup', component: Setup },
    { path: '/unlock', name: 'unlock', component: Unlock },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

router.beforeEach(async (to) => {
  const appStore = useAppStore()
  await appStore.bootstrap()

  if (!appStore.initialized) {
    return to.name === 'setup' ? true : { name: 'setup' }
  }
  if (!appStore.unlocked) {
    return to.name === 'unlock' ? true : { name: 'unlock' }
  }
  if (to.name === 'setup' || to.name === 'unlock') {
    return { name: 'credentials' }
  }
  return true
})

export default router