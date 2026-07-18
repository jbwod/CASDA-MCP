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

This is the public surface at the start of the broader CASDA overhaul. A **Partial** label here does
not mean the existing contract is untested; it means the tool covers only part of the archive
capability to which it maps.

| Current surface | Status | Coverage boundary |
| --- | --- | --- |
| `casda_search_products` | **Partial** | Bounded generated ADQL over a supported ObsCore subset |
| `casda_get_product` | **Implemented** | One exact identifier and the currently supported ObsCore model |
| `casda_get_observation` | **Partial** | ASKAP SBID convention, related projects, bounded products |
| `casda_stage_products` | **Partial** | Full-file async service only; no cutout/spectrum/Pawsey mode |
| `casda_get_staging_status` | **Partial** | One status read for a job previously created by this server |
| `casda_download_product` | **Partial** | One recorded ready artifact under a restricted local root |
| `casda_create_manifest` | **Partial** | Deterministic product manifest without full collection/DOI citation metadata |
| Product, observation, staging, manifest, and server-status resources | **Partial** | Five resource templates; archive discovery resources are not yet exposed |
| MCP prompts | **Planned** | No prompt templates are currently registered |

## Confirmed CASDA endpoints

The endpoint paths and advertised versions in this table were checked against the live CASDA
deployment on 18 July 2026. Standard behavior is defined by the linked IVOA specifications.

| Capability | Protocol observed | Endpoint | Access | MCP status |
| --- | --- | --- | --- | --- |
| TAP service | TAP 1.0; ADQL 2.0 | `https://casda.csiro.au/casda_vo_tools/tap` | Public | **Partial** |
| TAP synchronous query | TAP/DALI | `https://casda.csiro.au/casda_vo_tools/tap/sync` | Public | **Partial**: generated, allowlisted queries only |
| TAP asynchronous jobs | TAP/UWS | `https://casda.csiro.au/casda_vo_tools/tap/async` | Public | **Planned** |
| Authenticated TAP | TAP/UWS | `https://data.csiro.au/casda_vo_proxy/vo/tap/{sync,async}` | OPAL/Nexus | **Planned**; proxy availability is currently used only to verify credentials |
| TAP availability | VOSI | `https://casda.csiro.au/casda_vo_tools/tap/availability` | Public | **Planned** as an archive-status surface |
| TAP capabilities | VOSI | `https://casda.csiro.au/casda_vo_tools/tap/capabilities` | Public | **Planned** |
| TAP schemas and tables | VOSI | `https://casda.csiro.au/casda_vo_tools/tap/tables` | Public | **Planned** |
| TAP examples | TAP examples | `https://casda.csiro.au/casda_vo_tools/tap/examples` | Public | **Planned** |
| Multidimensional image discovery | SIA 2.0 | `https://casda.csiro.au/casda_vo_tools/sia2/query` | Public discovery | **Planned** |
| Legacy image discovery | SIA 1.0 | `https://casda.csiro.au/casda_vo_tools/sia1/query` | Public discovery | **Planned** |
| Image survey inventory | CASDA SIA extension | `https://casda.csiro.au/casda_vo_tools/sia1/surveys` | Public | **Planned** |
| Catalogue cone search | SCS 1.03 | `https://casda.csiro.au/casda_vo_tools/scs/{catalogue_short_name}` | Public; one endpoint per catalogue | **Planned** |
| Spectrum discovery | SSA 1.1 | `https://casda.csiro.au/casda_vo_tools/ssa/query` | Public | **Planned** |
| Public DataLink | DataLink 1.1 | `https://casda.csiro.au/casda_vo_tools/datalink/links?ID={publisher_did}` | Public metadata | **Partial**, used internally |
| Authenticated DataLink | DataLink 1.1 | `https://data.csiro.au/casda_vo_proxy/vo/datalink/links?ID={publisher_did}` | OPAL/Nexus | **Partial**, used internally |
| Full-file staging | SODA/UWS | `https://casda.csiro.au/casda_data_access/data/async` | Authenticated DataLink token | **Partial** |
| Spatial/spectral cutout | SODA/UWS | Same async endpoint; DataLink descriptor `cutout_service` | Authenticated | **Planned** |
| Integrated spectrum generation | SODA/UWS | Same async endpoint; descriptor `spectrum_generation_service` | Authenticated | **Planned** |
| Pawsey staging | SODA/UWS | Descriptor `pawsey_async_service` | Authenticated/Pawsey workflow | **Upstream-dependent** |
| Observation events | VOEvent-style HTTP feed | `https://casda.csiro.au/casda_data_access/observations/events` | Public | **Planned** |
| Data Access API description | OpenAPI | `https://casda.csiro.au/casda_data_access/swagger-ui.html` | Public documentation | Research/discovery source |

The CASDA VO Tools implementation also documents protocol bases for TAP, SCS, SSA, SIA 1, SIA 2,
and DataLink. Service URLs returned by VOSI and DataLink should ultimately be discovered and
validated rather than copied into workflow logic.

## Service discovery and TAP

| Required function | Target MCP surface | Current implementation | Evidence needed for completion |
| --- | --- | --- | --- |
| Local server health | `/healthz`; `casda://server/status` | **Implemented** | Existing contract and MCP tests |
| CASDA archive availability | `casda_get_archive_status`; `casda://archive/status` | **Planned**; local health is not archive availability | Real VOSI fixture, outage mapping, live read-only test |
| Protocol capabilities | `casda_list_capabilities`; `casda://archive/capabilities` | **Planned** | Parse and preserve the live VOSI document |
| Schema inventory | `casda_list_schemas` | **Planned** | VOSI/TAP_SCHEMA tests and stable pagination |
| Table inventory | `casda_list_tables` | **Planned** | Schema filter and pagination tests |
| Table description | `casda_describe_table` | **Planned** | Column name, type, UCD, unit, description, and key fidelity |
| Foreign keys | `casda_list_foreign_keys` | **Planned** | TAP_SCHEMA relationship tests |
| Generated ObsCore query | `casda_search_products` | **Partial**: safe bounded filter and sort subset | Complete product/facility filters, stable cursor pagination, release semantics |
| Exact product metadata | `casda_get_product`; `casda://products/{id}` | **Implemented** for the supported ObsCore model | Continue drift tests as ObsCore changes |
| Advanced read-only ADQL | `casda_tap_query` | **Planned** | SELECT-only policy, read-only table policy, row/time/response limits, query audit |
| ADQL construction and validation | `casda_build_adql`; `casda_validate_adql` | **Planned** | Deterministic no-network tests |
| Async TAP submission | `casda_submit_tap_query` | **Planned** | UWS job-creation and ambiguous-response tests |
| TAP job lifecycle | `casda_get_tap_job`; `casda_get_tap_results`; `casda_abort_tap_job`; `casda_delete_tap_job` | **Planned** | Phase, result, abort, delete, expiry, and live lifecycle tests |

The advanced ADQL surface must remain isolated from the higher-level safe search tool. It must be
read-only and bounded; a generic query tool must not become an arbitrary database mutation or
unlimited-result interface. CASDA currently advertises [TAP 1.0][tap-1] and
[ADQL 2.0][adql-2], while product discovery follows [ObsCore 1.1][obscore-1-1].

## Scientific discovery

| Required function | Target MCP surface | Current implementation |
| --- | --- | --- |
| ObsCore product search | `casda_search_products` | **Partial**: bounded allowlisted criteria |
| Product inspection | `casda_get_product`; product resource | **Implemented** for supported fields |
| Observation inspection | `casda_get_observation`; observation resource | **Partial**: ASKAP `obs_id = 'ASKAP-{sbid}'` convention |
| Facility/instrument-aware discovery | Filters on higher-level search tools | **Planned** |
| Image/cube discovery | `casda_search_images` using SIA 2 | **Planned** |
| Survey image inventory and search | `casda_list_image_surveys`; `casda_search_survey_images` using SIA 1 | **Planned** |
| Catalogue inventory | `casda_list_catalogues` | **Planned** |
| Catalogue row cone search | `casda_search_catalogue` using SCS | **Planned** |
| Spectrum discovery | `casda_search_spectra` using SSA | **Planned** |
| Project and collection discovery | `casda_search_projects`; `casda_get_project`; `casda_get_collection` | **Planned** |
| Observation lifecycle events | `casda_list_events`; event resource template | **Planned** |
| DOI/collection metadata read | `casda_resolve_collection_doi` | **Upstream-dependent** until a stable machine contract is confirmed from OpenAPI |
| Astronomical name resolution | Accept resolved ICRS coordinates | External service, not a CASDA protocol |

SIA, SCS, and SSA are separate supported archive interfaces, not aliases to be marked complete when
only TAP works. Their response metadata and protocol-specific query parameters must be preserved.
See [SIA 2.0][sia-2], [Simple Cone Search 1.03][scs-1-03], and [SSA 1.1][ssa-1-1].

## Data access and server-side processing

| Required function | Target MCP surface | Current implementation |
| --- | --- | --- |
| Authentication state | `casda_get_auth_status` | **Planned**; authentication verification is internal |
| Inspect DataLink response | `casda_get_datalink` | **Planned**; a constrained parser is used internally |
| Select advertised access service | Allowlisted descriptor input | **Partial**: full-file staging path only |
| Full-file staging | `casda_stage_products` | **Partial**: bounded, idempotent, serialised submission; authenticated live conformance still missing |
| Data job status | `casda_get_data_job`, retaining `casda_get_staging_status` as a focused alias | **Partial**: one-shot status for known staging jobs |
| Spatial/spectral cutout | `casda_create_cutout` with `CIRCLE`, `POLYGON`, `BAND`, `CHANNEL`, `POL`, and `COORD` | **Planned** |
| Integrated spectrum | `casda_create_spectrum` | **Planned** |
| Job results | `casda_get_data_job_results` | **Partial**: results are consumed within staging status rather than exposed generally |
| Abort/delete data job | `casda_abort_data_job`; `casda_delete_data_job` | **Planned** |
| Download one result | `casda_download_product` | **Partial**: guarded stream/checksum path exists; validator-aware resume and destination-race hardening remain |
| Download all selected results | `casda_download_job_results` | **Planned** |
| Verify a local artifact | Integrated verification and optional `casda_verify_file` | **Partial** |
| Reproducible selection | `casda_create_manifest`; manifest resource | **Partial**: typed deterministic manifest exists; collection DOI/citation metadata is not yet included |

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

| Product family | Current coverage | Work required for complete coverage |
| --- | --- | --- |
| 2D images and 3D cubes | Metadata, full-file staging, status, download | SIA 1/2, spatial/spectral/channel cutout, integrated spectrum |
| Weights, moment maps, and cubelets | Weight and moment-map aliases are partly represented | Cubelet identifier/type support and subtype-preserving discovery |
| Catalogues | File-level ObsCore metadata | Catalogue inventory, SCS row query, table/column metadata |
| Spectra | File-level metadata | SSA discovery and generated-spectrum workflow |
| Visibilities/measurement sets | Generic metadata/staging/download path | Explicit subtype coverage and representative large-product tests |
| Evaluation/validation products | Not fully allowlisted | Evaluation identifiers, quality metrics, validation-file metadata |
| Scans and ancillary files | Not fully represented | Scan identifiers and ancillary subtype coverage |
| Observation/project metadata | ASKAP-focused subset | Facility-neutral identity, commensal projects, quality and event tables |

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
| Tool failures | Protocol-level MCP errors and `isError: true`, not only a successful response envelope containing an `error` field |
| Tool metadata | Titles plus correct `readOnlyHint`, `destructiveHint`, `idempotentHint`, and `openWorldHint` annotations |
| Resources | Archive services, schemas, tables, catalogues, products, observations, projects, jobs, and manifests |
| Prompts | `find-and-inspect-products`, `query-catalogue`, `make-cutout`, `stage-and-download`, `monitor-releases`, and `build-reproducible-selection` |
| Pagination | Stable opaque cursors for large tables, catalogues, and event feeds |
| Long-running operations | MCP progress notifications and cancellation propagation |
| Load safety | Global and per-operation rate/concurrency limits; bounded decoded responses |
| Principal isolation | No shared credentials, authorization results, job state, ready URLs, caches, or manifests across remote users |
| Transport security | stdio support; remote HTTP only behind MCP authorization or a trusted authenticating proxy |
| Service health | Separate liveness and readiness; archive availability is not inferred from process liveness |
| Provenance | Sanitised endpoint, parameters, timestamps, query/correlation IDs, result counts, cache state, and server version |

These requirements follow the official MCP specifications for [tools][mcp-tools],
[resources][mcp-resources], [prompts][mcp-prompts], and [authorization][mcp-authorization].

## DAP and administrative boundary

The DAP remains important to CASDA, but UI parity does not authorize browser scraping or privileged
mutations. The supported boundary is:

| DAP behavior | MCP policy |
| --- | --- |
| Observation Search form | **DAP boundary for the UI**; expose equivalent supported TAP/SIA operations |
| Interactive Skymap/Aladin selection and preview | **DAP boundary**; accept explicit coordinates or return a DAP navigation link |
| Curated Cutout UI | **DAP boundary for the UI**; the underlying documented SODA cutout operation remains in scope |
| CARTA viewer/session launch | **DAP boundary** unless CSIRO publishes a stable supported session API |
| Automatic email notifications | Archive/DAP responsibility; MCP returns job status and result information |
| Licence acknowledgement or Pawsey account confirmation | Human action; never auto-accept legal terms |
| Project role assignment | Privileged DAP administration, out of scope |
| Quality flags, validation notes, and validation tasks | Privileged science-team workflow, out of scope |
| Release or rejection of an observation | CASDA administrator mutation, out of scope |
| Level 7 deposit creation and publishing | Privileged DAP/Pawsey workflow, out of scope |
| DOI minting and DataCite administration | Administrative boundary; public DOI/collection metadata remains a read target |
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
- Public live tests should eventually cover VOSI availability/capabilities/tables, TAP sync and async
  lifecycle, SIA 2, SIA 1 surveys, one representative SCS catalogue, SSA, public DataLink, and the
  event feed.
- Authenticated staging, cutout, spectrum, and download conformance must be reported separately from
  mocked protocol coverage.
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
