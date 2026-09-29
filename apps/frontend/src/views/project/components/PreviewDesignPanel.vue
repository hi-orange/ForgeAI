<template>
  <aside class="design-panel" aria-label="Design 编辑器">
    <header class="design-heading">
      <div>
        <span>DESIGN</span>
        <strong>设计与预览</strong>
      </div>
      <button type="button" aria-label="退出 Design" title="退出 Design" @click="$emit('close')">
        ×
      </button>
    </header>

    <nav class="design-tabs" aria-label="Design 工具">
      <button
        v-for="item in tabs"
        :key="item.id"
        type="button"
        :class="{ active: activeTab === item.id }"
        @click="activeTab = item.id"
      >
        {{ item.label }}
      </button>
    </nav>

    <div class="design-scroll">
      <section v-if="activeTab === 'theme'" class="design-section theme-section">
        <label class="search-field">
          <span>搜索主题</span>
          <input v-model="themeQuery" type="search" placeholder="Search Themes" />
        </label>

        <div class="section-title">
          <strong>Current Theme</strong>
          <span>修订 {{ state.revision }}</span>
        </div>
        <article class="current-theme">
          <div class="theme-preview" :style="themePreviewStyle(state.theme)">
            <i /><b /><span />
          </div>
          <div>
            <strong>{{ state.theme.name }}</strong>
            <div class="swatches">
              <i
                v-for="color in themeColors(state.theme)"
                :key="color"
                :style="{ background: color }"
              />
            </div>
          </div>
        </article>

        <div class="section-title"><strong>Default Theme</strong><span>精选预设</span></div>
        <div class="theme-grid">
          <button
            v-for="theme in filteredThemes"
            :key="theme.preset_id"
            type="button"
            class="theme-card"
            :class="{ selected: state.theme.preset_id === theme.preset_id }"
            @click="$emit('apply-theme', theme)"
          >
            <span class="theme-preview" :style="themePreviewStyle(theme)"><i /><b /><span /></span>
            <strong>{{ theme.name }}</strong>
            <span class="swatches">
              <i v-for="color in themeColors(theme)" :key="color" :style="{ background: color }" />
            </span>
          </button>
        </div>

        <div class="section-title"><strong>Theme Manager</strong><span>实时调整</span></div>
        <div class="control-grid">
          <label
            ><span>主题名称</span
            ><input :value="state.theme.name" @input="updateTheme('name', eventValue($event))"
          /></label>
          <label
            ><span>Primary</span
            ><input
              type="color"
              :value="state.theme.primary"
              @input="updateTheme('primary', eventValue($event))"
          /></label>
          <label
            ><span>Background</span
            ><input
              type="color"
              :value="state.theme.background"
              @input="updateTheme('background', eventValue($event))"
          /></label>
          <label
            ><span>Surface</span
            ><input
              type="color"
              :value="state.theme.surface"
              @input="updateTheme('surface', eventValue($event))"
          /></label>
          <label
            ><span>Text</span
            ><input
              type="color"
              :value="state.theme.text"
              @input="updateTheme('text', eventValue($event))"
          /></label>
          <label
            ><span>Border</span
            ><input
              type="color"
              :value="state.theme.border"
              @input="updateTheme('border', eventValue($event))"
          /></label>
          <label class="wide"
            ><span>Font</span
            ><select
              :value="state.theme.font_family"
              @change="updateTheme('font_family', eventValue($event))"
            >
              <option v-for="font in fonts" :key="font" :value="font">
                {{ font.split(',')[0] }}
              </option>
            </select></label
          >
          <label class="wide"
            ><span>Radius · {{ state.theme.radius }}px</span
            ><input
              type="range"
              min="0"
              max="32"
              :value="state.theme.radius"
              @input="updateTheme('radius', Number(eventValue($event)))"
          /></label>
        </div>
      </section>

      <section v-else-if="activeTab === 'edit'" class="design-section">
        <div v-if="!selection" class="selection-empty">
          <span class="selection-icon">⌖</span>
          <strong>选择一个元素开始编辑</strong>
          <p>在右侧预览中点击文字、按钮、图片或容器。</p>
        </div>
        <template v-else>
          <div class="selection-card">
            <div>
              <strong>{{ selection.label }}</strong
              ><code>{{ selection.selector }}</code>
            </div>
            <span>{{ selection.rect.width }} × {{ selection.rect.height }}</span>
          </div>

          <details open>
            <summary>Layout</summary>
            <div class="control-grid">
              <label
                ><span>Margin</span
                ><input
                  :value="styleValue('margin')"
                  @input="updateStyle('margin', eventValue($event))"
              /></label>
              <label
                ><span>Padding</span
                ><input
                  :value="styleValue('padding')"
                  @input="updateStyle('padding', eventValue($event))"
              /></label>
            </div>
          </details>
          <details open>
            <summary>Appearance</summary>
            <div class="control-grid">
              <label
                ><span>背景</span
                ><input
                  type="color"
                  :value="colorValue(styleValue('backgroundColor'), '#ffffff')"
                  @input="updateStyle('backgroundColor', eventValue($event))"
              /></label>
              <label
                ><span>文字</span
                ><input
                  type="color"
                  :value="colorValue(styleValue('color'), '#111827')"
                  @input="updateStyle('color', eventValue($event))"
              /></label>
              <label class="wide"
                ><span>不透明度 · {{ styleValue('opacity') || '1' }}</span
                ><input
                  type="range"
                  min="0"
                  max="1"
                  step="0.05"
                  :value="styleValue('opacity') || '1'"
                  @input="updateStyle('opacity', eventValue($event))"
              /></label>
              <label class="wide"
                ><span>Radius</span
                ><select
                  :value="styleValue('borderRadius')"
                  @change="updateStyle('borderRadius', eventValue($event))"
                >
                  <option v-for="radius in radii" :key="radius.value" :value="radius.value">
                    {{ radius.label }}
                  </option>
                </select></label
              >
            </div>
          </details>
          <details v-if="selection.tag !== 'img'" open>
            <summary>Content & Style</summary>
            <label class="stacked"
              ><span>内容</span
              ><textarea
                :value="selection.text ?? ''"
                rows="3"
                @input="updateText(eventValue($event))"
              />
            </label>
            <div class="control-grid">
              <label
                ><span>字号</span
                ><input
                  :value="styleValue('fontSize')"
                  @input="updateStyle('fontSize', eventValue($event))"
              /></label>
              <label
                ><span>字重</span
                ><select
                  :value="styleValue('fontWeight')"
                  @change="updateStyle('fontWeight', eventValue($event))"
                >
                  <option v-for="weight in weights" :key="weight" :value="weight">
                    {{ weight }}
                  </option>
                </select></label
              >
              <label
                ><span>对齐</span
                ><select
                  :value="styleValue('textAlign')"
                  @change="updateStyle('textAlign', eventValue($event))"
                >
                  <option
                    v-for="align in ['left', 'center', 'right', 'justify']"
                    :key="align"
                    :value="align"
                  >
                    {{ align }}
                  </option>
                </select></label
              >
              <label
                ><span>装饰</span
                ><select
                  :value="styleValue('textDecoration')"
                  @change="updateStyle('textDecoration', eventValue($event))"
                >
                  <option value="none">none</option>
                  <option value="underline">underline</option>
                  <option value="line-through">line-through</option>
                </select></label
              >
            </div>
          </details>
          <details>
            <summary>Class</summary>
            <code class="class-value">{{ selection.class_name || 'No class' }}</code>
          </details>
        </template>
      </section>

      <section v-else class="design-section library-section">
        <p v-if="!selection" class="library-hint">
          请先在预览中选择图片或容器，再从 Library 应用素材。
        </p>
        <div class="library-tabs">
          <button
            v-for="source in librarySources"
            :key="source"
            type="button"
            :class="{ active: librarySource === source }"
            @click="librarySource = source"
          >
            {{ source }}
          </button>
        </div>
        <template v-if="librarySource === 'Image'">
          <label class="search-field"
            ><span>图片地址</span><input v-model="imageUrl" type="url" placeholder="https://…"
          /></label>
          <button
            class="apply-url"
            type="button"
            :disabled="!selection || !imageUrl.trim()"
            @click="applyImage(imageUrl.trim())"
          >
            使用图片地址
          </button>
          <div class="section-title"><strong>Popular Images</strong><span>内置精选</span></div>
          <div class="image-grid">
            <button
              v-for="image in libraryImages"
              :key="image.url"
              type="button"
              :disabled="!selection"
              @click="applyImage(image.url)"
            >
              <img :src="image.url" :alt="image.label" loading="lazy" />
              <span>Use asset</span>
            </button>
          </div>
        </template>
        <template v-else-if="librarySource === 'Upload'">
          <label class="upload-zone">
            <input
              type="file"
              accept="image/png,image/jpeg,image/gif,image/webp,image/bmp"
              @change="uploadImage"
            />
            <strong>点击上传图片</strong>
            <span>PNG、JPEG、GIF、WEBP 或 BMP，最大 4MB</span>
          </label>
          <p v-if="uploadError" class="panel-error">{{ uploadError }}</p>
        </template>
        <div v-else class="selection-empty">
          <span class="selection-icon">✦</span>
          <strong>图片生成稍后接入</strong>
          <p>当前版本先支持内置图片、URL 与本地上传。</p>
        </div>
      </section>
    </div>

    <footer v-if="dirty" class="design-savebar">
      <button type="button" class="discard" :disabled="saving" @click="$emit('discard')">
        Discard
      </button>
      <button type="button" class="save" :disabled="saving" @click="$emit('save')">
        {{ saving ? '保存中…' : 'Save' }}
      </button>
    </footer>
  </aside>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import type {
  PreviewDesignState,
  PreviewElementOverride,
  PreviewSelection,
  PreviewTheme,
} from '@/api/modules/preview'

type DesignTab = 'theme' | 'edit' | 'library'

const props = defineProps<{
  state: PreviewDesignState
  selection: PreviewSelection | null
  dirty: boolean
  saving: boolean
}>()

const emit = defineEmits<{
  close: []
  save: []
  discard: []
  'apply-theme': [theme: PreviewTheme]
  'update-theme': [patch: Partial<PreviewTheme>]
  'update-selection': [
    patch: Partial<Pick<PreviewElementOverride, 'styles' | 'text' | 'image_url'>>,
  ]
}>()

const activeTab = ref<DesignTab>('theme')
const themeQuery = ref('')
const librarySource = ref<'Image' | 'Generate' | 'Upload'>('Image')
const imageUrl = ref('')
const uploadError = ref('')
const tabs: Array<{ id: DesignTab; label: string }> = [
  { id: 'theme', label: 'Theme' },
  { id: 'edit', label: '可视化编辑' },
  { id: 'library', label: 'Library' },
]
const fonts = [
  'Inter, system-ui, sans-serif',
  'Manrope, system-ui, sans-serif',
  'Georgia, serif',
  'ui-monospace, SFMono-Regular, monospace',
]
const radii = [
  { label: 'None', value: '0px' },
  { label: 'Small', value: '4px' },
  { label: 'Medium', value: '8px' },
  { label: 'Large', value: '12px' },
  { label: 'XL', value: '16px' },
  { label: '2XL', value: '24px' },
  { label: 'Full', value: '9999px' },
]
const weights = ['300', '400', '500', '600', '700', '800']
const themes: PreviewTheme[] = [
  {
    preset_id: 'forge',
    name: 'Forge',
    background: '#f8fafc',
    surface: '#ffffff',
    text: '#0f172a',
    primary: '#2563eb',
    muted: '#64748b',
    border: '#e2e8f0',
    font_family: fonts[0]!,
    radius: 12,
    shadow: '0 12px 32px rgba(15, 23, 42, 0.08)',
  },
  {
    preset_id: 'midnight',
    name: 'Midnight',
    background: '#09090b',
    surface: '#18181b',
    text: '#fafafa',
    primary: '#8b5cf6',
    muted: '#a1a1aa',
    border: '#3f3f46',
    font_family: fonts[1]!,
    radius: 16,
    shadow: '0 18px 48px rgba(0, 0, 0, 0.35)',
  },
  {
    preset_id: 'sage',
    name: 'Sage Studio',
    background: '#f4f7f1',
    surface: '#ffffff',
    text: '#233127',
    primary: '#4f7657',
    muted: '#748078',
    border: '#d9e2d7',
    font_family: fonts[2]!,
    radius: 8,
    shadow: '0 10px 28px rgba(35, 49, 39, 0.10)',
  },
  {
    preset_id: 'sunset',
    name: 'Sunset',
    background: '#fff7ed',
    surface: '#ffffff',
    text: '#431407',
    primary: '#ea580c',
    muted: '#9a6b5b',
    border: '#fed7aa',
    font_family: fonts[0]!,
    radius: 20,
    shadow: '0 16px 38px rgba(154, 52, 18, 0.12)',
  },
  {
    preset_id: 'editorial',
    name: 'Editorial',
    background: '#f5f5f4',
    surface: '#fafaf9',
    text: '#1c1917',
    primary: '#be123c',
    muted: '#78716c',
    border: '#d6d3d1',
    font_family: fonts[2]!,
    radius: 2,
    shadow: '0 8px 20px rgba(28, 25, 23, 0.08)',
  },
  {
    preset_id: 'ocean',
    name: 'Ocean',
    background: '#ecfeff',
    surface: '#ffffff',
    text: '#083344',
    primary: '#0891b2',
    muted: '#527581',
    border: '#a5f3fc',
    font_family: fonts[1]!,
    radius: 14,
    shadow: '0 14px 36px rgba(8, 145, 178, 0.13)',
  },
]
const libraryImages = [
  {
    label: 'Architecture',
    url: 'https://images.unsplash.com/photo-1486406146926-c627a92ad1ab?auto=format&fit=crop&w=900&q=80',
  },
  {
    label: 'Workspace',
    url: 'https://images.unsplash.com/photo-1497366754035-f200968a6e72?auto=format&fit=crop&w=900&q=80',
  },
  {
    label: 'Nature',
    url: 'https://images.unsplash.com/photo-1500530855697-b586d89ba3ee?auto=format&fit=crop&w=900&q=80',
  },
  {
    label: 'People',
    url: 'https://images.unsplash.com/photo-1521737711867-e3b97375f902?auto=format&fit=crop&w=900&q=80',
  },
]
const librarySources = ['Image', 'Generate', 'Upload'] as const
const filteredThemes = computed(() => {
  const query = themeQuery.value.trim().toLowerCase()
  return themes.filter((theme) => !query || theme.name.toLowerCase().includes(query))
})

function eventValue(event: Event) {
  return (event.target as HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement).value
}
function updateTheme<Key extends keyof PreviewTheme>(key: Key, value: PreviewTheme[Key]) {
  emit('update-theme', { [key]: value } as Partial<PreviewTheme>)
}
function styleValue(key: keyof PreviewElementOverride['styles']) {
  return props.selection?.styles[key] ?? ''
}
function updateStyle(key: keyof PreviewElementOverride['styles'], value: string) {
  emit('update-selection', { styles: { [key]: value } })
}
function updateText(value: string) {
  emit('update-selection', { text: value })
}
function applyImage(url: string) {
  if (!url) return
  emit('update-selection', { image_url: url })
}
function themeColors(theme: PreviewTheme) {
  return [theme.primary, theme.background, theme.surface, theme.text]
}
function themePreviewStyle(theme: PreviewTheme) {
  return {
    '--theme-bg': theme.background,
    '--theme-surface': theme.surface,
    '--theme-text': theme.text,
    '--theme-primary': theme.primary,
    '--theme-radius': `${theme.radius}px`,
  }
}
function colorValue(value: string, fallback: string) {
  if (/^#[0-9a-f]{6}$/i.test(value)) return value
  const match = value.match(/^rgba?\((\d+),\s*(\d+),\s*(\d+)/i)
  if (!match) return fallback
  return `#${match
    .slice(1, 4)
    .map((part) => Number(part).toString(16).padStart(2, '0'))
    .join('')}`
}
function uploadImage(event: Event) {
  uploadError.value = ''
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  if (file.size > 4 * 1024 * 1024) {
    uploadError.value = '图片不能超过 4MB。'
    input.value = ''
    return
  }
  const reader = new FileReader()
  reader.addEventListener('load', () => {
    if (typeof reader.result === 'string') applyImage(reader.result)
  })
  reader.readAsDataURL(file)
}
</script>

<style scoped lang="scss">
.design-panel {
  display: flex;
  width: 320px;
  min-width: 280px;
  min-height: 0;
  flex-direction: column;
  border-right: 1px solid #e4e4e7;
  background: #fff;
}
.design-heading,
.section-title,
.selection-card,
.design-savebar {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.design-heading {
  min-height: 55px;
  padding: 0 14px;
  border-bottom: 1px solid #ededf0;
  div {
    display: grid;
    gap: 2px;
  }
  span {
    color: #9a9ca8;
    font-size: 9px;
    letter-spacing: 1.6px;
  }
  strong {
    font-size: 13px;
  }
  button {
    border: 0;
    background: transparent;
    color: #777985;
    font-size: 22px;
    cursor: pointer;
  }
}
.design-tabs,
.library-tabs {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  padding: 8px;
  gap: 4px;
  border-bottom: 1px solid #ededf0;
  button {
    border: 0;
    border-radius: 7px;
    padding: 7px 4px;
    background: transparent;
    color: #777985;
    font-size: 11px;
    cursor: pointer;
  }
  button.active {
    background: #f0f1ff;
    color: #4649a8;
    font-weight: 650;
  }
}
.design-scroll {
  flex: 1;
  min-height: 0;
  overflow: auto;
  scrollbar-width: thin;
  scrollbar-color: transparent transparent;
}
.design-scroll:hover {
  scrollbar-color: #d5d6dc transparent;
}
.design-section {
  display: grid;
  gap: 14px;
  padding: 14px;
}
.section-title {
  margin-top: 2px;
  strong {
    font-size: 12px;
  }
  span {
    color: #9a9ca8;
    font-size: 10px;
  }
}
.search-field,
.stacked,
.control-grid label {
  display: grid;
  gap: 5px;
  color: #777985;
  font-size: 10px;
}
input,
select,
textarea {
  width: 100%;
  box-sizing: border-box;
  border: 1px solid #dedfe5;
  border-radius: 7px;
  background: #fff;
  padding: 7px 8px;
  color: #27272a;
  font: inherit;
  outline: none;
}
input:focus,
select:focus,
textarea:focus {
  border-color: #818cf8;
  box-shadow: 0 0 0 2px #e0e7ff;
}
input[type='color'] {
  height: 34px;
  padding: 3px;
}
input[type='range'] {
  padding: 0;
  box-shadow: none;
}
.current-theme {
  display: grid;
  grid-template-columns: 94px 1fr;
  gap: 10px;
  align-items: center;
  padding: 9px;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
}
.theme-preview {
  position: relative;
  display: block;
  height: 62px;
  overflow: hidden;
  border-radius: var(--theme-radius);
  background: var(--theme-bg);
  border: 1px solid #00000010;
  i {
    position: absolute;
    inset: 10px 8px auto;
    height: 8px;
    border-radius: 4px;
    background: var(--theme-primary);
  }
  b {
    position: absolute;
    inset: 24px 8px 8px;
    border-radius: calc(var(--theme-radius) * 0.6);
    background: var(--theme-surface);
  }
  span {
    position: absolute;
    left: 14px;
    right: 28px;
    bottom: 15px;
    height: 4px;
    border-radius: 3px;
    background: var(--theme-text);
    opacity: 0.7;
  }
}
.swatches {
  display: flex;
  gap: 3px;
  margin-top: 7px;
  i {
    width: 12px;
    height: 12px;
    border: 1px solid #00000010;
    border-radius: 50%;
  }
}
.theme-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}
.theme-card {
  display: grid;
  gap: 5px;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  background: #fff;
  padding: 7px;
  text-align: left;
  cursor: pointer;
  strong {
    font-size: 10px;
  }
  &.selected {
    border-color: #6366f1;
    box-shadow: 0 0 0 1px #6366f1;
  }
}
.control-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 9px;
  .wide {
    grid-column: 1 / -1;
  }
}
details {
  border-top: 1px solid #ededf0;
  padding-top: 11px;
  summary {
    color: #3f3f46;
    font-size: 11px;
    font-weight: 650;
    cursor: pointer;
    margin-bottom: 10px;
  }
}
.selection-empty {
  display: grid;
  justify-items: center;
  gap: 7px;
  padding: 38px 12px;
  color: #8b8d98;
  text-align: center;
  strong {
    color: #444650;
    font-size: 12px;
  }
  p {
    margin: 0;
    font-size: 11px;
    line-height: 1.6;
  }
}
.selection-icon {
  display: grid;
  width: 42px;
  height: 42px;
  place-items: center;
  border-radius: 50%;
  background: #eef0ff;
  color: #6366f1;
  font-size: 21px;
}
.selection-card {
  align-items: flex-start;
  gap: 8px;
  padding: 10px;
  border-radius: 9px;
  background: #f6f7fb;
  div {
    min-width: 0;
    display: grid;
    gap: 4px;
  }
  strong {
    overflow: hidden;
    font-size: 11px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  code {
    overflow: hidden;
    color: #8b8d98;
    font-size: 9px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  > span {
    flex-shrink: 0;
    color: #777985;
    font-size: 9px;
  }
}
.class-value {
  display: block;
  overflow-wrap: anywhere;
  padding: 8px;
  border-radius: 7px;
  background: #f5f5f7;
  color: #777985;
  font-size: 9px;
}
.library-hint {
  margin: 0;
  padding: 9px;
  border-radius: 8px;
  background: #fff7ed;
  color: #9a5b1f;
  font-size: 10px;
  line-height: 1.5;
}
.image-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  button {
    position: relative;
    height: 96px;
    overflow: hidden;
    border: 0;
    border-radius: 9px;
    padding: 0;
    cursor: pointer;
  }
  img {
    width: 100%;
    height: 100%;
    object-fit: cover;
  }
  span {
    position: absolute;
    inset: auto 6px 6px;
    padding: 5px;
    border-radius: 6px;
    background: #18181bcc;
    color: #fff;
    font-size: 9px;
    opacity: 0;
  }
  button:hover span {
    opacity: 1;
  }
}
.apply-url {
  border: 0;
  border-radius: 7px;
  background: #27272a;
  color: #fff;
  padding: 8px;
  font-size: 11px;
  cursor: pointer;
  &:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }
}
.upload-zone {
  display: grid;
  justify-items: center;
  gap: 7px;
  padding: 34px 12px;
  border: 1px dashed #c7c9d3;
  border-radius: 12px;
  color: #8b8d98;
  text-align: center;
  cursor: pointer;
  input {
    display: none;
  }
  strong {
    color: #444650;
    font-size: 12px;
  }
  span {
    font-size: 10px;
  }
}
.panel-error {
  color: #b91c1c;
  font-size: 10px;
}
.design-savebar {
  gap: 8px;
  padding: 10px 12px;
  border-top: 1px solid #e4e4e7;
  background: #fff;
  button {
    flex: 1;
    border-radius: 8px;
    padding: 8px;
    font-size: 11px;
    font-weight: 650;
    cursor: pointer;
  }
  .discard {
    border: 1px solid #dedfe5;
    background: #fff;
  }
  .save {
    border: 1px solid #4f46e5;
    background: #4f46e5;
    color: #fff;
  }
}
</style>
