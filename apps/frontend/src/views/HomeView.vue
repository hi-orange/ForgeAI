<template>
  <div class="dashboard">
    <AppSidebar />

    <main class="main">
      <div class="stage">
        <section class="hero">
          <div class="avatar-row">
            <button
              v-for="(item, index) in agents"
              :key="item.name"
              type="button"
              class="buddy"
              :style="{
                zIndex: agents.length - index,
                background: item.bg,
                '--buddy-glow': item.glow,
              }"
              :title="`${item.name} - ${item.role}`"
              :aria-label="`${item.name} - ${item.role}`"
            >
              <span class="buddy-face" aria-hidden="true">{{ item.emoji }}</span>
              <span class="buddy-tip">{{ item.name }} · {{ item.role }}</span>
            </button>
          </div>
          <h1>你的下一个产品从这里开始，{{ displayName }}。</h1>
        </section>

        <section class="composer-wrap">
          <div class="composer">
            <textarea v-model="prompt" rows="4" placeholder="描述你想构建的 Web 应用..." />

            <div class="composer-toolbar">
              <div class="toolbar-left">
                <button type="button" class="round-btn" title="添加附件">+</button>
              </div>

              <div class="toolbar-right">
                <button type="button" class="build-btn" @click="onBuild">
                  <span>构建</span>
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="m6 9 6 6 6-6" />
                  </svg>
                </button>
                <button type="button" class="send-btn" title="开始构建" @click="onBuild">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M12 19V5" />
                    <path d="m6 11 6-6 6 6" />
                  </svg>
                </button>
              </div>
            </div>
          </div>
        </section>

        <section class="features">
          <article v-for="item in features" :key="item.title" class="feature-card">
            <div class="feature-icon" :style="{ background: item.accent }">{{ item.icon }}</div>
            <div>
              <h3>{{ item.title }}</h3>
              <p>{{ item.desc }}</p>
            </div>
          </article>
        </section>
      </div>
    </main>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

import AppSidebar from '@/components/AppSidebar.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const prompt = ref('')

const displayName = computed(() => {
  const raw = auth.user?.username || auth.user?.email?.split('@')[0] || '创作者'
  return raw.replace(/[._-]+/g, ' ').trim()
})

const agents = [
  {
    key: 'mike',
    name: 'Mike',
    role: 'AI 团队领导 Agent',
    emoji: '🦊',
    bg: '#ffedd5',
    glow: 'rgba(251, 146, 60, 0.75)',
  },
  {
    key: 'adrian',
    name: 'Adrian',
    role: 'AI 广告专家 Agent',
    emoji: '🐼',
    bg: '#dbeafe',
    glow: 'rgba(96, 165, 250, 0.85)',
  },
  {
    key: 'sarah',
    name: 'Sarah',
    role: 'AI SEO 专家 Agent',
    emoji: '🐯',
    bg: '#fee2e2',
    glow: 'rgba(248, 113, 113, 0.8)',
  },
  {
    key: 'emma',
    name: 'Emma',
    role: 'AI 产品经理 Agent',
    emoji: '🐰',
    bg: '#ede9fe',
    glow: 'rgba(167, 139, 250, 0.85)',
  },
  {
    key: 'bob',
    name: 'Bob',
    role: 'AI 架构师 Agent',
    emoji: '🐻',
    bg: '#d1fae5',
    glow: 'rgba(52, 211, 153, 0.8)',
  },
  {
    key: 'alex',
    name: 'Alex',
    role: 'AI 工程师 Agent',
    emoji: '🐶',
    bg: '#e0f2fe',
    glow: 'rgba(56, 189, 248, 0.85)',
  },
  {
    key: 'david',
    name: 'David',
    role: 'AI 数据分析师 Agent',
    emoji: '🦉',
    bg: '#fef3c7',
    glow: 'rgba(251, 191, 36, 0.85)',
  },
  {
    key: 'iris',
    name: 'Iris',
    role: 'AI 深度研究员 Agent',
    emoji: '🦄',
    bg: '#fce7f3',
    glow: 'rgba(244, 114, 182, 0.85)',
  },
] as const

const features = [
  {
    title: 'AI 协作',
    desc: '多个 AI Agent 协同工作，帮你更快打磨产品。',
    accent: '#dbeafe',
    icon: '🤝',
  },
  {
    title: '极速构建',
    desc: '从想法到可运行原型，往往只需几分钟。',
    accent: '#ede9fe',
    icon: '⚡',
  },
  {
    title: '安全可靠',
    desc: '企业级安全与隐私保护，数据始终由你掌控。',
    accent: '#dcfce7',
    icon: '🛡️',
  },
]

function onBuild() {
  if (!prompt.value.trim()) {
    prompt.value = '帮我构建一个现代化的招聘网站'
  }
}
</script>

<style scoped>
.dashboard {
  display: flex;
  min-height: 100vh;
  background: #f7f8fb;
  color: #0f172a;
}

.main {
  flex: 1;
  min-width: 0;
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  justify-content: center;
  padding: 2.5rem 1.75rem 3rem;
  background:
    radial-gradient(ellipse 55% 35% at 50% 18%, rgba(147, 197, 253, 0.22), transparent 70%),
    linear-gradient(180deg, #fbfcfe 0%, #f5f7fb 100%);
}

.stage {
  width: 100%;
  max-width: 52rem;
  margin: 0 auto;
  transform: translateY(-2vh);
}

.hero {
  text-align: center;
  margin: 0 auto 1.75rem;
  animation: rise 0.5s ease-out both;
}

.hero h1 {
  margin: 0;
  font-family: var(--font-sans);
  font-size: clamp(1.65rem, 2.8vw, 2.05rem);
  font-weight: 650;
  letter-spacing: 0.01em;
  line-height: 1.55;
  color: #0f172a;
}

.avatar-row {
  --doll-line-avatar-size: clamp(32px, 5.5vw, 46px);
  --doll-line-avatar-offset: -12px;
  display: inline-flex;
  justify-content: center;
  align-items: center;
  margin-bottom: 1.1rem;
}

.buddy {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: var(--doll-line-avatar-size);
  height: var(--doll-line-avatar-size);
  margin-left: var(--doll-line-avatar-offset);
  border: 2px solid #fff;
  border-radius: 9999px;
  overflow: visible;
  cursor: pointer;
  transition:
    transform 0.2s ease,
    box-shadow 0.2s ease,
    filter 0.2s ease;
  box-shadow: 0 6px 16px rgba(15, 23, 42, 0.08);
}

.buddy:first-child {
  margin-left: 0;
}

.buddy-face {
  font-size: clamp(1rem, 2.4vw, 1.25rem);
  line-height: 1;
  pointer-events: none;
}

.buddy:hover,
.buddy:focus-visible {
  z-index: 20 !important;
  transform: translateY(-4px) scale(1.08);
  outline: none;
  box-shadow:
    0 0 0 3px rgba(255, 255, 255, 0.95),
    0 0 18px 6px var(--buddy-glow),
    0 10px 24px rgba(15, 23, 42, 0.12);
  filter: saturate(1.15);
}

.buddy-tip {
  position: absolute;
  left: 50%;
  bottom: calc(100% + 0.55rem);
  transform: translateX(-50%) translateY(4px);
  white-space: nowrap;
  padding: 0.35rem 0.6rem;
  border-radius: 0.55rem;
  background: rgba(15, 23, 42, 0.92);
  color: #fff;
  font-size: 0.75rem;
  font-weight: 600;
  opacity: 0;
  pointer-events: none;
  transition:
    opacity 0.18s ease,
    transform 0.18s ease;
  box-shadow: 0 8px 20px rgba(15, 23, 42, 0.18);
}

.buddy-tip::after {
  content: '';
  position: absolute;
  top: 100%;
  left: 50%;
  margin-left: -5px;
  border: 5px solid transparent;
  border-top-color: rgba(15, 23, 42, 0.92);
}

.buddy:hover .buddy-tip,
.buddy:focus-visible .buddy-tip {
  opacity: 1;
  transform: translateX(-50%) translateY(0);
}

@media (max-width: 640px) {
  .avatar-row {
    --doll-line-avatar-offset: -8px;
  }

  .buddy-tip {
    display: none;
  }
}

.composer-wrap {
  width: 100%;
  margin: 0 auto 1.75rem;
  animation: rise 0.55s ease-out 0.05s both;
}

.composer {
  background: #fff;
  border: 1px solid #e6ebf2;
  border-radius: 1.25rem;
  box-shadow:
    0 1px 2px rgba(15, 23, 42, 0.03),
    0 18px 40px rgba(15, 23, 42, 0.06);
  padding: 1rem 1rem 0.85rem;
}

.composer textarea {
  width: 100%;
  min-height: 6.5rem;
  border: 0;
  resize: none;
  outline: none;
  font: inherit;
  color: #0f172a;
  line-height: 1.55;
  background: transparent;
}

.composer textarea::placeholder {
  color: #94a3b8;
}

.composer-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  margin-top: 0.35rem;
}

.toolbar-left,
.toolbar-right {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.round-btn,
.send-btn {
  display: grid;
  place-items: center;
  width: 2.25rem;
  height: 2.25rem;
  border-radius: 999px;
  cursor: pointer;
}

.round-btn {
  border: 1px solid #e2e8f0;
  background: #fff;
  color: #64748b;
  font-size: 1.15rem;
  line-height: 1;
}

.round-btn:hover {
  border-color: #bfdbfe;
  color: #1d4ed8;
}

.build-btn {
  display: inline-flex;
  align-items: center;
  gap: 0.2rem;
  border: 0;
  border-radius: 999px;
  background: #2f6bff;
  color: #fff;
  font-weight: 700;
  font-size: 0.9rem;
  padding: 0.5rem 0.85rem 0.5rem 1rem;
  cursor: pointer;
}

.build-btn svg {
  width: 1rem;
  height: 1rem;
}

.build-btn:hover {
  background: #1f54e0;
}

.send-btn svg {
  width: 1.05rem;
  height: 1.05rem;
}

.send-btn {
  border: 0;
  background: #2f6bff;
  color: #fff;
}

.send-btn:hover {
  background: #1f54e0;
}

.features {
  width: 100%;
  margin: 0 auto;
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 0.9rem;
  animation: rise 0.55s ease-out 0.1s both;
}

.feature-card {
  display: flex;
  gap: 0.8rem;
  padding: 1rem;
  border: 1px solid #e8ecf2;
  border-radius: 1rem;
  background: rgba(255, 255, 255, 0.88);
  box-shadow: 0 8px 24px rgba(15, 23, 42, 0.03);
}

.feature-icon {
  display: grid;
  place-items: center;
  width: 2.4rem;
  height: 2.4rem;
  border-radius: 0.75rem;
  font-size: 1.05rem;
  flex-shrink: 0;
}

.feature-card h3 {
  margin: 0 0 0.3rem;
  font-size: 0.95rem;
  font-weight: 750;
}

.feature-card p {
  margin: 0;
  color: #64748b;
  font-size: 0.82rem;
  line-height: 1.5;
}

@keyframes rise {
  from {
    opacity: 0;
    transform: translateY(10px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

@media (max-width: 980px) {
  .features {
    grid-template-columns: 1fr;
  }

  .composer-toolbar {
    flex-direction: column;
    align-items: stretch;
  }

  .toolbar-left,
  .toolbar-right {
    justify-content: space-between;
  }
}

@media (max-width: 900px) {
  .main {
    padding: 1.5rem 1rem 2rem;
    justify-content: flex-start;
  }

  .stage {
    transform: none;
    padding-top: 1.5rem;
  }
}
</style>
