from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from repository_secret_scan import SecretFinding, scan_repository, scan_text


def _opaque_payload(seed: str, length: int = 72) -> str:
    digest = hashlib.sha512(seed.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")[:length]


def _alphanumeric_payload(seed: str, length: int = 72) -> str:
    return hashlib.sha512(seed.encode("utf-8")).hexdigest()[:length]


def _provider_prefix(*parts: str) -> str:
    return "".join(parts)


def test_detects_anthropic_token_reconstructed_across_ansi_controls():
    prefix = _provider_prefix("sk", "-ant-", "oat1-")
    candidate = prefix + _opaque_payload("ansi-reconstruction-fixture")
    first_split = len(prefix) + 11
    second_split = first_split + 19
    source = (
        'captured = "'
        + candidate[:first_split]
        + "\\x1b[1C"
        + candidate[first_split:second_split]
        + "\\r\\x1b[1B"
        + candidate[second_split:]
        + '"\n'
    )

    findings = scan_text("fixture.py", source)

    assert {finding.rule_id for finding in findings} == {"provider.anthropic"}


def test_detects_common_provider_prefixes():
    cases = (
        (_provider_prefix("sk", "-proj-"), "provider.openai"),
        (_provider_prefix("gh", "p_"), "provider.github"),
        (_provider_prefix("sk", "_live_"), "provider.stripe"),
        (_provider_prefix("xo", "xb-"), "provider.slack"),
        (_provider_prefix("AI", "za"), "provider.google"),
        (_provider_prefix("np", "m_"), "provider.npm"),
        (_provider_prefix("h", "f_"), "provider.huggingface"),
    )
    for index, (prefix, expected_rule) in enumerate(cases):
        candidate = prefix + _alphanumeric_payload(f"provider-{index}")
        findings = scan_text("fixture.txt", f"credential={candidate}\n")
        assert expected_rule in {finding.rule_id for finding in findings}


def test_detects_unprefixed_high_entropy_literal_in_secret_context():
    candidate = _opaque_payload("generic-sensitive-fixture")
    findings = scan_text("fixture.env", f"ACCESS_TOKEN={candidate}\n")
    assert "generic.high-entropy-secret" in {
        finding.rule_id for finding in findings
    }


def test_detects_multiline_python_secret_assignment_context():
    candidate = _opaque_payload("multiline-sensitive-fixture")
    source = f'ACCESS_TOKEN = (\n    "{candidate}"\n)\n'
    findings = scan_text("fixture.py", source)
    assert "generic.high-entropy-secret" in {
        finding.rule_id for finding in findings
    }


def test_plainly_synthetic_fixture_is_allowed():
    candidate = _provider_prefix("sk", "-ant-", "oat1-")
    candidate += "synthetic-test-token"
    assert scan_text("fixture.py", f'token = "{candidate}"\n') == ()


def test_synthetic_marker_does_not_allow_an_opaque_provider_value():
    prefix = _provider_prefix("sk", "-ant-", "oat1-")
    candidate = prefix + _opaque_payload("opaque-before-marker") + "-test"
    findings = scan_text("fixture.py", f'token = "{candidate}"\n')
    assert "provider.anthropic" in {finding.rule_id for finding in findings}


def test_masked_finding_never_renders_matched_material():
    candidate = _opaque_payload("masking-fixture")
    findings = scan_text("fixture.env", f"CLIENT_SECRET={candidate}\n")
    assert findings
    assert all(candidate not in finding.masked() for finding in findings)
    assert all("fingerprint=sha256:" in finding.masked() for finding in findings)


def test_finding_shape_cannot_store_matched_value():
    assert set(SecretFinding.__dataclass_fields__) == {
        "path",
        "line",
        "rule_id",
        "fingerprint",
    }


def test_current_repository_has_no_secret_shaped_literals():
    repository_root = Path(__file__).resolve().parents[2]
    scanned, findings = scan_repository(repository_root)
    assert scanned > 0
    assert findings == ()
