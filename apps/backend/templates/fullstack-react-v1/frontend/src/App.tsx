import { useQuery } from '@tanstack/react-query'
import { ArrowRight, CheckCircle2, Database, Palette, Server, Sparkles } from 'lucide-react'

import { Button } from './components/ui/button'
import { ApiError, getHealth } from './lib/api'

export function HomePage() {
  const health = useQuery({
    queryKey: ['health'],
    queryFn: getHealth,
  })

  const status = health.data
    ? `${health.data.status} · ${health.data.template_version}`
    : health.isPending
      ? 'Checking API…'
      : 'API unavailable'
  const error = health.error
    ? health.error instanceof ApiError
      ? health.error.detail
      : health.error.message
    : null

  return (
    <main className="relative min-h-[calc(100vh-73px)] overflow-hidden">
      <div className="hero-orb hero-orb-left" />
      <div className="hero-orb hero-orb-right" />
      <section className="relative mx-auto grid max-w-7xl gap-12 px-6 py-16 lg:grid-cols-[1.08fr_.92fr] lg:items-center lg:py-24">
        <div>
          <div className="mb-7 inline-flex items-center gap-2 rounded-full border border-primary/20 bg-card/70 px-3.5 py-1.5 text-sm font-medium text-primary shadow-sm backdrop-blur">
            <Sparkles className="size-4 text-accent" aria-hidden="true" />
            Full-stack baseline ready
          </div>
          <h1 className="max-w-3xl text-5xl font-semibold tracking-[-0.045em] sm:text-7xl">
            Build the product,
            <span className="block bg-gradient-to-r from-primary via-highlight to-accent bg-clip-text text-transparent">
              not the scaffolding.
            </span>
          </h1>
          <p className="mt-7 max-w-2xl text-lg leading-8 text-muted-foreground">
            A production-minded React and FastAPI workspace with real routing, typed server state,
            database migrations and a visual system ready to become your product.
          </p>
          <div className="mt-9 flex flex-wrap items-center gap-4">
            <Button onClick={() => health.refetch()}>
              Check backend
              <ArrowRight className="size-4" aria-hidden="true" />
            </Button>
            <div
              className="flex items-center gap-2 rounded-full border border-border bg-card/80 px-4 py-2.5 text-sm text-muted-foreground shadow-sm"
              role="status"
            >
              <CheckCircle2 className="size-4 text-success" aria-hidden="true" />
              {status}
            </div>
          </div>
          {error ? (
            <p className="mt-5 max-w-xl rounded-2xl border border-destructive/20 bg-destructive/5 px-4 py-3 text-sm text-destructive">
              {error}
            </p>
          ) : null}
        </div>

        <div className="relative mx-auto w-full max-w-xl">
          <div className="visual-grid rounded-[2rem] border border-white/70 bg-card/75 p-5 shadow-2xl backdrop-blur sm:p-7">
            <div className="mb-7 flex items-center justify-between">
              <div>
                <p className="text-sm font-medium text-muted-foreground">Product foundation</p>
                <p className="mt-1 text-2xl font-semibold">Connected by default</p>
              </div>
              <div className="flex size-12 items-center justify-center rounded-2xl bg-primary text-primary-foreground shadow-lg shadow-primary/20">
                <Server className="size-5" aria-hidden="true" />
              </div>
            </div>
            <div className="grid gap-4 sm:grid-cols-2">
              {[
                {
                  icon: Database,
                  title: 'Real data flow',
                  text: 'React → FastAPI → SQLite',
                  tone: 'bg-primary/10 text-primary',
                },
                {
                  icon: Palette,
                  title: 'Visual direction',
                  text: 'Domain palettes and assets',
                  tone: 'bg-highlight/10 text-highlight',
                },
                {
                  icon: CheckCircle2,
                  title: 'Journey checks',
                  text: 'API and browser evidence',
                  tone: 'bg-success/10 text-success',
                },
                {
                  icon: Sparkles,
                  title: 'Ready to evolve',
                  text: 'Typed, routed, responsive',
                  tone: 'bg-accent/15 text-accent-foreground',
                },
              ].map(({ icon: Icon, title, text, tone }) => (
                <article key={title} className="rounded-2xl border border-border/70 bg-card p-4 shadow-sm">
                  <div className={`mb-5 flex size-10 items-center justify-center rounded-xl ${tone}`}>
                    <Icon className="size-5" aria-hidden="true" />
                  </div>
                  <h2 className="font-semibold">{title}</h2>
                  <p className="mt-1 text-sm text-muted-foreground">{text}</p>
                </article>
              ))}
            </div>
          </div>
        </div>
      </section>
    </main>
  )
}
