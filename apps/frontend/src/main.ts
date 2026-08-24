import { createApp } from 'vue'
import { createPinia } from 'pinia'

import './assets/main.css'
import './assets/styles/global.scss'
import App from './App.vue'
import { AUTH_EXPIRED_EVENT, type AuthExpiredDetail } from './auth/session'
import router from './router'
import { useAuthStore } from './stores'

const app = createApp(App)
const pinia = createPinia()

app.use(pinia)

let handlingExpiredSession = false
window.addEventListener(AUTH_EXPIRED_EVENT, (event) => {
  if (handlingExpiredSession) return
  handlingExpiredSession = true

  const auth = useAuthStore(pinia)
  const detail = (event as CustomEvent<AuthExpiredDetail>).detail
  const redirect = detail?.redirect || router.currentRoute.value.fullPath || '/'
  auth.logout()

  if (router.currentRoute.value.name !== 'login') {
    void router
      .replace({ name: 'login', query: { redirect } })
      .finally(() => (handlingExpiredSession = false))
  } else {
    handlingExpiredSession = false
  }
})

app.use(router)

app.mount('#app')
