"""Contract tests for the detached public release-source identity."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import export_release_source as export_module
from scripts import release_source_manifest as manifest_module
from scripts.export_release_source import export_release_source
from scripts.release_source_manifest import (
    DeniedPathError,
    ExcludedPathError,
    ManifestFormatError,
    ManifestMismatch,
    ReleaseSourceError,
    ReleaseSourceManifest,
    SymlinkNotAllowed,
    UnclassifiedPathError,
    build_manifest,
    build_public_checkout_manifest,
    compare_manifests,
    load_policy,
    manifest_digest,
    read_manifest,
    verify_manifest,
    verify_public_checkout_manifest,
    write_manifest,
)
from scripts.verify_public_source import ForbiddenContentError, scan_public_checkout

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def canonical_public_source(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path, Path, Path, ReleaseSourceManifest]:
    """Create S and a clean public-checkout analogue from the real source tree."""
    repository_root = Path(__file__).resolve().parents[2]
    policy_path = repository_root / "packaging/release-source-allowlist.txt"
    policy = load_policy(policy_path)
    root = tmp_path_factory.mktemp("canonical-public-source")
    snapshot = root / "snapshot"
    sidecar = root / "SOURCE-MANIFEST.json"
    manifest = export_release_source(repository_root, snapshot, policy, sidecar)
    public_checkout = root / "public-checkout"
    shutil.copytree(snapshot, public_checkout)
    _write(public_checkout / ".git" / "HEAD", "ref: refs/heads/main\n", 0o600)
    return repository_root, snapshot, public_checkout, sidecar, manifest


def test_default_policy_includes_public_gui_trial_tests() -> None:
    """Release source retains the wheel-first product and its public checks."""
    repository_root = Path(__file__).resolve().parents[2]
    policy = load_policy(repository_root / "packaging/release-source-allowlist.txt")

    assert policy.classify("gui_tests/test_launcher_project_recovery.py") == "include"
    assert policy.classify("gui_tests/test_trial_launcher_capabilities.py") == "include"
    assert policy.classify("gui_tests/test_welcome_support.py") == "include"
    assert policy.classify("gui_tests/test_workspace_window.py") == "include"
    assert (
        policy.classify("tests/architecture/test_headless_boundaries.py") == "include"
    )
    assert policy.classify("scripts/verify_signal_io.py") == "include"
    assert policy.classify("scripts/capture_trial_resolution.py") == "include"
    assert policy.classify("scripts/run_trial_technical_gate.py") == "include"
    assert policy.classify("scripts/assemble_trial_bundle.py") == "include"
    assert policy.classify("scripts/verify_trial_bundle.py") == "include"
    assert policy.classify("scripts/extract_trial_artifact.py") == "include"
    assert policy.classify("docs/Quick-Start.md") == "include"
    assert policy.classify("docs/Quick-Start.ja.md") == "include"
    assert policy.classify("docs/Feedback.ja.md") == "include"
    assert policy.classify("scripts/package_trial_release.py") == "include"
    assert policy.classify("scripts/verify_trial_release.py") == "include"
    assert policy.classify(".github/workflows/ci.yml") == "include"
    assert policy.classify(".github/workflows/source-identity.yml") == "include"
    assert policy.classify(".github/workflows/build-trial-wheel.yml") == "include"
    assert policy.classify(".github/workflows/publish-trial.yml") == "include"
    assert policy.classify(".github/workflows/trial-package.yml") == "exclude"
    assert policy.classify("scripts/build_appimage.py") == "exclude"
    assert policy.classify("scripts/build_standalone.py") == "exclude"
    assert policy.classify("scripts/artifact_inventory.py") == "exclude"
    assert policy.classify("scripts/artifact_inventory_input.py") == "exclude"
    assert policy.classify("scripts/artifact_tree_manifest.py") == "exclude"
    assert policy.classify("scripts/release_provenance.py") == "exclude"
    assert policy.classify("tests/release/test_release_provenance.py") == "exclude"
    assert policy.classify("tests/release/test_standalone_build.py") == "exclude"
    assert policy.classify("packaging/appimage/README.md") == "exclude"
    assert policy.classify("packaging/standalone/launcher.py") == "exclude"
    assert policy.classify("ROADMAP.md") == "include"


def test_public_source_identity_workflow_verifies_p_without_private_inputs() -> None:
    """The M1 verifier compares M(P) directly without private release data."""
    workflow = (
        Path(__file__).resolve().parents[2]
        / ".github"
        / "workflows"
        / "source-identity.yml"
    ).read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "push:" not in workflow
    assert "pull_request:" not in workflow
    assert "runs-on: ubuntu-24.04" in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "timeout-minutes:" in workflow
    assert "concurrency:" in workflow
    assert "source_sha:" in workflow
    assert "source_manifest_sha256:" in workflow
    assert "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683" in workflow
    assert (
        "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in workflow
    )
    assert "GITHUB_REF" in workflow
    assert "GITHUB_SHA" in workflow
    assert "DEFAULT_BRANCH" in workflow
    assert '"refs/heads/" + os.environ["DEFAULT_BRANCH"]' in workflow
    assert 'os.environ["SOURCE_SHA"] != os.environ["GITHUB_SHA"]' in workflow
    assert "ref: ${{ inputs.source_sha }}" in workflow
    assert "git rev-parse --verify HEAD" in workflow
    assert "scripts/verify_public_source.py" in workflow
    assert "scripts/release_source_manifest.py build" in workflow
    assert "--public-checkout" in workflow
    assert "M(P) does not equal" in workflow
    assert "retention-days: 7" in workflow
    assert "private_rc_commit" not in workflow
    assert "private_rc_tree" not in workflow
    assert "export_release_source.py" not in workflow
    assert "release_provenance.py" not in workflow
    assert "gh release" not in workflow
    assert "git tag" not in workflow


def test_real_public_checkout_manifest_equals_canonical_snapshot(
    canonical_public_source: tuple[Path, Path, Path, Path, ReleaseSourceManifest],
) -> None:
    """M1 proves M(S) == M(P) without re-exporting the public checkout."""
    repository_root, _snapshot, public_checkout, sidecar, source_manifest = (
        canonical_public_source
    )
    policy = load_policy(repository_root / "packaging/release-source-allowlist.txt")

    assert build_public_checkout_manifest(public_checkout, policy) == source_manifest
    assert (
        verify_public_checkout_manifest(public_checkout, policy, sidecar)
        == source_manifest
    )


def test_canonical_public_checkout_passes_forbidden_content_scan(
    canonical_public_source: tuple[Path, Path, Path, Path, ReleaseSourceManifest],
) -> None:
    """Selected P content is free from the explicit public-source hazards."""
    repository_root, _snapshot, public_checkout, _sidecar, source_manifest = (
        canonical_public_source
    )
    policy = load_policy(repository_root / "packaging/release-source-allowlist.txt")

    assert scan_public_checkout(public_checkout, policy) == source_manifest


def test_canonical_public_snapshot_executes_retained_test_closures(
    canonical_public_source: tuple[Path, Path, Path, Path, ReleaseSourceManifest],
    tmp_path: Path,
) -> None:
    """Retained tests do not import source that M1 intentionally excludes."""
    _repository_root, _snapshot, public_checkout, _sidecar, _source_manifest = (
        canonical_public_source
    )
    runtime_checkout = tmp_path / "runtime-checkout"
    shutil.copytree(public_checkout, runtime_checkout)
    environment = os.environ.copy()
    environment.update(
        {
            "MPLBACKEND": "Agg",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join(
                (str(runtime_checkout / "src"), str(runtime_checkout))
            ),
        }
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/unit/test_signal_io_matrix.py",
            "tests/architecture/test_headless_boundaries.py",
            (
                "tests/release/test_release_source_manifest.py::"
                "test_public_source_identity_workflow_verifies_p_without_private_inputs"
            ),
        ],
        cwd=runtime_checkout,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_canonical_public_snapshot_contains_required_public_files(
    canonical_public_source: tuple[Path, Path, Path, Path, ReleaseSourceManifest],
) -> None:
    """The canonical snapshot retains the public source and CI closure."""
    _repository_root, _snapshot, _public_checkout, _sidecar, manifest = (
        canonical_public_source
    )
    paths = {entry.path for entry in manifest.entries}

    assert {
        "README.md",
        "README.ja.md",
        "ROADMAP.md",
        "LICENSE",
        "SECURITY.md",
        "pyproject.toml",
        ".github/workflows/ci.yml",
        ".github/workflows/source-identity.yml",
        "constraints/signal-linux-py312.txt",
        "examples/alpha-timeseries.csv",
        "src/gwexpy_studio/assets/alpha-timeseries.csv",
        "src/gwexpy_studio/ui/app.py",
        "packaging/release-source-allowlist.txt",
        "scripts/release_source_manifest.py",
        "scripts/export_release_source.py",
        "scripts/verify_public_source.py",
        "scripts/verify_signal_io.py",
        "tests/architecture/test_headless_boundaries.py",
        "tests/release/test_release_source_manifest.py",
        "gui_tests/test_launcher_project_recovery.py",
        "gui_tests/test_welcome_support.py",
        "gui_tests/test_workspace_window.py",
        "docs/development.md",
        "docs/release/0.1.0a1-trial-readiness.md",
    }.issubset(paths)


def test_canonical_public_snapshot_excludes_private_only_paths(
    canonical_public_source: tuple[Path, Path, Path, Path, ReleaseSourceManifest],
) -> None:
    """A public source snapshot does not carry private evidence or workflows."""
    _repository_root, _snapshot, _public_checkout, _sidecar, manifest = (
        canonical_public_source
    )
    paths = {entry.path for entry in manifest.entries}
    private_prefixes = (
        ".agent/",
        ".claude/",
        ".codex/",
        ".harness/",
        "docs/evidence/",
        "docs/notes/",
        "docs/plans/",
        "docs/progress/",
        "docs/superpowers/",
        "gui_tests/contracts/",
        "harness/",
        "memory-bank/",
        "notes/",
        "tests/contracts/",
        "tests/meta/",
    )
    private_leaves = {
        "AGENTS.md",
        "CLAUDE.md",
        "STATUS.md",
        "contract-baseline.toml",
        "gui-contract-baseline.toml",
        ".github/copilot-instructions.md",
        ".github/workflows/trial-package.yml",
        "packaging/appimage/README.md",
        "packaging/artifact-inventory/input-template.json",
        "packaging/standalone/launcher.py",
        "scripts/verify_alpha_trial_export.py",
        "scripts/run_gui_tests.py",
    }

    assert not any(path.startswith(private_prefixes) for path in paths)
    assert paths.isdisjoint(private_leaves)


def test_canonical_snapshot_preserves_public_source_bytes_and_modes(
    canonical_public_source: tuple[Path, Path, Path, Path, ReleaseSourceManifest],
) -> None:
    """S preserves selected bytes and writes canonical portable permissions."""
    repository_root, snapshot, _public_checkout, _sidecar, manifest = (
        canonical_public_source
    )
    for entry in manifest.entries:
        source_path = repository_root / entry.path
        snapshot_path = snapshot / entry.path
        snapshot_status = snapshot_path.lstat()

        assert source_path.exists()
        assert stat.S_IMODE(snapshot_status.st_mode) == int(entry.mode, 8)
        if entry.file_type == "directory":
            assert snapshot_path.is_dir()
            assert entry.sha256 == ""
            continue

        assert snapshot_path.is_file()
        assert source_path.read_bytes() == snapshot_path.read_bytes()
        assert hashlib.sha256(snapshot_path.read_bytes()).hexdigest() == entry.sha256


def _write(path: Path, content: str, mode: int = 0o644) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)
    return path


def _policy(tmp_path: Path, *rules: str) -> Path:
    path = tmp_path / "allowlist.txt"
    path.write_text("\n".join(rules) + "\n", encoding="utf-8")
    return path


def test_manifest_is_path_sorted_and_ignores_mtime(tmp_path: Path) -> None:
    root = tmp_path / "release"
    _write(root / "src" / "z.py", "z = 1\n", 0o755)
    _write(root / "src" / "a.py", "a = 1\n", 0o644)
    _write(root / "README.md", "trial\n")
    policy = load_policy(_policy(tmp_path, "include src/**", "include README.md"))

    first = build_manifest(root, policy)
    paths = [entry.path for entry in first.entries]

    assert paths == sorted(paths, key=lambda path: path.encode("utf-8"))
    assert [entry.file_type for entry in first.entries] == [
        "file",
        "directory",
        "file",
        "file",
    ]
    assert first.to_bytes().endswith(b"\n")

    os.utime(root / "src" / "a.py", (1_700_000_000, 1_700_000_000))
    second = build_manifest(root, policy)

    assert second.to_bytes() == first.to_bytes()
    assert manifest_digest(second) == manifest_digest(first)


def test_manifest_reports_content_and_mode_differences(tmp_path: Path) -> None:
    root = tmp_path / "release"
    executable = _write(root / "src" / "tool.py", "print('one')\n", 0o755)
    policy = load_policy(_policy(tmp_path, "include src/**"))
    expected = build_manifest(root, policy)

    executable.write_text("print('two')\n", encoding="utf-8")
    executable.chmod(0o644)
    actual = build_manifest(root, policy)
    differences = compare_manifests(expected, actual)

    changed = [
        difference for difference in differences if difference.path == "src/tool.py"
    ]
    assert len(changed) == 1
    assert changed[0].kind == "changed"
    assert "mode" in changed[0].detail
    assert "sha256" in changed[0].detail

    with pytest.raises(ManifestMismatch, match="src/tool.py"):
        verify_manifest(root, policy, expected)


def test_public_checkout_manifest_normalizes_git_relevant_modes(
    tmp_path: Path,
) -> None:
    """Source identity uses Git-relevant modes, not checkout umask details."""
    source = tmp_path / "private-source"
    _write(source / "src" / "tool.py", "print('tool')\n", 0o700)
    (source / "src").chmod(0o700)
    policy = load_policy(
        _policy(
            tmp_path,
            "include src/**",
            "exclude .git/**",
        )
    )
    snapshot = tmp_path / "snapshot"
    sidecar = tmp_path / "source-manifest.json"

    source_manifest = export_release_source(source, snapshot, policy, sidecar)

    assert (
        next(entry for entry in source_manifest.entries if entry.path == "src").mode
        == "0755"
    )
    assert (
        next(
            entry for entry in source_manifest.entries if entry.path == "src/tool.py"
        ).mode
        == "0755"
    )
    assert stat.S_IMODE((snapshot / "src").stat().st_mode) == 0o755
    assert stat.S_IMODE((snapshot / "src" / "tool.py").stat().st_mode) == 0o755

    public_checkout = tmp_path / "public-checkout"
    shutil.copytree(snapshot, public_checkout)
    _write(public_checkout / ".git" / "HEAD", "ref: refs/heads/main\n", 0o600)
    (public_checkout / "src").chmod(0o755)
    (public_checkout / "src" / "tool.py").chmod(0o755)

    assert build_public_checkout_manifest(public_checkout, policy) == source_manifest
    assert (
        verify_public_checkout_manifest(public_checkout, policy, sidecar)
        == source_manifest
    )


def test_detached_manifest_rejects_duplicate_paths_and_non_hex_hashes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "release"
    _write(root / "src" / "app.py", "x = 1\n")
    policy = load_policy(_policy(tmp_path, "include src/**"))
    manifest = build_manifest(root, policy)
    payload = json.loads(manifest.to_bytes())
    sidecar = tmp_path / "manifest.json"

    payload["entries"].append(payload["entries"][-1])
    sidecar.write_text(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ManifestFormatError, match="duplicate"):
        read_manifest(sidecar)

    payload = json.loads(manifest.to_bytes())
    file_entry = next(
        entry for entry in payload["entries"] if entry["file_type"] == "file"
    )
    file_entry["sha256"] = "z" * 64
    sidecar.write_text(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ManifestFormatError, match="SHA-256"):
        read_manifest(sidecar)


def test_detached_manifest_rejects_non_string_fields_and_boolean_schema(
    tmp_path: Path,
) -> None:
    """Canonical JSON does not coerce values into the manifest schema."""
    root = tmp_path / "release"
    _write(root / "src" / "app.py", "x = 1\n")
    manifest = build_manifest(root, load_policy(_policy(tmp_path, "include src/**")))
    sidecar = tmp_path / "manifest.json"
    payload = json.loads(manifest.to_bytes())

    payload["schema"] = True
    sidecar.write_text(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ManifestFormatError, match="schema"):
        read_manifest(sidecar)

    payload = json.loads(manifest.to_bytes())
    payload["entries"][0]["path"] = 1
    sidecar.write_text(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ManifestFormatError, match="path must be a string"):
        read_manifest(sidecar)


def test_release_tree_rejects_symlinks_even_when_the_target_is_allowed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "release"
    target = _write(root / "src" / "target.py", "x = 1\n")
    (root / "src" / "link.py").symlink_to(target.name)
    policy = load_policy(_policy(tmp_path, "include src/**"))

    with pytest.raises(SymlinkNotAllowed, match="src/link.py"):
        build_manifest(root, policy)


def test_release_tree_rejects_special_permission_bits(tmp_path: Path) -> None:
    """Set-ID and sticky bits are not portable release-source permissions."""
    root = tmp_path / "release"
    _write(root / "src" / "tool.py", "print('trial')\n", 0o4755)
    policy = load_policy(_policy(tmp_path, "include src/**"))

    with pytest.raises(ReleaseSourceError, match="special permission"):
        build_manifest(root, policy)

    (root / "src" / "tool.py").chmod(0o644)
    root.chmod(0o1755)
    with pytest.raises(ReleaseSourceError, match="root.*special permission"):
        build_manifest(root, policy)


def test_detached_manifest_rejects_special_permission_bits(tmp_path: Path) -> None:
    """A sidecar cannot reintroduce set-ID or sticky modes by hand."""
    root = tmp_path / "release"
    _write(root / "src" / "app.py", "x = 1\n")
    manifest = build_manifest(root, load_policy(_policy(tmp_path, "include src/**")))
    payload = json.loads(manifest.to_bytes())
    payload["entries"][0]["mode"] = "4755"
    sidecar = tmp_path / "manifest.json"
    sidecar.write_text(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ManifestFormatError, match="special permissions"):
        read_manifest(sidecar)


def test_read_manifest_rejects_symlink_and_special_permission_leaf(
    tmp_path: Path,
) -> None:
    """Detached evidence is a no-follow, portable regular file input."""
    root = tmp_path / "release"
    _write(root / "src" / "app.py", "x = 1\n")
    manifest = build_manifest(root, load_policy(_policy(tmp_path, "include src/**")))
    target = tmp_path / "target.json"
    write_manifest(manifest, target)
    symlink = tmp_path / "manifest-link.json"
    symlink.symlink_to(target)

    with pytest.raises(ManifestFormatError, match="safely open"):
        read_manifest(symlink)

    target.chmod(0o4755)
    with pytest.raises(ManifestFormatError, match="unsafe special permissions"):
        read_manifest(target)


def test_release_tree_rejects_excluded_denied_and_unclassified_paths(
    tmp_path: Path,
) -> None:
    root = tmp_path / "release"
    _write(root / "src" / "app.py", "x = 1\n")
    _write(root / "private" / "note.txt", "not public\n")
    policy = load_policy(
        _policy(
            tmp_path,
            "include src/**",
            "exclude private/**",
            "deny .harness/**",
        )
    )

    with pytest.raises(ExcludedPathError, match="private"):
        build_manifest(root, policy)

    (root / "private" / "note.txt").unlink()
    (root / "private").rmdir()
    _write(root / ".harness" / "secret.txt", "private\n")
    with pytest.raises(DeniedPathError, match=r"\.harness"):
        build_manifest(root, policy)

    (root / ".harness" / "secret.txt").unlink()
    (root / ".harness").rmdir()
    _write(root / "unexpected.txt", "unclassified\n")
    with pytest.raises(UnclassifiedPathError, match="unexpected.txt"):
        build_manifest(root, policy)


def test_allowlist_rejects_literal_path_escapes_and_absolute_patterns(
    tmp_path: Path,
) -> None:
    escaped = _policy(tmp_path, "include ../outside")
    with pytest.raises(ValueError, match="relative"):
        load_policy(escaped)

    absolute = _policy(tmp_path, "include /etc/passwd")
    with pytest.raises(ValueError, match="relative"):
        load_policy(absolute)

    ambiguous = _policy(tmp_path, "include ./src")
    with pytest.raises(ValueError, match="relative"):
        load_policy(ambiguous)


def test_export_and_verify_preserve_source_content_and_modes(tmp_path: Path) -> None:
    source = tmp_path / "private-source"
    _write(source / "src" / "gwexpy_studio" / "app.py", "print('studio')\n", 0o755)
    _write(source / "tests" / "unit" / "test_public.py", "def test_ok(): pass\n")
    _write(source / "gui_tests" / "test_window.py", "def test_window(): pass\n")
    _write(source / "docs" / "release" / "trial.md", "public contract\n")
    _write(source / "private" / "audit.txt", "internal\n")
    _write(source / ".harness" / "memory.md", "private\n")
    policy = load_policy(
        _policy(
            tmp_path,
            "include src/**",
            "include tests/unit/**",
            "include gui_tests/test_*.py",
            "include docs/release/**",
            "exclude private/**",
            "deny .harness/**",
        )
    )
    snapshot = tmp_path / "snapshot"
    sidecar = tmp_path / "release-source-manifest.json"

    exported = export_release_source(source, snapshot, policy, sidecar)

    assert (snapshot / "src" / "gwexpy_studio" / "app.py").read_text(
        encoding="utf-8"
    ) == "print('studio')\n"
    assert (
        stat.S_IMODE((snapshot / "src" / "gwexpy_studio" / "app.py").stat().st_mode)
        == 0o755
    )
    assert not (snapshot / "private").exists()
    assert not (snapshot / ".harness").exists()
    assert not (snapshot / "release-source-manifest.json").exists()
    assert sidecar.exists()
    assert verify_manifest(snapshot, policy, exported) == exported

    _write(snapshot / "src" / "gwexpy_studio" / "app.py", "changed\n", 0o755)
    with pytest.raises(ManifestMismatch, match="src/gwexpy_studio/app.py"):
        verify_manifest(snapshot, policy, exported)


def test_export_rejects_forbidden_selected_content_before_writing_evidence(
    tmp_path: Path,
) -> None:
    """S is never emitted when a selected file contains unsafe public content."""
    source = tmp_path / "private-source"
    leaked_value = b"gh" + b"p_" + (b"x" * 36)
    _write(source / "README.md", leaked_value.decode("ascii"))
    policy = load_policy(_policy(tmp_path, "include README.md"))
    snapshot = tmp_path / "snapshot"
    sidecar = tmp_path / "release-source-manifest.json"

    with pytest.raises(ForbiddenContentError) as raised:
        export_release_source(source, snapshot, policy, sidecar)

    assert "README.md" in str(raised.value)
    assert leaked_value.decode("ascii") not in str(raised.value)
    assert not snapshot.exists()
    assert not sidecar.exists()


def test_export_preserves_multiple_files_after_staging_directory_binding(
    tmp_path: Path,
) -> None:
    """Directory witnesses are refreshed for ordinary sibling-file copies."""
    source = tmp_path / "private-source"
    _write(source / "src" / "package" / "first.py", "first = True\n")
    _write(source / "src" / "package" / "second.py", "second = True\n")
    policy = load_policy(_policy(tmp_path, "include src/**"))
    snapshot = tmp_path / "snapshot"
    sidecar = tmp_path / "release-source-manifest.json"

    exported = export_release_source(source, snapshot, policy, sidecar)

    assert (snapshot / "src" / "package" / "first.py").read_text(
        encoding="utf-8"
    ) == "first = True\n"
    assert (snapshot / "src" / "package" / "second.py").read_text(
        encoding="utf-8"
    ) == "second = True\n"
    assert verify_manifest(snapshot, policy, exported) == exported


def test_export_rejects_sticky_staging_root_replaced_before_initial_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raced sticky stage root cannot become the copy destination."""
    source = tmp_path / "private-source"
    _write(source / "src" / "payload.py", "approved = True\n")
    victim = tmp_path / "external-victim"
    victim.mkdir(mode=0o1700)
    victim.chmod(0o1700)
    policy = load_policy(_policy(tmp_path, "include src/**"))
    snapshot = tmp_path / "snapshot"
    sidecar = tmp_path / "release-source-manifest.json"
    original_mkdir = export_module.os.mkdir
    raced_stage: Path | None = None

    def replace_stage_after_mkdir(
        path: str | bytes,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal raced_stage
        original_mkdir(path, mode, dir_fd=dir_fd)
        if (
            raced_stage is not None
            or dir_fd is None
            or not isinstance(path, str)
            or not path.startswith(".snapshot.stage-")
        ):
            return
        os.rmdir(path, dir_fd=dir_fd)
        os.rename(victim.name, path, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        raced_stage = tmp_path / path

    monkeypatch.setattr(export_module.os, "mkdir", replace_stage_after_mkdir)

    with pytest.raises(ReleaseSourceError, match="staging root.*special permission"):
        export_release_source(source, snapshot, policy, sidecar)

    assert raced_stage is not None
    assert raced_stage.is_dir()
    assert stat.S_IMODE(raced_stage.stat().st_mode) & 0o7000
    assert not (raced_stage / "src").exists()
    assert not snapshot.exists()
    assert not sidecar.exists()
    raced_stage.chmod(0o700)
    raced_stage.rmdir()


def test_export_rechecks_staging_root_mode_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sticky stage root introduced after evidence cannot be published."""
    source = tmp_path / "private-source"
    _write(source / "src" / "payload.py", "approved = True\n")
    policy = load_policy(_policy(tmp_path, "include src/**"))
    snapshot = tmp_path / "snapshot"
    sidecar = tmp_path / "release-source-manifest.json"
    original_write_sidecar = export_module._write_sidecar
    staged_root: Path | None = None

    def make_staging_root_sticky_after_evidence(
        manifest: object,
        parent_descriptor: int,
        path: Path,
    ) -> os.stat_result:
        nonlocal staged_root
        status = original_write_sidecar(manifest, parent_descriptor, path)
        staged_root = next(tmp_path.glob(".snapshot.stage-*"))
        staged_root.chmod(0o1700)
        return status

    monkeypatch.setattr(
        export_module,
        "_write_sidecar",
        make_staging_root_sticky_after_evidence,
    )

    with pytest.raises(ReleaseSourceError, match="staging root.*special permission"):
        export_release_source(source, snapshot, policy, sidecar)

    assert staged_root is not None
    assert staged_root.is_dir()
    assert stat.S_IMODE(staged_root.stat().st_mode) & 0o7000
    assert not snapshot.exists()
    assert sidecar.is_file()
    staged_root.chmod(0o700)
    shutil.rmtree(staged_root)


def test_export_rejects_staging_root_replaced_after_descriptor_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replaced stage pathname cannot publish an external directory."""
    source = tmp_path / "private-source"
    _write(source / "src" / "payload.py", "approved = True\n")
    victim = tmp_path / "external-victim"
    _write(victim / "sentinel.txt", "must not be published\n")
    victim.chmod(0o700)
    policy = load_policy(_policy(tmp_path, "include src/**"))
    snapshot = tmp_path / "snapshot"
    sidecar = tmp_path / "release-source-manifest.json"
    original_write_sidecar = export_module._write_sidecar
    raced_stage: Path | None = None
    displaced_stage: Path | None = None

    def replace_stage_after_evidence(
        manifest: object,
        parent_descriptor: int,
        path: Path,
    ) -> os.stat_result:
        nonlocal raced_stage, displaced_stage
        status = original_write_sidecar(manifest, parent_descriptor, path)
        raced_stage = next(tmp_path.glob(".snapshot.stage-*"))
        displaced_stage = tmp_path / ".displaced-stage"
        raced_stage.rename(displaced_stage)
        victim.rename(raced_stage)
        return status

    monkeypatch.setattr(
        export_module,
        "_write_sidecar",
        replace_stage_after_evidence,
    )

    with pytest.raises(
        ReleaseSourceError,
        match="staging root changed during detached evidence binding",
    ):
        export_release_source(source, snapshot, policy, sidecar)

    assert raced_stage is not None
    assert displaced_stage is not None
    assert (raced_stage / "sentinel.txt").read_text(encoding="utf-8") == (
        "must not be published\n"
    )
    assert (displaced_stage / "src" / "payload.py").read_text(encoding="utf-8") == (
        "approved = True\n"
    )
    assert not snapshot.exists()
    assert sidecar.is_file()


def test_export_rejects_parent_symlink_replacement_and_cleans_partial_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source directory swapped after selection cannot inject external data."""
    source = tmp_path / "private-source"
    outside = tmp_path / "outside"
    _write(source / "src" / "payload.py", "approved = True\n")
    _write(outside / "payload.py", "external = True\n")
    policy = load_policy(_policy(tmp_path, "include src/**"))
    snapshot = tmp_path / "snapshot"
    sidecar = tmp_path / "release-source-manifest.json"
    original_select_nodes = export_module._select_nodes

    def replace_parent_after_selection(
        *args: object,
        **kwargs: object,
    ) -> tuple[object, ...]:
        selected = original_select_nodes(*args, **kwargs)
        shutil.rmtree(source / "src")
        (source / "src").symlink_to(outside, target_is_directory=True)
        return selected

    monkeypatch.setattr(
        export_module,
        "_select_nodes",
        replace_parent_after_selection,
    )

    with pytest.raises(SymlinkNotAllowed, match="src"):
        export_module.export_release_source(source, snapshot, policy, sidecar)

    assert not snapshot.exists()
    assert not sidecar.exists()


def test_export_rejects_source_root_replaced_by_symlink_during_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The source-root path itself is locked without resolving a raced link."""
    source = tmp_path / "private-source"
    outside = tmp_path / "outside"
    _write(source / "src" / "payload.py", "approved = True\n")
    _write(outside / "src" / "payload.py", "external = True\n")
    policy = load_policy(_policy(tmp_path, "include src/**"))
    snapshot = tmp_path / "snapshot"
    sidecar = tmp_path / "release-source-manifest.json"
    original_is_symlink = Path.is_symlink
    injected = False

    def replace_root_after_check(path: Path) -> bool:
        nonlocal injected
        result = original_is_symlink(path)
        if path == source and not injected:
            shutil.rmtree(source)
            source.symlink_to(outside, target_is_directory=True)
            injected = True
        return result

    monkeypatch.setattr(Path, "is_symlink", replace_root_after_check)

    with pytest.raises(SymlinkNotAllowed, match="root"):
        export_release_source(source, snapshot, policy, sidecar)

    assert not snapshot.exists()
    assert not sidecar.exists()


@pytest.mark.parametrize(
    "output_kind",
    ("snapshot", "sidecar"),
    ids=("snapshot", "sidecar"),
)
def test_export_rejects_output_physically_inside_source_through_parent_symlink(
    tmp_path: Path,
    output_kind: str,
) -> None:
    """A lexical alias cannot route a snapshot or sidecar back into source."""
    physical_parent = tmp_path / "physical-parent"
    physical_source = physical_parent / "private-source"
    _write(physical_source / "src" / "payload.py", "approved = True\n")
    alias_parent = tmp_path / "alias-parent"
    alias_parent.symlink_to(physical_parent, target_is_directory=True)
    source = alias_parent / "private-source"
    policy = load_policy(_policy(tmp_path, "include src/**"))
    snapshot = (
        physical_source / "snapshot"
        if output_kind == "snapshot"
        else tmp_path / "snapshot"
    )
    sidecar = (
        physical_source / "release-source-manifest.json"
        if output_kind == "sidecar"
        else tmp_path / "release-source-manifest.json"
    )

    with pytest.raises(ValueError, match="outside the source"):
        export_release_source(source, snapshot, policy, sidecar)

    assert not snapshot.exists()
    assert not sidecar.exists()


@pytest.mark.parametrize(
    "missing_output",
    ("snapshot", "sidecar"),
    ids=("snapshot", "sidecar"),
)
def test_export_requires_existing_output_parents(
    tmp_path: Path,
    missing_output: str,
) -> None:
    """Export does not create a mutable parent path before it can pin it."""
    source = tmp_path / "private-source"
    _write(source / "src" / "payload.py", "approved = True\n")
    policy = load_policy(_policy(tmp_path, "include src/**"))
    snapshot = (
        tmp_path / "missing-snapshot-parent" / "snapshot"
        if missing_output == "snapshot"
        else tmp_path / "snapshot"
    )
    sidecar = (
        tmp_path / "missing-sidecar-parent" / "release-source-manifest.json"
        if missing_output == "sidecar"
        else tmp_path / "release-source-manifest.json"
    )

    with pytest.raises(FileNotFoundError, match="parent directory"):
        export_release_source(source, snapshot, policy, sidecar)

    assert not snapshot.exists()
    assert not sidecar.exists()


def test_export_rejects_in_place_source_change_after_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Content written after inspection cannot become an unproven snapshot."""
    source = tmp_path / "private-source"
    source_file = _write(source / "src" / "payload.py", "approved = True\n")
    policy = load_policy(_policy(tmp_path, "include src/**"))
    snapshot = tmp_path / "snapshot"
    sidecar = tmp_path / "release-source-manifest.json"
    original_select_nodes = export_module._select_nodes

    def modify_after_selection(
        *args: object,
        **kwargs: object,
    ) -> tuple[object, ...]:
        selected = original_select_nodes(*args, **kwargs)
        source_file.write_text("changed after inspection\n", encoding="utf-8")
        return selected

    monkeypatch.setattr(export_module, "_select_nodes", modify_after_selection)

    with pytest.raises(ReleaseSourceError, match="changed"):
        export_module.export_release_source(source, snapshot, policy, sidecar)

    assert not snapshot.exists()
    assert not sidecar.exists()


def test_export_rejects_staging_change_after_evidence_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Final publication revalidates the bytes described by detached evidence."""
    source = tmp_path / "private-source"
    _write(source / "src" / "payload.py", "approved = True\n")
    policy = load_policy(_policy(tmp_path, "include src/**"))
    snapshot = tmp_path / "snapshot"
    sidecar = tmp_path / "release-source-manifest.json"
    original_write_sidecar = export_module._write_sidecar

    def alter_staging_after_evidence(
        manifest: object,
        parent_descriptor: int,
        path: Path,
    ) -> os.stat_result:
        status = original_write_sidecar(manifest, parent_descriptor, path)
        stage = next(tmp_path.glob(".snapshot.stage-*"))
        (stage / "src" / "payload.py").write_text("tampered = True\n", encoding="utf-8")
        return status

    monkeypatch.setattr(export_module, "_write_sidecar", alter_staging_after_evidence)

    with pytest.raises(ReleaseSourceError, match="staging tree changed"):
        export_module.export_release_source(source, snapshot, policy, sidecar)

    assert not snapshot.exists()
    assert sidecar.is_file()


def test_export_failure_leaves_staging_and_evidence_without_name_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure retains owned names rather than racing a final unlink or rmdir."""
    source = tmp_path / "private-source"
    _write(source / "src" / "payload.py", "approved = True\n")
    policy = load_policy(_policy(tmp_path, "include src/**"))
    snapshot = tmp_path / "snapshot"
    sidecar = tmp_path / "release-source-manifest.json"
    original_write_sidecar = export_module._write_sidecar
    staged_root: Path | None = None

    def alter_stage_after_evidence(
        manifest: object,
        parent_descriptor: int,
        path: Path,
    ) -> os.stat_result:
        nonlocal staged_root
        status = original_write_sidecar(manifest, parent_descriptor, path)
        staged_root = next(tmp_path.glob(".snapshot.stage-*"))
        (staged_root / "src" / "payload.py").write_text(
            "tampered = True\n",
            encoding="utf-8",
        )
        return status

    def unexpected_name_cleanup(*args: object, **kwargs: object) -> None:
        raise AssertionError("failed export must not delete by a pathname")

    monkeypatch.setattr(export_module, "_write_sidecar", alter_stage_after_evidence)
    monkeypatch.setattr(export_module.os, "rmdir", unexpected_name_cleanup)
    monkeypatch.setattr(export_module.os, "unlink", unexpected_name_cleanup)

    with pytest.raises(ReleaseSourceError, match="staging tree changed"):
        export_release_source(source, snapshot, policy, sidecar)

    assert staged_root is not None
    assert staged_root.is_dir()
    assert sidecar.is_file()
    assert not snapshot.exists()


def test_export_failure_does_not_name_remove_empty_staging_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-evidence failure cannot race an empty stage-root rmdir."""
    source = tmp_path / "private-source"
    _write(source / "src" / "payload.py", "approved = True\n")
    policy = load_policy(_policy(tmp_path, "include src/**"))
    snapshot = tmp_path / "snapshot"
    sidecar = tmp_path / "release-source-manifest.json"

    def fail_copy(*args: object, **kwargs: object) -> dict[str, str]:
        raise ReleaseSourceError("forced copy failure")

    def unexpected_name_cleanup(*args: object, **kwargs: object) -> None:
        raise AssertionError("failed export must not remove a stage name")

    monkeypatch.setattr(export_module, "_copy_selected_nodes", fail_copy)
    monkeypatch.setattr(export_module.os, "rmdir", unexpected_name_cleanup)

    with pytest.raises(ReleaseSourceError, match="forced copy failure"):
        export_release_source(source, snapshot, policy, sidecar)

    staged_root = next(tmp_path.glob(".snapshot.stage-*"))
    assert staged_root.is_dir()
    assert not snapshot.exists()
    assert not sidecar.exists()


def test_export_failure_does_not_recursively_delete_raced_staging_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure preserves a directory moved into staging by a racer."""
    source = tmp_path / "private-source"
    _write(source / "src" / "payload.py", "approved = True\n")
    victim = tmp_path / "victim"
    _write(victim / "secret.txt", "must not be deleted\n")
    policy = load_policy(_policy(tmp_path, "include src/**"))
    snapshot = tmp_path / "snapshot"
    sidecar = tmp_path / "release-source-manifest.json"
    original_write_sidecar = export_module._write_sidecar
    raced_stage: Path | None = None

    def inject_external_directory_then_fail(
        manifest: object,
        parent_descriptor: int,
        path: Path,
    ) -> os.stat_result:
        nonlocal raced_stage
        status = original_write_sidecar(manifest, parent_descriptor, path)
        raced_stage = next(tmp_path.glob(".snapshot.stage-*"))
        victim.rename(raced_stage / "injected")
        (raced_stage / "src" / "payload.py").write_text(
            "tampered = True\n",
            encoding="utf-8",
        )
        return status

    monkeypatch.setattr(
        export_module,
        "_write_sidecar",
        inject_external_directory_then_fail,
    )

    with pytest.raises(ReleaseSourceError):
        export_release_source(source, snapshot, policy, sidecar)

    assert raced_stage is not None
    assert (raced_stage / "injected" / "secret.txt").read_text(encoding="utf-8") == (
        "must not be deleted\n"
    )
    assert not snapshot.exists()
    assert sidecar.is_file()
    shutil.rmtree(raced_stage)


def test_export_does_not_write_selected_files_into_raced_stage_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replacement stage child must fail before it becomes a copy parent."""
    source = tmp_path / "private-source"
    _write(source / "src" / "payload.py", "approved = True\n")
    victim = tmp_path / "victim"
    _write(victim / "sentinel.txt", "must not receive source files\n")
    policy = load_policy(_policy(tmp_path, "include src/**"))
    snapshot = tmp_path / "snapshot"
    sidecar = tmp_path / "release-source-manifest.json"
    original_make_directory = export_module._make_snapshot_directory
    raced_stage: Path | None = None

    def replace_created_src_directory(
        snapshot_descriptor: int,
        relative_path: str,
        expected_directories: dict[str, os.stat_result],
    ) -> os.stat_result:
        nonlocal raced_stage
        status = original_make_directory(
            snapshot_descriptor,
            relative_path,
            expected_directories,
        )
        if relative_path != "src":
            return status
        raced_stage = next(tmp_path.glob(".snapshot.stage-*"))
        shutil.rmtree(raced_stage / "src")
        victim.rename(raced_stage / "src")
        return status

    monkeypatch.setattr(
        export_module,
        "_make_snapshot_directory",
        replace_created_src_directory,
    )

    with pytest.raises(ReleaseSourceError):
        export_release_source(source, snapshot, policy, sidecar)

    assert raced_stage is not None
    assert not (raced_stage / "src" / "payload.py").exists()
    assert (raced_stage / "src" / "sentinel.txt").read_text(encoding="utf-8") == (
        "must not receive source files\n"
    )
    assert not snapshot.exists()
    assert not sidecar.exists()
    shutil.rmtree(raced_stage)


def test_export_rejects_sidecar_replaced_after_creation_before_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Evidence must retain the identity of the file it just wrote."""
    source = tmp_path / "private-source"
    _write(source / "src" / "payload.py", "approved = True\n")
    policy = load_policy(_policy(tmp_path, "include src/**"))
    snapshot = tmp_path / "snapshot"
    sidecar = tmp_path / "release-source-manifest.json"
    original_write_sidecar = export_module._write_sidecar

    def replace_sidecar_after_creation(
        manifest: object,
        parent_descriptor: int,
        path: Path,
    ) -> os.stat_result:
        status = original_write_sidecar(manifest, parent_descriptor, path)
        path.unlink()
        _write(path, '{"entries":[],"schema":1}\n')
        return status

    monkeypatch.setattr(
        export_module,
        "_write_sidecar",
        replace_sidecar_after_creation,
    )

    with pytest.raises(ReleaseSourceError, match="detached manifest changed"):
        export_release_source(source, snapshot, policy, sidecar)

    assert not snapshot.exists()
    assert sidecar.is_file()


def test_verify_manifest_rejects_evidence_replaced_during_source_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P == S verification retains the input sidecar identity until completion."""
    root = tmp_path / "release"
    _write(root / "src" / "app.py", "x = 1\n")
    policy = load_policy(_policy(tmp_path, "include src/**"))
    sidecar = tmp_path / "release-source-manifest.json"
    write_manifest(build_manifest(root, policy), sidecar)
    original_build_manifest = manifest_module.build_manifest

    def replace_after_source_walk(
        current_root: Path,
        current_policy: object,
    ) -> object:
        actual = original_build_manifest(current_root, current_policy)
        sidecar.unlink()
        _write(sidecar, '{"entries":[],"schema":1}\n')
        return actual

    monkeypatch.setattr(manifest_module, "build_manifest", replace_after_source_walk)

    with pytest.raises(ReleaseSourceError, match="detached manifest changed"):
        verify_manifest(root, policy, sidecar)


def test_export_rejects_sidecar_parent_replaced_after_evidence_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Publication cannot silently leave evidence under a replaced parent path."""
    source = tmp_path / "private-source"
    _write(source / "src" / "payload.py", "approved = True\n")
    output_parent = tmp_path / "output"
    output_parent.mkdir()
    policy = load_policy(_policy(tmp_path, "include src/**"))
    snapshot = tmp_path / "snapshot"
    sidecar = output_parent / "release-source-manifest.json"
    original_write_sidecar = export_module._write_sidecar

    def replace_parent_after_evidence(
        manifest: object,
        parent_descriptor: int,
        path: Path,
    ) -> os.stat_result:
        status = original_write_sidecar(manifest, parent_descriptor, path)
        shutil.rmtree(output_parent)
        output_parent.symlink_to(source, target_is_directory=True)
        return status

    monkeypatch.setattr(export_module, "_write_sidecar", replace_parent_after_evidence)

    with pytest.raises(ReleaseSourceError):
        export_release_source(source, snapshot, policy, sidecar)

    assert not snapshot.exists()
    assert not (source / "release-source-manifest.json").exists()


def test_export_rejects_snapshot_parent_replaced_during_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A final rename through an unlinked parent cannot be reported as success."""
    source = tmp_path / "private-source"
    _write(source / "src" / "payload.py", "approved = True\n")
    output_parent = tmp_path / "output"
    output_parent.mkdir()
    displaced_parent = tmp_path / "displaced-output"
    policy = load_policy(_policy(tmp_path, "include src/**"))
    snapshot = output_parent / "snapshot"
    sidecar = tmp_path / "release-source-manifest.json"
    original_publish = export_module._publish_staging_directory

    def replace_parent_before_publish(
        parent_descriptor: int,
        stage_name: str,
        snapshot_name: str,
    ) -> None:
        output_parent.rename(displaced_parent)
        output_parent.symlink_to(source, target_is_directory=True)
        original_publish(parent_descriptor, stage_name, snapshot_name)

    monkeypatch.setattr(
        export_module,
        "_publish_staging_directory",
        replace_parent_before_publish,
    )

    with pytest.raises(ReleaseSourceError):
        export_release_source(source, snapshot, policy, sidecar)

    assert not snapshot.exists()
    assert not (source / "snapshot").exists()
    assert (displaced_parent / "snapshot").is_dir()
    assert sidecar.is_file()


def test_write_manifest_refuses_sidecar_replaced_after_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sidecar path changed after preflight cannot overwrite its target."""
    root = tmp_path / "release"
    _write(root / "src" / "app.py", "x = 1\n")
    manifest = build_manifest(root, load_policy(_policy(tmp_path, "include src/**")))
    sidecar = tmp_path / "release-source-manifest.json"
    target = _write(tmp_path / "target.txt", "must stay unchanged\n")
    original_is_symlink = Path.is_symlink
    injected = False

    def replace_after_preflight(path: Path) -> bool:
        nonlocal injected
        result = original_is_symlink(path)
        if path == sidecar and not injected:
            path.symlink_to(target)
            injected = True
        return result

    monkeypatch.setattr(Path, "is_symlink", replace_after_preflight)

    with pytest.raises(FileExistsError):
        write_manifest(manifest, sidecar)

    assert target.read_text(encoding="utf-8") == "must stay unchanged\n"
    assert sidecar.is_symlink()


def test_export_rejects_unclassified_input_and_manifest_inside_snapshot(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _write(source / "src" / "app.py", "x = 1\n")
    _write(source / "forgotten.txt", "must be classified\n")
    policy = load_policy(_policy(tmp_path, "include src/**"))

    with pytest.raises(UnclassifiedPathError, match="forgotten.txt"):
        export_release_source(
            source, tmp_path / "snapshot", policy, tmp_path / "sidecar.json"
        )

    (source / "forgotten.txt").unlink()
    snapshot = tmp_path / "snapshot"
    with pytest.raises(ValueError, match="outside"):
        export_release_source(source, snapshot, policy, snapshot / "manifest.json")


def test_export_does_not_write_a_detached_manifest_into_its_input_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _write(source / "src" / "app.py", "x = 1\n")
    policy = load_policy(_policy(tmp_path, "include src/**"))
    snapshot = tmp_path / "snapshot"

    with pytest.raises(ValueError, match="outside the source"):
        export_release_source(source, snapshot, policy, source / "manifest.json")

    assert not snapshot.exists()
    assert not (source / "manifest.json").exists()


def test_command_line_tools_emit_stable_json_and_failure_diagnostics(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _write(source / "src" / "app.py", "x = 1\n")
    allowlist = _policy(tmp_path, "include src/**")
    snapshot = tmp_path / "snapshot"
    sidecar = tmp_path / "manifest.json"
    repository_root = Path(__file__).resolve().parents[2]

    exported = subprocess.run(
        [
            sys.executable,
            str(repository_root / "scripts" / "export_release_source.py"),
            "--source",
            str(source),
            "--snapshot",
            str(snapshot),
            "--allowlist",
            str(allowlist),
            "--manifest",
            str(sidecar),
            "--json",
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert exported.returncode == 0, exported.stderr
    assert sorted(json.loads(exported.stdout)) == [
        "entries",
        "manifest",
        "manifest_sha256",
        "snapshot",
    ]

    verified = subprocess.run(
        [
            sys.executable,
            str(repository_root / "scripts" / "release_source_manifest.py"),
            "verify",
            "--root",
            str(snapshot),
            "--allowlist",
            str(allowlist),
            "--expected",
            str(sidecar),
            "--json",
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert verified.returncode == 0, verified.stderr
    assert (
        json.loads(verified.stdout)["manifest_sha256"]
        == json.loads(exported.stdout)["manifest_sha256"]
    )

    _write(snapshot / "unexpected.txt", "not in sidecar\n")
    failed = subprocess.run(
        [
            sys.executable,
            str(repository_root / "scripts" / "release_source_manifest.py"),
            "verify",
            "--root",
            str(snapshot),
            "--allowlist",
            str(allowlist),
            "--expected",
            str(sidecar),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert failed.returncode == 2
    assert failed.stdout == ""
    assert "release-source: error:" in failed.stderr
    assert "unexpected.txt" in failed.stderr
