---
name: casda-stage-and-download
description: >-
  Authenticated CASDA full-file staging, cutout/spectrum jobs, and guarded local
  download. Use after explicit product IDs are chosen and the user wants archive
  processing and a verified local file.
---

# CASDA stage and download

## Prerequisites

- OPAL credentials configured (`CASDA_USERNAME`, `CASDA_PASSWORD`).
- `CASDA_ENABLE_STAGING=true` for WEB full-file, Pawsey, cutout, and spectrum jobs; `CASDA_ENABLE_DOWNLOADS=true` and an absolute `CASDA_DOWNLOAD_DIR` for downloads.
- Inspect sizes and access state with `casda_get_product` (optionally `casda_get_datalink`) before submitting jobs.

## Full-file workflow (WEB download)

1. Call `casda_stage_products` with explicit `product_ids` and a stable `idempotency_key` when retrying the same selection.
2. Later call `casda_get_staging_status` or `casda_get_data_job` once with the returned `request_id`. Do not assume background polling.
3. Download only when the status shows products ready for download.
4. Call `casda_download_product` for one product at a time, or `casda_download_job_results` for selected job results. Prefer checksum verification. Paths must stay under `CASDA_DOWNLOAD_DIR`.
5. Check returned byte length and checksum fields; optionally `casda_verify_file`.

## Pawsey staging

1. Call `casda_stage_pawsey` with the same staging flag/credentials when the user wants Pawsey network pull (DataLink `pawsey_async_service`).
2. Surface every `human_gate_warnings` entry: Pawsey HPC account required; licence/account confirmation is a human DAP action; this MCP never auto-accepts terms; results are Pawsey-network restricted.
3. Monitor with `casda_get_data_job`. Do not claim WEB download parity for Pawsey jobs.

## Cutout and spectrum jobs

1. Call `casda_create_cutout` with `CIRCLE` / `POLYGON` / `BAND` / `CHANNEL` / `POL` / `COORD` as supplied, or `casda_create_spectrum` for integrated spectra.
2. Monitor with `casda_get_data_job` and inspect `casda_get_data_job_results` when needed.
3. Abort or delete only with explicit user intent (`casda_abort_data_job` / `casda_delete_data_job`).
4. Download ready results with `casda_download_product` or `casda_download_job_results`.

## Safety

- Never auto-retry job create/start after a network failure without user intent and a clear idempotency policy.
- Do not overwrite existing destinations unless the administrator enabled overwrite.
- Do not scrape the DAP cutout UI or invent SODA endpoints outside DataLink descriptors.

## Related prompts

Use `stage-and-download` or `make-cutout`.
