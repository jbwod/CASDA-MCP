---
name: casda-safe-archive-access
description: >-
  Global safety rules for the CASDA MCP server. Use whenever searching, inspecting,
  staging, downloading, or recording CASDA products so the agent stays within
  allowlisted tools and never invents ADQL, URLs, or privileged DAP actions.
---

# CASDA safe archive access

## Rules

1. Use only the registered CASDA MCP tools and resources. Do not invent ADQL, raw TAP URLs, shell commands, or filesystem browsing tools.
2. Resolve astronomical names to ICRS coordinates outside this MCP (or use an exact CASDA `source_name`). This server does not resolve names.
3. Search and inspect before selecting explicit product identifiers. Never stage or download from guessed IDs.
4. Staging and downloads are disabled by default. Treat `STAGING_DISABLED` / `DOWNLOADS_DISABLED` as configuration, not archive failures.
5. Read every tool response's `error` and `provenance`. Do not claim access CASDA has not confirmed (`access_state`, `authorisation_state`).
6. Do not scrape or automate Data Access Portal legal-acceptance, administrative, or interactive UI flows.
7. Cutouts, SCS catalogue endpoints, SIA/SSA discovery, and observation-event feeds are not exposed. Say so; do not invent substitute tool calls.
8. Prefer prompts `find-and-inspect-products`, `stage-and-download`, and `build-reproducible-selection` for guided workflows. Read skill resources under `casda://skills/{skill_name}` when procedural detail is needed.

## Units and identifiers

- Coordinates: ICRS degrees (`ra_deg` in `[0, 360)`, `dec_deg` in `[-90, 90]`).
- Frequencies: hertz.
- Dates: ISO 8601 overlapping observation windows.
- Product IDs: exact CASDA `obs_publisher_did` values returned by search or get tools.
