# Security policy

## Supported version

Security fixes are applied to the current `0.1.x` development line.

## Reporting a vulnerability

Do not open a public issue containing credentials, signed CASDA URLs, private product metadata, or a
working exploit. Report the issue privately to the repository maintainers with:

- affected version and deployment mode;
- the smallest safe reproduction;
- expected and observed security boundary;
- whether any OPAL credential, signed URL, or local file may have been exposed.

Rotate affected OPAL credentials and remove exposed persisted state immediately. CASDA archive or
account incidents should also be reported through the official CASDA/ATNF support channel.

## Threat model

The MCP caller, all tool arguments, CASDA response metadata, redirects, filenames, VOTables, UWS XML,
checksums, and HTTP headers are treated as untrusted. Principal controls include:

- no caller-selected endpoint, arbitrary URL, ADQL, shell command, or unrestricted filesystem tool;
- fixed query templates and allowlisted clauses;
- HTTPS and archive-host validation on initial requests and every redirect;
- strict result, cone, staging, manifest, decoded metadata-response, checksum, and download limits;
- staging/download feature gates disabled by default;
- dedicated non-root download containment, target reservations, no-clobber publication by default,
  descriptor-bound temporary writes, validator-aware raw-byte resumption, temporary-file cleanup,
  and length/checksum validation;
- OPAL secret redaction and stderr-only structured logging;
- origin-scoped OPAL authentication that is stripped from cross-origin redirects;
- owner-only permissions for the optional SQLite file containing signed URLs;
- structured client errors without stack traces;
- loopback HTTP binding by default.

The server does not authenticate MCP clients itself. A remotely reachable HTTP deployment must use a
trusted TLS/authentication layer and must not forward untrusted users to a server holding OPAL
credentials or write access without an explicit authorization design.

Filesystem isolation also depends on OS account separation: an untrusted process running as the
same account as the MCP server can manipulate that account's paths despite mode checks. Keep the
canonical download directory and its ancestors outside locations writable by untrusted same-account
processes. On POSIX, existing download directories are rejected when another account owns them,
group/world write access is enabled, an ancestor is not owned by root/the server account, or a
writable ancestor lacks the sticky bit. Windows download
support relies on an operator-enforced ACL-isolated directory and server account; POSIX mode checks
do not provide a Windows ACL guarantee.
