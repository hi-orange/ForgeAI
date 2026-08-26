import type {
  Project,
  ProjectApproveSpecPayload,
  ProjectCreatePayload,
  ProjectElementAiEditPayload,
  ProjectElementAiReply,
  ProjectStartPayload,
  ProjectStartResult,
  ProjectWebsiteEditPayload,
  ProjectWebsiteRevisePayload,
  ProjectWebsiteReviseReply,
  WebsiteSpecification,
} from '@/api/modules/project'

const STORAGE_KEY = 'forgeai:mock-projects:v1'

function readProjects(): Project[] {
  try {
    const value = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]') as Project[]
    return Array.isArray(value) ? value : []
  } catch {
    return []
  }
}

function writeProjects(projects: Project[]) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(projects))
}

function saveProject(project: Project) {
  const projects = readProjects().filter((item) => item.id !== project.id)
  writeProjects([project, ...projects])
  return project
}

function requireProject(id: number) {
  const project = readProjects().find((item) => item.id === id)
  if (!project) throw new Error('模拟项目不存在')
  return project
}

function projectName(prompt: string) {
  const text = prompt.trim().replace(/\s+/g, ' ')
  return text.length > 40 ? `${text.slice(0, 40)}…` : text || '未命名项目'
}

function mockSpecification(project: Project): WebsiteSpecification {
  return {
    version: '1.0',
    product: {
      name: project.name,
      summary: project.prompt || '根据用户需求生成的网站演示',
      target_audience: '目标用户',
      primary_goal: '清晰展示产品价值并提供行动入口',
    },
    site: {
      type: 'web_app',
      language: 'zh-CN',
      pages: [
        {
          id: 'home',
          name: '首页',
          path: '/',
          purpose: '介绍产品并引导用户行动',
          sections: [
            {
              id: 'hero',
              type: 'hero',
              title: project.name,
              description: project.prompt || '让想法快速成为可运行产品。',
              content_points: ['清晰的价值主张', '直接的行动入口'],
              cta: '开始体验',
            },
            {
              id: 'features',
              type: 'features',
              title: '核心能力',
              description: '以结构化内容展示主要能力。',
              content_points: ['快速生成', '持续修改', '实时预览'],
              cta: null,
            },
          ],
        },
      ],
    },
    design: {
      style: '现代简洁',
      tone: '专业、可信',
      primary_color: '#3568f0',
      accent_color: '#14b8a6',
      font_style: '无衬线字体',
    },
    requirements: {
      features: ['响应式布局', '清晰导航', '行动按钮'],
      integrations: [],
      excluded: ['真实支付', '第三方登录'],
    },
    acceptance_criteria: ['页面可正常预览', '移动端布局可用'],
    assumptions: ['当前使用前端模拟数据，不调用旧生成接口'],
  }
}

function generatedFiles(project: Project) {
  const title = escapeHtml(project.name)
  const prompt = escapeHtml(project.prompt || '让想法快速成为产品。')
  return JSON.stringify({
    'index.html': `<!doctype html>
<html lang="zh-CN">
  <head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${title}</title></head>
  <body>
    <main>
      <section class="hero" data-forge-id="hero">
        <p class="eyebrow" data-forge-id="eyebrow">FORGEAI MOCK PREVIEW</p>
        <h1 data-forge-id="hero-title">${title}</h1>
        <p data-forge-id="hero-description">${prompt}</p>
        <button data-forge-id="hero-cta">开始体验</button>
      </section>
      <section class="features" data-forge-id="features">
        <article data-forge-id="feature-generate"><strong>快速生成</strong><p>把需求整理成清晰方案。</p></article>
        <article data-forge-id="feature-refine"><strong>持续修改</strong><p>围绕同一个项目继续对话。</p></article>
        <article data-forge-id="feature-preview"><strong>实时预览</strong><p>在工作台中查看结果。</p></article>
      </section>
    </main>
  </body>
</html>`,
    'style.css': `*{box-sizing:border-box}body{margin:0;background:#f5f7fb;color:#17243d;font-family:Inter,system-ui,sans-serif}main{min-height:100vh;padding:72px clamp(24px,7vw,110px)}.hero{max-width:840px}.eyebrow{color:#3568f0;font-size:12px;font-weight:800;letter-spacing:.18em}h1{margin:14px 0;font-size:clamp(52px,9vw,104px);line-height:.95;letter-spacing:-.06em}.hero>p:not(.eyebrow){max-width:680px;color:#52627a;font-size:20px;line-height:1.7}button{margin-top:22px;border:0;border-radius:12px;padding:14px 20px;color:white;background:#17243d;font-weight:750}.features{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-top:72px}.features article{padding:24px;border:1px solid #dbe2ee;border-radius:18px;background:white}.features p{color:#68758a}@media(max-width:720px){.features{grid-template-columns:1fr}}`,
    'script.js': '',
  })
}

function escapeHtml(value: string) {
  return value.replace(/[&<>'"]/g, (character) => {
    const entities: Record<string, string> = {
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      "'": '&#39;',
      '"': '&quot;',
    }
    return entities[character] || character
  })
}

export function createProject(payload: ProjectCreatePayload): Project {
  const projects = readProjects()
  const now = new Date().toISOString()
  const project: Project = {
    id: Math.max(0, ...projects.map((item) => item.id)) + 1,
    user_id: 1,
    name: payload.name?.trim() || projectName(payload.prompt),
    description: payload.description || null,
    prompt: payload.prompt.trim(),
    prd: null,
    approved_spec: null,
    approved_at: null,
    generated_files: null,
    build_error: null,
    validation_report: null,
    website_revision: 0,
    built_at: null,
    status: 'draft',
    created_at: now,
    updated_at: now,
  }
  return saveProject(project)
}

export function listProjects() {
  return readProjects().sort((a, b) => b.updated_at.localeCompare(a.updated_at))
}

export function getProject(id: number) {
  return requireProject(id)
}

export function startProject(id: number, payload: ProjectStartPayload = {}): ProjectStartResult {
  const current = requireProject(id)
  const next = {
    ...current,
    prompt: payload.prompt?.trim() || current.prompt,
  }
  const project = saveProject({
    ...next,
    prd: JSON.stringify(mockSpecification(next), null, 2),
    approved_spec: null,
    generated_files: null,
    website_revision: 0,
    status: 'prd_ready',
    updated_at: new Date().toISOString(),
  })
  return { project, workflow_id: `mock_${id}_${Date.now()}`, message: '模拟规格已生成' }
}

export function approveProjectSpec(id: number, payload: ProjectApproveSpecPayload) {
  const current = requireProject(id)
  if (!current.prd) throw new Error('请先生成模拟规格')
  const spec = JSON.parse(current.prd) as WebsiteSpecification
  const selected = new Set(
    payload.selected_sections.map((item) => `${item.page_id}:${item.section_id}`),
  )
  spec.site.pages = spec.site.pages
    .map((page) => ({
      ...page,
      sections: page.sections.filter((section) => selected.has(`${page.id}:${section.id}`)),
    }))
    .filter((page) => page.sections.length > 0)
  return saveProject({
    ...current,
    approved_spec: JSON.stringify(spec, null, 2),
    approved_at: new Date().toISOString(),
    status: 'spec_approved',
    updated_at: new Date().toISOString(),
  })
}

export function buildProject(id: number) {
  const current = requireProject(id)
  return saveProject({
    ...current,
    generated_files: generatedFiles(current),
    validation_report: JSON.stringify({ passed: true, source: 'frontend-mock' }, null, 2),
    website_revision: 1,
    built_at: new Date().toISOString(),
    status: 'completed',
    updated_at: new Date().toISOString(),
  })
}

export function editProjectWebsite(id: number, payload: ProjectWebsiteEditPayload) {
  const current = requireProject(id)
  if (!current.generated_files) throw new Error('模拟预览尚未生成')
  const files = JSON.parse(current.generated_files) as Record<string, string>
  const document = new DOMParser().parseFromString(files['index.html'] || '', 'text/html')
  for (const patch of payload.patches) {
    const element = Array.from(document.querySelectorAll<HTMLElement>('[data-forge-id]')).find(
      (candidate) => candidate.dataset.forgeId === patch.element_id,
    )
    if (!element) continue
    if (patch.changes.text !== undefined && patch.changes.text !== null) {
      element.textContent = patch.changes.text
    }
    for (const [name, value] of Object.entries(patch.changes.styles || {})) {
      if (value) element.style.setProperty(name, value)
    }
  }
  files['index.html'] = `<!doctype html>\n${document.documentElement.outerHTML}`
  return saveProject({
    ...current,
    generated_files: JSON.stringify(files),
    website_revision: current.website_revision + 1,
    updated_at: new Date().toISOString(),
  })
}

export function suggestProjectElementEdit(
  _id: number,
  _payload: ProjectElementAiEditPayload,
): ProjectElementAiReply {
  return { mode: 'message', message: '当前为 UI 模拟模式，已保留对话交互但不调用旧 Agent。' }
}

export function reviseProjectWebsite(
  _id: number,
  payload: ProjectWebsiteRevisePayload,
): ProjectWebsiteReviseReply {
  return {
    mode: 'message',
    message: `模拟模式已收到：${payload.instruction}。接入 Chat Update SOP 后会在这里发布新 Revision。`,
  }
}
