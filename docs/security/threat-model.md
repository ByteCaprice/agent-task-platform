# Security Model

Agent Task Platform provides boundaries that reduce common risks in agent execution. It does not make arbitrary plugins, model prompts, or external systems safe by default.

## Built-in Boundaries

- Scoped API keys protect Run, registry, and operations APIs.
- Callback payloads can be signed with HMAC and restricted by URL allowlist.
- Outbound URL policy rejects unsafe schemes and private-network destinations.
- Tool Gateway validates schemas, permissions, limits, timeouts, retries, and side-effect behavior.
- Skill snapshots make the instructions and artifacts used by a Run auditable.
- Logs redact common secret fields and remove URL query strings.
- Errors returned to callers avoid stack traces and readiness responses avoid database exception details.

## Trust Assumptions

- A Python Agent, Tool, or Skill script is trusted code. Registration grants execution capability.
- HTTP and MCP endpoints are external dependencies. Restrict destinations and authenticate them independently.
- A model may produce untrusted output. Do not map model output directly to privileged side effects without schema validation and application controls.
- Callback endpoints must verify the signature and tolerate at-least-once delivery.

## Operator Responsibilities

- Keep environment files, API keys, callback secrets, and model credentials out of Git.
- Use a secret scanner on the working tree, staged changes, and release artifacts.
- Review plugin code and registry changes before deployment.
- Run with least-privilege API keys and network access.
- Keep dependencies current and monitor security advisories.

Report security vulnerabilities privately once the project publishes a security policy. Do not include sensitive reproduction data in public issues.
