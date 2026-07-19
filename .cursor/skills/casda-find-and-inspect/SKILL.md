---
name: casda-find-and-inspect
description: >-
  Search and inspect CASDA ObsCore products with bounded filters. Use when the
  user needs candidates around a sky position, project, ASKAP SBID, or catalogue
  products without staging or downloading.
---

# CASDA find and inspect

## Workflow

1. Call `casda_search_products` with explicit bounded criteria. Do not broaden filters silently.
2. Present stable product identifiers, sizes, release state, and access fields to the user.
3. For selected IDs, call `casda_get_product`. For a known ASKAP scheduling block, call `casda_get_observation`.
4. Stop after inspection unless the user explicitly asks to stage, download, or create a manifest.

## Useful filters

- Position: `ra_deg`, `dec_deg`, `radius_deg` (server-bounded cone).
- Project: exact `project_code` (for example `AS102`).
- ASKAP: `scheduling_block_id`.
- Types: allowlisted `image`, `cube`, `visibility`, `spectrum`, `catalogue`, `weight`, `moment_map`.
- Time / frequency: overlapping ISO 8601 dates; overlapping frequencies in hertz.
- `released_only` defaults to true.

## Catalogue searches

For catalogue products only, set `product_types` to `["catalogue"]`. Dedicated SCS catalogue endpoints are not exposed.

## Related prompt

Use MCP prompt `find-and-inspect-products` or `query-catalogue` as a conversation starter.
