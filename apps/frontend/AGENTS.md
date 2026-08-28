# ForgeAI Frontend Agent Guide

> Architecture: [docs/architecture.md](../../docs/architecture.md)
> Repository rules: [AGENTS.md](../../AGENTS.md)

These rules apply under apps/frontend/.

## Stack

- Use Vue 3, Vite, TypeScript, Pinia, and SCSS.
- Use script setup with TypeScript for Vue components.
- Keep API requests, shared state, and reusable UI logic in the existing project layers.

## Reuse and file responsibilities

- Search for an existing component, composable, API module, store, type, or style token before
  adding a new abstraction.
- Route views should focus on page composition; move reusable or complex behavior into the nearest
  appropriate component, composable, store, or API module.
- Keep page-specific code near its page and genuinely shared code in the shared directories.
- Prefer existing SCSS variables, mixins, and common styles. Component styles are scoped by
  default.
- Keep frontend types aligned with backend contracts.

Current directory conventions are guidance for the present codebase, not permanent architecture.
Change them only when the current task benefits and all affected imports and consumers are updated.

## Change discipline

- State which current files or abstractions the change will reuse or affect.
- Implement the current user-approved increment without rewriting unrelated pages.
- Preserve unrelated local changes and existing behavior outside the request.
- Resolve whether a reported case is a general defect or a one-off condition before adding special
  handling.

## Impact-based review

Start with changed components, composables, stores, API modules, and types. Follow their direct
imports, route consumers, shared state, backend contract, and related tests. Expand farther only
when a shared component, global style, public contract, security boundary, or cross-page state can
affect a wider surface. A routine UI change does not require rescanning the entire frontend.

## Verification

Run focused checks for the changed behavior first. Add type checking, linting, a production build,
or broader tests according to the files and contracts affected.

Common commands:

    pnpm --filter @forgeai/frontend type-check
    pnpm --filter @forgeai/frontend build
    pnpm --filter @forgeai/frontend lint
    pnpm encoding:check

Report which checks ran, their results, and any relevant checks that were intentionally not run.
