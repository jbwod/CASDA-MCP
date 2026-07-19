---
name: casda-reproducible-manifest
description: >-
  Record an explicit CASDA product selection as a versioned manifest for papers
  and pipelines. Use after search/inspect when the user needs citable, machine-
  readable provenance without persisting download URLs.
---

# CASDA reproducible manifest

## Workflow

1. Confirm the exact `product_ids` with the user after search/inspect.
2. Optionally call `casda_get_product` for each ID so sizes, SBIDs, and release state are clear.
3. Call `casda_create_manifest` with those IDs. Set `source_name` and `workflow_name` when the user supplies labels.
4. Leave `include_download_urls` false. Artifact URLs are never persisted because opaque paths may be short-lived credentials; requesting true only records an omission warning.
5. Re-read the manifest later via `casda://manifests/{manifest_id}` when persistent server state is enabled.

## What the manifest includes

Typed product metadata, filenames, estimated sizes, available checksums, SBIDs, project codes, spatial/spectral fields, access state, known originating search criteria, and sanitised provenance. It does not stage or download files.

## Related prompt

Use MCP prompt `build-reproducible-selection`.
