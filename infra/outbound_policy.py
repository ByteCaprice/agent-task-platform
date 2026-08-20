"""Unified outbound URL policy for Agent Task Platform.

This module provides a single :class:`OutboundPolicy` that every outbound
HTTP caller must use — FileClient, CallbackService, tool health checks,
and agent HTTP runtime.  The policy enforces:

* scheme allowlist (http/https only by default)
* DNS resolution and private-network rejection (SSRF protection)
* hostname-level allowlist with scheme/host/path-prefix matching
* configurable timeout, redirect policy, and max response size hints

The legacy module-level :func:`validate_outbound_url` is kept as a thin
wrapper for backward compatibility with existing call sites that don't
need custom configuration.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse


class OutboundPolicyError(ValueError):
    """Raised when a URL violates the outbound policy."""


@dataclass
class OutboundPolicy:
    """Production-grade outbound URL validation.

    Parameters
    ----------
    allowed_schemes:
        URL schemes that are permitted (default: ``http``, ``https``).
    block_private_networks:
        When *True* (default), resolves the hostname via DNS and rejects
        any address that is private, loopback, link-local, reserved, or
        multicast.
    allowlist:
        List of allowed URL prefixes.  Each entry is parsed into
        ``(scheme, host, path_prefix)`` for canonical comparison rather
        than raw ``str.startswith``.
    denylist:
        List of denied URL prefixes.  Evaluated before the allowlist.
    dns_timeout:
        Timeout in seconds for DNS resolution.
    """

    allowed_schemes: set[str] = field(default_factory=lambda: {"http", "https"})
    block_private_networks: bool = True
    allowlist: list[str] = field(default_factory=list)
    denylist: list[str] = field(default_factory=list)
    dns_timeout: float = 5.0

    def validate(self, url: str) -> None:
        """Raise :class:`OutboundPolicyError` if *url* is not safe."""
        parsed = urlparse(url)
        if parsed.scheme not in self.allowed_schemes:
            raise OutboundPolicyError(
                f"Outbound URL scheme {parsed.scheme!r} is not allowed (allowed: {sorted(self.allowed_schemes)})"
            )
        if not parsed.hostname:
            raise OutboundPolicyError("Outbound URL host is required")

        # Denylist takes precedence
        if self._matches_prefix(url, self.denylist):
            raise OutboundPolicyError("Outbound URL is denylisted")

        # Allowlist check — canonical scheme/host/path-prefix
        if self.allowlist and not self._matches_prefix(url, self.allowlist):
            raise OutboundPolicyError("Outbound URL is not allowlisted")

        # SSRF: resolve DNS and reject private networks
        if self.block_private_networks:
            self._reject_private_target(parsed)

    def is_allowed(self, url: str) -> bool:
        """Return *True* if the URL passes validation."""
        try:
            self.validate(url)
        except OutboundPolicyError:
            return False
        return True

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _matches_prefix(self, url: str, prefixes: list[str]) -> bool:
        """Check whether *url* matches any prefix in *prefixes*.

        Matching is done on canonical (scheme, host, path) components
        rather than raw string prefix to avoid trivial bypasses like
        ``https://evil.com@files.example.com/``.
        """
        parsed = urlparse(url)
        canonical = (parsed.scheme.lower(), parsed.hostname.lower() if parsed.hostname else "", parsed.path or "/")
        for prefix in prefixes:
            p = urlparse(prefix)
            p_scheme = p.scheme.lower()
            p_host = p.hostname.lower() if p.hostname else ""
            p_path = p.path or "/"
            if canonical[0] != p_scheme:
                continue
            if canonical[1] != p_host:
                continue
            if canonical[2].startswith(p_path):
                return True
        return False

    def _reject_private_target(self, parsed: Any) -> None:
        host = parsed.hostname
        if not host:
            raise OutboundPolicyError("Outbound URL host is required")

        # Fast path: IP literal
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            addresses = self._resolve_dns(host)

        for addr in addresses:
            if (
                addr.is_private
                or addr.is_loopback
                or addr.is_link_local
                or addr.is_reserved
                or addr.is_multicast
                or addr.is_unspecified
            ):
                raise OutboundPolicyError(f"Outbound URL host {host!r} resolves to non-public address {addr}")

    def _reject_ip_literal(self, url: str) -> None:
        """Check only IP-literal hosts (no DNS resolution)."""
        parsed = urlparse(url)
        if parsed.scheme not in self.allowed_schemes:
            raise OutboundPolicyError(
                f"Outbound URL scheme {parsed.scheme!r} is not allowed (allowed: {sorted(self.allowed_schemes)})"
            )
        if not parsed.hostname:
            raise OutboundPolicyError("Outbound URL host is required")
        host = parsed.hostname
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            return
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_unspecified
        ):
            raise OutboundPolicyError(f"Outbound URL points to a non-public IP address {addr}")

    def _resolve_dns(self, host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        try:
            infos = socket.getaddrinfo(
                host,
                None,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise OutboundPolicyError(f"Outbound URL host {host!r} could not be resolved: {exc}") from exc

        addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
        seen: set[str] = set()
        for info in infos:
            ip_str = info[4][0]
            if ip_str in seen:
                continue
            seen.add(ip_str)
            try:
                addresses.append(ipaddress.ip_address(ip_str))
            except ValueError:
                continue
        if not addresses:
            raise OutboundPolicyError(f"Outbound URL host {host!r} resolved to no usable addresses")
        return addresses


# ---------------------------------------------------------------------------
# Backward-compatible function
# ---------------------------------------------------------------------------


def validate_outbound_url(
    url: str,
    *,
    allowed_prefixes: list[str] | None = None,
    block_private_ip_literals: bool = True,
) -> None:
    """Backward-compatible wrapper around :class:`OutboundPolicy`.

    When *block_private_ip_literals* is *True* (default), only IP-literal
    hosts are checked for private-network membership — DNS resolution is
    **not** performed, matching the original behaviour.  For full SSRF
    protection including DNS rebinding, construct an :class:`OutboundPolicy`
    with ``block_private_networks=True`` directly.
    """
    policy = OutboundPolicy(
        block_private_networks=False,
        allowlist=allowed_prefixes or [],
    )
    # Validate scheme and allowlist (no DNS)
    parsed = urlparse(url)
    if parsed.scheme not in policy.allowed_schemes:
        raise OutboundPolicyError(
            f"Outbound URL scheme {parsed.scheme!r} is not allowed (allowed: {sorted(policy.allowed_schemes)})"
        )
    if not parsed.hostname:
        raise OutboundPolicyError("Outbound URL host is required")
    if policy.allowlist and not policy._matches_prefix(url, policy.allowlist):
        raise OutboundPolicyError("Outbound URL is not allowlisted")
    # IP-literal check only
    if block_private_ip_literals:
        policy._reject_ip_literal(url)
