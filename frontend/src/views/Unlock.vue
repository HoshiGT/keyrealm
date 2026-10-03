<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Unlock as UnlockIcon } from '@element-plus/icons-vue'

import { useAppStore } from '@/stores/app'

const router = useRouter()
const appStore = useAppStore()

const formRef = ref(null)
const submitting = ref(false)
const errorMessage = ref('')
const passwordRef = ref(null)

const form = reactive({ password: '' })

const rules = {
  password: [{ required: true, message: '请输入主密码', trigger: 'blur' }],
}

const mockHint = window.__VAULT_MOCK__ ? `开发模式 Mock 主密码：${window.__VAULT_MOCK__.password}` : ''

async function submit() {
  if (!formRef.value) return
  try {
    await formRef.value.validate()
  } catch {
    return
  }

  submitting.value = true
  errorMessage.value = ''
  try {
    await appStore.unlock(form.password)
    form.password = ''
    router.push('/')
  } catch (err) {
    errorMessage.value = err.message || '解锁失败'
    form.password = ''
    ElMessage.error(errorMessage.value)
  } finally {
    submitting.value = false
  }
}

onMounted(() => {
  if (passwordRef.value && passwordRef.value.focus) passwordRef.value.focus()
})
</script>

<template>
  <div class="auth-screen">
    <el-card class="auth-card" shadow="always">
      <div class="auth-brand">
        <span class="auth-logo">🔐</span>
        <h1>Credential Vault</h1>
        <p class="auth-subtitle">保险库已锁定，请输入主密码解锁</p>
      </div>

      <el-form ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent>
        <el-form-item label="主密码" prop="password">
          <el-input
            ref="passwordRef"
            v-model="form.password"
            type="password"
            show-password
            placeholder="输入主密码"
            autocomplete="current-password"
            @keyup.enter="submit"
          />
        </el-form-item>
      </el-form>

      <el-alert v-if="errorMessage" type="error" :closable="false" show-icon class="auth-alert">
        <template #title>{{ errorMessage }}</template>
      </el-alert>

      <el-button type="primary" size="large" class="auth-submit" :loading="submitting" :icon="UnlockIcon" @click="submit">
        解锁
      </el-button>

      <p v-if="mockHint" class="mock-hint">{{ mockHint }}</p>
    </el-card>
  </div>
</template>

<style scoped>
.auth-screen {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100vh;
  background: radial-gradient(circle at 50% 20%, rgba(64, 158, 255, 0.14), transparent 55%), var(--el-bg-color-page);
}

.auth-card {
  width: 420px;
  padding: 8px 12px 16px;
}

.auth-brand {
  text-align: center;
  margin-bottom: 20px;
}

.auth-logo {
  font-size: 40px;
  line-height: 1;
}

.auth-brand h1 {
  margin: 10px 0 4px;
  font-size: 22px;
}

.auth-subtitle {
  margin: 0;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.auth-alert {
  margin-bottom: 16px;
}

.auth-submit {
  width: 100%;
}

.mock-hint {
  margin: 14px 0 0;
  text-align: center;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>