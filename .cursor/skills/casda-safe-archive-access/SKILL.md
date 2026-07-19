---
name: casda-safe-archive-access
description: >-
  Global safety rules for the CASDA MCP server. Use whenever searching, inspecting,
  staging, downloading, or recording CASDA products so the agent stays within
  allowlisted tools and never invents URLs or privileged DAP actions.
---

# CASDA safe archive access

## Rules

1. Use only the registered CASDA MCP tools and resources. Do not invent raw TAP/SODA URLs, shell commands, or filesystem browsing tools.
2. Resolve astronomical names to ICRS coordinates outside this MCP (or use an exact CASDA `source_name`). This server does not resolve names.
3. Search and inspect before selecting explicit product identifiers. Never stage or download from guessed IDs.
4. Staging and downloads are disabled by default. Treat `STAGING_DISABLED` / `DOWNLOADS_DISABLED` as configuration, not archive failures.
5. Advanced ADQL (`casda_tap_query` / `casda_submit_tap_query`) requires `CASDA_ENABLE_ADVANCED_ADQL=true`. Treat `ADVANCED_ADQL_DISABLED` as configuration. Prefer allowlisted discovery tools when they suffice; validate with `casda_validate_adql` first.
6. Read every tool response's structured error (via protocol `isError`) and `provenance`. Do not claim access CASDA has not confirmed (`access_state`, `authorisation_state`).
7. Do not scrape or automate Data Access Portal legal-acceptance, administrative, or interactive UI flows. Use `casda_get_dap_navigation` for deep links and structured `unsupported_actions`. Never auto-accept licences, assign roles, deposit Level 7 data, release observations, mint DOIs, or launch CARTA.
8. Discovery tools are available: VOSI/TAP_SCHEMA, SIA/SCS/SSA, projects/collections, `casda_list_events`, `casda_resolve_collection_doi` (public citation read), and staging via `casda_stage_products` / `casda_stage_pawsey` / cutout / spectrum when enabled. Pawsey responses include human-gate warnings.
9. Prefer prompts such as `find-and-inspect-products`, `query-tables`, `run-adql`, `query-catalogue`, `make-cutout`, `stage-and-download`, `dap-navigate`, and `build-reproducible-selection`. Read `casda://skills/{skill_name}` and `casda://dap/navigation` when needed.

## Units and identifiers

- Coordinates: ICRS degrees (`ra_deg` in `[0, 360)`, `dec_deg` in `[-90, 90]`).
- Frequencies: hertz (ObsCore search); SODA `BAND` uses wavelength in metres.
- Dates: ISO 8601 overlapping observation windows.
- Product IDs: exact CASDA `obs_publisher_did` values returned by search or get tools.
