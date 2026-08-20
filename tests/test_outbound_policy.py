from __future__ import annotations

import pytest

from infra.outbound_policy import OutboundPolicy, OutboundPolicyError, validate_outbound_url


def test_outbound_policy_allows_http_and_https_public_urls() -> None:
    validate_outbound_url("https://files.example.com/a.png")
    validate_outbound_url("http://203.0.113.10/a.png", block_private_ip_literals=False)


def test_outbound_policy_rejects_invalid_scheme_missing_host_and_private_literals() -> None:
    invalid_urls = [
        "ftp://files.example.com/a.png",
        "https:///a.png",
        "http://127.0.0.1/a.png",
        "http://10.0.0.1/a.png",
        "http://172.16.0.1/a.png",
        "http://192.168.1.1/a.png",
        "http://169.254.1.1/a.png",
    ]

    for url in invalid_urls:
        with pytest.raises(OutboundPolicyError):
            validate_outbound_url(url)


def test_outbound_policy_enforces_allowlist_prefix() -> None:
    validate_outbound_url(
        "https://files.example.com/allowed/a.png",
        allowed_prefixes=["https://files.example.com/allowed/"],
    )

    with pytest.raises(OutboundPolicyError, match="allowlisted"):
        validate_outbound_url(
            "https://files.example.com/blocked/a.png",
            allowed_prefixes=["https://files.example.com/allowed/"],
        )


# ---------------------------------------------------------------------------
# OutboundPolicy class tests
# ---------------------------------------------------------------------------


def test_policy_class_rejects_private_ip_literals() -> None:
    policy = OutboundPolicy(block_private_networks=True)
    for url in [
        "http://127.0.0.1/x",
        "http://10.0.0.1/x",
        "http://192.168.1.1/x",
        "http://169.254.1.1/x",
        "http://172.16.0.1/x",
    ]:
        with pytest.raises(OutboundPolicyError, match="non-public"):
            policy.validate(url)


def test_policy_class_rejects_invalid_scheme() -> None:
    policy = OutboundPolicy(block_private_networks=False)
    with pytest.raises(OutboundPolicyError, match="scheme"):
        policy.validate("ftp://example.com/x")


def test_policy_class_rejects_missing_host() -> None:
    policy = OutboundPolicy(block_private_networks=False)
    with pytest.raises(OutboundPolicyError, match="host"):
        policy.validate("https:///path")


def test_policy_class_allowlist_canonical_matching() -> None:
    policy = OutboundPolicy(
        allowlist=["https://files.example.com/allowed/"],
        block_private_networks=False,
    )
    policy.validate("https://files.example.com/allowed/a.png")

    with pytest.raises(OutboundPolicyError, match="allowlisted"):
        policy.validate("https://files.example.com/blocked/a.png")

    with pytest.raises(OutboundPolicyError, match="allowlisted"):
        policy.validate("https://evil.com/allowed/a.png")


def test_policy_class_allowlist_userinfo_does_not_bypass() -> None:
    """Userinfo in URL should not bypass hostname-based allowlist.

    https://evil.com@files.example.com/ has hostname files.example.com,
    so it matches the allowlist — the HTTP request goes to files.example.com.
    """
    policy = OutboundPolicy(
        allowlist=["https://files.example.com/"],
        block_private_networks=False,
    )
    policy.validate("https://evil.com@files.example.com/allowed/a.png")

    with pytest.raises(OutboundPolicyError, match="allowlisted"):
        policy.validate("https://files.example.com@evil.com/allowed/a.png")


def test_policy_class_denylist_takes_precedence() -> None:
    policy = OutboundPolicy(
        allowlist=["https://files.example.com/"],
        denylist=["https://files.example.com/blocked/"],
        block_private_networks=False,
    )
    with pytest.raises(OutboundPolicyError, match="denylisted"):
        policy.validate("https://files.example.com/blocked/a.png")


def test_policy_class_is_allowed_helper() -> None:
    policy = OutboundPolicy(block_private_networks=False)
    assert policy.is_allowed("https://files.example.com/a.png")
    assert not policy.is_allowed("ftp://files.example.com/a.png")


def test_policy_class_custom_schemes() -> None:
    policy = OutboundPolicy(allowed_schemes={"http", "https", "ftp"}, block_private_networks=False)
    policy.validate("ftp://files.example.com/a.png")
