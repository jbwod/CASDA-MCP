# CASDA MCP Server

`casda-mcp` is a conservative Model Context Protocol server for the
[CSIRO ASKAP Science Data Archive (CASDA)](https://research.csiro.au/casda/). It converts an
AI client's structured selections into explicit, auditable archive operations. The server does not
interpret unrestricted natural language or expose a generic ADQL, URL-fetching, shell, deletion, or
filesystem tool.

The supported workflow is:

1. Search bounded CASDA ObsCore metadata.
2. Inspect one product or ASKAP scheduling block.
3. Select explicit product identifiers.
4. Optionally submit one authenticated SODA/UWS staging request.
5. Check that request with a separate, single status call.
6. Optionally download one archive-confirmed file into a restricted directory.
7. Create a reproducible JSON manifest.

Search and metadata inspection are enabled by default. Staging and downloads are disabled by
default and require separate administrator configuration.

## Status and confirmed interfaces

The implementation uses these CASDA interfaces:

- TAP 1.0/ADQL over `ivoa.obscore`, `casda.observation`, and `casda.project` for metadata;
- Datalink 1.1 VOTables for authenticated SODA service and opaque product-token discovery;
- asynchronous SODA/UWS jobs for staging submission and one-shot status reads;
- archive result URLs and checksum sidecars for streamed downloads.

The public TAP availability, schema, product types, project joins, spatial intersection query, and a
bounded cube search were validated live on 12 July 2026. Authenticated staging and downloads were
not exercised live because no OPAL credentials were supplied; those paths are covered by mocked
protocol, partial-failure, checksum, resumption, and filesystem tests.

## Requirements

- Python 3.10 or newer
- [`uv`](https://docs.astral.sh/uv/) for the documented locked setup
- Network access to the configured CASDA endpoints
- An [OPAL account](https://opal.atnf.csiro.au/) only for staging operations

## Installation

```bash
git clone <repository-url> casda-mcp
cd casda-mcp
uv sync --frozen --extra dev
```

Run the server over stdio:

```bash
uv run casda-mcp
```

Run the Streamable HTTP transport on loopback:

```bash
uv run casda-mcp --transport streamable-http --host 127.0.0.1 --port 8000
```

The MCP endpoint is `http://127.0.0.1:8000/mcp`; the non-sensitive health endpoint is
`http://127.0.0.1:8000/healthz`.

## MCP client configuration

For a stdio client, adjust the absolute project path:

```json
{
  "mcpServers": {
    "casda": {
      "command": "uv",
      "args": [
        "--directory",
        "/absolute/path/to/casda-mcp",
        "run",
        "casda-mcp"
      ],
      "env": {
        "CASDA_ENABLE_STAGING": "false",
        "CASDA_ENABLE_DOWNLOADS": "false"
      }
    }
  }
}
```

Codex CLI can register the same stdio command:

```bash
codex mcp add casda \
  --env CASDA_ENABLE_STAGING=false \
  --env CASDA_ENABLE_DOWNLOADS=false \
  -- uv --directory /absolute/path/to/casda-mcp run casda-mcp
```

Equivalent Codex `config.toml`:

```toml
[mcp_servers.casda]
command = "uv"
args = ["--directory", "/absolute/path/to/casda-mcp", "run", "casda-mcp"]

[mcp_servers.casda.env]
CASDA_ENABLE_STAGING = "false"
CASDA_ENABLE_DOWNLOADS = "false"
```

ChatGPT desktop and IDE MCP settings accept either the same STDIO command or the Streamable HTTP
URL. Restart the client after adding the server. See the current
[ChatGPT MCP configuration guide](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

## Configuration

Configuration is loaded from environment variables and validated at startup. Invalid state-changing
configuration fails fast.

| Variable | Default | Purpose and security effect |
| --- | --- | --- |
| `CASDA_BASE_URL` | `https://casda.csiro.au` | CASDA base host; HTTPS and no embedded credentials required. |
| `CASDA_TAP_URL` | CASDA TAP sync URL | Fixed metadata query endpoint. Tool callers cannot override it. |
| `CASDA_DATALINK_URL` | CASDA proxy Datalink URL | Establishes an allowed CASDA host; product Datalink URLs still come from TAP. |
| `CASDA_SODA_URL` | CASDA async SODA URL | Establishes the allowed staging host and documents the expected service. |
| `CASDA_LOGIN_URL` | CASDA proxy TAP availability URL | Safe credential verification endpoint. |
| `CASDA_USERNAME` | unset | OPAL username. Required with `CASDA_PASSWORD` for staging. |
| `CASDA_PASSWORD` | unset | OPAL password. Stored as a secret value and never logged. |
| `CASDA_ENABLE_STAGING` | `false` | Enables archive-side request creation only when OPAL credentials are complete. |
| `CASDA_ENABLE_DOWNLOADS` | `false` | Enables local file writes. |
| `CASDA_DOWNLOAD_DIR` | unset | Required absolute containment directory when downloads are enabled. |
| `CASDA_ALLOW_OVERWRITE` | `false` | Allows atomic replacement of an existing destination. Keep false normally. |
| `CASDA_MAX_RESULTS` | `100` | Maximum bounded search window, up to a hard limit of 1000. |
| `CASDA_MAX_CONE_RADIUS_DEG` | `5` | Maximum cone radius in degrees. |
| `CASDA_MAX_STAGE_PRODUCTS` | `20` | Maximum deduplicated products in one staging request. |
| `CASDA_MAX_STAGE_BYTES` | `107374182400` | Maximum summed estimated staging size in bytes. |
| `CASDA_ALLOW_UNKNOWN_STAGE_SIZE` | `false` | If false, products without estimated sizes cannot be staged. |
| `CASDA_MAX_MANIFEST_PRODUCTS` | `100` | Maximum deduplicated products in one manifest. |
| `CASDA_MAX_DOWNLOAD_BYTES` | `53687091200` | Maximum archive-reported and streamed bytes for one download. |
| `CASDA_REQUEST_TIMEOUT_SECONDS` | `30` | Metadata and control-request timeout. |
| `CASDA_DOWNLOAD_TIMEOUT_SECONDS` | `300` | Timeout used for a download response. |
| `CASDA_MAX_RETRIES` | `3` | Retries for safe reads only, with exponential backoff, jitter, and `Retry-After`. |
| `CASDA_CACHE_TTL_SECONDS` | `60` | Read-only metadata cache TTL; zero disables caching. |
| `CASDA_CACHE_MAX_ENTRIES` | `256` | Process-local cache bound; zero disables caching. |
| `CASDA_STATE_DB` | unset | Optional SQLite state file, forced to owner-only mode (`0600`) on POSIX. Default state is process-local memory. |

Copy `.env.example` to `.env` for local development; it is loaded automatically and ignored by Git.
Do not commit a populated `.env` file. Production deployments should inject secrets instead.

### Enabling staging

```bash
export CASDA_USERNAME='researcher@example.edu.au'
export CASDA_PASSWORD='use-a-secret-provider-in-production'
export CASDA_ENABLE_STAGING=true
uv run casda-mcp
```

Use the process environment, an OS credential provider that injects environment variables, or a
deployment secret store. Do not put credentials in command-line arguments. The implementation uses
the OPAL HTTP Basic authentication behavior confirmed by Astroquery's CASDA client.

### Enabling downloads

```bash
export CASDA_ENABLE_DOWNLOADS=true
export CASDA_DOWNLOAD_DIR=/srv/casda-downloads
export CASDA_MAX_DOWNLOAD_BYTES=10737418240
uv run casda-mcp
```

The directory must be absolute. Caller destinations are resolved and checked for containment,
including absolute paths and `..` traversal. Parent directories are created only inside this root.
Files are written to unique temporary paths and atomically moved after length and optional checksum
verification. Incomplete files are removed after failure.

## Tools

Every normal tool response has operation-specific data, `provenance`, and an optional structured
`error`. Provenance contains the server version, archive, timestamps, deterministic query identifier,
sanitised endpoint, parameters, result count, cache status, and correlation identifier. Credentials
and URL query strings are not included.

### `casda_search_products`

Read-only bounded product discovery. Supported filters are exact source/target name, ICRS position
and radius in degrees, OPAL project code, ASKAP SBID, overlapping ISO 8601 observation dates,
overlapping frequencies in hertz, exact collection, and these allowlisted product types:

`image`, `cube`, `visibility`, `spectrum`, `catalogue`, `weight`, `moment_map`.

It supports bounded one-based pagination and allowlisted sorting. It does not resolve astronomical
names, execute caller-supplied ADQL, stage, or download.

```json
{
  "ra_deg": 333.8,
  "dec_deg": -46.0,
  "radius_deg": 0.05,
  "project_code": "AS102",
  "product_types": ["cube", "weight", "moment_map"],
  "released_only": true,
  "page": 1,
  "page_size": 20
}
```

### `casda_get_product`

Read-only complete supported ObsCore metadata for one exact `obs_publisher_did`.

```json
{"product_id": "cube-1170"}
```

The response retains raw archive identifiers, nulls, units in field names, spatial footprint,
spectral coverage converted to hertz, estimated byte size, SBID when encoded as `ASKAP-<sbid>`,
project code where the collection maps to `casda.project.short_name`, release state, and quality.

### `casda_get_observation`

Read-only ASKAP observation lookup with related projects and a bounded product list.

```json
{"scheduling_block_id": 2338}
```

### `casda_stage_products`

Creates and starts one archive-side asynchronous SODA/UWS request. It requires staging to be enabled
and OPAL credentials to be present. Empty requests are rejected, identifiers are normalised and
deduplicated, count and total estimated size are bounded, and missing sizes are rejected by default.

```json
{
  "product_ids": ["cube-1170", "cube-1171"],
  "idempotency_key": "wallaby-run-2026-07-12",
  "allow_duplicate": false
}
```

The output includes the archive request ID, effective idempotency key, confirmed phase, submission
time, and per-product state. A reused idempotency key with different products is an error. An active
request for the same product set is returned rather than duplicated unless the caller uses a new key
and explicitly sets `allow_duplicate`.

The non-idempotent archive creation and start requests are never automatically retried.

### `casda_get_staging_status`

Performs exactly one uncached UWS status read:

```json
{"request_id": "archive-job-id"}
```

It returns the overall archive phase, expiry, archive failure reason, per-product state, and whether
every product has a confirmed matching result URL. Active phases advise the caller to make another
tool call later; no background polling is claimed or scheduled.

### `casda_download_product`

Downloads one product only after a completed status read recorded a matching archive result URL:

```json
{
  "product_id": "cube-1170",
  "destination": "wallaby/cube-1170.fits",
  "verify_checksum": true
}
```

The result includes the confirmed local path, actual bytes, Content-Length verification, checksum
result, whether a Range retry resumed within this call, staging request ID, and provenance. A local
path is never returned before the final file exists. The server does not expose a deletion tool.

### `casda_create_manifest`

Creates and retains a schema-versioned JSON manifest in server state:

```json
{
  "product_ids": ["cube-1170", "catalogue-10"],
  "source_name": "WALLABY J2214-4600",
  "workflow_name": "spectral-line-analysis",
  "include_download_urls": false
}
```

The manifest includes a deterministic SHA-256 identifier, creation time, full typed product
metadata, filenames, estimated file sizes, available checksums, SBIDs, project codes, types, spatial
and spectral metadata, access state, known originating search criteria, provenance, and server
version. Archive artifact URLs are never persisted in manifests because opaque paths may be
short-lived bearer credentials even when they contain no query string.

## Resources

The server exposes read-only JSON resources:

- `casda://products/{product_id}`
- `casda://observations/{scheduling_block_id}`
- `casda://staging/{request_id}`
- `casda://manifests/{manifest_id}`
- `casda://server/status`

Resources do not expose credentials, raw local state files, unrestricted filesystem content, or URL
query strings. The staging resource performs one current status read, like the tool.

## Example workflows

### Search and inspect

1. Call `casda_search_products` with explicit bounded criteria.
2. Present the candidates and stable product identifiers to the researcher.
3. Call `casda_get_product` only for selected identifiers.
4. Explain `access_state` and `authorisation_state` without claiming access that CASDA has not
   confirmed.

### Search by WALLABY source

1. Resolve the source name to coordinates in the AI client or a separately trusted resolver.
2. Call `casda_search_products` with the explicit coordinates, radius, `project_code: "AS102"`, and
   required product types.
3. Inspect candidates, including SBID, collection, footprint, spectral range, and file size.
4. Call `casda_create_manifest` for the explicit selection.

The generic model can represent WALLABY identifiers present in `target_name`, project code, SBID,
footprint, cube/weight/catalogue/spectrum/moment-map subtypes, channels, spatial metadata, size, and
access state. No `wallaby_find_source_products` tool is included because a stable, complete source
selection rule has not been established. WALLABY-specific rules should remain a future adapter.

### Stage and download

1. Inspect the selected product and size.
2. Call `casda_stage_products` with explicit IDs and an idempotency key.
3. Later, call `casda_get_staging_status`; do not assume automatic polling.
4. Only after `ready_for_download` is true, call `casda_download_product`.
5. Check returned length and checksum fields.

### Reproducible workflow manifest

1. Search with explicit criteria.
2. Select identifiers.
3. Inspect full metadata.
4. Call `casda_create_manifest`.
5. Read the result later through `casda://manifests/{manifest_id}` when persistent state is enabled.

## Security model

- Tool input is untrusted and validated before query construction.
- TAP table names, selected columns, product-type clauses, sort fields, and operators are hard-coded
  allowlists.
- Text wildcards and control characters are rejected; identifiers use restrictive patterns.
- Cone, result, page, staging, manifest, and download sizes are bounded.
- Only configured HTTPS CASDA hosts and current CASDA-controlled Pawsey download hosts are allowed.
  Redirect destinations are revalidated before they are followed.
- Safe metadata reads may retry; staging creation/start never automatically retry.
- OPAL credentials use environment/secret injection and are excluded from logs, provenance, and
  exceptions.
- Structured logs go to stderr so stdio JSON-RPC is not corrupted.
- Cache keys include the complete generated query and bound; authentication failures are not cached.
- Staging status is never cached.
- Streamable HTTP binds to loopback by default and has no built-in client authentication. Put a
  production remote deployment behind TLS and an authenticating reverse proxy or MCP authorization
  layer. Do not expose it directly when staging, credentials, or downloads are enabled.
- `CASDA_STATE_DB` may contain short-lived signed URLs needed to resume status/download workflows.
  The server rejects symlink/non-file targets and forces owner-only file permissions on POSIX;
  deployments should additionally use an owner-controlled directory and encrypted storage.
  In-memory state is the default.

See [SECURITY.md](SECURITY.md) for the threat model and reporting guidance.

## Architecture

```text
MCP client
  -> typed FastMCP tools/resources
  -> CasdaService (validation, limits, idempotency, provenance)
  -> QueryBuilder / parsers / TTL cache / StateStore
  -> CasdaClient (pooled HTTP, retries, host validation, OPAL auth)
  -> CASDA TAP | Datalink | SODA/UWS | staged file endpoint
```

The modules are deliberately separated so CASDA protocol behavior does not depend on a particular AI
client. See [docs/architecture.md](docs/architecture.md) for component and sequence details.

## Testing and validation

Run the default offline suite:

```bash
uv run pytest -m "not live" --cov=casda_mcp --cov-report=term-missing
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv build
```

The tests cover validation, coordinates, cone limits, dates, frequencies, identifiers, safe ADQL,
pagination, CSV/VOTable/UWS parsing, redaction, caching, error mapping, idempotency, partial staging,
path traversal, overwrite prevention, streamed byte limits, checksum mismatch cleanup, Range resume,
manifest determinism, MCP schemas, resources, health, and mocked end-to-end HTTP behavior.

Optional live tests are metadata-only and disabled by default:

```bash
CASDA_RUN_LIVE_TESTS=true uv run pytest -m live -v
```

They use a small public cone search and never stage or download. CI does not require credentials.

## Container

Build and run the default read-only HTTP server:

```bash
docker build -t casda-mcp .
docker run --rm -p 127.0.0.1:8000:8000 casda-mcp
```

The image runs as a non-root user and checks `/healthz`. Mount a dedicated directory and inject
secrets only when explicitly enabling downloads or staging.

## Troubleshooting

- **No products:** remove filters deliberately, check the exact target/collection name, and keep the
  radius explicit. The server will not silently broaden the request.
- **`ARCHIVE_QUERY_ERROR`:** CASDA rejected the generated bounded query. Record the correlation and
  query IDs; no stack trace or credentials are exposed to the client.
- **`AUTHENTICATION_REQUIRED` / `AUTHENTICATION_FAILED`:** configure both OPAL variables and verify
  the account at the OPAL site. Metadata search itself does not require login.
- **`STAGING_DISABLED` / `DOWNLOADS_DISABLED`:** these are safe defaults, not archive failures.
- **`STAGING_REQUEST_NOT_FOUND`:** in-memory state was lost after restart or the ID came from another
  instance. Configure `CASDA_STATE_DB` before submission when restart persistence is required.
- **`PRODUCT_NOT_READY`:** run a current status check for the original request. The server will not
  infer readiness from elapsed time.
- **`UNSAFE_ARCHIVE_URL`:** CASDA returned a host outside the configured allowlist. Do not bypass this
  check without verifying a documented archive migration.
- **Repeated stale metadata:** reduce/disable the short cache or restart; staging status bypasses it.
- **HTTP works but remote access should not:** the default bind is loopback. Remote exposure requires
  an explicit host plus a secure front end.

## Known limitations

- Authenticated staging and file download behavior is protocol-tested with mocks, not live-validated
  in this repository run.
- ASKAP SBID product relationships use the confirmed ObsCore `obs_id = 'ASKAP-<sbid>'` convention.
- Project codes are joined where `ivoa.obscore.obs_collection` matches
  `casda.project.short_name`; CASDA does not expose a direct generic project foreign key in ObsCore.
- CASDA's current ADQL service does not support `CURRENT_TIMESTAMP`; public-only search retrieves the
  configured bounded window and removes future release dates locally.
- UWS reports an overall job phase. A product is marked individually ready only when a completed job
  returns a result URL matching its archive filename; otherwise its state remains `UNKNOWN`.
- Resumption is attempted within one download call after a transient read failure. Final failure
  removes the temporary file, so resumption does not persist across separate calls.
- Source-name resolution and row-level catalogue science queries are outside this server. No generic
  unrestricted ADQL tool is exposed.
- Beam identifiers may be retained in filenames or target metadata, but CASDA ObsCore does not expose
  a generic structured neighbouring-beam relationship used by this implementation.

## References

- [CASDA user guide](https://research.csiro.au/casda/casda-user-guide/)
- [Astroquery CASDA module](https://astroquery.readthedocs.io/en/latest/casda/casda.html)
- [CASDA VO Tools](https://github.com/csiro-rds/casda_vo_tools)
- [Model Context Protocol server guide](https://modelcontextprotocol.io/docs/develop/build-server)
