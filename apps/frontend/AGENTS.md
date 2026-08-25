# ForgeAI Frontend Agent Guide

Rules in this file apply when working under `apps/frontend/`.

## Stack

- Use Vue 3, Vite, TypeScript, Pinia, and SCSS.
- Use `<script setup lang="ts">`.
- Do not add new JavaScript files in place of TypeScript.

## Reuse first

- Before changing code, search for existing components, composables, API modules, types, and SCSS variables.
- Prefer composing or extending what already exists. Do not copy an existing component and rename it.
- Before creating a component, confirm no existing one already owns the same responsibility.
- If the same logic appears twice, consider extracting a function or composable; if it appears three times, extract it.
- If the same UI pattern appears twice, consider extracting a component.

## File responsibilities

- `src/views/` — route entry and page composition only.
- Page-specific UI lives under `views/<page>/components/` (and nearby helpers such as `*.ts` / `*.scss` for that page).
- Shared / public UI lives under `src/components/`.
- API requests live under `src/api/modules/`.
- Cross-page state lives under `src/stores/modules/`.
- Reusable stateful logic lives under `src/composables/` when introduced.
- Shared types live under `src/types/` when introduced; page-local types may stay next to the page.
- Do not introduce a `features/` directory.
- Do not keep dumping complex business logic into large Vue pages; split into page components, composables, or modules.

## Styles

- Use SCSS.
- Prefer existing variables, mixins, and shared styles (`src/assets/styles/`).
- Component styles are `scoped` by default.
- Do not add global CSS that affects all generated websites just to fix one case.
- Avoid repeating ad-hoc colors, spacing, and shadow values.

## Change discipline

- Before editing, briefly state which existing files will be reused or changed.
- Touch only files needed for the current request.
- Do not casually rewrite whole components.
- Do not remove existing user functionality or unrelated local changes.
- When fixing a concrete case, decide first whether it is a general defect or a one-off special case.

## Verification

After frontend changes, run at least:

```bash
pnpm --filter @forgeai/frontend type-check
pnpm --filter @forgeai/frontend build
pnpm --filter @forgeai/frontend lint
pnpm encoding:check
```

Do not claim the task is done if any of these fail.
