<template>
  <details class="quality-evidence">
    <summary>独立验收：{{ passedCount }}/{{ results.length }} 项通过</summary>
    <p>以下状态来自这次应用版本的逐项执行。阻塞和未执行项不会计为通过。</p>
    <ul>
      <li v-for="result in results" :key="result.test_id">
        <strong :data-status="result.status">{{ labels[result.status] }}</strong>
        <span>{{ result.acceptance_ids.join('、') }}</span>
        <span v-if="result.failure_kind"> · {{ kinds[result.failure_kind] }}</span>
        <details>
          <summary>查看执行证据</summary>
          <pre>{{ result.evidence }}</pre>
          <small>用例 {{ result.test_id }} · {{ result.execution_id || '尚未执行' }}</small>
        </details>
      </li>
    </ul>
  </details>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { TestVerification } from '@/api/modules/requirements'

const props = defineProps<{ results: TestVerification[] }>()
const passedCount = computed(
  () => props.results.filter((result) => result.status === 'passed').length,
)
const labels = { passed: '通过', failed: '失败', blocked: '阻塞', not_run: '未执行' }
const kinds = {
  product: '业务缺陷',
  script: '测试脚本',
  environment: '环境问题',
  conflict: '标准争议',
}
</script>

<style scoped>
.quality-evidence {
  margin-block: 1rem;
  padding: 1rem;
  border: 1px solid #ddd;
  border-radius: 12px;
}
summary {
  cursor: pointer;
}
p,
small {
  color: #666;
}
ul {
  list-style: none;
  padding: 0;
}
li {
  padding-block: 0.65rem;
  border-top: 1px solid #eee;
}
strong {
  margin-right: 0.5rem;
}
strong[data-status='passed'] {
  color: #167044;
}
strong[data-status='failed'] {
  color: #b42318;
}
strong[data-status='blocked'],
strong[data-status='not_run'] {
  color: #854d0e;
}
pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 0.8rem;
}
</style>
