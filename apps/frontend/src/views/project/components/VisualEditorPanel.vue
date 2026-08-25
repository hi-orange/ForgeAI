<template>
  <div class="visual-editor">
    <header class="visual-editor-head">
      <nav class="editor-tabs" aria-label="设计工具">
        <button
          v-for="tab in tabs"
          :key="tab.key"
          type="button"
          :class="{ active: editorTab === tab.key }"
          @click="$emit('update:editorTab', tab.key)"
        >
          {{ tab.label }}
        </button>
      </nav>
    </header>

    <div v-if="editorTab !== 'visual'" class="editor-empty editor-placeholder">
      <span class="editor-cursor" aria-hidden="true">◇</span>
      <strong>{{
        editorTab === 'library' ? '组件库将在下一阶段接入' : '全局主题将在下一阶段接入'
      }}</strong>
      <p>
        {{
          editorTab === 'library'
            ? '这里将用于插入图标、图片和预制组件。'
            : '这里将统一管理颜色、字体、间距和圆角变量。'
        }}
      </p>
    </div>

    <template v-else>
      <!-- Ask conversation: left/right chat bubbles (same idea as main chat). -->
      <div v-if="conversationActive" class="element-ai-conversation" ref="conversationRef">
        <div v-if="selectedElement" class="selected-element-toolbar compact chat-head">
          <div class="selected-element-meta">
            <span class="element-ai-chip">{{ selectedElement.tagName.toLowerCase() }}</span>
            <code>{{ selectionMeta }}</code>
          </div>
          <button
            type="button"
            class="editor-icon-btn"
            title="撤销上一步"
            :disabled="!canUndo"
            @click="$emit('undo')"
          >
            ↶
          </button>
        </div>

        <div class="element-ai-thread" ref="threadRef">
          <div
            v-for="(item, index) in chatMessages"
            :key="`${item.role}-${index}`"
            class="element-ai-row"
            :class="item.role"
          >
            <div class="element-ai-bubble" :class="item.role">
              <p>{{ item.content }}</p>
            </div>
          </div>
          <div v-if="suggesting" class="element-ai-row assistant">
            <div class="element-ai-bubble assistant working">
              <p>想一下…</p>
            </div>
          </div>
        </div>
      </div>

      <div v-else-if="!selectedElement" class="editor-empty">
        <strong>选择一个元素开始可视化编辑</strong>
        <p>在右侧画布点击元素；双击文字可直接编辑。</p>
      </div>

      <div v-else class="editor-controls">
        <div class="selected-element-toolbar">
          <div class="selected-element-meta">
            <span class="element-ai-chip">{{ selectedElement.tagName.toLowerCase() }}</span>
            <code>{{ selectionMeta }}</code>
          </div>
          <button
            type="button"
            class="editor-icon-btn"
            title="撤销上一步"
            :disabled="!canUndo"
            @click="$emit('undo')"
          >
            ↶
          </button>
        </div>

        <section class="editor-section">
          <h3>Content</h3>
          <p v-if="selectedElement.textEditable" class="editor-note">
            可在这里修改，也可以双击右侧文字直接编辑。
          </p>
          <label v-if="selectedElement.textEditable" class="editor-field">
            <textarea
              :value="selectedElement.text"
              rows="4"
              maxlength="2000"
              @input="$emit('update-text', $event)"
            />
          </label>
          <p v-else class="editor-note">该元素包含子元素，暂不支持直接替换文字。</p>
        </section>

        <section class="editor-section">
          <h3>Typography</h3>
          <div class="editor-grid two-columns">
            <label class="compact-field">
              <span>Size</span>
              <input
                type="number"
                min="8"
                max="200"
                :value="cssNumber('font-size')"
                @input="$emit('update-pixel', 'font-size', $event)"
              />
            </label>
            <label class="compact-field">
              <span>Weight</span>
              <select
                :value="styleValue('font-weight')"
                @change="$emit('update-select', 'font-weight', $event)"
              >
                <option v-for="weight in fontWeights" :key="weight" :value="weight">
                  {{ weight }}
                </option>
              </select>
            </label>
            <label class="compact-field full-width">
              <span>Font</span>
              <select
                :value="styleValue('font-family')"
                @change="$emit('update-select', 'font-family', $event)"
              >
                <option v-for="font in fontFamilies" :key="font" :value="font">
                  {{ font }}
                </option>
              </select>
            </label>
          </div>
          <div class="alignment-control" aria-label="文本对齐">
            <button
              v-for="alignment in textAlignments"
              :key="alignment.value"
              type="button"
              :class="{ active: styleValue('text-align') === alignment.value }"
              @click="$emit('update-style', 'text-align', alignment.value)"
            >
              {{ alignment.icon }}
            </button>
          </div>
        </section>

        <section class="editor-section">
          <h3>Appearance</h3>
          <div class="editor-grid three-columns">
            <label
              v-for="colorControl in colorControls"
              :key="colorControl.property"
              class="compact-field color-compact"
            >
              <span>{{ colorControl.label }}</span>
              <input
                type="color"
                :value="styleValue(colorControl.property)"
                @input="$emit('update-color', colorControl.property, $event)"
              />
            </label>
          </div>
        </section>

        <section class="editor-section">
          <h3>Layout</h3>
          <p class="field-group-label">Margin</p>
          <div class="editor-grid four-columns">
            <label v-for="side in boxSides" :key="`margin-${side}`" class="compact-field">
              <span>{{ side.slice(0, 1).toUpperCase() }}</span>
              <input
                type="number"
                :value="cssNumber(`margin-${side}`)"
                @input="$emit('update-pixel', `margin-${side}`, $event)"
              />
            </label>
          </div>
          <p class="field-group-label">Padding</p>
          <div class="editor-grid four-columns">
            <label v-for="side in boxSides" :key="`padding-${side}`" class="compact-field">
              <span>{{ side.slice(0, 1).toUpperCase() }}</span>
              <input
                type="number"
                min="0"
                :value="cssNumber(`padding-${side}`)"
                @input="$emit('update-pixel', `padding-${side}`, $event)"
              />
            </label>
          </div>
          <div class="editor-grid two-columns layout-tail">
            <label class="compact-field">
              <span>Gap</span>
              <input
                type="number"
                min="0"
                :value="cssNumber('gap')"
                @input="$emit('update-pixel', 'gap', $event)"
              />
            </label>
            <label class="compact-field">
              <span>Radius</span>
              <input
                type="number"
                min="0"
                :value="cssNumber('border-radius')"
                @input="$emit('update-pixel', 'border-radius', $event)"
              />
            </label>
          </div>
        </section>
      </div>

      <p v-if="editorError" class="plan-error editor-error">{{ editorError }}</p>
      <section
        class="element-ai-composer"
        :class="{ selected: selectedElement, conversation: conversationActive }"
      >
        <div class="element-ai-context">
          <span class="element-ai-mode">Design</span>
          <span v-if="selectedElement" class="element-ai-chip">
            {{ selectedElement.tagName.toLowerCase() }}
          </span>
        </div>

        <div class="element-ai-input-row">
          <textarea
            :value="elementInstruction"
            rows="2"
            maxlength="2000"
            :disabled="!selectedElement || suggesting"
            placeholder="告诉 AI 如何修改当前元素…"
            @input="
              $emit('update:elementInstruction', ($event.target as HTMLTextAreaElement).value)
            "
            @keydown.enter.exact.prevent="$emit('ask-ai')"
          />
          <button
            type="button"
            class="element-ai-send"
            :disabled="!selectedElement || !elementInstruction.trim() || suggesting"
            title="修改当前元素"
            @click="$emit('ask-ai')"
          >
            {{ suggesting ? '…' : '↑' }}
          </button>
        </div>
        <p class="element-ai-hint">
          {{
            selectedElement
              ? '指令清晰时会改文件、校验并保存新版本；含糊时会先追问。'
              : conversationActive
                ? '对话保留在此；重新选中元素可继续提问。'
                : '选中元素后，可让 AI 修改文案、颜色、字体和间距。'
          }}
        </p>
      </section>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'

import type { EditableStyleName } from '@/api/modules/project'
import type { DesignChatMessage, EditorTab, SelectedEditableElement } from '../projectView'

const props = defineProps<{
  editorTab: EditorTab
  selectedElement: SelectedEditableElement | null
  elementInstruction: string
  editorError: string | null
  canUndo: boolean
  suggesting: boolean
  chatMessages: DesignChatMessage[]
  styleValue: (property: EditableStyleName) => string
  cssNumber: (property: EditableStyleName) => number
}>()

defineEmits<{
  'update:editorTab': [value: EditorTab]
  'update:elementInstruction': [value: string]
  undo: []
  'update-text': [event: Event]
  'update-style': [property: EditableStyleName, value: string]
  'update-pixel': [property: EditableStyleName, event: Event]
  'update-select': [property: EditableStyleName, event: Event]
  'update-color': [property: EditableStyleName, event: Event]
  'ask-ai': []
}>()

const threadRef = ref<HTMLElement | null>(null)
const conversationRef = ref<HTMLElement | null>(null)

const conversationActive = computed(() => props.chatMessages.length > 0 || props.suggesting)

async function scrollConversationIntoView() {
  await nextTick()
  if (conversationRef.value) {
    conversationRef.value.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  }
  if (threadRef.value) {
    threadRef.value.scrollTop = threadRef.value.scrollHeight
  }
}

watch(
  () => [props.chatMessages.length, props.suggesting] as const,
  async () => {
    if (!conversationActive.value) return
    await scrollConversationIntoView()
  },
)

const tabs: { key: EditorTab; label: string }[] = [
  { key: 'visual', label: 'Visual Editor' },
  { key: 'library', label: 'Library' },
  { key: 'theme', label: 'Theme' },
]

const fontWeights = ['100', '200', '300', '400', '500', '600', '700', '800', '900']
const fontFamilies = ['Inter', 'Arial', 'Georgia', 'system-ui', 'sans-serif', 'serif']
const textAlignments = [
  { value: 'left', icon: '≡' },
  { value: 'center', icon: '≣' },
  { value: 'right', icon: '≡' },
  { value: 'justify', icon: '☰' },
] as const
const colorControls: { label: string; property: EditableStyleName }[] = [
  { label: 'Text', property: 'color' },
  { label: 'Fill', property: 'background-color' },
  { label: 'Border', property: 'border-color' },
]
const boxSides = ['top', 'right', 'bottom', 'left'] as const

const selectionMeta = computed(() => {
  const element = props.selectedElement
  if (!element) return ''
  const fontSize = Number.parseFloat(element.styles['font-size'] || '')
  if (Number.isFinite(fontSize)) return `${Math.round(fontSize)}px`
  return `${Math.round(element.rect.width)} × ${Math.round(element.rect.height)}`
})
</script>

<style scoped lang="scss">
.visual-editor {
  display: flex;
  flex: 1;
  min-height: 0;
  flex-direction: column;
  background: #fff;
}

.visual-editor-head {
  display: flex;
  align-items: center;
  justify-content: flex-start;
  gap: 1rem;
  padding: 0.85rem 1rem;
  border-bottom: 1px solid #e8edf5;
}

.editor-tabs {
  display: flex;
  gap: 0.9rem;
  min-width: 0;
}

.editor-tabs button {
  border: 0;
  padding: 0.25rem 0;
  background: transparent;
  color: #64748b;
  font-size: 0.8rem;
  cursor: pointer;
}

.editor-tabs button.active {
  color: #0f172a;
  font-weight: 800;
}

.editor-empty {
  display: grid;
  flex: 1;
  place-content: center;
  gap: 0.5rem;
  padding: 2rem 1.5rem;
  text-align: center;
  color: #64748b;
}

.editor-empty strong {
  color: #0f172a;
  font-size: 0.9rem;
  font-weight: 600;
}

.editor-empty p {
  margin: 0;
  font-size: 0.78rem;
  line-height: 1.5;
  color: #94a3b8;
}

.editor-note {
  margin: 0;
  color: #94a3b8;
  font-size: 0.82rem;
  line-height: 1.5;
}

.editor-cursor {
  color: #2563eb;
  font-size: 2.25rem;
}

.editor-controls {
  display: grid;
  align-content: start;
  flex: 1;
  min-height: 0;
  gap: 0;
  overflow-x: hidden;
  overflow-y: auto;
  padding: 0;
  scrollbar-gutter: stable;
}

.element-ai-conversation {
  display: flex;
  flex: 1;
  min-height: 0;
  flex-direction: column;
  background: #f7f9fc;
}

.element-ai-conversation .element-ai-thread {
  flex: 1;
  max-height: none;
  margin: 0;
  padding: 0.7rem 0.75rem 0.55rem;
  overflow: auto;
}

.element-ai-composer {
  flex: 0 0 auto;
  margin: 0.65rem;
  margin-top: 0;
  padding: 0.75rem;
  border: 1px solid #e5e7eb;
  border-radius: 1rem;
  background: #fff;
  box-shadow: 0 8px 24px rgba(15, 23, 42, 0.08);
}

.element-ai-composer.selected {
  border-color: #c7d2fe;
}

.element-ai-composer.conversation {
  margin: 0.45rem 0.65rem 0.65rem;
  padding: 0.55rem 0.65rem 0.6rem;
  border-radius: 0.9rem;
  box-shadow: 0 4px 14px rgba(15, 23, 42, 0.06);
}

.element-ai-context {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  min-height: 1.35rem;
  margin-bottom: 0.35rem;
  color: #334155;
  font-size: 0.72rem;
  font-weight: 700;
}

.element-ai-thread {
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
  max-height: 11rem;
  margin-bottom: 0.55rem;
  overflow: auto;
}

.element-ai-row {
  display: flex;
  width: 100%;
}

.element-ai-row.user {
  justify-content: flex-end;
}

.element-ai-row.assistant {
  justify-content: flex-start;
}

.element-ai-bubble {
  width: fit-content;
  max-width: min(78%, 17.5rem);
  border-radius: 1rem;
  padding: 0.42rem 0.7rem;
  line-height: 1.4;
}

.element-ai-bubble.user {
  background: #2563eb;
  color: #fff;
  border-bottom-right-radius: 0.28rem;
}

.element-ai-bubble.assistant {
  background: #fff;
  border: 1px solid #e8edf5;
  border-bottom-left-radius: 0.28rem;
  color: #0f172a;
}

.element-ai-bubble.working {
  opacity: 0.88;
}

.element-ai-bubble.working p {
  color: #64748b;
}

.element-ai-bubble p {
  margin: 0;
  font-size: 0.8rem;
  white-space: pre-wrap;
  word-break: break-word;
}

.element-ai-bubble.user p {
  color: #fff;
}

.element-ai-mode,
.element-ai-chip {
  display: inline-flex;
  align-items: center;
  border-radius: 999px;
  padding: 0.12rem 0.5rem;
  font-size: 0.65rem;
  font-weight: 700;
  letter-spacing: 0.01em;
  line-height: 1.2;
}

.element-ai-mode {
  background: #f1f5f9;
  color: #475569;
  border-radius: 0.35rem;
}

.element-ai-chip {
  border: 1px solid #dbeafe;
  background: #f8fbff;
  color: #3b82f6;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  text-transform: lowercase;
}

.element-ai-composer.conversation .element-ai-input-row textarea {
  min-height: 2.4rem;
  font-size: 0.78rem;
}

.element-ai-composer.conversation .element-ai-hint {
  display: none;
}

.element-ai-input-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: end;
  gap: 0.5rem;
}

.element-ai-input-row textarea {
  width: 100%;
  min-height: 3.25rem;
  resize: none;
  border: 0;
  outline: 0;
  padding: 0.25rem;
  color: #0f172a;
  font: inherit;
  font-size: 0.82rem;
  line-height: 1.45;
}

.element-ai-input-row textarea:disabled {
  background: transparent;
  color: #94a3b8;
}

.element-ai-send {
  width: 2rem;
  height: 2rem;
  border: 0;
  border-radius: 999px;
  background: #2563eb;
  color: #fff;
  font-size: 1rem;
  font-weight: 800;
  cursor: pointer;
}

.element-ai-send:disabled {
  background: #cbd5e1;
  cursor: not-allowed;
}

.element-ai-hint {
  margin: 0.4rem 0 0;
  color: #94a3b8;
  font-size: 0.7rem;
  line-height: 1.4;
}

.selected-element-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  padding: 0.8rem 1rem;
  border-bottom: 1px solid #edf1f6;
}

.selected-element-toolbar.compact {
  flex: 0 0 auto;
}

.selected-element-toolbar.chat-head {
  padding: 0.55rem 0.75rem;
  background: #fff;
  border-bottom: 1px solid #e8edf5;
}

.selected-element-meta {
  display: flex;
  align-items: center;
  gap: 0.55rem;
}

.selected-element-meta .element-ai-chip {
  flex: 0 0 auto;
}

.selected-element-meta code {
  color: #64748b;
  font-size: 0.74rem;
}

.editor-field {
  display: grid;
  gap: 0.45rem;
  color: #334155;
  font-size: 0.82rem;
  font-weight: 700;
}

.editor-field textarea {
  width: 100%;
  resize: vertical;
  border: 1px solid #dbe2ea;
  border-radius: 0.65rem;
  padding: 0.7rem;
  color: #0f172a;
  font: inherit;
  font-weight: 400;
}

.editor-field textarea:focus {
  outline: 2px solid rgba(37, 99, 235, 0.15);
  border-color: #60a5fa;
}

.editor-error {
  margin: 0;
  padding: 0 1rem 0.5rem;
  color: #dc2626;
  font-size: 0.78rem;
}

.editor-icon-btn {
  display: grid;
  place-items: center;
  width: 2rem;
  height: 2rem;
  border: 1px solid #e2e8f0;
  border-radius: 0.55rem;
  background: #fff;
  color: #475569;
  cursor: pointer;
}

.editor-icon-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.editor-section {
  display: grid;
  gap: 0.7rem;
  padding: 1rem;
  border-bottom: 1px solid #edf1f6;
}

.editor-section h3 {
  margin: 0;
  color: #0f172a;
  font-size: 0.82rem;
}

.editor-grid {
  display: grid;
  gap: 0.55rem;
}

.editor-grid.two-columns {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.editor-grid.three-columns {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

.editor-grid.four-columns {
  grid-template-columns: repeat(4, minmax(0, 1fr));
}

.full-width {
  grid-column: 1 / -1;
}

.compact-field {
  display: grid;
  gap: 0.3rem;
  min-width: 0;
  color: #64748b;
  font-size: 0.68rem;
  font-weight: 700;
}

.compact-field input:not([type='color']),
.compact-field select {
  min-width: 0;
  width: 100%;
  height: 2rem;
  border: 1px solid #e2e8f0;
  border-radius: 0.5rem;
  padding: 0 0.5rem;
  background: #f8fafc;
  color: #0f172a;
  font: inherit;
  font-size: 0.75rem;
}

.color-compact input {
  width: 100%;
  height: 2rem;
  border: 1px solid #e2e8f0;
  border-radius: 0.5rem;
  padding: 0.15rem;
  background: #fff;
  cursor: pointer;
}

.alignment-control {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  overflow: hidden;
  border: 1px solid #e2e8f0;
  border-radius: 0.55rem;
}

.alignment-control button {
  height: 2rem;
  border: 0;
  border-right: 1px solid #e2e8f0;
  background: #fff;
  color: #64748b;
  cursor: pointer;
}

.alignment-control button:last-child {
  border-right: 0;
}

.alignment-control button.active {
  background: #eff6ff;
  color: #2563eb;
}

.field-group-label {
  margin: 0;
  color: #475569;
  font-size: 0.72rem;
  font-weight: 700;
}

.layout-tail {
  margin-top: 0.2rem;
}
</style>
