# Changelog

Not yet released. This project is preparing for its first public release; no versioned release history or compatibility guarantees are available yet.

## Unreleased

### Fixed

- Failed and timed-out Runs retain their execution outcome after callback delivery.
- Concurrent idempotent submissions reuse the winning Run; concurrent conversation creation retries safely.
- Recovery preserves live leases, reclamation checks observed ownership, and lease loss stops local execution.
- Rejected Agent registration and default selection leave registry maps unchanged.
- Running migrations no longer disables existing application loggers.

### Security

- File downloads reject redirects; HTTP Tools enforce outbound destination policy.
- Private HTTP Tool destinations require `OUTBOUND__TOOL_PRIVATE_URL_ALLOWLIST`.
- URL policy rejects ambiguous traversal paths before allowlist checks.
- HTTP access logs omit payloads by default and preserve response streaming. Bounded request logging requires `LOGGING__LOG_PAYLOADS=true`.
- Local credential files are excluded from Git and container build contexts.

### Added

- Packaged configuration, Skills, and migration history with `init-config` and `migrate` commands.
- `serve --api-only` and separate migration/API/worker Compose services; optional Pi worker image target.
- CI checks, installed-wheel smoke coverage, private security-reporting guidance, and guarded local PostgreSQL test support.

The deployment boundary remains a single trusted tenant. DNS address pinning, tenant isolation, standard telemetry export, and automatic Run/stage retention are not implemented.
