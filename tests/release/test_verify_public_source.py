"""Regression tests for fail-closed public-source content scanning."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.release_source_manifest import SymlinkNotAllowed, load_policy
from scripts.verify_public_source import (
    ForbiddenContentError,
    scan_public_checkout,
)


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _policy(tmp_path: Path) -> Path:
    policy = tmp_path / "allowlist.txt"
    policy.write_text("include README.md\n", encoding="utf-8")
    return policy


def test_scanner_accepts_a_clean_checkout_and_ignores_git_metadata(
    tmp_path: Path,
) -> None:
    root = tmp_path / "public"
    _write(root / "README.md", b"Public source only.\n")
    _write(root / ".git" / "HEAD", b"ref: refs/heads/main\n")

    manifest = scan_public_checkout(root, load_policy(_policy(tmp_path)))

    assert [entry.path for entry in manifest.entries] == ["README.md"]


@pytest.mark.parametrize(
    ("rule", "content"),
    [
        pytest.param(
            "github-token",
            b"gh" + b"p_" + (b"x" * 36),
            id="github-token",
        ),
        pytest.param(
            "pem-private-key",
            b"-----BEGIN " + b"PRIVATE KEY-----",
            id="pem-private-key",
        ),
        pytest.param(
            "local-build-path",
            b"/home/" + b"washimi/work/studio",
            id="local-build-path",
        ),
    ],
)
def test_scanner_rejects_forbidden_content_without_echoing_it(
    tmp_path: Path,
    rule: str,
    content: bytes,
) -> None:
    root = tmp_path / "public"
    _write(root / "README.md", content)

    with pytest.raises(ForbiddenContentError) as raised:
        scan_public_checkout(root, load_policy(_policy(tmp_path)))

    message = str(raised.value)
    assert rule in message
    assert "README.md" in message
    assert content.decode("ascii") not in message


def test_scanner_keeps_manifest_safety_checks(tmp_path: Path) -> None:
    root = tmp_path / "public"
    target = tmp_path / "target"
    _write(target, b"outside\n")
    root.mkdir()
    (root / "README.md").symlink_to(target)

    with pytest.raises(SymlinkNotAllowed):
        scan_public_checkout(root, load_policy(_policy(tmp_path)))


def test_scanner_rejects_forbidden_filename_without_echoing_it(
    tmp_path: Path,
) -> None:
    root = tmp_path / "public"
    leaked_name = "gh" + "p_" + ("x" * 36)
    _write(root / "src" / leaked_name, b"public content\n")
    policy = tmp_path / "allowlist.txt"
    policy.write_text("include src/**\n", encoding="utf-8")

    with pytest.raises(ForbiddenContentError) as raised:
        scan_public_checkout(root, load_policy(policy))

    message = str(raised.value)
    assert "github-token" in message
    assert "<redacted path>" in message
    assert leaked_name not in message


def test_command_line_scanner_reports_stable_json_and_redacts_matches(
    tmp_path: Path,
) -> None:
    root = tmp_path / "public"
    _write(root / "README.md", b"Public source only.\n")
    policy = _policy(tmp_path)
    script = Path(__file__).resolve().parents[2] / "scripts" / "verify_public_source.py"

    accepted = subprocess.run(
        [
            sys.executable,
            str(script),
            "--root",
            str(root),
            "--allowlist",
            str(policy),
            "--json",
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert accepted.returncode == 0, accepted.stderr
    assert json.loads(accepted.stdout) == {"entries": 1, "status": "ok"}
    assert accepted.stderr == ""

    leaked_value = "gh" + "p_" + ("x" * 36)
    _write(root / "README.md", leaked_value.encode("ascii"))
    rejected = subprocess.run(
        [
            sys.executable,
            str(script),
            "--root",
            str(root),
            "--allowlist",
            str(policy),
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert rejected.returncode == 2
    assert rejected.stdout == ""
    assert "github-token" in rejected.stderr
    assert leaked_value not in rejected.stderr
