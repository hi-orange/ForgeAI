# Artifact Schemas (Contract)

Pydantic models will mirror these in `app/schemas/artifacts/`. Every artifact JSON includes a common envelope.

## Common envelope

All artifacts share:

```json
{
  "schema_version": "1.0",
  "artifact_type": "app_spec",
  "version": 2,
  "run_id": "run_abc123",
  "project_id": 42,
  "created_at": "2026-08-26T12:00:00Z"
}
```

BuildRun agents read **pinned `version`**, not latest.

---

## app_spec (PM)

Product truth. Required body fields:

| Field | Type | Notes |
|-------|------|-------|
| `app_name` | string | slug, `[a-z0-9-]` |
| `summary` | string | one paragraph |
| `entities` | array | see below |
| `pages` | array | see below |
| `design` | object | `primary_color`, `language`, `style` |

**Entity:**

```json
{
  "id": "menu_item",
  "name": "MenuItem",
  "fields": [
    { "name": "title", "type": "string", "required": true },
    { "name": "price", "type": "float", "required": true }
  ]
}
```

Field types: `string`, `int`, `float`, `bool`, `json`, `enum` (with `values`).

**Page:**

```json
{ "id": "menu", "path": "/menu", "type": "list", "entity_id": "menu_item", "title": "Menu" }
```

Page types: `landing`, `list`, `form`, `admin_list`.

---

## system_design (Architect)

Must reference `app_spec.version` in envelope context. Body:

| Field | Type |
|-------|------|
| `app_spec_version` | int |
| `template_version` | string (e.g. `"fullstack-v1"`) |
| `tables` | array of `{ name, model_file, fields[] }` |
| `apis` | array of `{ method, path, handler }` |
| `pages` | array of `{ path, component, type, entity_id? }` |
| `file_list` | string[] |
| `seed_data` | object |

---

## code (Developer)

Manifest only — source lives on disk.

```json
{
  "schema_version": "1.0",
  "artifact_type": "code",
  "version": 1,
  "run_id": "run_abc123",
  "revision": 3,
  "app_spec_version": 2,
  "system_design_version": 2,
  "template_version": "fullstack-v1",
  "root": "storage/projects/42/revisions/3",
  "files": [
    { "path": "backend/models/menu_item.py", "sha256": "..." }
  ]
}
```

During build, `root` points to `runs/{run_id}/workspace/` until promoted.

---

## test_report (QA)

```json
{
  "schema_version": "1.0",
  "artifact_type": "test_report",
  "run_id": "run_abc123",
  "code_revision": 3,
  "passed": false,
  "checks": [
    { "name": "server_starts", "passed": true },
    { "name": "GET /api/menu-items", "passed": false, "error": "connection refused" }
  ],
  "issues": [
    { "severity": "error", "description": "...", "suggested_fix": "..." }
  ]
}
```

---

## Storage layout

```
storage/projects/{id}/
  artifacts/           ← JSON files (optional mirror of DB)
  runs/{run_id}/workspace/
  revisions/{n}/       ← immutable after promote
  active.json          ← { "revision": 3 }
```
