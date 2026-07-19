# CASDA capability matrix

This document is the source of truth for the CASDA protocol surface, its MCP mapping, current
implementation status, and deliberate Data Access Portal (DAP) boundaries. In this repository,
"complete CASDA support" means complete coverage of documented public and authenticated
programmatic CASDA capabilities. It does not mean scraping or silently automating privileged,
administrative, legal-acceptance, or interactive DAP workflows.

The live observations below were made on **18 July 2026**. They are dated observations, not a
promise that CASDA's schemas, holdings, limits, or deployment topology will never change. CASDA
currently contains ASKAP, BETA, Parkes/UWL, ATCA, and LBA/VLBI holdings, so new work must be
facility-aware and must not interpret every archive row as an ASKAP scheduling block.

Primary evidence is the live service metadata, the [CASDA User Guide][casda-user-guide], the
[CASDA Services page][casda-services], the [CASDA VO Tools source][casda-vo-tools], and the
[CASDA sample clients][casda-samples]. The MCP Market listing is useful project history, but is not
an authority for CASDA behavior or MCP conformance.

## Status definitions

| Status | Meaning |
| --- | --- |
| **Implemented** | A public MCP contract, production code, offline protocol tests, and applicable live metadata verification exist. |
| **Partial** | A useful and tested subset exists, but at least one documented operation, data family, protocol lifecycle action, or required safety property is missing. |
| **Planned** | CASDA exposes the capability programmatically, but this MCP has no public surface for it yet. |
| **DAP boundary** | The behavior is human-facing, privileged, administrative, legal-acceptance, or interactive and must not be scraped or silently automated. |
| **Upstream-dependent** | Coverage is desirable, but a stable supported API or policy still needs confirmation from CSIRO. |

An implementation row may move to **Implemented** only when its code, public contract, tests, and
documentation are updated together. A protocol family is not complete merely because equivalent
results can sometimes be obtained through TAP.

## Current MCP inventory

Public surface after the CASDA overhaul. Authenticated staging/cutout/download paths are protocol-
tested offline; live OPAL conformance remains optional and separate from default CI.

| Current surface | Status | Coverage boundary |
| --- | --- | --- |
| `casda_search_products` | **Implemented** | Bounded generated ADQL over ObsCore with facility/instrument filters and opaque cursors; `query.py`, `tests/test_query.py`, `tests/test_service.py` |
| `casda_get_product` | **Implemented** | One exact identifier and the supported ObsCore model |
| `casda_get_observation` | **Implemented** | ASKAP SBID convention, related projects, bounded products |
| Discovery tools (VOSI, TAP_SCHEMA, SIA/SCS/SSA, projects, events) | **Implemented** | `vosi.py`, `service.py`, `tests/test_discovery.py`, `tests/test_vo_search.py`, `tests/test_events.py`; optional `-m live` checks |
| Advanced ADQL / async TAP | **Implemented** | Flag-gated SELECT-only policy; `adql.py`, `tests/test_adql.py`, `tests/test_tap_jobs.py` |
| `casda_stage_products` / `casda_stage_pawsey` / cutout / spectrum / data-job tools | **Implemented** | Full-file WEB, Pawsey pull, cutout, and spectrum SODA jobs plus abort/delete/results/download; Pawsey responses include human-gate warnings; `tests/test_staging.py`, `tests/test_datalink_jobs.py` |
| `casda_download_product` / `casda_download_job_results` / `casda_verify_file` | **Implemented** | Restricted local root, checksum/length guards; `tests/test_downloads.py` |
| `casda_create_manifest` | **Implemented** | Deterministic product manifest with collection metadata fields |
| `casda_resolve_collection_doi` | **Implemented** | Public DataCite/doi.org read-only resolve; never mints; `tests/test_doi.py` |
| `casda_get_dap_navigation` | **Implemented** | Safe DAP deep links and structured unsupported privileged actions; `tests/test_dap_navigation.py` |
| Product, observation, staging, event, manifest, skill, archive, DAP navigation, and server-status resources | **Implemented** | Including `casda://archive/status`, `casda://archive/capabilities`, `casda://dap/navigation`, `casda://events/{event_id}` |
| MCP prompts | **Implemented** | Nine workflow prompts, including `make-cutout`, `dap-navigate`, `query-tables`, and `run-adql` |
| Agent skills | **Implemented** | Four packaged `SKILL.md` files exposed as `casda://skills` resources and mirrored under `.cursor/skills/` |

## Confirmed CASDA endpoints

The endpoint paths and advertised versions in this table were checked against the live CASDA
deployment on 18 July 2026. Standard behavior is defined by the linked IVOA specifications.

| Capability | Protocol observed | Endpoint | Access | MCP status |
| --- | --- | --- | --- | --- |
| TAP service | TAP 1.0; ADQL 2.0 | `https://casda.csiro.au/casda_vo_tools/tap` | Public | **Implemented** |
| TAP synchronous query | TAP/DALI | `https://casda.csiro.au/casda_vo_tools/tap/sync` | Public | **Implemented**: generated allowlisted queries plus flag-gated `casda_tap_query` |
| TAP asynchronous jobs | TAP/UWS | `https://casda.csiro.au/casda_vo_tools/tap/async` | Public | **Implemented**: `casda_submit_tap_query` and lifecycle tools (`tests/test_tap_jobs.py`) |
| Authenticated TAP | TAP/UWS | `https://data.csiro.au/casda_vo_proxy/vo/tap/{sync,async}` | OPAL/Nexus | **Implemented** for credential verification and authenticated DataLink/SODA; advanced ADQL uses configured TAP URLs |
| TAP availability | VOSI | `https://casda.csiro.au/casda_vo_tools/tap/availability` | Public | **Implemented**: `casda_get_archive_status`; `vosi.py`, `tests/test_discovery.py` |
| TAP capabilities | VOSI | `https://casda.csiro.au/casda_vo_tools/tap/capabilities` | Public | **Implemented**: `casda_list_capabilities` |
| TAP schemas and tables | VOSI | `https://casda.csiro.au/casda_vo_tools/tap/tables` | Public | **Implemented** via TAP_SCHEMA tools (`casda_list_schemas`, `casda_list_tables`, `casda_describe_table`, `casda_list_foreign_keys`) |
| TAP examples | TAP examples | `https://casda.csiro.au/casda_vo_tools/tap/examples` | Public | **Implemented** via `casda_list_tap_examples` |
| Multidimensional image discovery | SIA 2.0 | `https://casda.csiro.au/casda_vo_tools/sia2/query` | Public discovery | **Implemented**: `casda_search_images`; `tests/test_vo_search.py` |
| Legacy image discovery | SIA 1.0 | `https://casda.csiro.au/casda_vo_tools/sia1/query` | Public discovery | **Implemented**: `casda_search_survey_images` |
| Image survey inventory | CASDA SIA extension | `https://casda.csiro.au/casda_vo_tools/sia1/surveys` | Public | **Implemented**: `casda_list_image_surveys` |
| Catalogue cone search | SCS 1.03 | `https://casda.csiro.au/casda_vo_tools/scs/{catalogue_short_name}` | Public; one endpoint per catalogue | **Implemented**: `casda_list_catalogues`, `casda_search_catalogue` |
| Spectrum discovery | SSA 1.1 | `https://casda.csiro.au/casda_vo_tools/ssa/query` | Public | **Implemented**: `casda_search_spectra` |
| Public DataLink | DataLink 1.1 | `https://casda.csiro.au/casda_vo_tools/datalink/links?ID={publisher_did}` | Public metadata | **Implemented**: `casda_get_datalink` |
| Authenticated DataLink | DataLink 1.1 | `https://data.csiro.au/casda_vo_proxy/vo/datalink/links?ID={publisher_did}` | OPAL/Nexus | **Implemented** for staging/cutout/spectrum descriptor selection |
| Full-file staging | SODA/UWS | `https://casda.csiro.au/casda_data_access/data/async` | Authenticated DataLink token | **Implemented**: `casda_stage_products` |
| Spatial/spectral cutout | SODA/UWS | Same async endpoint; DataLink descriptor `cutout_service` | Authenticated | **Implemented**: `casda_create_cutout`; `tests/test_datalink_jobs.py` |
| Integrated spectrum generation | SODA/UWS | Same async endpoint; descriptor `spectrum_generation_service` | Authenticated | **Implemented**: `casda_create_spectrum` |
| Pawsey staging | SODA/UWS | Descriptor `pawsey_async_service` | Authenticated/Pawsey workflow | **Implemented**: `casda_stage_pawsey` (human licence/HPC gate documented) |
| Observation events | VOEvent-style HTTP feed | `https://casda.csiro.au/casda_data_access/observations/events` | Public | **Implemented**: `casda_list_events`; `tests/test_events.py` |
| Data Access API description | OpenAPI | `https://casda.csiro.au/casda_data_access/swagger-ui.html` | Public documentation | Research/discovery source |

The CASDA VO Tools implementation also documents protocol bases for TAP, SCS, SSA, SIA 1, SIA 2,
and DataLink. Service URLs returned by VOSI and DataLink should ultimately be discovered and
validated rather than copied into workflow logic.

## Service discovery and TAP

| Required function | Target MCP surface | Current implementation | Evidence |
| --- | --- | --- | --- |
| Local server health | `/healthz`; `casda://server/status` | **Implemented** | `tests/test_contract.py` |
| Readiness | `/readyz` | **Implemented**; last-known archive availability, never blocks on a live probe | `server.py`, `tests/test_contract.py` |
| CASDA archive availability | `casda_get_archive_status`; `casda://archive/status` | **Implemented** | `vosi.py`, `tests/test_discovery.py`, optional live |
| Protocol capabilities | `casda_list_capabilities`; `casda://archive/capabilities` | **Implemented** | `vosi.py`, `tests/test_discovery.py` |
| Schema inventory | `casda_list_schemas` | **Implemented** | `cursor.py` pagination; `tests/test_discovery.py` |
| Table inventory | `casda_list_tables` | **Implemented** | Schema filter + pagination |
| Table description | `casda_describe_table` | **Implemented** | Column name, type, UCD, unit, description |
| Foreign keys | `casda_list_foreign_keys` | **Implemented** | TAP_SCHEMA relationship tests |
| Generated ObsCore query | `casda_search_products` | **Implemented** | Product/facility filters, opaque cursors, release semantics |
| Exact product metadata | `casda_get_product`; `casda://products/{id}` | **Implemented** | Continue drift tests as ObsCore changes |
| Advanced read-only ADQL | `casda_tap_query` | **Implemented** | SELECT-only policy; `CASDA_ENABLE_ADVANCED_ADQL`; `adql.py`, `tests/test_adql.py` |
| ADQL construction and validation | `casda_build_adql`; `casda_validate_adql` | **Implemented** | Deterministic no-network tests |
| Async TAP submission | `casda_submit_tap_query` | **Implemented** | `tests/test_tap_jobs.py` |
| TAP job lifecycle | `casda_get_tap_job`; `casda_get_tap_results`; `casda_abort_tap_job`; `casda_delete_tap_job` | **Implemented** | Phase, result, abort, delete coverage |

The advanced ADQL surface remains isolated from the higher-level safe search tool. It is
read-only and bounded; a generic query tool must not become an arbitrary database mutation or
unlimited-result interface. CASDA currently advertises [TAP 1.0][tap-1] and
[ADQL 2.0][adql-2], while product discovery follows [ObsCore 1.1][obscore-1-1].

## Scientific discovery

| Required function | Target MCP surface | Current implementation |
| --- | --- | --- |
| ObsCore product search | `casda_search_products` | **Implemented**: bounded allowlisted criteria with cursors |
| Product inspection | `casda_get_product`; product resource | **Implemented** for supported fields |
| Observation inspection | `casda_get_observation`; observation resource | **Implemented**: ASKAP `obs_id = 'ASKAP-{sbid}'` convention |
| Facility/instrument-aware discovery | Filters on higher-level search tools | **Implemented**: `facility_name`, `instrument_name` |
| Image/cube discovery | `casda_search_images` using SIA 2 | **Implemented**; `tests/test_vo_search.py` |
| Survey image inventory and search | `casda_list_image_surveys`; `casda_search_survey_images` using SIA 1 | **Implemented** |
| Catalogue inventory | `casda_list_catalogues` | **Implemented** |
| Catalogue row cone search | `casda_search_catalogue` using SCS | **Implemented** |
| Spectrum discovery | `casda_search_spectra` using SSA | **Implemented** |
| Project and collection discovery | `casda_search_projects`; `casda_get_project`; `casda_get_collection` | **Implemented**; `tests` in discovery/events suites |
| Observation lifecycle events | `casda_list_events`; event resource template | **Implemented**; `tests/test_events.py` |
| DOI/collection metadata read | `casda_resolve_collection_doi` | **Implemented** as public DataCite/doi.org read (`tests/test_doi.py`); minting remains DAP boundary |
| Safe DAP navigation helpers | `casda_get_dap_navigation`; `casda://dap/navigation` | **Implemented**; privileged actions return structured unsupported responses |
| Astronomical name resolution | Accept resolved ICRS coordinates | External service, not a CASDA protocol |

SIA, SCS, and SSA are separate supported archive interfaces, not aliases of TAP. Their response
metadata and protocol-specific query parameters are preserved.
See [SIA 2.0][sia-2], [Simple Cone Search 1.03][scs-1-03], and [SSA 1.1][ssa-1-1].

## Data access and server-side processing

| Required function | Target MCP surface | Current implementation |
| --- | --- | --- |
| Authentication state | `casda_get_auth_status` | **Implemented** |
| Inspect DataLink response | `casda_get_datalink` | **Implemented**; `tests/test_datalink_jobs.py` |
| Select advertised access service | Allowlisted descriptor input | **Implemented**: full-file, Pawsey, cutout, and spectrum descriptors |
| Full-file staging | `casda_stage_products` | **Implemented**: bounded, idempotent, serialised submission |
| Pawsey staging | `casda_stage_pawsey` | **Implemented**: DataLink `pawsey_async_service`; human licence/HPC gate warnings; distinct from WEB download staging |
| Data job status | `casda_get_data_job`, retaining `casda_get_staging_status` as a focused alias | **Implemented** |
| Spatial/spectral cutout | `casda_create_cutout` with `CIRCLE`, `POLYGON`, `BAND`, `CHANNEL`, `POL`, and `COORD` | **Implemented** |
| Integrated spectrum | `casda_create_spectrum` | **Implemented** |
| Job results | `casda_get_data_job_results` | **Implemented** |
| Abort/delete data job | `casda_abort_data_job`; `casda_delete_data_job` | **Implemented** |
| Download one result | `casda_download_product` | **Implemented**: hardened, bounded, validator-aware single-result download |
| Download all selected results | `casda_download_job_results` | **Implemented** |
| Verify a local artifact | Integrated verification and optional `casda_verify_file` | **Implemented** |
| Reproducible selection | `casda_create_manifest`; manifest resource | **Implemented**: typed deterministic manifest with collection metadata |
| Public DOI resolve | `casda_resolve_collection_doi` | **Implemented**: DataCite/doi.org read-only; collection/project lookup never invents DOIs |

CASDA DataLink responses advertise `async_service`, `pawsey_async_service`, `cutout_service`, and
`spectrum_generation_service`. The opaque authenticated token returned by DataLink is passed as the
SODA `ID`; it must be treated as sensitive. The cutout parameters observed in CASDA clients are
`CIRCLE`, `POLYGON`, `BAND` (wavelength in metres), `CHANNEL`, `POL`, and `COORD`.

The workflow is governed by [DataLink 1.1][datalink-1-1], [SODA 1.0][soda-1], and
[UWS 1.1][uws-1-1]. No tool may claim that a result is downloadable until a completed job reports a
unique matching result. Signed or opaque result URLs must not appear in logs, manifests, cache keys,
or cross-principal state.

## Product-family coverage

The [CASDA User Guide][casda-user-guide] describes images and cubes, catalogues, measurement sets,
evaluation/validation material, extracted spectra, and derived products. The live archive also uses
the identifier families `cube-*`, `catalogue-*`, `spectrum-*`, `moment_map-*`, `cubelet-*`,
`evaluation-*`, `visibility-*`, and `scan-*`.

| Product family | Current coverage | Notes |
| --- | --- | --- |
| 2D images and 3D cubes | Metadata, SIA 1/2, full-file staging, cutout, spectrum, status, download | Authenticated live OPAL conformance optional |
| Weights, moment maps, and cubelets | Allowlisted ObsCore types including cubelets | Subtype-preserving discovery via ObsCore/SIA |
| Catalogues | File-level ObsCore metadata, inventory, SCS row query | Table/column metadata via TAP_SCHEMA tools |
| Spectra | File-level metadata, SSA discovery, generated-spectrum jobs | — |
| Visibilities/measurement sets | Generic metadata/staging/download path | Large-product bounds apply |
| Evaluation/validation products | Allowlisted evaluation identifiers | Privileged validation tasks remain DAP-boundary |
| Scans and ancillary files | Allowlisted scan product type | — |
| Observation/project metadata | Projects, collections, events, ASKAP and facility filters | — |

## Live TAP metadata snapshot

This section records what the public TAP service returned on **18 July 2026**. It is intended to
make schema drift visible, not to replace VOSI discovery.

Observed schemas:

`AS101`, `AS102`, `AS103`, `AS110`, `AS207`, `C1967`, `casda`, `internal`, `ivoa`, and
`TAP_SCHEMA`.

Core tables relevant to this MCP:

- `ivoa.obscore`
- `ivoa.spectrum_dm`
- `casda.catalogue`
- `casda.observation`
- `casda.observation_evaluation_file`
- `casda.observation_event`
- `casda.observation_validation_metric`
- `casda.project`

Observed TAP limits and capabilities:

- default retention: 43,200 seconds;
- hard retention limit: 432,000 seconds;
- advertised execution duration: 360,000 seconds;
- hard output limit: 20,000,000 rows;
- TAP 1.0, ADQL 2.0, and ObsCore 1.1.

`casda.observation` columns observed: `deposit_state`, `id`, `obs_end`, `obs_end_mjd`,
`obs_program`, `obs_start`, `obs_start_mjd`, `sbid`, and `telescope`.

`casda.project` columns observed: `id`, `opal_code`, `principal_first_name`,
`principal_last_name`, and `short_name`.

`casda.catalogue` columns observed: `filename`, `format`, `freq_ref`, `id`, `image_id`,
`observation_id`, `project_id`, `quality_level`, `released_date`, `time_obs`, and `time_obs_mjd`.

<details>
<summary>All 42 observed <code>ivoa.obscore</code> columns</summary>

`access_estsize`, `access_format`, `access_url`, `calib_level`, `dataproduct_subtype`,
`dataproduct_type`, `em_max`, `em_min`, `em_resolution`, `em_res_power`, `em_ucd`, `em_unit`,
`em_xel`, `facility_name`, `filename`, `instrument_name`, `obs_collection`, `obs_id`,
`obs_publisher_did`, `obs_release_date`, `o_ucd`, `pol_states`, `pol_xel`, `quality_level`, `s_dec`,
`s_fov`, `s_ra`, `s_region`, `s_resolution`, `s_resolution_max`, `s_resolution_min`, `s_ucd`,
`s_unit`, `s_xel1`, `s_xel2`, `target_name`, `t_exptime`, `t_max`, `t_min`, `t_resolution`, `t_xel`,
and `thumbnail_id`.

</details>

## Astroquery compatibility

The target is semantic compatibility with documented [`astroquery.casda`][astroquery-casda]
workflows, not a Python-specific response format.

| Astroquery behavior | MCP equivalent |
| --- | --- |
| `Casda.query_region()` | Bounded ObsCore or SIA 2 search |
| `Casda.query_region_async()` | A real TAP UWS job, not a synchronous response relabelled as asynchronous |
| `filter_out_unreleased()` | Server-side release constraint where supported; conservative bounded fallback with truncation disclosed |
| `login()` | Per-principal authentication verification and status |
| `stage_data()` | Stage products, inspect one-shot job state, enumerate results |
| `cutout()` | Select the DataLink descriptor and submit a SODA spatial/spectral/channel job |
| `download_files()` | Guarded single/batch download with length, checksum, retry, and safe resume |
| Direct `TapPlus` use | TAP sync/async tools, schema discovery, jobs, and results |

The MCP should not inherit known client limitations: naive rectangular RA wrapping,
pseudo-asynchronous responses, serial DataLink resolution, unbounded polling, incomplete
client-side release filtering, or downloads without robust retry/resume/checksum guarantees. The
[official Astroquery CASDA source][astroquery-source] and [CSIRO samples][casda-samples] are the
behavioral references.

## MCP and AI contract requirements

Full CASDA coverage also requires a complete and safe MCP contract:

| Requirement | Target state |
| --- | --- |
| Tool failures | **Met**: protocol-level MCP errors via `ToolError` / `isError: true` (`tests/test_mcp_tools.py`) |
| Tool metadata | **Met**: titles plus `readOnlyHint`, `destructiveHint`, `idempotentHint`, and `openWorldHint` annotations |
| Resources | **Met**: products, observations, staging, events, manifests, server status, archive status/capabilities, packaged skill index/markdown |
| Prompts | **Met**: `find-and-inspect-products`, `query-catalogue`, `query-tables`, `run-adql`, `make-cutout` (supported cutout workflow), `stage-and-download`, `monitor-releases`, and `build-reproducible-selection` |
| Agent skills | **Met**: packaged `SKILL.md` guidance via `casda://skills` and `.cursor/skills/` (not first-class MCP `skills/*` protocol methods) |
| Pagination | **Met**: stable opaque cursors (`cursor.py`) for large tables, catalogues, and event feeds |
| Long-running operations | **Met**: MCP progress notifications on downloads; cancellation propagation where FastMCP supports it |
| Load safety | **Met**: global and per-operation rate/concurrency limits; bounded decoded responses |
| Principal isolation | **Met** for single-principal process deployments; remote multi-user must run one process per principal (no shared credentials, jobs, ready URLs, caches, or manifests across principals) |
| Transport security | **Met**: stdio support; remote HTTP only behind MCP authorization or a trusted authenticating proxy |
| Service health | **Met**: separate `/healthz` liveness and `/readyz` readiness; archive availability is not inferred from process liveness alone |
| Provenance | **Met**: sanitised endpoint, parameters, timestamps, query/correlation IDs, result counts, cache state, and server version |

These requirements follow the official MCP specifications for [tools][mcp-tools],
[resources][mcp-resources], [prompts][mcp-prompts], and [authorization][mcp-authorization].

## DAP and administrative boundary

The DAP remains important to CASDA, but UI parity does not authorize browser scraping or privileged
mutations. The supported boundary is:

| DAP behavior | MCP policy |
| --- | --- |
| Observation Search form | **DAP boundary for the UI**; TAP/SIA tools plus `casda_get_dap_navigation` deep links |
| Interactive Skymap/Aladin selection and preview | **DAP boundary**; accept explicit coordinates or return a DAP navigation link via `casda_get_dap_navigation` |
| Curated Cutout UI | **DAP boundary for the UI**; the underlying documented SODA cutout operation is implemented via MCP tools |
| CARTA viewer/session launch | **DAP boundary**; `casda_get_dap_navigation(action="launch_carta")` returns structured unsupported_actions |
| Automatic email notifications | Archive/DAP responsibility; MCP returns job status and result information |
| Licence acknowledgement or Pawsey account confirmation | Human action; never auto-accept; Pawsey staging surfaces `human_gate_warnings` |
| Project role assignment | Privileged DAP administration; structured unsupported via `casda_get_dap_navigation` |
| Quality flags, validation notes, and validation tasks | Privileged science-team workflow, out of scope |
| Release or rejection of an observation | CASDA administrator mutation; structured unsupported via navigation helper |
| Level 7 deposit creation and publishing | Privileged DAP/Pawsey workflow; structured unsupported via navigation helper |
| DOI minting and DataCite administration | Administrative boundary; public read via `casda_resolve_collection_doi` only |
| Unreleased data access | In scope only through the current user's authenticated principal and CASDA authorization; never bypass or infer entitlement |

The User Guide documents the DAP's [validation, project-role, and Level 7 deposit
workflows][casda-user-guide]. They should be represented as explicit navigation or unsupported-action
responses when useful, never as inferred programmatic APIs.

## Verification and drift policy

- Every **Implemented** row must name its production module and contract/protocol test in the
  implementing change.
- Every live endpoint or metadata snapshot must carry a verification date.
- Live tests are read-only unless a dedicated account and explicit test authority are supplied.
- VOSI, TAP_SCHEMA, DataLink descriptors, and the Data Access OpenAPI description take precedence
  over copied endpoint strings.
- Newly observed schemas, product families, facilities, or service descriptors update this matrix
  and protocol fixtures before or with the implementation.
- Public live tests (`tests/test_live.py`, `-m live`) cover VOSI availability/capabilities, TAP sync,
  schema listing, SIA 2 cone, SIA 1 surveys, catalogue inventory, and the event feed when
  `CASDA_RUN_LIVE_TESTS=true`.
- Authenticated staging, cutout, spectrum, and download conformance must be reported separately from
  mocked protocol coverage and are never exercised by default live tests.
- An upstream timeout or transient outage does not erase capability support; it must produce a
  bounded, retry-aware, accurately classified error.

## References

### CASDA and client behavior

- [CASDA User Guide][casda-user-guide]
- [CASDA Services][casda-services]
- [CASDA Data Products][casda-data-products]
- [CASDA Release Notes][casda-release-notes]
- [CASDA Data Access OpenAPI UI][casda-openapi]
- [CSIRO CASDA VO Tools][casda-vo-tools]
- [CSIRO CASDA sample clients][casda-samples]
- [Astroquery CASDA documentation][astroquery-casda]
- [Astroquery CASDA source][astroquery-source]

### IVOA standards

- [TAP 1.0][tap-1]
- [ADQL 2.0][adql-2]
- [ObsCore 1.1][obscore-1-1]
- [SIA 2.0][sia-2]
- [Simple Cone Search 1.03][scs-1-03]
- [SSA 1.1][ssa-1-1]
- [DataLink 1.1][datalink-1-1]
- [SODA 1.0][soda-1]
- [UWS 1.1][uws-1-1]
- [VOSI 1.1][vosi-1-1]

### MCP specifications

- [MCP tools][mcp-tools]
- [MCP resources][mcp-resources]
- [MCP prompts][mcp-prompts]
- [MCP authorization][mcp-authorization]

[casda-user-guide]: https://research.csiro.au/casda/casda-user-guide/
[casda-services]: https://research.csiro.au/casda/services/
[casda-data-products]: https://research.csiro.au/casda/data-products/
[casda-release-notes]: https://research.csiro.au/casda/support/casda-release-notes/
[casda-openapi]: https://casda.csiro.au/casda_data_access/swagger-ui.html
[casda-vo-tools]: https://github.com/csiro-rds/casda_vo_tools
[casda-samples]: https://github.com/csiro-rds/casda-samples
[astroquery-casda]: https://astroquery.readthedocs.io/en/latest/casda/casda.html
[astroquery-source]: https://github.com/astropy/astroquery/blob/main/astroquery/casda/core.py
[tap-1]: https://www.ivoa.net/documents/TAP/20100327/
[adql-2]: https://www.ivoa.net/documents/ADQL/20081030/
[obscore-1-1]: https://www.ivoa.net/documents/ObsCore/20170509/
[sia-2]: https://www.ivoa.net/documents/SIA/20151223/
[scs-1-03]: https://www.ivoa.net/documents/ConeSearch/20080222/
[ssa-1-1]: https://www.ivoa.net/documents/SSA/20120210/
[datalink-1-1]: https://www.ivoa.net/documents/DataLink/20231215/
[soda-1]: https://www.ivoa.net/documents/SODA/20170517/
[uws-1-1]: https://www.ivoa.net/documents/UWS/20161024/
[vosi-1-1]: https://www.ivoa.net/documents/VOSI/20170524/
[mcp-tools]: https://modelcontextprotocol.io/specification/latest/server/tools
[mcp-resources]: https://modelcontextprotocol.io/specification/latest/server/resources
[mcp-prompts]: https://modelcontextprotocol.io/specification/latest/server/prompts
[mcp-authorization]: https://modelcontextprotocol.io/specification/latest/basic/authorization
