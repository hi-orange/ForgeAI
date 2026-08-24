# Feature modules

Feature modules own cohesive business capabilities. A feature may contain its own views,
components, composables, styles, and private TypeScript contracts.

```text
features/project/
├─ views/ProjectWorkbench.vue
├─ components/
├─ composables/
├─ styles/
└─ types/
```

Feature internals are private by default. Route entries under `src/views` may compose them;
other features should consume explicit public exports instead of importing private files.
