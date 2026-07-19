---
name: casda-find-and-inspect
description: >-
  Search and inspect CASDA products with bounded ObsCore filters or VO discovery
  (SIA/SCS/SSA), plus projects and events. Use when the user needs candidates
  without staging or downloading.
---

# CASDA find and inspect

## Workflow

1. Choose the discovery path that matches the need:
   - ObsCore products: `casda_search_products` with explicit bounded criteria.
   - Images/cubes: `casda_search_images` (SIA 2) or `casda_list_image_surveys` /
     `casda_search_survey_images` (SIA 1).
   - Catalogue rows: `casda_list_catalogues` then `casda_search_catalogue` (SCS).
   - Spectra: `casda_search_spectra` (SSA).
   - Projects/collections: `casda_search_projects`, `casda_get_project`, `casda_get_collection`.
   - Lifecycle notices: `casda_list_events`.
2. Present stable identifiers, sizes, release state, and access fields to the user.
3. For selected ObsCore IDs, call `casda_get_product`. For a known ASKAP scheduling block, call `casda_get_observation`.
4. Stop after inspection unless the user explicitly asks to stage, cut out, download, or create a manifest.

## Useful ObsCore filters

- Position: `ra_deg`, `dec_deg`, `radius_deg` (server-bounded cone).
- Project: exact `project_code` (for example `AS102`).
- ASKAP: `scheduling_block_id`.
- Facility/instrument: `facility_name`, `instrument_name`.
- Types: allowlisted `image`, `cube`, `visibility`, `spectrum`, `catalogue`, `weight`, `moment_map`, `cubelet`, `evaluation`, `scan`.
- Time / frequency: overlapping ISO 8601 dates; overlapping frequencies in hertz.
- `released_only` defaults to true.

## Schema discovery

For TAP table exploration use `casda_list_schemas` → `casda_list_tables` → `casda_describe_table` (prompt `query-tables`).

## Related prompts

Use `find-and-inspect-products`, `query-catalogue`, `query-tables`, or `monitor-releases`.
