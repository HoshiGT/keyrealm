<script setup>
import { computed, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Lock } from '@element-plus/icons-vue'

import { useAppStore } from '@/stores/app'

const router = useRouter()
const appStore = useAppStore()

const formRef = ref(null)
const submitting = ref(false)

const form = reactive({ password: '', confirm: '' })

const rules = {
  password: [
    { required: true, message: '请输入主密码', trigger: 'blur' },
    { min: 8, message: '主密码至少需要 8 个字符', trigger: 'blur' },
  ],
  confirm: [
    { required: true, message: '请再次输入主密码', trigger: 'blur' },
    {
      validator: (_rule, value, callback) => {
        if (value !== form.password) callback(new Error('两次输入的密码不一致'))
        else callback()
      },
      trigger: 'blur',
    },
  ],
}

const strength = computed(() => {
  const value = form.password
  if (!value) return { score: 0, label: '未输入', color: '#909399' }
  let score = 0
  if (value.length >= 8) score += 1
  if (value.length >= 12) score += 1
  if (/[a-z]/.test(value) && /[A-Z]/.test(value)) score += 1
  if (/\d/.test(value)) score += 1
  if (/[^A-Za-z0-9]/.test(value)) score += 1
  score = Math.min(score, 4)

  const table = [
    { label: '很弱', color: '#F56C6C' },
    { label: '较弱', color: '#F56C6C' },
    { label: '一般', color: '#E6A23C' },
    { label: '较强', color: '#67C23A' },
    { label: '很强', color: '#409EFF' },
  ]
  return { score, ...table[score] }
})

async function submit() {
  if (!formRef.value) return
  try {
    await formRef.value.validate()
  } catch {
    return
  }

  submitting.value = true
  try {
    await appStore.setupMasterPassword(form.password)
    ElMessage.success('保险库创建成功，请妥善保管主密码')
    router.push('/')
  } catch (err) {
    ElMessage.error(err.message || '创建失败')
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="auth-screen">
    <el-card class="auth-card" shadow="always">
      <div class="auth-brand">
        <span class="auth-logo">🔐</span>
        <h1>Credential Vault</h1>
        <p class="auth-subtitle">设置主密码（首次使用）</p>
      </div>

      <el-alert type="info" :closable="false" show-icon class="auth-alert">
        <template #title>主密码用于派生加密密钥，一旦遗忘将无法恢复任何凭据。</template>
      </el-alert>

      <el-form ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent>
        <el-form-item label="主密码" prop="password">
          <el-input
            v-model="form.password"
            type="password"
            show-password
            placeholder="至少 8 个字符"
            autocomplete="new-password"
            @keyup.enter="submit"
          />
        </el-form-item>

        <el-form-item label="确认主密码" prop="confirm">
          <el-input
            v-model="form.confirm"
            type="password"
            show-password
            placeholder="再次输入以确认"
            autocomplete="new-password"
            @keyup.enter="submit"
          />
        </el-form-item>
      </el-form>

      <div class="strength">
        <div class="strength-bars">
          <span
            v-for="index in 4"
            :key="index"
            class="strength-bar"
            :style="{ background: index <= strength.score ? strength.color : 'var(--el-border-color)' }"
          />
        </div>
        <span class="strength-label" :style="{ color: strength.color }">密码强度：{{ strength.label }}</span>
      </div>

      <el-button type="primary" size="large" class="auth-submit" :loading="submitting" :icon="Lock" @click="submit">
        创建保险库
      </el-button>
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
  width: 460px;
  padding: 8px 12px 16px;
}

.auth-brand {
  text-align: center;
  margin-bottom: 18px;
}

.auth-logo {
  font-size: 40px;
  line-height: 1;
}

.auth-brand h1 {
  margin: 10px 0 4px;
  font-size: 22px;
  letter-spacing: 0.01em;
}

.auth-subtitle {
  margin: 0;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.auth-alert {
  margin-bottom: 18px;
}

.strength {
  margin: 4px 0 20px;
}

.strength-bars {
  display: flex;
  gap: 6px;
}

.strength-bar {
  height: 6px;
  flex: 1;
  border-radius: 3px;
  transition: background 0.2s ease;
}

.strength-label {
  display: inline-block;
  margin-top: 8px;
  font-size: 12px;
}

.auth-submit {
  width: 100%;
}
</style>