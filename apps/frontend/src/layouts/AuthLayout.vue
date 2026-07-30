<template>
  <div class="auth-page">
    <aside class="hero">
      <div class="hero-bg" aria-hidden="true">
        <div class="orb orb-a" />
        <div class="orb orb-b" />
        <div class="orb orb-c" />

        <svg class="network" viewBox="0 0 720 900" preserveAspectRatio="xMidYMid slice">
          <defs>
            <linearGradient id="lineGrad" x1="0%" y1="0%" x2="100%" y2="100%">
              <stop offset="0%" stop-color="#60a5fa" stop-opacity="0.55" />
              <stop offset="100%" stop-color="#a78bfa" stop-opacity="0.35" />
            </linearGradient>
            <radialGradient id="nodeGlow" cx="50%" cy="50%" r="50%">
              <stop offset="0%" stop-color="#93c5fd" stop-opacity="0.9" />
              <stop offset="100%" stop-color="#93c5fd" stop-opacity="0" />
            </radialGradient>
          </defs>

          <g stroke="url(#lineGrad)" stroke-width="1.2" fill="none" opacity="0.7">
            <path d="M80 120 L180 210 L290 160 L410 250 L520 180 L640 260" />
            <path d="M120 320 L220 280 L340 360 L460 300 L580 390 L680 340" />
            <path d="M60 480 L170 520 L280 450 L400 540 L530 470 L650 560" />
            <path d="M180 210 L220 280 L280 450 L340 360 L410 250" />
            <path d="M460 300 L410 250 L520 180 L580 390 L530 470" />
            <path d="M290 160 L340 360 L400 540" />
            <path d="M100 700 L220 640 L360 720 L500 650 L620 730" />
            <path d="M220 640 L280 450 L360 720 L460 300" />
          </g>

          <g fill="#3b82f6">
            <circle cx="80" cy="120" r="3.5" opacity="0.75" />
            <circle cx="180" cy="210" r="4.5" />
            <circle cx="290" cy="160" r="3.5" opacity="0.8" />
            <circle cx="410" cy="250" r="5" />
            <circle cx="520" cy="180" r="3.5" opacity="0.7" />
            <circle cx="640" cy="260" r="4" />
            <circle cx="120" cy="320" r="3" opacity="0.65" />
            <circle cx="220" cy="280" r="4" />
            <circle cx="340" cy="360" r="5" />
            <circle cx="460" cy="300" r="3.5" />
            <circle cx="580" cy="390" r="4.5" />
            <circle cx="680" cy="340" r="3" opacity="0.7" />
            <circle cx="170" cy="520" r="4" />
            <circle cx="280" cy="450" r="5" />
            <circle cx="400" cy="540" r="3.5" />
            <circle cx="530" cy="470" r="4" />
            <circle cx="220" cy="640" r="4.5" />
            <circle cx="360" cy="720" r="3.5" />
            <circle cx="500" cy="650" r="4" />
          </g>

          <g opacity="0.45">
            <circle cx="410" cy="250" r="16" fill="url(#nodeGlow)" />
            <circle cx="340" cy="360" r="20" fill="url(#nodeGlow)" />
            <circle cx="280" cy="450" r="18" fill="url(#nodeGlow)" />
          </g>
        </svg>

        <svg class="ribbons" viewBox="0 0 800 500" preserveAspectRatio="none">
          <defs>
            <linearGradient id="ribbonA" x1="0%" y1="0%" x2="100%" y2="0%">
              <stop offset="0%" stop-color="#93c5fd" stop-opacity="0" />
              <stop offset="35%" stop-color="#60a5fa" stop-opacity="0.35" />
              <stop offset="70%" stop-color="#a78bfa" stop-opacity="0.28" />
              <stop offset="100%" stop-color="#c4b5fd" stop-opacity="0" />
            </linearGradient>
            <linearGradient id="ribbonB" x1="0%" y1="0%" x2="100%" y2="0%">
              <stop offset="0%" stop-color="#bfdbfe" stop-opacity="0" />
              <stop offset="40%" stop-color="#818cf8" stop-opacity="0.22" />
              <stop offset="100%" stop-color="#ddd6fe" stop-opacity="0" />
            </linearGradient>
          </defs>
          <path
            d="M-40 280 C120 200, 220 360, 380 280 S620 180, 860 260"
            fill="none"
            stroke="url(#ribbonA)"
            stroke-width="42"
            stroke-linecap="round"
          />
          <path
            d="M-60 340 C140 420, 260 250, 420 340 S680 430, 880 320"
            fill="none"
            stroke="url(#ribbonB)"
            stroke-width="34"
            stroke-linecap="round"
          />
          <path
            d="M-20 400 C180 330, 300 470, 480 390 S720 310, 900 400"
            fill="none"
            stroke="url(#ribbonA)"
            stroke-width="26"
            stroke-linecap="round"
            opacity="0.7"
          />
        </svg>
      </div>

      <div class="hero-inner">
        <div class="brand">
          <ForgeLogo :size="34" />
          <span class="brand-name">Forge</span>
        </div>

        <div class="copy">
          <h1>{{ headline }}</h1>
          <p v-if="tagline" class="tagline">{{ tagline }}</p>
          <p class="hero-desc">{{ description }}</p>
          <ul v-if="points.length" class="points">
            <li v-for="point in points" :key="point">{{ point }}</li>
          </ul>
        </div>

        <div class="stage" aria-hidden="true">
          <div class="glass panel-card">
            <div class="panel-avatar" />
            <div class="panel-lines">
              <span />
              <span />
              <span />
              <span class="short" />
            </div>
            <div class="panel-bars"><i /><i /><i /><i /><i /></div>
          </div>

          <div class="glass icon-card card-flow">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
              <circle cx="6" cy="6" r="2.2" />
              <circle cx="18" cy="6" r="2.2" />
              <circle cx="12" cy="18" r="2.2" />
              <path d="M8 7.2 10.5 15M16 7.2 13.5 15" />
            </svg>
          </div>

          <div class="glass icon-card card-spark">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
              <path d="M12 3.5 13.6 9l5.4 1.6-5.4 1.6L12 17.8 10.4 12.2 5 10.6 10.4 9Z" />
              <path
                d="M18.5 4.5 19.2 6.8 21.5 7.5 19.2 8.2 18.5 10.5 17.8 8.2 15.5 7.5 17.8 6.8Z"
              />
            </svg>
          </div>

          <div class="glass icon-card card-chart">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
              <path d="M4 19h16" />
              <path d="M7 16V10" />
              <path d="M12 16V7" />
              <path d="M17 16v-4" />
            </svg>
          </div>
        </div>
      </div>
    </aside>

    <main class="form-pane">
      <div class="card">
        <div class="card-brand">
          <ForgeLogo :size="28" />
          <span class="brand-name">Forge</span>
        </div>
        <slot />
      </div>
    </main>
  </div>
</template>

<script setup lang="ts">
import ForgeLogo from '@/components/ForgeLogo.vue'

withDefaults(
  defineProps<{
    headline: string
    tagline?: string
    description: string
    points?: string[]
  }>(),
  {
    tagline: '',
    points: () => [],
  },
)
</script>

<style scoped>
.auth-page {
  min-height: 100vh;
  display: grid;
  grid-template-columns: minmax(0, 1.08fr) minmax(0, 0.92fr);
  background: #fff;
}

.hero {
  position: relative;
  overflow: hidden;
  min-height: 100vh;
  padding: 2.4rem 3rem 2rem;
  color: #0f172a;
  background: linear-gradient(155deg, #f4f8ff 0%, #eef3ff 38%, #f6f3ff 72%, #edf5ff 100%);
}

.hero-bg {
  position: absolute;
  inset: 0;
  pointer-events: none;
}

.orb {
  position: absolute;
  border-radius: 50%;
  filter: blur(40px);
}

.orb-a {
  width: 320px;
  height: 320px;
  top: -60px;
  left: -40px;
  background: rgba(96, 165, 250, 0.45);
}

.orb-b {
  width: 280px;
  height: 280px;
  right: -40px;
  top: 28%;
  background: rgba(167, 139, 250, 0.32);
}

.orb-c {
  width: 360px;
  height: 360px;
  left: 15%;
  bottom: -80px;
  background: rgba(125, 211, 252, 0.28);
}

.network,
.ribbons {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
}

.network {
  opacity: 0.9;
  animation: networkPulse 8s ease-in-out infinite;
}

.ribbons {
  top: auto;
  height: 58%;
  bottom: 0;
  opacity: 0.95;
}

.hero-inner {
  position: relative;
  z-index: 1;
  display: flex;
  flex-direction: column;
  min-height: calc(100vh - 4.4rem);
  max-width: 38rem;
  animation: rise 0.6s ease-out both;
}

.brand,
.card-brand {
  display: flex;
  align-items: center;
  gap: 0.65rem;
}

.brand-name {
  font-family: var(--font-sans);
  font-size: 1.4rem;
  font-weight: 700;
  letter-spacing: -0.01em;
  color: #0f172a;
}

.copy {
  margin-top: 3.2rem;
}

.hero h1 {
  margin: 0;
  font-family: var(--font-display);
  font-size: clamp(2.35rem, 4vw, 3.35rem);
  font-weight: 800;
  line-height: 1.08;
  letter-spacing: -0.045em;
  color: #1d4ed8;
  max-width: 11ch;
}

.tagline {
  margin: 0.9rem 0 0;
  color: #2563eb;
  font-size: 1.15rem;
  font-weight: 700;
  letter-spacing: -0.02em;
}

.hero-desc {
  margin: 0.7rem 0 0;
  color: #64748b;
  font-size: 1.02rem;
  line-height: 1.65;
  max-width: 28rem;
}

.points {
  margin: 1.1rem 0 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-wrap: wrap;
  gap: 0.35rem 0.85rem;
  color: #64748b;
  font-size: 0.95rem;
}

.points li::before {
  content: '';
  display: inline-block;
  width: 0.35rem;
  height: 0.35rem;
  margin-right: 0.45rem;
  border-radius: 50%;
  background: #3b82f6;
  vertical-align: middle;
}

.stage {
  position: relative;
  flex: 1;
  margin-top: 2.5rem;
  min-height: 220px;
}

.glass {
  background: rgba(255, 255, 255, 0.55);
  border: 1px solid rgba(255, 255, 255, 0.75);
  box-shadow:
    0 10px 30px rgba(37, 99, 235, 0.08),
    inset 0 1px 0 rgba(255, 255, 255, 0.8);
  backdrop-filter: blur(14px);
  -webkit-backdrop-filter: blur(14px);
}

.panel-card {
  position: absolute;
  left: 0;
  bottom: 1.5rem;
  width: min(100%, 17.5rem);
  padding: 1rem 1.05rem 1.1rem;
  border-radius: 1.15rem;
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 0.75rem 0.9rem;
  animation: floatY 5.5s ease-in-out infinite;
}

.panel-avatar {
  width: 2.6rem;
  height: 2.6rem;
  border-radius: 999px;
  background: linear-gradient(145deg, #93c5fd, #6366f1);
  box-shadow: 0 6px 16px rgba(99, 102, 241, 0.25);
}

.panel-lines {
  display: grid;
  gap: 0.35rem;
  align-content: center;
}

.panel-lines span {
  display: block;
  height: 0.42rem;
  border-radius: 999px;
  background: rgba(148, 163, 184, 0.35);
}

.panel-lines span:nth-child(1) {
  width: 88%;
}

.panel-lines span:nth-child(2) {
  width: 72%;
}

.panel-lines span:nth-child(3) {
  width: 80%;
}

.panel-lines .short {
  width: 48%;
}

.panel-bars {
  grid-column: 1 / -1;
  display: flex;
  align-items: flex-end;
  gap: 0.35rem;
  height: 2.4rem;
  margin-top: 0.15rem;
}

.panel-bars i {
  display: block;
  flex: 1;
  border-radius: 0.35rem 0.35rem 0.15rem 0.15rem;
  background: linear-gradient(180deg, rgba(96, 165, 250, 0.85), rgba(99, 102, 241, 0.35));
}

.panel-bars i:nth-child(1) {
  height: 45%;
}
.panel-bars i:nth-child(2) {
  height: 70%;
}
.panel-bars i:nth-child(3) {
  height: 55%;
}
.panel-bars i:nth-child(4) {
  height: 88%;
}
.panel-bars i:nth-child(5) {
  height: 62%;
}

.icon-card {
  position: absolute;
  display: grid;
  place-items: center;
  width: 3.35rem;
  height: 3.35rem;
  border-radius: 1rem;
  color: #2563eb;
}

.icon-card svg {
  width: 1.35rem;
  height: 1.35rem;
}

.card-flow {
  left: 19rem;
  bottom: 7.2rem;
  animation: floatY 4.8s ease-in-out 0.2s infinite;
}

.card-spark {
  left: 23.2rem;
  bottom: 3.4rem;
  animation: floatY 5.2s ease-in-out 0.55s infinite;
}

.card-chart {
  left: 19.6rem;
  bottom: 0.4rem;
  animation: floatY 4.6s ease-in-out 0.9s infinite;
}

.form-pane {
  display: grid;
  place-items: center;
  padding: 2rem 1.25rem;
  background: #f3f5fa;
}

.card {
  width: min(100%, 26.5rem);
  padding: 2rem 1.75rem 1.75rem;
  border-radius: 1.25rem;
  background: #fff;
  box-shadow:
    0 1px 2px rgba(15, 23, 42, 0.04),
    0 18px 50px rgba(15, 23, 42, 0.08);
  animation: rise 0.55s ease-out 0.08s both;
}

.card-brand {
  justify-content: center;
  margin-bottom: 1.35rem;
}

@keyframes rise {
  from {
    opacity: 0;
    transform: translateY(12px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

@keyframes floatY {
  0%,
  100% {
    transform: translateY(0);
  }
  50% {
    transform: translateY(-10px);
  }
}

@keyframes networkPulse {
  0%,
  100% {
    opacity: 0.82;
  }
  50% {
    opacity: 1;
  }
}

@media (max-width: 1100px) {
  .card-flow {
    left: 16.5rem;
  }

  .card-spark {
    left: 20.5rem;
  }

  .card-chart {
    left: 17rem;
  }
}

@media (max-width: 900px) {
  .auth-page {
    grid-template-columns: 1fr;
  }

  .hero {
    min-height: auto;
    padding: 1.5rem 1.25rem 2.25rem;
  }

  .hero-inner {
    min-height: auto;
  }

  .copy {
    margin-top: 1.6rem;
  }

  .hero h1 {
    font-size: 2.1rem;
    max-width: none;
  }

  .stage {
    min-height: 180px;
    margin-top: 1.75rem;
  }

  .panel-card {
    width: min(100%, 15.5rem);
  }

  .card-flow {
    left: auto;
    right: 4.8rem;
    bottom: 6.2rem;
  }

  .card-spark {
    left: auto;
    right: 0.8rem;
    bottom: 3.4rem;
  }

  .card-chart {
    left: auto;
    right: 4.2rem;
    bottom: 0.5rem;
  }
}
</style>
