import type { RouteRecordRaw } from 'vue-router'

export const homeRoutes = [
  {
    path: '/',
    name: 'home',
    component: () => import('@/views/home/HomeView.vue'),
    meta: { requiresAuth: true },
  },
] satisfies RouteRecordRaw[]
