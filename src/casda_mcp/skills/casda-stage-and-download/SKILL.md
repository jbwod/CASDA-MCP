---
name: casda-stage-and-download
description: >-
  Authenticated CASDA full-file staging and guarded local download. Use after
  explicit product IDs are chosen and the user wants archive staging and a
  verified local file.
---

# CASDA stage and download

## Prerequisites

- OPAL credentials configured (`CASDA_USERNAME`, `CASDA_PASSWORD`).
- `CASDA_ENABLE_STAGING=true` for staging; `CASDA_ENABLE_DOWNLOADS=true` and an absolute `CASDA_DOWNLOAD_DIR` for downloads.
- Inspect sizes and access state with `casda_get_product` before staging.

## Workflow

1. Call `casda_stage_products` with explicit `product_ids` and a stable `idempotency_key` when retrying the same selection.
2. Later call `casda_get_staging_status` once with the returned `request_id`. Do not assume background polling; call again only after waiting.
3. Download only when the status shows products ready for download.
4. Call `casda_download_product` for one product at a time. Prefer `verify_checksum=true`. Paths must stay under `CASDA_DOWNLOAD_DIR`.
5. Check returned byte length and checksum fields before treating the file as complete.

## Safety

- Do not invent cutout or spectrum-generation staging; only full-file staging is exposed.
- Never auto-retry staging create/start after a network failure without user intent and a new or reused idempotency policy.
- Do not overwrite existing destinations unless the administrator enabled overwrite.

## Related prompt

Use MCP prompt `stage-and-download`.
