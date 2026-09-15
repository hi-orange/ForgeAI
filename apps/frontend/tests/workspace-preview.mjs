// Isolated browser fixture for the real build timeline and editor; no backend or model calls.
// Run: node tests/workspace-preview.mjs, then open http://127.0.0.1:5199
import { createServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath } from 'node:url'

const fixtureApi = `
export const contents = { 'frontend/src/App.vue': '<template>\\n  <main>应用初始模板</main>\\n</template>', 'backend/app/main.py': 'from fastapi import FastAPI\\napp = FastAPI()' }
export async function getWorkspace() { return { ready: true, run_id: 'fixture', files: Object.keys(contents).map(path => ({ path, size_bytes: contents[path].length })) } }
export async function getWorkspaceFile(_token, _id, path) { return { run_id: 'fixture', path, content: contents[path] } }
`
const fixtureApp = `
import { createApp, h, reactive } from 'vue'
import { createPinia } from 'pinia'
import { useAuthStore } from '/src/stores/modules/auth.ts'
import Timeline from '/src/views/project/components/BuildTimeline.vue'
import Editor from '/src/views/project/components/WorkspaceEditorPane.vue'
import { contents } from '@/api/modules/workspace'
const pinia = createPinia()
useAuthStore(pinia).token = 'local-fixture'
const activities = reactive([
 {id:'s1', name:'summary', detail:'先确认后端接口与页面结构，再开始编写职位列表。', ok:true, work_item_id:'jobs'},
 {id:'r1', name:'read_file', detail:'backend/app/main.py', ok:true, work_item_id:'jobs'},
 {id:'r2', name:'read_file', detail:'frontend/src/App.vue', ok:true, work_item_id:'jobs'},
 {id:'r3', name:'list_files', detail:'frontend/src', ok:true, work_item_id:'jobs'},
])
const source = reactive({projectId:1, runId:'fixture', ready:true, codeReady:false, generation:0, writtenPath:null, requestedPath:null, requestSequence:0})
function write() {
 source.generation++
 source.codeReady = true
 source.writtenPath = 'frontend/src/Jobs.vue'
 contents[source.writtenPath] = '<script setup>\\nimport { ref } from "vue"\\nconst jobs = ref([])\\n// 第 ' + source.generation + ' 次真实文件响应\\n</script>\\n<template><main><h1>职位列表</h1></main></template>'
 if (source.generation === 1) activities.push({id:'s2',name:'summary',detail:'接口结构已确认，现在编写职位列表页面。',ok:true,work_item_id:'jobs'})
 activities.push({id:'w'+source.generation,name:'apply_patch',detail:source.writtenPath,ok:true,work_item_id:'jobs'})
}
createApp({setup:() => () => h('main', {style:'height:100vh;display:grid;grid-template-rows:48px 1fr;grid-template-columns:360px 1fr;background:#f6f6f6;color:#343943;font:13px system-ui'}, [
 h('header',{style:'grid-column:1/-1;display:flex;align-items:center;gap:20px;padding:0 20px'},[h('strong','构建交互验证'), h('button',{onClick:write},'模拟下一次写入')]),
 h('aside',{style:'overflow:auto;padding:20px 12px'},[h('p','Forge · 工程师'),h(Timeline,{activities,running:true,onOpenFile:path=>{source.requestedPath=path;source.requestSequence++}})]),
 h(Editor,source)
])}).use(pinia).mount('#app')
`
const server = await createServer({
  configFile: false,
  root: fileURLToPath(new URL('../', import.meta.url)),
  resolve: { alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) } },
  plugins: [
    {
      name: 'workspace-fixture',
      enforce: 'pre',
      resolveId(id) {
        if (id.endsWith('/api/modules/workspace') || id === '@/api/modules/workspace')
          return '\0fixture-api'
        if (id === 'fixture-app') return '\0fixture-app'
      },
      load(id) {
        if (id === '\0fixture-api') return fixtureApi
        if (id === '\0fixture-app') return fixtureApp
      },
      configureServer(server) {
        server.middlewares.use(async (req, res, next) => {
          if (req.url !== '/') return next()
          res.setHeader('Content-Type', 'text/html; charset=utf-8')
          res.end(
            await server.transformIndexHtml(
              '/',
              '<html lang="zh"><head><meta charset="utf-8"><title>构建交互验证</title></head><body style="margin:0"><div id="app"></div><script type="module" src="/@id/__x00__fixture-app"></script></body></html>',
            ),
          )
        })
      },
    },
    vue(),
  ],
  server: { host: '127.0.0.1', port: 5199, strictPort: true },
})
await server.listen()
server.printUrls()
