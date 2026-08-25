"""Deterministic scanner for credential-shaped current-tree literals.

Output is deliberately limited to a rule identifier, path, line number, and
one-way fingerprint. Matched material is never retained in a finding or
rendered to stdout/stderr.

This current-tree control does not replace credential rotation or a separate
history scan after a confirmed exposure. Test fixtures may use only short or
plainly synthetic values. The synthetic allowance requires the entire value
to follow a small explicit marker grammar; a marker embedded in opaque data is
not an allowance.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import math
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


_TERMINAL_JOIN_RE = re.compile(r"\r?\x1b\[(?:1C|1B)")
_TEXTUAL_TERMINAL_JOIN_RE = re.compile(
    r"(?:\\r)?(?:\\x1[bB]|\\033|\\u001[bB]|\\u\{1[bB]\})\[(?:1C|1B)",
)
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_TEXTUAL_ANSI_RE = re.compile(
    r"(?:\\x1[bB]|\\033|\\u001[bB]|\\u\{1[bB]\})"
    r"\[[0-9;?]*[ -/]*[@-~]",
)
_OPAQUE_RUN_RE = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z0-9_+/=.-]{64,}(?![A-Za-z0-9])",
)
_SENSITIVE_CONTEXT_RE = re.compile(
    r"(?i)(?:"
    r"api[_-]?key|access[_-]?token|auth[_-]?token|oauth[_-]?token|"
    r"refresh[_-]?token|client[_-]?secret|webhook[_-]?secret|"
    r"password|passwd|credential|authorization|bearer|private[_-]?key|"
    r"\btoken\b|\bsecret\b"
    r")",
)
_NON_SECRET_CONTEXT_RE = re.compile(
    r"(?i)(?:"
    r"public[_-]?key|key[_-]?id|sha(?:256|384|512)|digest|fingerprint|"
    r"checksum|signature|immutable[_-]?locator|commit|tree"
    r")",
)
_PEM_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:ENCRYPTED |RSA |EC |OPENSSH )?PRIVATE KEY-----",
)

# Provider rules use realistic credential-capable payload lengths. Short test
# examples remain available, while setup tokens and provider keys with enough
# material to be live are rejected.
_PROVIDER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "provider.anthropic",
        re.compile(
            r"(?<![A-Za-z0-9])sk-ant-(?:api|oat)\d*-[A-Za-z0-9_-]{64,}",
        ),
    ),
    (
        "provider.openai",
        re.compile(
            r"(?<![A-Za-z0-9])sk-(?!ant-)(?:proj-|svcacct-)?"
            r"[A-Za-z0-9_-]{32,}",
        ),
    ),
    (
        "provider.github",
        re.compile(
            r"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{30,}|"
            r"github_pat_[A-Za-z0-9_]{30,})",
        ),
    ),
    (
        "provider.stripe",
        re.compile(
            r"(?<![A-Za-z0-9])(?:(?:sk|rk)_(?:live|test)_"
            r"[A-Za-z0-9]{16,}|whsec_[A-Za-z0-9]{16,})",
        ),
    ),
    (
        "provider.slack",
        re.compile(r"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{20,}"),
    ),
    (
        "provider.google",
        re.compile(r"(?<![A-Za-z0-9])AIza[A-Za-z0-9_-]{30,}"),
    ),
    (
        "provider.aws-access-key",
        re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"),
    ),
    (
        "provider.npm",
        re.compile(r"(?<![A-Za-z0-9])npm_[A-Za-z0-9]{30,}"),
    ),
    (
        "provider.huggingface",
        re.compile(r"(?<![A-Za-z0-9])hf_[A-Za-z0-9]{30,}"),
    ),
    (
        "provider.sendgrid",
        re.compile(
            r"(?<![A-Za-z0-9])SG\.[A-Za-z0-9_-]{16,}\."
            r"[A-Za-z0-9_-]{20,}",
        ),
    ),
)

_SYNTHETIC_MARKERS = ("synthetic", "example", "dummy", "fake", "test")
_SYNTHETIC_WHOLE_VALUE_RE = re.compile(
    r"(?i)^(?:(?:sk-ant-(?:api|oat)\d*-)|(?:sk-(?:proj-|svcacct-)?)|"
    r"(?:gh[pousr]_|github_pat_)|(?:(?:sk|rk)_(?:live|test)_|whsec_)|"
    r"(?:xox[baprs]-)|AIza|npm_|hf_|SG\.)?"
    r"(?:synthetic|example|dummy|fake|test)"
    r"(?:[-_.](?:synthetic|example|dummy|fake|test|token|value|payload|"
    r"segment|fixture|preview|[0-9]{1,3}))*[-_.]*$",
)


@dataclass(frozen=True, order=True)
class SecretFinding:
    """Safe-to-render finding that cannot store matched material."""

    path: str
    line: int
    rule_id: str
    fingerprint: str

    def masked(self) -> str:
        return (
            f"{self.path}:{self.line}: {self.rule_id} "
            f"fingerprint=sha256:{self.fingerprint}"
        )


def _fingerprint(value: str) -> str:
    return hashlib.sha256(
        value.encode("utf-8", errors="surrogatepass"),
    ).hexdigest()[:16]


def _normalise_terminal_noise(value: str) -> str:
    value = _TERMINAL_JOIN_RE.sub("", value)
    value = _TEXTUAL_TERMINAL_JOIN_RE.sub("", value)
    value = _ANSI_OSC_RE.sub("", value)
    value = _ANSI_CSI_RE.sub("", value)
    value = _TEXTUAL_ANSI_RE.sub("", value)
    # Ordinary line boundaries remain separators. Only explicit cursor-right
    # and single-line-wrap controls may join token bytes.
    return _CONTROL_RE.sub(" ", value)


def _shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = Counter(value)
    length = len(value)
    return -sum(
        (count / length) * math.log2(count / length)
        for count in counts.values()
    )


def _looks_unmistakably_synthetic(value: str) -> bool:
    lower = value.casefold()
    if not any(marker in lower for marker in _SYNTHETIC_MARKERS):
        return False
    return _SYNTHETIC_WHOLE_VALUE_RE.fullmatch(value) is not None


def _looks_like_synthetic_pem(value: str) -> bool:
    collapsed = " ".join(value.replace(r"\n", " ").split())
    return re.search(
        r"(?i)-----BEGIN (?:ENCRYPTED |RSA |EC |OPENSSH )?PRIVATE KEY----- "
        r"(?:synthetic|example|dummy|fake|test)"
        r"(?:[-_ ]?(?:private|key|value|fixture|only))* "
        r"-----END (?:ENCRYPTED |RSA |EC |OPENSSH )?PRIVATE KEY-----",
        collapsed,
    ) is not None


def _looks_like_opaque_secret(value: str, context: str) -> bool:
    if len(value) < 64 or _looks_unmistakably_synthetic(value):
        return False
    if not _SENSITIVE_CONTEXT_RE.search(context):
        return False
    if _NON_SECRET_CONTEXT_RE.search(context):
        return False
    if value.count("/") >= 2 or value.count(".") >= 2:
        return False
    has_mixed_alphanumeric = all(
        pattern.search(value)
        for pattern in (
            re.compile(r"[a-z]"),
            re.compile(r"[A-Z]"),
            re.compile(r"[0-9]"),
        )
    )
    return bool(has_mixed_alphanumeric) and _shannon_entropy(value) >= 3.7


def _make_finding(
    path: str,
    line: int,
    rule_id: str,
    value: str,
) -> SecretFinding:
    return SecretFinding(
        path=path,
        line=max(1, line),
        rule_id=rule_id,
        fingerprint=_fingerprint(value),
    )


def _scan_value(
    *,
    path: str,
    line: int,
    value: str,
    context: str,
) -> set[SecretFinding]:
    findings: set[SecretFinding] = set()
    normalised = _normalise_terminal_noise(value)

    if (
        _PEM_PRIVATE_KEY_RE.search(normalised)
        and not _looks_like_synthetic_pem(normalised)
    ):
        findings.add(_make_finding(path, line, "private-key.pem", normalised))

    for rule_id, pattern in _PROVIDER_PATTERNS:
        for match in pattern.finditer(normalised):
            candidate = match.group(0)
            if not _looks_unmistakably_synthetic(candidate):
                findings.add(_make_finding(path, line, rule_id, candidate))

    for match in _OPAQUE_RUN_RE.finditer(normalised):
        candidate = match.group(0)
        if _looks_like_opaque_secret(candidate, context):
            findings.add(
                _make_finding(
                    path,
                    line,
                    "generic.high-entropy-secret",
                    candidate,
                ),
            )

    return findings


def _python_literal_findings(path: str, text: str) -> set[SecretFinding]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return set()

    source_lines = text.splitlines()
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    def identifier_context(node: ast.AST) -> str:
        identifiers: list[str] = []

        def add_identifiers(value: ast.AST) -> None:
            for child in ast.walk(value):
                if isinstance(child, ast.Name):
                    identifiers.append(child.id)
                elif isinstance(child, ast.Attribute):
                    identifiers.append(child.attr)

        current = node
        while current in parents:
            parent = parents[current]
            if isinstance(parent, ast.Assign):
                for target in parent.targets:
                    add_identifiers(target)
            elif isinstance(parent, ast.AnnAssign):
                add_identifiers(parent.target)
            elif isinstance(parent, ast.NamedExpr):
                add_identifiers(parent.target)
            elif isinstance(parent, ast.keyword) and parent.arg:
                identifiers.append(parent.arg)
            elif isinstance(parent, ast.Call):
                add_identifiers(parent.func)
            current = parent
        return " ".join(identifiers)

    findings: set[SecretFinding] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        source_context = (
            source_lines[node.lineno - 1]
            if 0 < node.lineno <= len(source_lines)
            else ""
        )
        context = f"{source_context} {identifier_context(node)}"
        findings.update(
            _scan_value(
                path=path,
                line=node.lineno,
                value=node.value,
                context=context,
            ),
        )
    return findings


def scan_text(path: str, text: str) -> tuple[SecretFinding, ...]:
    """Return safe, deterministically ordered findings for decoded text."""

    findings: set[SecretFinding] = set()
    for line_number, source_line in enumerate(text.splitlines(), start=1):
        findings.update(
            _scan_value(
                path=path,
                line=line_number,
                value=source_line,
                context=source_line,
            ),
        )
    if Path(path).suffix == ".py":
        findings.update(_python_literal_findings(path, text))
    return tuple(sorted(findings))


def _repository_paths(root: Path) -> tuple[Path, ...]:
    completed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"],
        check=True,
        capture_output=True,
    )
    relative_paths = sorted(
        (
            Path(item.decode("utf-8", errors="surrogateescape"))
            for item in completed.stdout.split(b"\0")
            if item
        ),
        key=lambda path: path.as_posix(),
    )
    return tuple(relative_paths)


def scan_repository(root: Path) -> tuple[int, tuple[SecretFinding, ...]]:
    """Scan tracked and non-ignored current-tree text files under ``root``."""

    root = root.resolve()
    findings: set[SecretFinding] = set()
    scanned = 0
    for relative_path in _repository_paths(root):
        absolute_path = root / relative_path
        if not absolute_path.is_file() or absolute_path.is_symlink():
            continue
        try:
            raw = absolute_path.read_bytes()
        except OSError:
            continue
        if b"\0" in raw:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        scanned += 1
        findings.update(scan_text(relative_path.as_posix(), text))
    return scanned, tuple(sorted(findings))


def _render_result(
    scanned: int,
    findings: Iterable[SecretFinding],
) -> int:
    ordered = tuple(sorted(findings))
    if not ordered:
        print(f"repository-secret-scan: PASS files={scanned} findings=0")
        return 0
    path_count = len({finding.path for finding in ordered})
    print(
        "repository-secret-scan: FAIL "
        f"files={scanned} paths={path_count} findings={len(ordered)}",
    )
    for finding in ordered:
        print(finding.masked())
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan current repository files for credentials",
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        scanned, findings = scan_repository(args.root)
    except (OSError, subprocess.CalledProcessError):
        print(
            "repository-secret-scan: ERROR unable-to-enumerate-repository",
            file=sys.stderr,
        )
        return 2
    return _render_result(scanned, findings)


if __name__ == "__main__":
    raise SystemExit(main())
