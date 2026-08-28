# ForgeAI Frontend

Vue 3 and TypeScript interface for project conversations, build progress, and runnable app
previews.

Follow [the frontend agent guide](AGENTS.md) for current code conventions and
[the architecture document](../../docs/architecture.md) for product-level constraints.

## Development

From the repository root:

    pnpm install
    pnpm --filter @forgeai/frontend dev

Useful checks:

    pnpm --filter @forgeai/frontend type-check
    pnpm --filter @forgeai/frontend lint
    pnpm --filter @forgeai/frontend build
