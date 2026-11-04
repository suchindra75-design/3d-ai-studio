# AI 3D Studio — Database Schema

Source of truth: `backend/ai3d/models/*.py` and the Alembic migrations in
`backend/ai3d/database/migrations/versions/`. This document explains them.
`backend/tests/test_database.py` fails if models and migrations drift apart.

## Conventions

| Topic | Rule |
|---|---|
| Primary keys | UUIDv4 (`sqlalchemy.Uuid` → `CHAR(32)` on SQLite, native `UUID` on PostgreSQL). Exception: `job_inputs` uses `(job_id, position)`. |
| Timestamps | `UTCDateTime`: stored as UTC, always returned timezone-aware. Naive datetimes are rejected on write. |
| Enums | Stored as `VARCHAR(32)` (non-native) so values can be added without migrations. |
| JSON | `JSON` on SQLite, `JSONB` on PostgreSQL. Used for flexible, non-relational detail (EXIF subset, analysis, capabilities, stats). |
| Binary data | Never in the database. Rows hold storage keys (`projects/{id}/…`). |
| Constraint names | Deterministic naming convention (`pk_`, `fk_`, `uq_`, `ix_`) — required for SQLite batch migrations. |
| Deletes | Foreign keys declare `ON DELETE` behaviour; SQLite runs with `PRAGMA foreign_keys=ON`. History-preserving links use `SET NULL`. |
| SQLite pragmas | `journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`, `synchronous=NORMAL`. |

## Entity overview

```
projects ─┬─< images ─< images (derived_from)
          ├─< generation_jobs ─┬─< job_inputs >── images
          │                    └─< job_events
          ├─< models ─< model_versions ─┬─< model_parts
          │       └── current_version ──┘  ├─< quality_reports
          ├─< assets >───────────────────┤  └─< exports ── assets
          └─< exports
ai_providers ─< provider_configurations ─< generation_jobs (provider_config_id)
local_devices          app_settings
```

Cyclic references (`models.current_version_id`, `model_versions.primary_asset_id`,
`model_versions.job_id`) are declared `use_alter`; the initial migration adds
them after table creation on databases that support `ALTER`.

## Tables

### projects
| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| name | varchar(200) | trimmed, non-empty |
| description | text | default `''` |
| status | enum `active|archived` | |
| input_mode | enum `single_image|multi_view` | |
| default_quality_mode | enum `fast|high_quality|professional` | |
| default_processing_mode | enum `cloud|local|auto` | |
| allow_cloud_upload | bool | default **false**; gate for sending images to providers |
| archived_at | timestamptz null | set when archived |
| created_at, updated_at | timestamptz | `updated_at` also bumped by image changes |

Index: `(status, updated_at)` for the dashboard.

### images
| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| project_id | FK projects CASCADE | indexed |
| kind | enum `original|preprocessed|segmented|mask` | uploads are `original` |
| derived_from_id | FK images CASCADE, null | derived copies point at their source |
| position | int | display/capture order |
| original_filename | varchar(255) | sanitised, display only |
| storage_key / thumbnail_key | varchar(512) | `storage_key` unique |
| mime_type, size_bytes, sha256 | | sha256 of the exact uploaded bytes |
| width, height | int | after EXIF orientation |
| exif_orientation | int null | |
| camera_make, camera_model | varchar(128) null | |
| focal_length_mm, focal_length_35mm | float null | photogrammetry intrinsics prior |
| exif | json | curated subset; **GPS excluded** |
| quality_level | enum `good|warning|poor` | from analysis |
| sharpness | float null | variance of Laplacian (queryable) |
| analysis | json | brightness, contrast, clipping, megapixels, subject coverage, warnings |
| created_at | timestamptz | |

Unique `(project_id, sha256, kind)` makes re-uploads idempotent.
Index `(project_id, position)`.

### generation_jobs
Persistent queue + history. Key columns: `kind`, `status`
(`queued|running|cancelling|succeeded|failed|cancelled`), `quality_mode`,
`processing_mode`, `provider_key`, `provider_config_id` (SET NULL),
`provider_task_id`, `params` json, `stage`, `progress` (0–1), `error_code`,
`error_message`, `error_details`, `attempt`, `max_attempts`,
`idempotency_key` (unique), `parent_job_id` (retries, SET NULL),
`source_model_version_id` (enhance), `result_model_version_id`,
`cloud_upload_consented`, `cancel_requested`, `cost` json (as reported by the
provider), `lease_owner`, `lease_expires_at`, `queued_at`, `started_at`,
`finished_at`.
Indexes: `(status, queued_at)` for claiming, `lease_expires_at` for recovery,
`project_id`.

### job_inputs
`(job_id, position)` PK, `image_id` (SET NULL so history survives image
deletion), `image_sha256` snapshot, `role` (`source`, `mask`…).

### job_events
Append-only timeline: `job_id` (CASCADE), `seq` (unique per job; SSE event id),
`type` (`status|stage|progress|log|warning|error`), `stage`, `message`,
`progress`, `data` json, `created_at`.

### ai_providers / provider_configurations
`ai_providers`: `key` (unique), `display_name`, `kind`
(`cloud_api|local|mock`), `enabled`, `capabilities` snapshot, `description`,
`homepage_url`.
`provider_configurations`: `provider_id` (CASCADE), `name` (unique per
provider), `is_default`, `settings` json (non-secret), `credential_source`
(`none|environment|stored`), `api_key_encrypted` (Fernet ciphertext, never
returned by the API), `api_key_hint` (last 4 chars).

### models / model_versions / model_parts
`models`: logical asset per project; `current_version_id`.
`model_versions`: immutable snapshots; `version_number` unique per model,
`parent_version_id` (lineage for enhancements), `job_id`, `source`
(`generated|reconstructed|enhanced|imported|edited`), `quality_mode`,
`provider_key`, `surface_provenance` (`measured|inferred|mixed|unknown`),
`primary_asset_id`, `stats` json, `notes`.
`model_parts`: scene hierarchy nodes (`node_path`, `parent_part_id`,
`mesh_index`, `material_name`, vertex/triangle counts, `bbox`).

### assets
Stored files other than photos: `kind`
(`mesh|texture|point_cloud|preview_image|provider_raw|export_file|report`),
`storage_backend`, `storage_key` (unique), `mime_type`, `size_bytes`, `sha256`,
`meta` json, optional `model_version_id` (CASCADE).

### quality_reports
Per model version: `status` (`pass|warn|fail`), `report_version`, `metrics`
json, `issues` json list, `job_id`.

### exports
`model_version_id` (CASCADE), `format` (`glb|gltf|obj|stl|fbx|usdz|3mf`),
`status` (`pending|running|ready|failed`), `options`, `asset_id`, `job_id`,
`validation` json (result of re-opening the file), `error_message`,
`completed_at`.

### local_devices
Last detection of a machine: `machine_id` (unique, hashed), OS/arch/CPU,
cores, `ram_total_bytes`, `disk_free_bytes`, `gpus`, `accelerators`,
`dependencies`, `capabilities`, `last_seen_at`.

### app_settings
Key/value (`key` unique, `value` json) for non-secret user preferences.

## Migrations

```bash
# apply (the backend also does this on start-up when AI3D_AUTO_MIGRATE=true)
cd backend && uv run alembic upgrade head
# create a new revision after changing models
cd backend && uv run alembic revision --autogenerate -m "describe change"
```

Review every autogenerated revision: SQLite needs batch operations (enabled
automatically), and cyclic foreign keys need the pattern used in the initial
revision. `test_migrations_match_models` must stay green.

**Verified (Phase 1):** the full backend test suite passes on SQLite and on
PostgreSQL 17 (`AI3D_TEST_DATABASE_URL=postgresql+psycopg://… uv run pytest`),
including upgrade → downgrade → re-upgrade, model/migration parity, cyclic
foreign keys and cascades. CI runs both (`backend` and `backend-postgres` jobs).
