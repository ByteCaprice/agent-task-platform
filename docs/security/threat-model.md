# Security Model

Agent Task Platform provides boundaries that reduce common risks in agent execution. It does not make arbitrary plugins, model prompts, or external systems safe by default.

## Built-in Boundaries

- Scoped API keys protect Run, registry, and operations APIs.
- Callback payloads can be signed with HMAC and restricted by URL allowlist.
- Outbound URL policy rejects unsafe schemes and private-network destinations for file fetches and HTTP Tools. HTTP Tool private destinations require an operator-configured `outbound.tool_private_url_allowlist` exact URL/prefix entry; this is trusted deployment configuration, not caller-controlled.
- File fetches reject redirects. Tool HTTP requests do not follow redirects by default.
- HTTP access logs contain request/response metadata only by default. `logging.log_payloads=true` opts in to bounded request-body logging; response streams are never buffered for logging.
- Tool Gateway validates schemas, permissions, limits, timeouts, retries, and side-effect behavior.
- Skill snapshots make the instructions and artifacts used by a Run auditable.
- The Pi worker receives only declared platform Tools and Run-scoped Skills through a bounded local protocol; it does not receive coding-agent filesystem or shell Tools.
- Logs redact common secret fields and remove URL query strings.
- Errors returned to callers avoid stack traces and readiness responses avoid database exception details.

## Trust Assumptions

- API keys with the same scope share deployment resources; Runs, traces, and callbacks are not isolated by key owner. Do not issue these keys to mutually untrusted consumers.
- The submission's `caller` is a client-supplied routing/idempotency/scheduling label, not an authenticated identity or an authorization boundary. Per-key rate limiting is separate from per-caller scheduling.
- A Python Agent, Tool, or Skill script is trusted code. Registration grants execution capability.
- The task-scoped Pi Node process and its locked npm dependencies are trusted runtime code, not a security sandbox. It receives the selected model credential and can reach the configured model endpoint.
- HTTP and MCP endpoints are external dependencies. Restrict destinations and authenticate them independently. `outbound.tool_private_url_allowlist` should contain only explicitly trusted internal HTTP Tool destinations.
- DNS validation occurs before the HTTP client resolves/connects, but the platform does not pin the validated address to the connection. DNS rebinding/time-of-check-to-time-of-use attacks are not solved; use network egress controls as a second boundary.
- A model may produce untrusted output. Do not map model output directly to privileged side effects without schema validation and application controls.
- Callback endpoints must verify the signature and tolerate at-least-once delivery.

## Operator Responsibilities

- Keep environment files, API keys, callback secrets, and model credentials out of Git.
- Use a secret scanner on the working tree, staged changes, and release artifacts.
- Review plugin code and registry changes before deployment.
- Run with least-privilege API keys and network access. The platform assumes a single trusted operator/tenant boundary: trusted registry entries and endpoint allowlists are not isolated per tenant. This release does not implement multi-tenant security.
- Restrict Pi worker network access to required model endpoints and approved proxies, and install npm dependencies from a controlled registry or build environment.
- Keep dependencies current and monitor security advisories.

Report security vulnerabilities using the private options in [SECURITY.md](../../SECURITY.md). Do not include sensitive reproduction data in public issues. This project is pre-release and makes no response-time or remediation commitment.
