<template>
  <article class="workspace-document" aria-label="项目文档">
    <template v-for="(block, index) in blocks" :key="index">
      <component :is="`h${block.level}`" v-if="block.kind === 'heading'">{{
        block.text
      }}</component>
      <ul v-else-if="block.kind === 'list'">
        <li v-for="(item, itemIndex) in block.items" :key="itemIndex">{{ item }}</li>
      </ul>
      <p v-else>{{ block.text }}</p>
    </template>
  </article>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ content: string }>()
type Block =
  | { kind: 'heading'; level: number; text: string }
  | { kind: 'list'; items: string[] }
  | { kind: 'paragraph'; text: string }

// The platform emits headings, paragraphs and lists. Render text through Vue, never raw HTML,
// executable Markdown, or remote images embedded in model-authored artifact content.
const blocks = computed(() => {
  const result: Block[] = []
  let paragraph: string[] = []
  let list: string[] = []
  const flush = () => {
    if (paragraph.length) result.push({ kind: 'paragraph', text: paragraph.join('\n') })
    if (list.length) result.push({ kind: 'list', items: list })
    paragraph = []
    list = []
  }
  for (const line of props.content.split(/\r?\n/)) {
    const heading = /^(#{1,6})\s+(.+)$/.exec(line)
    if (heading) {
      flush()
      result.push({ kind: 'heading', level: heading[1]!.length, text: heading[2]! })
    } else if (line.startsWith('- ')) {
      if (paragraph.length) flush()
      list.push(line.slice(2))
    } else if (!line.trim()) {
      flush()
    } else {
      if (list.length) flush()
      paragraph.push(line)
    }
  }
  flush()
  return result
})
</script>

<style scoped lang="scss">
.workspace-document {
  box-sizing: border-box;
  width: 100%;
  max-width: 1000px;
  padding: 28px 36px 64px;
  color: #30343b;
  font-size: 14px;
  line-height: 1.8;
  overflow-wrap: anywhere;
  h1 {
    margin: 0 0 24px;
    font-size: 26px;
  }
  h2 {
    margin: 28px 0 14px;
    padding-bottom: 8px;
    border-bottom: 1px solid #ececf0;
    font-size: 19px;
  }
  h3,
  h4,
  h5,
  h6 {
    margin: 20px 0 8px;
    font-size: 15px;
  }
  p {
    margin: 8px 0;
    white-space: pre-wrap;
  }
  ul {
    padding-left: 24px;
  }
  li {
    margin: 6px 0;
    white-space: pre-wrap;
  }
}
</style>
