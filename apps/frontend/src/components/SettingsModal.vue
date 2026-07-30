<template>
  <Teleport to="body">
    <div v-if="open" class="overlay" @click.self="close">
      <div class="dialog" role="dialog" aria-modal="true" aria-labelledby="settings-title">
        <aside class="nav-pane">
          <h2 id="settings-title" class="nav-title">设置</h2>

          <p class="nav-section">工作区</p>
          <button
            type="button"
            class="nav-item"
            :class="{ active: activeTab === 'workspace' }"
            @click="activeTab = 'workspace'"
          >
            <span class="nav-mark workspace" aria-hidden="true">{{ initials }}</span>
            <span class="nav-label">{{ workspaceForm.name || workspaceInitial }}</span>
          </button>

          <p class="nav-section">账户</p>
          <button
            type="button"
            class="nav-item"
            :class="{ active: activeTab === 'account' }"
            @click="activeTab = 'account'"
          >
            <span class="nav-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7">
                <circle cx="12" cy="12" r="9" />
                <circle cx="12" cy="9" r="3" />
                <path d="M6.8 18.2c1.4-2.1 3.1-3.2 5.2-3.2s3.8 1.1 5.2 3.2" />
              </svg>
            </span>
            <span class="nav-label">{{ displayName }}</span>
          </button>
        </aside>

        <section class="content-pane">
          <header class="content-header">
            <div>
              <h3>{{ activeTab === 'workspace' ? '工作区设置' : '账户设置' }}</h3>
              <p v-if="activeTab === 'workspace'" class="subtitle">
                Workspaces 允许你实时协作处理项目。
              </p>
            </div>
            <button type="button" class="close-btn" aria-label="关闭" @click="close">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
                <path d="M6 6l12 12M18 6 6 18" />
              </svg>
            </button>
          </header>

          <div v-if="activeTab === 'account'" class="panel">
            <div class="rows">
              <div class="row">
                <span class="label">头像</span>
                <span class="avatar-lg" aria-hidden="true">{{ initials }}</span>
              </div>

              <div class="row">
                <span class="label">用户名</span>
                <span class="value">
                  {{ auth.user?.username || displayName }}
                  <button
                    type="button"
                    class="edit-btn"
                    title="编辑用户名"
                    @click="openUsernameModal"
                  >
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
                      <path d="M4 20h4L18.5 9.5a2.1 2.1 0 0 0-3-3L5 17v3Z" />
                      <path d="m13.5 6.5 3 3" />
                    </svg>
                  </button>
                </span>
              </div>

              <div class="row">
                <span class="label">邮箱</span>
                <span class="value muted">{{ auth.user?.email || '—' }}</span>
              </div>

              <div class="row">
                <span class="label">密码</span>
                <span class="value">
                  ••••••••
                  <button
                    type="button"
                    class="edit-btn"
                    title="修改密码"
                    @click="openPasswordModal"
                  >
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
                      <path d="M4 20h4L18.5 9.5a2.1 2.1 0 0 0-3-3L5 17v3Z" />
                      <path d="m13.5 6.5 3 3" />
                    </svg>
                  </button>
                </span>
              </div>
            </div>

            <div class="footer">
              <button type="button" class="logout-btn" @click="onLogout">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
                  <path
                    d="M10 7V6a2 2 0 0 1 2-2h6a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-6a2 2 0 0 1-2-2v-1"
                  />
                  <path d="M15 12H4" />
                  <path d="m7 9-3 3 3 3" />
                </svg>
                退出登录
              </button>
            </div>
          </div>

          <div v-else class="panel workspace-panel">
            <div class="field-block avatar-block">
              <div>
                <p class="field-title">工作区头像</p>
                <p class="field-desc">为你的工作区设置头像。</p>
              </div>
              <button type="button" class="workspace-avatar" title="更换头像">
                {{ (workspaceDraft.name || workspaceInitial).slice(0, 2).toUpperCase() }}
              </button>
            </div>

            <div class="field-block">
              <p class="field-title">工作区名称</p>
              <p class="field-desc">你的完整工作区名称。</p>
              <input
                v-model="workspaceDraft.name"
                class="text-input"
                type="text"
                maxlength="100"
                placeholder="输入工作区名称"
              />
              <p class="counter">{{ workspaceDraft.name.length }} / 100 字符</p>
            </div>

            <div class="field-block">
              <p class="field-title">工作区描述</p>
              <p class="field-desc">关于你的工作区或团队的描述。</p>
              <textarea
                v-model="workspaceDraft.description"
                class="text-area"
                maxlength="500"
                rows="4"
                placeholder="添加描述..."
              />
              <p class="counter">{{ workspaceDraft.description.length }} / 500 字符</p>
            </div>

            <div class="footer workspace-footer">
              <p v-if="saveHint" class="save-hint">{{ saveHint }}</p>
              <button type="button" class="ghost-btn" @click="cancelWorkspace">取消</button>
              <button
                type="button"
                class="primary-btn"
                :disabled="!workspaceDirty"
                @click="updateWorkspace"
              >
                更新
              </button>
            </div>
          </div>
        </section>
      </div>

      <div v-if="usernameOpen" class="sub-overlay" @click.self="closeUsernameModal">
        <div
          class="sub-dialog"
          role="dialog"
          aria-modal="true"
          aria-labelledby="username-title"
          @click.stop
        >
          <header class="sub-header">
            <h3 id="username-title">设置用户名</h3>
            <button type="button" class="close-btn" aria-label="关闭" @click="closeUsernameModal">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
                <path d="M6 6l12 12M18 6 6 18" />
              </svg>
            </button>
          </header>

          <label class="sub-label" for="username-input">用户名</label>
          <input
            id="username-input"
            v-model="usernameDraft"
            class="text-input"
            type="text"
            maxlength="35"
            autocomplete="username"
            :disabled="usernameSaving"
            @keydown.enter.prevent="saveUsername"
          />
          <p class="sub-hint">输入3到35个字符的用户名。请确保用户名的开头和结尾没有空格。</p>
          <p v-if="usernameError" class="sub-error">{{ usernameError }}</p>

          <div class="sub-footer">
            <button
              type="button"
              class="ghost-btn"
              :disabled="usernameSaving"
              @click="closeUsernameModal"
            >
              取消
            </button>
            <button
              type="button"
              class="save-btn"
              :disabled="!canSaveUsername || usernameSaving"
              @click="saveUsername"
            >
              {{ usernameSaving ? '保存中…' : '保存' }}
            </button>
          </div>
        </div>
      </div>

      <div v-if="passwordOpen" class="sub-overlay" @click.self="closePasswordModal">
        <div
          class="sub-dialog"
          role="dialog"
          aria-modal="true"
          aria-labelledby="password-title"
          @click.stop
        >
          <header class="sub-header">
            <h3 id="password-title">修改密码</h3>
            <button type="button" class="close-btn" aria-label="关闭" @click="closePasswordModal">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
                <path d="M6 6l12 12M18 6 6 18" />
              </svg>
            </button>
          </header>

          <label class="sub-label" for="old-password-input">当前密码</label>
          <input
            id="old-password-input"
            v-model="passwordDraft.oldPassword"
            class="text-input"
            type="password"
            maxlength="128"
            autocomplete="current-password"
            :disabled="passwordSaving"
          />

          <label class="sub-label sub-label-spaced" for="new-password-input">新密码</label>
          <input
            id="new-password-input"
            v-model="passwordDraft.newPassword"
            class="text-input"
            type="password"
            maxlength="128"
            autocomplete="new-password"
            :disabled="passwordSaving"
          />

          <label class="sub-label sub-label-spaced" for="confirm-password-input">确认新密码</label>
          <input
            id="confirm-password-input"
            v-model="passwordDraft.confirmPassword"
            class="text-input"
            type="password"
            maxlength="128"
            autocomplete="new-password"
            :disabled="passwordSaving"
            @keydown.enter.prevent="savePassword"
          />

          <p class="sub-hint">新密码至少 6 个字符，修改成功后需重新登录。</p>
          <p v-if="passwordError" class="sub-error">{{ passwordError }}</p>

          <div class="sub-footer">
            <button
              type="button"
              class="ghost-btn"
              :disabled="passwordSaving"
              @click="closePasswordModal"
            >
              取消
            </button>
            <button
              type="button"
              class="save-btn"
              :disabled="!canSavePassword || passwordSaving"
              @click="savePassword"
            >
              {{ passwordSaving ? '保存中…' : '保存' }}
            </button>
          </div>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

const open = defineModel<boolean>('open', { default: false })

type SettingsTab = 'workspace' | 'account'

const props = withDefaults(
  defineProps<{
    defaultTab?: SettingsTab
  }>(),
  { defaultTab: 'account' },
)

const auth = useAuthStore()
const router = useRouter()
const activeTab = ref<SettingsTab>('account')
const saveHint = ref('')

const usernameOpen = ref(false)
const usernameDraft = ref('')
const usernameError = ref('')
const usernameSaving = ref(false)

const passwordOpen = ref(false)
const passwordDraft = reactive({
  oldPassword: '',
  newPassword: '',
  confirmPassword: '',
})
const passwordError = ref('')
const passwordSaving = ref(false)

const displayName = computed(() => {
  const raw = auth.user?.username || auth.user?.email?.split('@')[0] || 'user'
  return raw.replace(/[._-]+/g, ' ').trim()
})

const canSaveUsername = computed(() => {
  const value = usernameDraft.value
  if (value !== value.trim()) return false
  if (value.length < 3 || value.length > 35) return false
  return value !== (auth.user?.username ?? '')
})

const canSavePassword = computed(() => {
  const { oldPassword, newPassword, confirmPassword } = passwordDraft
  if (!oldPassword || newPassword.length < 6 || newPassword.length > 128) return false
  if (newPassword !== confirmPassword) return false
  if (oldPassword === newPassword) return false
  return true
})

const initials = computed(() => {
  const parts = displayName.value.split(/\s+/).filter(Boolean)
  if (parts.length >= 2) {
    return (parts[0]![0]! + parts[1]![0]!).toUpperCase()
  }
  return displayName.value.slice(0, 2).toUpperCase() || 'U'
})

const workspaceKey = computed(() => `forge_workspace_${auth.user?.id ?? 'guest'}`)

const workspaceForm = reactive({
  name: '',
  description: '',
})

const workspaceDraft = reactive({
  name: '',
  description: '',
})

const workspaceInitial = computed(() => `${displayName.value}'s Forge`)

const workspaceDirty = computed(
  () =>
    workspaceDraft.name !== workspaceForm.name ||
    workspaceDraft.description !== workspaceForm.description,
)

function loadWorkspace() {
  const fallbackName = workspaceInitial.value
  try {
    const raw = localStorage.getItem(workspaceKey.value)
    if (raw) {
      const parsed = JSON.parse(raw) as { name?: string; description?: string }
      workspaceForm.name = parsed.name?.trim() || fallbackName
      workspaceForm.description = parsed.description ?? ''
    } else {
      workspaceForm.name = fallbackName
      workspaceForm.description = ''
    }
  } catch {
    workspaceForm.name = fallbackName
    workspaceForm.description = ''
  }
  workspaceDraft.name = workspaceForm.name
  workspaceDraft.description = workspaceForm.description
  saveHint.value = ''
}

function close() {
  usernameOpen.value = false
  passwordOpen.value = false
  open.value = false
}

function openUsernameModal() {
  usernameDraft.value = auth.user?.username ?? ''
  usernameError.value = ''
  usernameOpen.value = true
}

function closeUsernameModal() {
  if (usernameSaving.value) return
  usernameOpen.value = false
  usernameError.value = ''
}

async function saveUsername() {
  if (!canSaveUsername.value || usernameSaving.value) return

  const value = usernameDraft.value
  if (value !== value.trim()) {
    usernameError.value = '用户名开头和结尾不能有空格'
    return
  }
  if (value.length < 3 || value.length > 35) {
    usernameError.value = '用户名长度需为 3 到 35 个字符'
    return
  }

  usernameSaving.value = true
  usernameError.value = ''
  try {
    await auth.updateUsername({ username: value })
    usernameOpen.value = false
  } catch (err) {
    usernameError.value = err instanceof Error ? err.message : '修改用户名失败'
  } finally {
    usernameSaving.value = false
  }
}

function resetPasswordDraft() {
  passwordDraft.oldPassword = ''
  passwordDraft.newPassword = ''
  passwordDraft.confirmPassword = ''
  passwordError.value = ''
}

function openPasswordModal() {
  resetPasswordDraft()
  passwordOpen.value = true
}

function closePasswordModal() {
  if (passwordSaving.value) return
  passwordOpen.value = false
  resetPasswordDraft()
}

async function savePassword() {
  if (!canSavePassword.value || passwordSaving.value) return

  const { oldPassword, newPassword, confirmPassword } = passwordDraft
  if (newPassword !== confirmPassword) {
    passwordError.value = '两次输入的新密码不一致'
    return
  }
  if (newPassword.length < 6) {
    passwordError.value = '新密码至少 6 个字符'
    return
  }
  if (oldPassword === newPassword) {
    passwordError.value = '新密码不能与当前密码相同'
    return
  }

  passwordSaving.value = true
  passwordError.value = ''
  try {
    await auth.changePassword({
      old_password: oldPassword,
      new_password: newPassword,
    })
    passwordOpen.value = false
    open.value = false
    await router.replace({ name: 'login' })
  } catch (err) {
    passwordError.value = err instanceof Error ? err.message : '修改密码失败'
  } finally {
    passwordSaving.value = false
  }
}

function cancelWorkspace() {
  workspaceDraft.name = workspaceForm.name
  workspaceDraft.description = workspaceForm.description
  saveHint.value = ''
}

function updateWorkspace() {
  const name = workspaceDraft.name.trim() || workspaceInitial.value
  workspaceDraft.name = name.slice(0, 100)
  workspaceDraft.description = workspaceDraft.description.slice(0, 500)
  workspaceForm.name = workspaceDraft.name
  workspaceForm.description = workspaceDraft.description
  localStorage.setItem(
    workspaceKey.value,
    JSON.stringify({
      name: workspaceForm.name,
      description: workspaceForm.description,
    }),
  )
  saveHint.value = '已更新'
}

async function onLogout() {
  auth.logout()
  open.value = false
  await router.replace({ name: 'login' })
}

function onKeydown(event: KeyboardEvent) {
  if (event.key !== 'Escape' || !open.value) return
  if (passwordOpen.value) {
    closePasswordModal()
    return
  }
  if (usernameOpen.value) {
    closeUsernameModal()
    return
  }
  close()
}

watch(open, (value) => {
  document.body.style.overflow = value ? 'hidden' : ''
  if (value) {
    activeTab.value = props.defaultTab
    loadWorkspace()
  } else {
    usernameOpen.value = false
    usernameError.value = ''
    passwordOpen.value = false
    resetPasswordDraft()
  }
})

onMounted(() => window.addEventListener('keydown', onKeydown))
onUnmounted(() => {
  window.removeEventListener('keydown', onKeydown)
  document.body.style.overflow = ''
})
</script>

<style scoped>
.overlay {
  position: fixed;
  inset: 0;
  z-index: 50;
  display: grid;
  place-items: center;
  padding: 1.25rem;
  background: rgba(15, 23, 42, 0.35);
  backdrop-filter: blur(2px);
}

.dialog {
  display: grid;
  grid-template-columns: 252px minmax(0, 1fr);
  width: min(100%, 900px);
  height: min(82vh, 640px);
  overflow: hidden;
  border-radius: 1rem;
  background: #fff;
  box-shadow: 0 24px 80px rgba(15, 23, 42, 0.2);
  animation: pop 0.2s ease-out;
}

.nav-pane {
  padding: 1.25rem 0.85rem;
  background: #f7f8fb;
  border-right: 1px solid #eef1f6;
  overflow: auto;
}

.nav-title {
  margin: 0 0 1.25rem;
  padding: 0 0.55rem;
  font-size: 1.05rem;
  font-weight: 750;
  color: #0f172a;
}

.nav-section {
  margin: 1rem 0 0.35rem;
  padding: 0 0.55rem;
  color: #94a3b8;
  font-size: 0.78rem;
  font-weight: 650;
}

.nav-section:first-of-type {
  margin-top: 0;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 0.65rem;
  width: 100%;
  padding: 0.55rem 0.65rem;
  border: 0;
  border-radius: 0.7rem;
  background: transparent;
  color: #334155;
  font-size: 0.9rem;
  font-weight: 600;
  text-align: left;
  cursor: pointer;
  text-decoration: none;
}

.nav-item:hover {
  background: #eceff4;
}

.nav-item.active {
  background: #eceff4;
}

.nav-item.active:has(.nav-mark.workspace) {
  background: #eef4ff;
}

.nav-mark {
  display: grid;
  place-items: center;
  width: 1.45rem;
  height: 1.45rem;
  border-radius: 0.4rem;
  font-size: 0.58rem;
  font-weight: 750;
  color: #fff;
  flex-shrink: 0;
  background: linear-gradient(145deg, #60a5fa, #2563eb);
}

.nav-icon {
  display: grid;
  place-items: center;
  width: 1.35rem;
  height: 1.35rem;
  color: #64748b;
  flex-shrink: 0;
}

.nav-icon svg {
  width: 1.05rem;
  height: 1.05rem;
}

.nav-item.active .nav-icon {
  color: #475569;
}

.nav-label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
  flex: 1;
}

.content-pane {
  display: flex;
  flex-direction: column;
  min-width: 0;
  padding: 1.25rem 1.4rem 1.1rem;
  overflow: auto;
}

.content-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 1rem;
  margin-bottom: 0.75rem;
}

.content-header h3 {
  margin: 0;
  font-size: 1.15rem;
  font-weight: 750;
}

.subtitle {
  margin: 0.4rem 0 0;
  color: #64748b;
  font-size: 0.88rem;
}

.close-btn {
  display: grid;
  place-items: center;
  width: 2rem;
  height: 2rem;
  border: 0;
  border-radius: 0.5rem;
  background: transparent;
  color: #64748b;
  cursor: pointer;
  flex-shrink: 0;
}

.close-btn:hover {
  background: #f1f5f9;
  color: #0f172a;
}

.close-btn svg {
  width: 1.05rem;
  height: 1.05rem;
}

.panel {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
}

.rows {
  flex: 1;
}

.row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  min-height: 4.1rem;
  border-bottom: 1px solid #eef1f6;
}

.label {
  color: #0f172a;
  font-size: 0.95rem;
  font-weight: 600;
}

.value {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  color: #334155;
  font-size: 0.95rem;
}

.value.muted {
  color: #64748b;
}

.avatar-lg {
  display: grid;
  place-items: center;
  width: 2.8rem;
  height: 2.8rem;
  border-radius: 0.75rem;
  background: linear-gradient(145deg, #60a5fa, #2563eb);
  color: #fff;
  font-size: 0.95rem;
  font-weight: 750;
}

.edit-btn {
  display: grid;
  place-items: center;
  width: 1.5rem;
  height: 1.5rem;
  border: 0;
  border-radius: 0.35rem;
  background: transparent;
  color: #94a3b8;
  cursor: pointer;
}

.edit-btn:hover {
  background: #f1f5f9;
  color: #475569;
}

.edit-btn svg {
  width: 0.95rem;
  height: 0.95rem;
}

.sub-overlay {
  position: absolute;
  inset: 0;
  z-index: 2;
  display: grid;
  place-items: center;
  padding: 1.25rem;
  background: rgba(15, 23, 42, 0.28);
  border-radius: inherit;
}

.sub-dialog {
  width: min(100%, 420px);
  padding: 1.15rem 1.2rem 1.1rem;
  border-radius: 0.9rem;
  background: #fff;
  box-shadow: 0 18px 50px rgba(15, 23, 42, 0.22);
  animation: pop 0.18s ease-out;
}

.sub-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  margin-bottom: 1rem;
}

.sub-header h3 {
  margin: 0;
  font-size: 1.05rem;
  font-weight: 750;
  color: #0f172a;
}

.sub-label {
  display: block;
  margin-bottom: 0.4rem;
  color: #0f172a;
  font-size: 0.9rem;
  font-weight: 650;
}

.sub-label-spaced {
  margin-top: 0.85rem;
}

.sub-hint {
  margin: 0.55rem 0 0;
  color: #94a3b8;
  font-size: 0.82rem;
  line-height: 1.45;
}

.sub-error {
  margin: 0.55rem 0 0;
  color: #dc2626;
  font-size: 0.84rem;
  font-weight: 600;
}

.sub-footer {
  display: flex;
  justify-content: flex-end;
  gap: 0.55rem;
  margin-top: 1.15rem;
}

.save-btn {
  border: 0;
  border-radius: 0.65rem;
  background: #0f172a;
  color: #fff;
  font-size: 0.9rem;
  font-weight: 700;
  padding: 0.55rem 0.95rem;
  cursor: pointer;
}

.save-btn:hover:not(:disabled) {
  background: #1e293b;
}

.save-btn:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.footer {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 0.55rem;
  padding-top: 1rem;
}

.logout-btn {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  border: 0;
  border-radius: 0.65rem;
  background: #fee2e2;
  color: #dc2626;
  font-size: 0.9rem;
  font-weight: 700;
  padding: 0.55rem 0.9rem;
  cursor: pointer;
}

.logout-btn:hover {
  background: #fecaca;
}

.logout-btn svg {
  width: 1rem;
  height: 1rem;
}

.workspace-panel {
  gap: 1.15rem;
}

.field-block {
  display: grid;
  gap: 0.35rem;
}

.avatar-block {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  padding-bottom: 0.35rem;
  border-bottom: 1px solid #eef1f6;
}

.field-title {
  margin: 0;
  color: #0f172a;
  font-size: 0.95rem;
  font-weight: 700;
}

.field-desc {
  margin: 0;
  color: #94a3b8;
  font-size: 0.84rem;
}

.workspace-avatar {
  display: grid;
  place-items: center;
  width: 3rem;
  height: 3rem;
  border: 0;
  border-radius: 0.75rem;
  background: linear-gradient(145deg, #60a5fa, #2563eb);
  color: #fff;
  font-size: 0.95rem;
  font-weight: 750;
  cursor: pointer;
  flex-shrink: 0;
}

.text-input,
.text-area {
  width: 100%;
  box-sizing: border-box;
  border: 1px solid #e2e8f0;
  border-radius: 0.7rem;
  background: #f8fafc;
  color: #0f172a;
  font: inherit;
  padding: 0.75rem 0.85rem;
  outline: none;
}

.text-input:focus,
.text-area:focus {
  border-color: #a78bfa;
  background: #fff;
  box-shadow: 0 0 0 3px rgba(167, 139, 250, 0.18);
}

.text-area {
  resize: vertical;
  min-height: 6rem;
  line-height: 1.5;
}

.counter {
  margin: 0;
  text-align: right;
  color: #94a3b8;
  font-size: 0.78rem;
}

.workspace-footer {
  margin-top: auto;
}

.save-hint {
  margin: 0 auto 0 0;
  color: #16a34a;
  font-size: 0.86rem;
  font-weight: 600;
}

.ghost-btn,
.primary-btn {
  border-radius: 0.65rem;
  font-size: 0.9rem;
  font-weight: 700;
  padding: 0.55rem 0.95rem;
  cursor: pointer;
}

.ghost-btn {
  border: 1px solid #e2e8f0;
  background: #fff;
  color: #334155;
}

.ghost-btn:hover {
  background: #f8fafc;
}

.primary-btn {
  border: 0;
  background: #94a3b8;
  color: #fff;
}

.primary-btn:not(:disabled) {
  background: #2f6bff;
}

.primary-btn:not(:disabled):hover {
  background: #1f54e0;
}

.primary-btn:disabled {
  cursor: not-allowed;
  opacity: 0.7;
}

@keyframes pop {
  from {
    opacity: 0;
    transform: translateY(8px) scale(0.98);
  }
  to {
    opacity: 1;
    transform: translateY(0) scale(1);
  }
}

@media (max-width: 720px) {
  .dialog {
    grid-template-columns: 1fr;
    height: auto;
    max-height: 90vh;
  }

  .nav-pane {
    border-right: 0;
    border-bottom: 1px solid #eef1f6;
  }
}
</style>
