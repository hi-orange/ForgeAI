export const AUTH_EXPIRED_EVENT = 'forgeai:auth-expired'

export type AuthExpiredDetail = {
  redirect: string
}

export function notifyAuthExpired() {
  const { pathname, search, hash } = window.location
  const redirect = `${pathname}${search}${hash}`
  window.dispatchEvent(
    new CustomEvent<AuthExpiredDetail>(AUTH_EXPIRED_EVENT, {
      detail: { redirect },
    }),
  )
}
