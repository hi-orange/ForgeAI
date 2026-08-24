import type { RouteRecordRaw } from 'vue-router'

export const projectRoutes = [
  {
    path: '/projects/:id',
    name: 'project',
    component: () => import('@/views/project/ProjectView.vue'),
    meta: { requiresAuth: true },
  },
] satisfies RouteRecordRaw[]
