"""Fail closed when a selected public-source file contains unsafe content.

The release-source policy decides which files may be public.  This verifier
adds content checks for a small, explicit set of accidental secret and local
build-path indicators.  It reports only a rule name and relative path, never a
matched value.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# This script runs before the source tree is qualified.  Its import of the
# manifest helper must not create an excluded ``__pycache__`` entry in P.
sys.dont_write_bytecode = True

if __package__:
    from .release_source_manifest import (
        ReleaseSourceError,
        ReleaseSourceManifest,
        ReleaseSourcePolicy,
        build_public_checkout_manifest,
        load_policy,
    )
else:  # pragma: no cover - exercised by direct CLI invocation.
    from release_source_manifest import (  # type: ignore[no-redef]
        ReleaseSourceError,
        ReleaseSourceManifest,
        ReleaseSourcePolicy,
        build_public_checkout_manifest,
        load_policy,
    )


@dataclass(frozen=True)
class ForbiddenContentFinding:
    """One content- or path-policy violation in a selected public file."""

    path: str
    rule: str
    redact_path: bool = False

    @property
    def display_path(self) -> str:
        """Return a safe location label that cannot disclose a forbidden filename."""
        return "<redacted path>" if self.redact_path else self.path


class ForbiddenContentError(ReleaseSourceError):
    """Raised when a selected public file contains forbidden content."""

    def __init__(self, findings: Sequence[ForbiddenContentFinding]) -> None:
        """Summarize violations without including any matched content."""
        self.findings = tuple(
            sorted(findings, key=lambda item: (item.path, item.rule))
        )
        lines = ["forbidden public-source content:"]
        lines.extend(f"- {item.display_path}: {item.rule}" for item in self.findings)
        super().__init__("\n".join(lines))


class PublicSourceContentChanged(ReleaseSourceError):
    """Raised when a selected file changes while its content is being scanned."""


_LOCAL_BUILD_PREFIX = b"/home/" + b"washimi/"
_WINDOWS_BUILD_PREFIX = b"C:\\Users\\" + b"washimi"
_FORBIDDEN_CONTENT = (
    ("aws-access-key", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9_]{20,}\b")),
    ("github-fine-grained-token", re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("local-build-path", re.compile(re.escape(_LOCAL_BUILD_PREFIX))),
    ("local-build-path", re.compile(re.escape(_WINDOWS_BUILD_PREFIX))),
    ("pem-private-key", re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)


def scan_public_checkout(
    root: Path,
    policy: ReleaseSourcePolicy,
) -> ReleaseSourceManifest:
    """Verify content in the selected files of public checkout ``root``.

    Building the direct ``M(P)`` manifest first makes the scan inherit the
    policy, symlink, special-file, and portable-mode checks used for source
    identity.  A digest comparison rejects a file changed after that check.
    """
    manifest = build_public_checkout_manifest(root, policy)
    findings: list[ForbiddenContentFinding] = []
    for entry in manifest.entries:
        if entry.file_type != "file":
            continue
        path_rules = _matching_rules(
            entry.path.encode("utf-8", errors="surrogateescape")
        )
        findings.extend(
            ForbiddenContentFinding(entry.path, rule, redact_path=True)
            for rule in path_rules
        )
        try:
            content = (root / entry.path).read_bytes()
        except OSError as error:
            raise PublicSourceContentChanged(
                "selected public file became unreadable during content scan"
            ) from error
        if hashlib.sha256(content).hexdigest() != entry.sha256:
            raise PublicSourceContentChanged(
                "selected public file changed during content scan"
            )
        findings.extend(
            ForbiddenContentFinding(entry.path, rule, redact_path=bool(path_rules))
            for rule in _matching_rules(content)
        )
    if findings:
        raise ForbiddenContentError(findings)
    return manifest


def _matching_rules(value: bytes) -> tuple[str, ...]:
    """Return all forbidden-content rule names that match ``value``."""
    return tuple(
        rule
        for rule, pattern in _FORBIDDEN_CONTENT
        if pattern.search(value) is not None
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify selected public-source files contain no forbidden content."
    )
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--allowlist", type=Path, required=True)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the public-source content scan without printing matched values."""
    arguments = _parser().parse_args(argv)
    try:
        manifest = scan_public_checkout(
            arguments.root,
            load_policy(arguments.allowlist),
        )
    except (OSError, ReleaseSourceError) as error:
        print(f"public-source: error: {error}", file=sys.stderr)
        return 2
    if arguments.as_json:
        print(
            json.dumps(
                {"entries": len(manifest.entries), "status": "ok"},
                ensure_ascii=True,
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
