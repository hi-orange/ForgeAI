// Isolated visual fixture for AppPreviewPane and the Design sidebar.
// Run: node tests/design-preview.mjs, then open http://127.0.0.1:5200
import { createServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath } from 'node:url'

const fixtureApi = `
const design = {
  revision: 2,
  saved_at: '2026-09-29T00:00:00Z',
  theme: { preset_id: 'forge', name: 'Forge', background: '#f8fafc', surface: '#ffffff', text: '#0f172a', primary: '#2563eb', muted: '#64748b', border: '#e2e8f0', font_family: 'Inter, system-ui, sans-serif', radius: 12, shadow: '0 12px 32px rgba(15, 23, 42, 0.08)' },
  elements: [],
}
export async function getPreview() { return { status: 'ready', url: 'http://127.0.0.1:5200/fixture-app', run_id: 'design-fixture', message: null } }
export async function startPreview() { return getPreview() }
export async function stopPreview() { return { status: 'stopped', url: null, run_id: null, message: null } }
export async function getPreviewDesign() { return structuredClone(design) }
export async function savePreviewDesign(_projectId, state) { Object.assign(design, structuredClone(state), { revision: design.revision + 1 }); return structuredClone(design) }
`

const fixtureApp = `
import { createApp, h, nextTick } from 'vue'
import AppPreviewPane from '/src/views/project/components/AppPreviewPane.vue'
const status = {
  project_id: 1, run_id: 'design-fixture', plan_id: 'plan', task_id: 'task', message_id: 1,
  state: 'completed', execution_id: null, execution_expires_at: null, error: null,
  result: null, app_spec: { product_intent: '招聘网站' }, workspace_ready: true, code_ready: true,
  activities: [{ id: 'done', name: 'quality', label: '独立验收通过', detail: '16/16 checks passed', ok: true }],
}
createApp({
  setup() {
    nextTick(() => window.setTimeout(() => document.querySelector('.design-toggle')?.click(), 250))
    return () => h('main', { style: 'height:100vh;display:flex' }, [
      h(AppPreviewPane, { projectId: 1, status, planGoal: '招聘网站', planItems: [], canApprove: false, canResume: false, busy: false, refreshing: false }),
    ])
  },
}).mount('#app')
`

const previewHtml = `<!doctype html><html lang="zh"><head><meta charset="utf-8"><title>招聘网站</title><style>
*{box-sizing:border-box}body{margin:0;background:#f8fafc;color:#0f172a;font:14px Inter,system-ui}nav{height:64px;display:flex;align-items:center;justify-content:space-between;padding:0 48px;border-bottom:1px solid #e2e8f0;background:#fff}nav a{color:#2563eb;text-decoration:none;margin-left:24px}.hero{max-width:960px;margin:72px auto;padding:48px;border-radius:20px;background:#fff;box-shadow:0 18px 50px #0f172a12}.hero h1{font-size:44px;margin:0 0 14px}.hero p{color:#64748b;font-size:18px}.hero button{border:0;border-radius:10px;background:#2563eb;color:#fff;padding:12px 20px}
</style></head><body><nav><strong>Forge Jobs</strong><div><a href="/fixture-app">首页</a><a href="/jobs">职位列表</a></div></nav><section class="hero"><h1>找到适合你的下一份工作</h1><p>浏览精选职位，与优秀团队建立连接。</p><button>开始浏览</button></section><script>
const channel='forgeai-preview-design';
const pages=[{path:'/fixture-app',label:'首页'},{path:'/jobs',label:'职位列表'}];
const post=(type,payload={})=>parent.postMessage({channel,type,...payload},'*');
addEventListener('message',(event)=>{if(event.source!==parent||event.data?.channel!==channel)return;if(event.data.type==='request-state')post('route',{path:location.pathname,pages});if(event.data.type==='navigate'){history.pushState({},'',event.data.path);post('route',{path:location.pathname,pages})}});
post('ready',{path:location.pathname,pages});
</script></body></html>`

const server = await createServer({
  configFile: false,
  root: fileURLToPath(new URL('../', import.meta.url)),
  resolve: { alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) } },
  plugins: [
    {
      name: 'design-preview-fixture',
      enforce: 'pre',
      resolveId(id) {
        if (id.endsWith('/api/modules/preview') || id === '@/api/modules/preview') return '\0fixture-preview-api'
        if (id === 'fixture-app') return '\0fixture-app'
      },
      load(id) {
        if (id === '\0fixture-preview-api') return fixtureApi
        if (id === '\0fixture-app') return fixtureApp
      },
      configureServer(viteServer) {
        viteServer.middlewares.use(async (req, res, next) => {
          if (req.url === '/fixture-app' || req.url === '/jobs') {
            res.setHeader('Content-Type', 'text/html; charset=utf-8')
            res.end(previewHtml)
            return
          }
          if (req.url !== '/') return next()
          res.setHeader('Content-Type', 'text/html; charset=utf-8')
          res.end(await viteServer.transformIndexHtml('/', '<html lang="zh"><head><meta charset="utf-8"><title>Design 验收</title></head><body style="margin:0"><div id="app"></div><script type="module" src="/@id/__x00__fixture-app"></script></body></html>'))
        })
      },
    },
    vue(),
  ],
  server: { host: '127.0.0.1', port: 5200, strictPort: true },
})
await server.listen()
server.printUrls()
