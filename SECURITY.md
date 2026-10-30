# Security Policy

## Supported Versions

This project is preparing for its first public release. There are currently no released versions with a support or security-fix commitment. Security fixes, when made, will target the current development branch; no response-time or backport commitment is offered before a versioned release.

## Reporting a Vulnerability

Please do not publish credentials, exploit details, or sensitive reproduction data in a public issue. If GitHub private vulnerability reporting is enabled for this repository, use the repository's **Security → Report a vulnerability** flow. Otherwise, contact a maintainer privately through the contact mechanism on that maintainer's GitHub profile. Do not assume that public issues or discussions are private.

Include the affected revision or version, impact, a concise reproduction, and any mitigations that may help. Avoid sending real secrets or personal data. We will acknowledge reports when practicable, but pre-release maintainers cannot promise a response or remediation timeframe.

## Scope Notes

The platform is intended for trusted, single-tenant deployments. Python extensions execute as trusted code; the platform is not a sandbox or a tenant-isolation boundary. Durable records and recovery do not make external side effects exactly-once. See the [threat model](docs/security/threat-model.md).
